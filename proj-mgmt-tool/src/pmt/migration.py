"""Fixture-tier local-to-Host migration preparation and isolated restore.

This module deliberately has no protocol or HTTP registration. It exports a
sanitized, content-addressed business snapshot and imports it into an existing
empty Host namespace without replacing the target database or its auth state.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from . import __version__
from . import resources as local_resources
from .db import SCHEMA_VERSION
from .errors import PmtError
from .host.auth import HOST_SCHEMA_VERSION
from .host.resources import HostResourceStore
from .planning.graph import SCHEMA_VERSION as GRAPH_SCHEMA_VERSION
from .util import canonical_json, fingerprint, new_id, utc_now
from .workspace import canonical_workspace

MIGRATION_SCHEMA = "pmt-migration-bundle-v1"
MIGRATION_VERSION = 1
TRANSFER_TABLES = (
    "scopes", "records", "artifacts", "plans", "step_specs", "execution_jobs",
    "execution_runs", "events", "project_baselines", "verifications", "artifact_refs",
)
INSERT_ORDER = (
    "scopes", "records", "artifacts", "plans", "step_specs", "execution_jobs",
    "execution_runs", "events", "project_baselines", "verifications", "artifact_refs",
)
QUIET_RUN_STATES = {"succeeded", "failed", "blocked", "canceled", "cancelled"}
QUIET_JOURNAL_STATES = {"completed", "succeeded", "failed", "canceled", "cancelled", "aborted", "recovered", "done"}
BLOCKING_OUTBOX_STATES = {"pending", "leased"}
SENSITIVE_KEYS = {"claimtoken", "token", "apikey", "authorization", "credential", "password",
                  "secret", "accesstoken", "refreshtoken", "bearer"}
LOCAL_ONLY_KEYS = {"environment", "env", "argv", "pid", "ppid", "nativehandle", "handle",
                   "spoolpath", "stdoutpath", "stderrpath", "statepath", "controlpath",
                   "workingdirectory", "callbackcommand", "supervisorpid", "childpid",
                   "environmentid", "deviceid", "sessionid", "ownersession"}
COMMAND_KEYS = {"command", "commandline", "commandargs", "argv"}
ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/][^\s\"']+|\\\\[^\\\s]+\\[^\s\"']+|/(?:[^\s\"']+/)+[^\s\"']*)")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_BUNDLE_MARKER_PREFIX = "pmt_migration_import_v1:"


@dataclass(frozen=True)
class WorkspaceMapping:
    repository_id: str
    local_workspace: Path
    branch: str | None
    reviewed_commit: str
    canonical_ref: str

    def manifest(self) -> dict[str, Any]:
        return {"repository_id": self.repository_id, "branch": self.branch,
                "branch_key_sha256": self.canonical_ref.rsplit("/", 1)[-1],
                "canonical_workspace": self.canonical_ref,
                "reviewed_commit": self.reviewed_commit,
                "local_workspace_sha256": hashlib.sha256(str(self.local_workspace).encode("utf-8")).hexdigest()}


@dataclass(frozen=True)
class MigrationReceipt:
    bundle_id: str
    manifest_sha256: str
    state: str
    counts: dict[str, int]
    resource_count: int
    invalidated_derived: dict[str, Any]
    target_namespace_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"schema": "pmt-migration-receipt-v1", "bundle_id": self.bundle_id,
                "manifest_sha256": self.manifest_sha256, "state": self.state,
                "counts": dict(self.counts), "resource_count": self.resource_count,
                "invalidated_derived": dict(self.invalidated_derived),
                "target_namespace_id": self.target_namespace_id}


def _bad(code: str, message: str, exit_code: int = 2, details: dict | None = None) -> None:
    raise PmtError(code, message, exit_code, False, details)


def _digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _uuid(value: object, label: str) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise PmtError("migration_input_invalid", f"{label} must be a canonical UUID") from exc
    return value


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    if not _table_exists(conn, "meta"):
        return None
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _quote(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise PmtError("migration_schema_invalid", "SQLite schema contains an unsupported identifier", 5)
    return '"' + identifier + '"'


def _acquire_maintenance(db, owner: str) -> None:
    with closing(db.connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = _meta(conn, "maintenance_owner") or ""
        if current:
            conn.rollback()
            raise PmtError("migration_maintenance_active", "Database is already in maintenance", 4, True)
        db._put_meta(conn, "maintenance_owner", owner)
        conn.commit()


def _release_maintenance(db, owner: str) -> None:
    with closing(db.connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = _meta(conn, "maintenance_owner") or ""
        if current != owner:
            conn.rollback()
            raise PmtError("migration_maintenance_lost", "Migration no longer owns the maintenance barrier", 4)
        db._put_meta(conn, "maintenance_owner", "")
        conn.commit()


def _count(conn: sqlite3.Connection, table: str, where: str = "", params: tuple = ()) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {_quote(table)} {where}", params).fetchone()[0])


def _assert_quiescent(conn: sqlite3.Connection, *, label: str) -> dict[str, int]:
    busy: dict[str, int] = {}
    if _table_exists(conn, "execution_runs"):
        states = {row[0]: row[1] for row in conn.execute(
            "SELECT state,COUNT(*) FROM execution_runs WHERE state NOT IN ('succeeded','failed','blocked','canceled','cancelled') GROUP BY state")}
        if states:
            busy["execution_runs"] = sum(states.values())
    for table in ("scope_locks", "claims"):
        if _count(conn, table):
            busy[table] = _count(conn, table)
    if _table_exists(conn, "file_jobs"):
        count = _count(conn, "file_jobs", "WHERE state NOT IN ('completed','succeeded','failed','canceled','cancelled','aborted')")
        if count:
            busy["file_jobs"] = count
    if _table_exists(conn, "operation_journal"):
        count = _count(conn, "operation_journal", "WHERE lower(state) NOT IN (" +
                       ",".join("?" for _ in QUIET_JOURNAL_STATES) + ")", tuple(sorted(QUIET_JOURNAL_STATES)))
        if count:
            busy["operation_journal"] = count
    if _table_exists(conn, "phase3_journal"):
        count = _count(conn, "phase3_journal", "WHERE outcome_json IS NULL OR completed_at IS NULL")
        if count:
            busy["phase3_journal"] = count
    if _table_exists(conn, "phase3_outbox"):
        count = _count(conn, "phase3_outbox", "WHERE state IN ('pending','leased')")
        if count:
            busy["phase3_outbox"] = count
    if _table_exists(conn, "phase3_objects"):
        count = _count(conn, "phase3_objects",
            "WHERE kind='host_local_file_effect' AND state!='completed'")
        if count:
            busy["host_local_file_effect"] = count
    if _table_exists(conn, "host_resource_journal"):
        count = _count(conn, "host_resource_journal", "WHERE state NOT IN ('committed','failed','canceled','cancelled')")
        if count:
            busy["host_resource_journal"] = count
    if _table_exists(conn, "host_claim_leases"):
        count = _count(conn, "host_claim_leases", "WHERE state='active'")
        if count:
            busy["host_claim_leases"] = count
    if busy:
        code = "migration_target_not_quiescent" if label.casefold().startswith("target") \
            else "migration_source_not_quiescent"
        raise PmtError(code, f"{label} has active work or unresolved journals", 3,
                       False, {"counts": busy})
    return {"execution_runs": _count(conn, "execution_runs"), "scope_locks": 0,
            "claims": 0, "file_jobs": _count(conn, "file_jobs"),
            "phase3_journal": _count(conn, "phase3_journal"),
            "phase3_outbox": _count(conn, "phase3_outbox"),
            "host_local_file_effect": _count(conn, "phase3_objects",
                "WHERE kind='host_local_file_effect' AND state!='completed'"),
            "operation_journal": _count(conn, "operation_journal")}


def _git(mapping: dict, repository_id: str) -> WorkspaceMapping:
    expected = {"repository_id", "local_workspace", "branch"}
    if not isinstance(mapping, dict) or set(mapping) != expected:
        _bad("migration_mapping_invalid", "Each workspace mapping requires repository_id/local_workspace/branch")
    repository_id = _uuid(mapping["repository_id"], "repository_id")
    raw_path = mapping["local_workspace"]
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        _bad("migration_mapping_invalid", "local_workspace must be an absolute path")
    if Path(raw_path).is_symlink():
        _bad("migration_mapping_invalid", "local_workspace may not be a symlink")
    workspace = Path(raw_path).resolve(strict=True)
    if not workspace.is_dir() or workspace.is_symlink():
        _bad("migration_mapping_invalid", "local_workspace must be a real directory")

    def run(*args):
        try:
            return subprocess.run(["git", "-C", str(workspace), *args], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, check=False, timeout=8)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PmtError("migration_git_unavailable", "Workspace Git metadata could not be checked", 3) from exc

    root = run("rev-parse", "--show-toplevel")
    head = run("rev-parse", "--verify", "HEAD")
    if root.returncode != 0 or head.returncode != 0 or Path(os.fsdecode(root.stdout).strip()).resolve() != workspace:
        _bad("migration_mapping_invalid", "local_workspace must be a clean Git repository root")
    branch_result = run("symbolic-ref", "--quiet", "--short", "HEAD")
    actual_branch = os.fsdecode(branch_result.stdout).strip() if branch_result.returncode == 0 else None
    actual_head = os.fsdecode(head.stdout).strip()
    requested_branch = mapping["branch"]
    if requested_branch != actual_branch:
        _bad("migration_mapping_stale", "Mapped branch does not match the checked-out Git branch",
             details={"repository_id": repository_id, "branch_mismatch": True})
    status = run("status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0 or status.stdout.strip():
        _bad("migration_source_dirty", "A workspace with uncommitted changes cannot be assigned a canonical branch ref")
    branch_key = actual_branch if actual_branch is not None else "detached:" + actual_head
    return WorkspaceMapping(repository_id, workspace, actual_branch, actual_head,
                             canonical_workspace(repository_id, branch_key))


def _resolve_mappings(conn: sqlite3.Connection, mappings: list[dict], db_root: Path) -> list[WorkspaceMapping]:
    if not isinstance(mappings, list):
        _bad("migration_mapping_invalid", "workspace_mappings must be an array")
    result = []
    seen = set()
    for value in mappings:
        if not isinstance(value, dict) or not isinstance(value.get("repository_id"), str):
            _bad("migration_mapping_invalid", "Workspace mappings must name a repository scope")
        repo_id = _uuid(value["repository_id"], "repository_id")
        row = conn.execute("SELECT kind FROM scopes WHERE id=?", (repo_id,)).fetchone()
        if not row or row["kind"] != "repository":
            _bad("migration_mapping_invalid", "repository_id must identify a source repository scope")
        mapping = _git(value, repo_id)
        if mapping.local_workspace.is_relative_to(db_root.resolve()):
            _bad("migration_mapping_invalid", "A workspace mapping cannot resolve inside the SQLite data root")
        identity = (mapping.repository_id, mapping.canonical_ref)
        if identity in seen:
            _bad("migration_mapping_invalid", "Workspace mappings must be unique by repository and branch")
        seen.add(identity)
        result.append(mapping)
    return result


def _inventory_spool(db) -> dict[str, Any]:
    root = Path(db.root) / "runner-spool"
    if not root.exists():
        return {"file_count": 0, "total_bytes": 0, "inventory_sha256": fingerprint([]), "unknown_links": 0}
    local_resources._reject_links(root)
    entries, total, links = [], 0, 0
    for current, dirs, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        safe_dirs = []
        for name in sorted(dirs):
            candidate = current_path / name
            if candidate.is_symlink() or bool(getattr(candidate, "is_junction", lambda: False)()):
                links += 1
            else:
                safe_dirs.append(name)
        dirs[:] = safe_dirs
        for name in sorted(names):
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                links += 1
                continue
            digest, size = _digest_file(path)
            total += size
            relative = path.relative_to(root).as_posix()
            entries.append({"relative_path_sha256": _digest_bytes(relative.encode("utf-8")),
                            "sha256": digest, "size_bytes": size})
    if links:
        raise PmtError("migration_spool_unsafe", "Runner spool contains links that cannot be inventoried safely", 3,
                       False, {"link_count": links})
    return {"file_count": len(entries), "total_bytes": total,
            "inventory_sha256": fingerprint(entries), "unknown_links": 0}


def _mapping_for_path(value: object, mappings: list[WorkspaceMapping]) -> tuple[WorkspaceMapping | None, str | None]:
    if not isinstance(value, str) or not value:
        return None, None
    if value.startswith("pmt://"):
        if re.fullmatch(r"pmt://[0-9a-f-]{36}/[0-9a-f]{64}", value):
            return next((item for item in mappings if item.canonical_ref == value), None), value
        return None, None
    try:
        raw = Path(value)
        if not raw.is_absolute():
            return None, None
        candidate = raw.resolve(strict=False)
    except (OSError, ValueError):
        return None, None
    for item in mappings:
        try:
            relative = candidate.relative_to(item.local_workspace)
        except ValueError:
            continue
        return item, relative.as_posix() if str(relative) != "." else ""
    return None, None


def _workspace_value(value: object, mappings: list[WorkspaceMapping], stats: dict) -> str:
    if isinstance(value, str) and re.fullmatch(r"pmt://[0-9a-f-]{36}/[0-9a-f]{64}", value):
        stats["canonical_workspace_preserved"] = stats.get("canonical_workspace_preserved", 0) + 1
        return value
    mapping, relative = _mapping_for_path(value, mappings)
    if isinstance(value, str) and value.startswith("pmt://") and mapping is None:
        stats["canonical_workspace_unmapped"] = stats.get("canonical_workspace_unmapped", 0) + 1
        return "unknown-workspace:" + _digest_bytes(value.encode("utf-8"))
    if mapping is None:
        stats["workspace_unmapped"] = stats.get("workspace_unmapped", 0) + 1
        return "unknown-workspace:" + _digest_bytes(str(value).encode("utf-8"))
    if relative:
        stats["workspace_subdirectory_unmapped"] = stats.get("workspace_subdirectory_unmapped", 0) + 1
        return "unknown-workspace:" + _digest_bytes((mapping.canonical_ref + "#" + relative).encode("utf-8"))
    stats["workspace_mapped"] = stats.get("workspace_mapped", 0) + 1
    return mapping.canonical_ref


def _portable_path_text(value: str, mappings: list[WorkspaceMapping], stats: dict) -> str:
    if value.startswith("pmt://") and re.fullmatch(r"pmt://[0-9a-f-]{36}/[0-9a-f]{64}", value):
        return value
    mapping, relative = _mapping_for_path(value, mappings)
    if mapping:
        return mapping.canonical_ref if not relative else mapping.canonical_ref + "#" + relative
    if Path(value).is_absolute():
        stats["path_redacted"] = stats.get("path_redacted", 0) + 1
        return "path-sha256:" + _digest_bytes(value.encode("utf-8"))
    def replace_match(match):
        raw = match.group(0).rstrip(",;)]}")
        suffix = match.group(0)[len(raw):]
        mapped, subpath = _mapping_for_path(raw, mappings)
        if mapped:
            stats["embedded_path_mapped"] = stats.get("embedded_path_mapped", 0) + 1
            token = mapped.canonical_ref if not subpath else mapped.canonical_ref + "#" + subpath
        else:
            stats["embedded_path_redacted"] = stats.get("embedded_path_redacted", 0) + 1
            token = "path-sha256:" + _digest_bytes(raw.encode("utf-8"))
        return token + suffix
    return ABSOLUTE_PATH_RE.sub(replace_match, value)


def _sanitize(value: Any, mappings: list[WorkspaceMapping], stats: dict, key: str | None = None) -> Any:
    lowered = key.casefold().replace("_", "").replace("-", "") if isinstance(key, str) else ""
    if lowered in SENSITIVE_KEYS:
        stats["secret_fields_redacted"] = stats.get("secret_fields_redacted", 0) + 1
        return {"redacted": True}
    if lowered in COMMAND_KEYS or lowered in LOCAL_ONLY_KEYS:
        stats["local_runtime_fields_redacted"] = stats.get("local_runtime_fields_redacted", 0) + 1
        return {"redacted": True, "value_sha256": fingerprint(value)}
    if lowered in {"workspace", "workspaceroot", "physicalworkspace"}:
        return _workspace_value(value, mappings, stats)
    if isinstance(value, dict):
        result = {}
        for child_key, child in value.items():
            result[child_key] = _sanitize(child, mappings, stats, child_key)
        return result
    if isinstance(value, list):
        return [_sanitize(item, mappings, stats, key) for item in value]
    if isinstance(value, str):
        return _portable_path_text(value, mappings, stats)
    return value


def _json_text(value: Any, mappings: list[WorkspaceMapping], stats: dict, label: str) -> str:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PmtError("migration_source_corrupt", f"Source {label} is not valid JSON", 5) from exc
    return canonical_json(_sanitize(parsed, mappings, stats))


def _safe_route(value: str, mappings: list[WorkspaceMapping], stats: dict) -> str:
    try:
        route = json.loads(value)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise PmtError("migration_source_corrupt", "Execution route JSON is invalid", 5) from exc
    if not isinstance(route, dict):
        raise PmtError("migration_source_corrupt", "Execution route must be an object", 5)
    allowed = {"agent", "provider", "model", "mode", "selection_reason"}
    safe = {key: route[key] for key in allowed if key in route and isinstance(route[key], (str, int, bool))}
    if isinstance(safe.get("selection_reason"), str):
        safe["selection_reason"] = _portable_path_text(safe["selection_reason"], mappings, stats)
    return canonical_json(safe)


def _sanitize_row(table: str, row: dict, mappings: list[WorkspaceMapping], stats: dict) -> dict:
    result = dict(row)
    json_columns = {"scopes": ("body_json",), "records": ("body_json",), "events": ("payload_json",),
                    "plans": (), "step_specs": ("scopes_json", "criteria_json", "dependencies_json"),
                    "execution_runs": ("scopes_json", "intent_json", "result_json"),
                    "project_baselines": ("body_json",)}
    for column in json_columns.get(table, ()):
        if result.get(column) is not None:
            result[column] = _json_text(result[column], mappings, stats, f"{table}.{column}")
    if table == "plans":
        result["workspace"] = _workspace_value(result["workspace"], mappings, stats)
    elif table == "step_specs":
        result["workspace"] = _workspace_value(result["workspace"], mappings, stats)
    elif table == "project_baselines":
        result["workspace"] = _workspace_value(result["workspace"], mappings, stats)
    elif table == "events":
        if isinstance(result.get("reason"), str):
            result["reason"] = _portable_path_text(result["reason"], mappings, stats)
    elif table == "records":
        if isinstance(result.get("title"), str):
            result["title"] = _portable_path_text(result["title"], mappings, stats)
    elif table == "execution_runs":
        old_session = result["owner_session"]
        result["owner_session"] = "migration:" + _digest_bytes(old_session.encode("utf-8"))[:24]
        result["workspace"] = _workspace_value(result["workspace"], mappings, stats)
        result["route_json"] = _safe_route(result["route_json"], mappings, stats)
        result["handle_json"] = None
        if result["intent_json"] is not None:
            intent = json.loads(result["intent_json"])
            intent.pop("context_ref", None)
            intent.pop("reuse_decision_ref", None)
            intent.pop("batch_ref", None)
            intent.pop("batch_parent_run_id", None)
            intent.pop("batch_role", None)
            intent.pop("batch_context_required", None)
            if isinstance(intent.get("route"), dict):
                intent["route"] = json.loads(_safe_route(canonical_json(intent["route"]), mappings, stats))
            result["intent_json"] = canonical_json(intent)
        if result["result_json"] is not None:
            result["result_json"] = _json_text(result["result_json"], mappings, stats,
                                                "execution_runs.result_json")
    elif table == "verifications":
        result["environment_id"] = "migrated:" + _digest_bytes(str(result["environment_id"]).encode("utf-8"))[:24]
        try:
            command = json.loads(result["command_json"])
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise PmtError("migration_source_corrupt", "Verification command snapshot is invalid", 5) from exc
        command_hash = fingerprint(command)
        prior = command if isinstance(command, dict) else {}
        result["command_json"] = canonical_json({"migration_stale": True, "source_snapshot_sha256": command_hash,
            "before_fingerprint": prior.get("before_fingerprint"),
            "snapshot_hash": prior.get("snapshot_hash"),
            "snapshot": {"verification_scope_id": (prior.get("snapshot") or {}).get("verification_scope_id")}})
        result["state"] = "migrated_stale"
    elif table == "artifacts":
        if not _HEX64.fullmatch(str(result.get("sha256", ""))) or type(result.get("size_bytes")) is not int:
            raise PmtError("migration_source_corrupt", "Artifact metadata is invalid", 5)
        result["relative_path"] = "resources/objects/" + result["sha256"]
    return result


def _primary_key(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = sorted((row[5], row[1]) for row in conn.execute(f"PRAGMA table_info({_quote(table)})") if row[5])
    return [name for _, name in rows]


def _sortable_rows(conn: sqlite3.Connection, table: str, rows: list[dict]) -> list[dict]:
    if table not in {"scopes", "records"}:
        return rows
    by_id = {row["id"]: row for row in rows}
    depths = {}
    active = set()

    def depth(row_id):
        if row_id in depths:
            return depths[row_id]
        if row_id in active:
            raise PmtError("migration_source_corrupt", f"{table} contains a parent cycle", 5)
        active.add(row_id)
        parent = by_id[row_id].get("parent_id")
        value = 0 if not parent or parent not in by_id else depth(parent) + 1
        active.remove(row_id)
        depths[row_id] = value
        return value

    return sorted(rows, key=lambda row: (depth(row["id"]), row["id"]))


def _canonical_rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    cols = _columns(conn, table)
    key = _primary_key(conn, table)
    order = ",".join(_quote(name) for name in key) if key else ",".join(_quote(name) for name in cols)
    return [dict(row) for row in conn.execute(f"SELECT * FROM {_quote(table)} ORDER BY {order}")]


def _row_digest(rows: list[dict]) -> str:
    normalized = []
    for row in rows:
        normalized.append({key: ({"blob_sha256": _digest_bytes(value), "size": len(value)}
                                 if isinstance(value, bytes) else value) for key, value in row.items()})
    return fingerprint(normalized)


def _id_digest(rows: list[dict], key_columns: list[str]) -> str:
    ids = [tuple(row[name] for name in key_columns) for row in rows]
    return fingerprint(ids)


def _create_staged_database(snapshot: sqlite3.Connection, path: Path, mappings: list[WorkspaceMapping],
                            stats: dict) -> dict:
    source_tables = _table_names(snapshot)
    missing = set(TRANSFER_TABLES) - source_tables
    if missing:
        raise PmtError("migration_schema_incompatible", "Source database lacks required business tables", 3,
                       False, {"missing_tables": sorted(missing)})
    schema_rows = snapshot.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                                   "AND type IN ('table','index','trigger') AND name NOT LIKE 'sqlite_%' "
                                   "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END,name").fetchall()
    with closing(sqlite3.connect(str(path))) as staged:
        staged.row_factory = sqlite3.Row
        staged.execute("PRAGMA foreign_keys=OFF")
        for item in schema_rows:
            staged.execute(item["sql"])
        for row in staged.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            table = row[0]
            if table != "meta":
                staged.execute(f"DELETE FROM {_quote(table)}")
        table_stats = {}
        for table in INSERT_ORDER:
            source_rows = _canonical_rows(snapshot, table)
            transformed = [_sanitize_row(table, row, mappings, stats) for row in source_rows]
            transformed = _sortable_rows(snapshot, table, transformed)
            cols = _columns(staged, table)
            if any(set(row) != set(cols) for row in transformed):
                raise PmtError("migration_schema_incompatible", f"Source {table} columns changed", 3)
            if transformed:
                placeholders = ",".join("?" for _ in cols)
                names = ",".join(_quote(col) for col in cols)
                staged.executemany(f"INSERT INTO {_quote(table)}({names}) VALUES({placeholders})",
                                   [tuple(row[col] for col in cols) for row in transformed])
            staged_rows = _canonical_rows(staged, table)
            table_stats[table] = {"count": len(staged_rows),
                "ids_sha256": _id_digest(staged_rows, _primary_key(staged, table)),
                "rows_sha256": _row_digest(staged_rows)}
        staged.execute("DELETE FROM meta")
        staged.executemany("INSERT INTO meta(key,value) VALUES(?,?)", [
            ("schema_version", str(SCHEMA_VERSION)), ("db_id", new_id()), ("maintenance_owner", "")])
        fk_errors = staged.execute("PRAGMA foreign_key_check").fetchall()
        if fk_errors:
            raise PmtError("migration_reference_invalid", "Sanitized transfer database failed foreign-key checks", 5,
                           False, {"violation_count": len(fk_errors)})
        staged.commit()
    return table_stats


def _resource_specs(source_db, snapshot: sqlite3.Connection, mappings: list[WorkspaceMapping],
                    bundle_resource_root: Path) -> list[dict]:
    rows = snapshot.execute("SELECT id,scope_id,sha256,size_bytes,relative_path,state FROM artifacts ORDER BY id").fetchall()
    directive_specs = {row[0]: (row[1], row[2]) for row in snapshot.execute(
        "SELECT directive_id,step_id,directive_version FROM step_specs")}
    specs = []
    for row in rows:
        if row["state"] != "ready":
            raise PmtError("migration_resource_unavailable", "All source artifacts must be ready before export", 3,
                           False, {"resource_count_invalid": 1})
        artifact_path = local_resources._artifact_path(source_db, row["relative_path"])
        checked = local_resources.check_artifact(source_db, snapshot, row["id"])
        if not checked.get("valid") or checked.get("sha256") != row["sha256"]:
            raise PmtError("migration_resource_corrupt", "A referenced source artifact failed hash verification", 5)
        raw_hash, size = _digest_file(artifact_path)
        if raw_hash != row["sha256"] or size != row["size_bytes"]:
            raise PmtError("migration_resource_corrupt", "A source artifact changed after its SQLite snapshot", 5)
        destination = bundle_resource_root / row["sha256"]
        if destination.exists():
            existing_hash, existing_size = _digest_file(destination)
            if (existing_hash, existing_size) != (raw_hash, size):
                raise PmtError("migration_bundle_conflict", "A staged content-addressed resource path contains different bytes", 5)
        else:
            local_resources._reject_links(bundle_resource_root)
            with artifact_path.open("rb") as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            copied_hash, copied_size = _digest_file(destination)
            if (copied_hash, copied_size) != (raw_hash, size):
                raise PmtError("migration_resource_corrupt", "Staged resource bytes failed hash verification", 5)
        private = directive_specs.get(row["id"])
        specs.append({"artifact_id": row["id"], "scope_id": row["scope_id"],
            "sha256": raw_hash, "size_bytes": size, "bundle_path": "resources/" + raw_hash,
            "purpose": "step_directive" if private else "migrated_resource",
            "private_step_id": private[0] if private else None,
            "directive_version": private[1] if private else None})
    return specs


def _baseline_mapping_status(snapshot: sqlite3.Connection, mappings: list[WorkspaceMapping]) -> list[dict]:
    if not _table_exists(snapshot, "project_baselines"):
        return []
    result = []
    for row in snapshot.execute("SELECT scope_id,workspace,selected_ref,reviewed_commit FROM project_baselines ORDER BY scope_id"):
        mapping, _relative = _mapping_for_path(row["workspace"], mappings)
        if mapping is None and isinstance(row["workspace"], str) and re.fullmatch(
                r"pmt://[0-9a-f-]{36}/[0-9a-f]{64}", row["workspace"]):
            result.append({"project_scope_id": row["scope_id"], "canonical_workspace": row["workspace"],
                "stored_selected_ref": row["selected_ref"], "stored_reviewed_commit": row["reviewed_commit"],
                "mapping_status": "canonical_reference_preserved_unverified", "ready_for_resume": False,
                "resume_reason": "fresh_F12_mapping_and_F0_source_pin_required"})
            continue
        current = bool(mapping and row["selected_ref"] == mapping.branch
                       and row["reviewed_commit"] == mapping.reviewed_commit)
        result.append({"project_scope_id": row["scope_id"],
            "canonical_workspace": mapping.canonical_ref if mapping else None,
            "stored_selected_ref": row["selected_ref"],
            "stored_reviewed_commit": row["reviewed_commit"],
            "mapping_status": "current_source_match" if current else
                ("workspace_mapping_unknown" if not mapping else "baseline_differs_from_current_checkout"),
            "ready_for_resume": False,
            "resume_reason": "fresh_F12_mapping_and_F0_source_pin_required"})
    return result


def _excluded_table_digest(conn: sqlite3.Connection, table: str) -> dict:
    if not _table_exists(conn, table):
        return {"count": 0, "ids_sha256": fingerprint([])}
    rows = _canonical_rows(conn, table)
    keys = _primary_key(conn, table)
    return {"count": len(rows), "ids_sha256": _id_digest(rows, keys or _columns(conn, table)[:1])}


def _business_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {table: _count(conn, table) for table in TRANSFER_TABLES}


def _business_empty(conn: sqlite3.Connection) -> None:
    counts = {key: value for key, value in _business_counts(conn).items() if value}
    if counts:
        raise PmtError("migration_target_not_empty", "Target namespace contains business data", 3, False,
                       {"counts": counts})
    if _table_exists(conn, "host_resource_metadata") and _count(conn, "host_resource_metadata"):
        raise PmtError("migration_target_not_empty", "Target already has Host resource metadata", 3)
    if _table_exists(conn, "host_resource_journal") and _count(conn, "host_resource_journal",
        "WHERE state NOT IN ('committed','failed','canceled','cancelled')"):
        raise PmtError("migration_target_not_quiescent", "Target has unresolved Host resource publication", 3)
    if _table_exists(conn, "host_claim_leases") and _count(conn, "host_claim_leases", "WHERE state='active'"):
        raise PmtError("migration_target_not_quiescent", "Target has an active Host claim", 3)


class MigrationCoordinator:
    """Prepare and validate fixture-tier transfer bundles; never switches a primary."""

    def create_backup(self, source_db, destination, workspace_mappings: list[dict], *,
                      source_kind: str = "local", source_authorizer=None,
                      bundle_id: str | None = None) -> dict:
        """Create a sanitized SQLite/resource bundle and release the source barrier afterward."""
        if source_kind not in {"local", "host"}:
            _bad("migration_input_invalid", "source_kind must be local or host")
        if source_kind == "host" and workspace_mappings:
            _bad("migration_mapping_invalid", "Host snapshots preserve canonical workspace refs and may not inspect local mappings")
        dest = Path(destination).expanduser().absolute()
        if dest.exists():
            raise PmtError("migration_destination_exists", "Backup destination must be new; originals are never overwritten", 3)
        for mapping in workspace_mappings if isinstance(workspace_mappings, list) else []:
            if isinstance(mapping, dict) and isinstance(mapping.get("local_workspace"), str):
                workspace = Path(mapping["local_workspace"]).expanduser().resolve(strict=True)
                if dest.resolve(strict=False).is_relative_to(workspace):
                    raise PmtError("migration_destination_invalid", "Backup destination cannot be inside a mapped source workspace", 3)
        parent = dest.parent.resolve(strict=True)
        if dest.resolve(strict=False).is_relative_to(source_db.root.resolve()) or \
                dest.resolve(strict=False).is_relative_to(source_db.config_root.resolve()):
            raise PmtError("migration_destination_invalid", "Backup destination cannot be inside source data or config roots", 3)
        if dest.is_symlink() or parent.is_symlink():
            raise PmtError("migration_destination_invalid", "Backup destination may not use a symlink", 3)
        local_resources._reject_links(dest.parent)
        bundle_id = _uuid(bundle_id, "bundle_id") if bundle_id is not None else new_id()
        stage = parent / ("." + dest.name + ".pmt-stage-" + bundle_id)
        stage.mkdir(mode=0o700)
        owner = "migration-source-" + bundle_id
        acquired = False
        try:
            _acquire_maintenance(source_db, owner)
            acquired = True
            spool_before = _inventory_spool(source_db)
            with closing(source_db.connect()) as source:
                _assert_quiescent(source, label="Source database")
                if source_authorizer is not None:
                    source_authorizer(source)
                version = int(_meta(source, "schema_version") or 0)
                if version != SCHEMA_VERSION:
                    raise PmtError("migration_schema_unsupported", "Source DB schema must exactly match this core", 3,
                                   False, {"source_schema": version, "supported": SCHEMA_VERSION})
                if _meta(source, "maintenance_owner") != owner:
                    raise PmtError("migration_maintenance_lost", "Source maintenance barrier was lost", 4)
                snapshot = sqlite3.connect(":memory:")
                snapshot.row_factory = sqlite3.Row
                source.backup(snapshot)
            try:
                with closing(snapshot):
                    mappings = _resolve_mappings(snapshot, workspace_mappings, source_db.root)
                    stats: dict[str, int] = {}
                    resource_root = stage / "resources"
                    resource_root.mkdir()
                    resource_specs = _resource_specs(source_db, snapshot, mappings, resource_root)
                    staged_db_path = stage / "transfer.sqlite3"
                    table_stats = _create_staged_database(snapshot, staged_db_path, mappings, stats)
                    with closing(sqlite3.connect(str(staged_db_path))) as staged:
                        staged.row_factory = sqlite3.Row
                        resource_rows = staged.execute("SELECT id,scope_id,sha256,size_bytes,relative_path FROM artifacts ORDER BY id").fetchall()
                        if len(resource_rows) != len(resource_specs):
                            raise PmtError("migration_reference_invalid", "Sanitized artifact rows do not match resource files", 5)
                        for row in resource_rows:
                            if row["relative_path"] != "resources/objects/" + row["sha256"]:
                                raise PmtError("migration_reference_invalid", "Transfer artifact path is not canonical", 5)
                        request_ids = [row[0] for row in snapshot.execute("SELECT request_id FROM requests ORDER BY request_id")]
                        request_history = {"count": len(request_ids), "ids": request_ids,
                            "ids_sha256": fingerprint(request_ids),
                            "responses_excluded": True, "live_replay_preserved": False}
                        excluded_names = ("meta", "scope_paths", "requests", "claims", "file_jobs", "scope_locks",
                            "routing_settings", "run_progress", "operation_journal", "phase3_objects",
                            "phase3_journal", "phase3_outbox", "host_devices", "host_sessions",
                            "host_environments", "host_claim_leases", "host_request_scopes",
                            "host_resource_journal", "host_resource_requests", "host_resource_metadata",
                            "host_transfer_receipts")
                        excluded = {name: _excluded_table_digest(snapshot, name) for name in excluded_names}
                        core_graph_version = GRAPH_SCHEMA_VERSION
                        resource_info = [{key: value for key, value in spec.items() if key != "bundle_path"}
                                         | {"bundle_path": spec["bundle_path"]} for spec in resource_specs]
                        staged_hash, staged_size = _digest_file(staged_db_path)
                        assets = []
                        for file in sorted(resource_root.iterdir(), key=lambda item: item.name):
                            digest, size = _digest_file(file)
                            assets.append({"sha256": digest, "size_bytes": size, "bundle_path": "resources/" + file.name})
                    mappings_manifest = [item.manifest() for item in mappings]
                    baseline_mappings = _baseline_mapping_status(snapshot, mappings)
                    created = utc_now()
                    body = {"schema": MIGRATION_SCHEMA, "version": MIGRATION_VERSION,
                        "bundle_id": bundle_id, "created_at": created,
                        "source_kind": source_kind,
                        "source_versions": {"core": __version__, "db_schema": version,
                            "graph_schema": core_graph_version,
                            "host_schema": int(_meta(snapshot, "host_schema_version") or 0)},
                        "portable_workspace_mappings": mappings_manifest,
                        "baseline_mapping_status": baseline_mappings,
                        "workspace_source_status": ("canonical_refs_preserved_checkout_not_inspected"
                            if source_kind == "host" else "supplied_local_git_mappings_checked"),
                        "tables": table_stats, "resources": resource_info, "resource_objects": assets,
                        "excluded_history": excluded,
                        "request_id_history": request_history,
                        "derived_state_invalidated": excluded["phase3_objects"],
                        "terminal_runner_spool_preserved_in_place": spool_before,
                        "quiescence": _assert_quiescent(snapshot, label="Snapshot"),
                        "sanitization_counts": stats,
                        "transfer_database": {"bundle_path": "transfer.sqlite3",
                            "sha256": staged_hash, "size_bytes": staged_size},
                        "source_database_identity_sha256": _digest_bytes(str(_meta(snapshot, "db_id") or "unknown").encode("utf-8")),
                        "migratable_primary_switch": False,
                        "tier": source_kind + "-fixture-preparation"}
                    body["manifest_sha256"] = fingerprint(body)
                    manifest_path = stage / "manifest.json"
                    with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
                        stream.write(canonical_json(body) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    self.verify_backup(stage)
            finally:
                if 'snapshot' in locals():
                    snapshot.close()
            if _inventory_spool(source_db) != spool_before:
                raise PmtError("migration_source_changed", "Local runner spool changed while the backup was prepared", 3)
            if dest.exists():
                raise PmtError("migration_destination_exists", "Backup destination appeared during staging", 3)
            os.rename(stage, dest)
            with closing(source_db.connect()) as current:
                _assert_quiescent(current, label="Source database after backup")
                if _meta(current, "maintenance_owner") != owner:
                    raise PmtError("migration_maintenance_lost", "Source maintenance barrier was lost during backup", 4)
            return self.verify_backup(dest)
        except Exception:
            # Only this unique stage directory is removed; the source database/resources are untouched.
            if stage.exists() and stage.resolve().parent == parent and stage.name.endswith(bundle_id):
                shutil.rmtree(stage)
            raise
        finally:
            if acquired:
                _release_maintenance(source_db, owner)

    def verify_backup(self, bundle_path) -> dict:
        raw_root = Path(bundle_path).expanduser()
        if raw_root.is_symlink():
            raise PmtError("migration_bundle_invalid", "Backup bundle may not be a symlink", 3)
        root = raw_root.resolve(strict=True)
        if not root.is_dir():
            raise PmtError("migration_bundle_invalid", "Backup bundle must be a real directory", 3)
        local_resources._reject_links(root)
        manifest_path = root / "manifest.json"
        database_path = root / "transfer.sqlite3"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise PmtError("migration_manifest_invalid", "Migration manifest cannot be read", 5) from exc
        if (not isinstance(manifest, dict) or manifest.get("schema") != MIGRATION_SCHEMA
                or manifest.get("version") != MIGRATION_VERSION):
            raise PmtError("migration_manifest_unsupported", "Migration manifest version is unsupported", 3)
        supplied = manifest.get("manifest_sha256")
        body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        if not isinstance(supplied, str) or fingerprint(body) != supplied:
            raise PmtError("migration_manifest_corrupt", "Manifest hash does not match its content", 5)
        if manifest.get("source_versions", {}).get("core") != __version__ or \
                manifest.get("source_versions", {}).get("db_schema") != SCHEMA_VERSION or \
                manifest.get("source_versions", {}).get("graph_schema") != GRAPH_SCHEMA_VERSION:
            raise PmtError("migration_version_incompatible", "Bundle core/DB/graph versions do not match this runtime", 3)
        transfer_meta = manifest.get("transfer_database")
        if not isinstance(transfer_meta, dict) or transfer_meta.get("bundle_path") != "transfer.sqlite3":
            raise PmtError("migration_manifest_invalid", "Transfer database reference is invalid", 5)
        db_hash, db_size = _digest_file(database_path)
        if (db_hash, db_size) != (transfer_meta.get("sha256"), transfer_meta.get("size_bytes")):
            raise PmtError("migration_bundle_corrupt", "Staged transfer database hash/size changed", 5)
        expected_assets = {item["sha256"]: item for item in manifest.get("resource_objects", [])}
        expected_files = {"manifest.json", "transfer.sqlite3"}
        for digest, item in expected_assets.items():
            if not _HEX64.fullmatch(digest) or item.get("bundle_path") != "resources/" + digest:
                raise PmtError("migration_manifest_invalid", "A staged resource reference is invalid", 5)
            path = root / item["bundle_path"]
            local_resources._reject_links(path.parent)
            actual_hash, actual_size = _digest_file(path)
            if (actual_hash, actual_size) != (digest, item.get("size_bytes")):
                raise PmtError("migration_bundle_corrupt", "Staged resource hash/size changed", 5,
                               False, {"sha256": digest})
            expected_files.add(item["bundle_path"])
        actual_files = set()
        for path in root.rglob("*"):
            if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
                raise PmtError("migration_bundle_invalid", "Bundle may not contain symlinks or reparse points", 5)
            if path.is_file():
                actual_files.add(path.relative_to(root).as_posix())
        if actual_files != expected_files:
            raise PmtError("migration_bundle_invalid", "Bundle contains an unmanifested file", 5)
        with closing(sqlite3.connect(str(database_path))) as staged:
            staged.row_factory = sqlite3.Row
            if not staged.execute("PRAGMA integrity_check").fetchone()[0] == "ok":
                raise PmtError("migration_database_corrupt", "Staged database failed SQLite integrity_check", 5)
            fk = staged.execute("PRAGMA foreign_key_check").fetchall()
            if fk:
                raise PmtError("migration_reference_invalid", "Staged database has foreign-key violations", 5,
                               False, {"violation_count": len(fk)})
            if int(_meta(staged, "schema_version") or 0) != SCHEMA_VERSION:
                raise PmtError("migration_schema_incompatible", "Staged database schema version changed", 5)
            safe_meta_keys = {"schema_version", "db_id", "maintenance_owner"}
            if {row[0] for row in staged.execute("SELECT key FROM meta")} - safe_meta_keys:
                raise PmtError("migration_sanitization_failed", "Transfer snapshot contains local profile or Host identity metadata", 5)
            for table in manifest.get("excluded_history", {}):
                if table != "meta" and _table_exists(staged, table) and _count(staged, table):
                    raise PmtError("migration_sanitization_failed", f"Excluded table {table} contains transferable rows", 5)
            for table, expected in manifest["tables"].items():
                if table not in TRANSFER_TABLES:
                    raise PmtError("migration_manifest_invalid", "Manifest contains an unsupported transfer table", 5)
                rows = _canonical_rows(staged, table)
                actual = {"count": len(rows), "ids_sha256": _id_digest(rows, _primary_key(staged, table)),
                          "rows_sha256": _row_digest(rows)}
                if actual != expected:
                    raise PmtError("migration_database_corrupt", f"Staged {table} rows do not match the manifest", 5)
            artifact_rows = {row["id"]: row for row in staged.execute("SELECT id,scope_id,sha256,size_bytes,relative_path,state FROM artifacts")}
            resources = {item["artifact_id"]: item for item in manifest.get("resources", [])}
            if set(artifact_rows) != set(resources):
                raise PmtError("migration_reference_invalid", "Artifact rows and byte references differ", 5)
            if set(expected_assets) != {item.get("sha256") for item in resources.values()}:
                raise PmtError("migration_reference_invalid", "Resource object bytes and artifact refs differ", 5)
            directive_specs = {row["directive_id"]: (row["step_id"], row["directive_version"])
                               for row in staged.execute("SELECT directive_id,step_id,directive_version FROM step_specs")}
            for artifact_id, row in artifact_rows.items():
                spec = resources[artifact_id]
                private = directive_specs.get(artifact_id)
                expected_purpose = "step_directive" if private else "migrated_resource"
                expected_private = (private[0], private[1]) if private else (None, None)
                if ((row["scope_id"], row["sha256"], row["size_bytes"], row["state"], row["relative_path"])
                        != (spec["scope_id"], spec["sha256"], spec["size_bytes"], "ready",
                            "resources/objects/" + spec["sha256"])
                        or (spec.get("purpose"), spec.get("private_step_id"), spec.get("directive_version"))
                        != (expected_purpose, expected_private[0], expected_private[1])):
                    raise PmtError("migration_reference_invalid", "Artifact row differs from its resource manifest", 5)
            _validate_business_references(staged)
        return manifest

    def create_host_backup(self, source_host, destination, headers: dict, *, bundle_id: str | None = None) -> dict:
        """Create a sanitized Host snapshot after current admin/write auth; no Git checkout is inspected."""
        source_db = source_host.db

        def authorize(conn):
            principal = source_host.principal(conn, headers)
            principal.require("admin")
            principal.require("write")

        with closing(source_db.connect()) as conn:
            authorize(conn)
        return self.create_backup(source_db, destination, [], source_kind="host", source_authorizer=authorize,
                                  bundle_id=bundle_id)

    def restore_backup(self, target_host, bundle_path, headers: dict) -> dict:
        """Import into an empty existing Host namespace; never swaps DBs or changes source state."""
        manifest = self.verify_backup(bundle_path)
        target_db = target_host.db
        if int(_meta_from_db(target_db, "schema_version") or 0) != SCHEMA_VERSION:
            raise PmtError("migration_target_version_incompatible", "Target core DB schema does not match", 3)
        auth = target_host.auth
        if int(_meta_from_db(target_db, "host_schema_version") or 0) != HOST_SCHEMA_VERSION:
            raise PmtError("migration_target_version_incompatible", "Target Host auth schema does not match", 3)
        HostResourceStore(target_db, auth)
        owner = "migration-target-" + manifest["bundle_id"]
        _acquire_maintenance(target_db, owner)
        try:
            with closing(target_db.connect()) as conn:
                principal = target_host.principal(conn, headers)
                principal.require("admin")
                principal.require("write")
                namespace_id = auth.namespace_id
                existing = _meta(conn, _BUNDLE_MARKER_PREFIX + manifest["bundle_id"])
                if existing:
                    if existing != manifest["manifest_sha256"]:
                        raise PmtError("migration_bundle_conflict", "Bundle ID was already imported with different content", 3)
                    _verify_imported_rows(conn, Path(bundle_path) / "transfer.sqlite3", manifest)
                    return MigrationReceipt(manifest["bundle_id"], manifest["manifest_sha256"], "replayed",
                        {name: row["count"] for name, row in manifest["tables"].items()},
                        len(manifest["resources"]), manifest["derived_state_invalidated"], namespace_id).to_dict()
                _assert_quiescent(conn, label="Target namespace")
                _business_empty(conn)
            self._publish_resources(target_db, Path(bundle_path), manifest)
            transfer_db = Path(bundle_path) / "transfer.sqlite3"
            with closing(target_db.connect()) as conn:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("ATTACH DATABASE ? AS pmt_migration", (str(transfer_db),))
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if _meta(conn, "maintenance_owner") != owner:
                        raise PmtError("migration_maintenance_lost", "Target maintenance barrier was lost", 4)
                    principal = target_host.principal(conn, headers)
                    principal.require("admin")
                    principal.require("write")
                    if _meta(conn, "host_namespace_id") != namespace_id:
                        raise PmtError("migration_target_changed", "Host namespace changed during import", 3)
                    if _meta(conn, "schema_version") != str(SCHEMA_VERSION) or \
                            _meta(conn, "host_schema_version") != str(HOST_SCHEMA_VERSION):
                        raise PmtError("migration_target_version_incompatible", "Target versions changed during import", 3)
                    if _meta(conn, _BUNDLE_MARKER_PREFIX + manifest["bundle_id"]):
                        raise PmtError("migration_target_changed", "Migration marker appeared during import", 3)
                    _assert_quiescent(conn, label="Target namespace")
                    _business_empty(conn)
                    stage_conn = sqlite3.connect(str(transfer_db))
                    stage_conn.row_factory = sqlite3.Row
                    try:
                        for table in INSERT_ORDER:
                            target_columns = _columns(conn, table)
                            staged_columns = _columns(stage_conn, table)
                            if target_columns != staged_columns:
                                raise PmtError("migration_schema_incompatible", f"Target {table} columns differ", 3)
                            names = ",".join(_quote(name) for name in target_columns)
                            conn.execute(f"INSERT INTO {_quote(table)}({names}) SELECT {names} FROM pmt_migration.{_quote(table)}")
                        now = utc_now()
                        metadata_rows = []
                        for item in manifest["resources"]:
                            metadata_rows.append((item["artifact_id"], item["scope_id"], item["purpose"],
                                item.get("private_step_id"), None, item.get("directive_version"),
                                principal.device_id, principal.session_id, str(uuid.uuid5(uuid.UUID(manifest["bundle_id"]),
                                    "resource-metadata:" + item["artifact_id"])), now))
                        if metadata_rows:
                            conn.executemany("INSERT INTO host_resource_metadata(artifact_id,scope_id,purpose,private_step_id,run_id,directive_version,owner_device_id,owner_session_id,request_id,created_at) "
                                             "VALUES(?,?,?,?,?,?,?,?,?,?)", metadata_rows)
                        _validate_business_references(conn)
                        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
                        if fk_errors:
                            raise PmtError("migration_reference_invalid", "Imported rows failed foreign-key verification", 5,
                                           False, {"violation_count": len(fk_errors)})
                        _verify_imported_rows(conn, transfer_db, manifest)
                        db_identity = _meta(conn, "db_id")
                        if not db_identity:
                            raise PmtError("migration_target_metadata_invalid", "Target Host database identity is missing", 5)
                        receipt = {"bundle_id": manifest["bundle_id"],
                            "manifest_sha256": manifest["manifest_sha256"], "state": "imported",
                            "counts": {name: row["count"] for name, row in manifest["tables"].items()},
                            "resource_count": len(manifest["resources"]),
                            "invalidated_derived": manifest["derived_state_invalidated"],
                            "target_namespace_id": namespace_id,
                            "target_database_identity_sha256": _digest_bytes(db_identity.encode("utf-8")),
                            "imported_at": now, "tier": "local-fixture-preparation"}
                        target_db._put_meta(conn, _BUNDLE_MARKER_PREFIX + manifest["bundle_id"],
                                            manifest["manifest_sha256"])
                        target_db._put_meta(conn, _BUNDLE_MARKER_PREFIX + manifest["bundle_id"] + ":receipt",
                                            canonical_json(receipt))
                        conn.commit()
                    except Exception:
                        if conn.in_transaction:
                            conn.rollback()
                        raise
                    finally:
                        stage_conn.close()
                        conn.execute("DETACH DATABASE pmt_migration")
                except Exception:
                    if conn.in_transaction:
                        conn.rollback()
                    raise
            self._after_commit(target_db, manifest)
            with closing(target_db.connect()) as conn:
                _verify_imported_rows(conn, transfer_db, manifest)
                receipt_json = _meta(conn, _BUNDLE_MARKER_PREFIX + manifest["bundle_id"] + ":receipt")
                receipt = json.loads(receipt_json) if receipt_json else None
            if not isinstance(receipt, dict):
                raise PmtError("migration_receipt_missing", "Committed import has no durable receipt", 5)
            return receipt
        finally:
            _release_maintenance(target_db, owner)

    def _publish_resources(self, target_db, bundle_root: Path, manifest: dict) -> None:
        for item in manifest["resources"]:
            source = bundle_root / item["bundle_path"]
            digest, size = _digest_file(source)
            if (digest, size) != (item["sha256"], item["size_bytes"]):
                raise PmtError("migration_bundle_corrupt", "Resource changed before Host publication", 5)
            relative = "resources/objects/" + digest
            target = local_resources._artifact_path(target_db, relative)
            local_resources._reject_links(target.parent)
            target.parent.mkdir(parents=True, exist_ok=True)
            local_resources._reject_links(target.parent)
            if target.exists():
                if _digest_file(target) != (digest, size):
                    raise PmtError("migration_resource_conflict", "Target content-addressed object has different bytes", 5)
                continue
            fd, name = tempfile.mkstemp(prefix=".pmt-migration-", dir=target.parent)
            tmp = Path(name)
            try:
                with os.fdopen(fd, "wb") as stream, source.open("rb") as source_stream:
                    shutil.copyfileobj(source_stream, stream, length=1024 * 1024)
                    stream.flush()
                    os.fsync(stream.fileno())
                local_resources._reject_links(target.parent)
                try:
                    os.link(tmp, target)
                except FileExistsError:
                    if _digest_file(target) != (digest, size):
                        raise PmtError("migration_resource_conflict", "Concurrent target object has different bytes", 5)
                except OSError as exc:
                    raise PmtError("migration_link_unsupported", "Target filesystem cannot publish resources without replacement", 4,
                                   True, {"errno": getattr(exc, "errno", None)}) from exc
            finally:
                tmp.unlink(missing_ok=True)
            if _digest_file(target) != (digest, size):
                raise PmtError("migration_resource_corrupt", "Published Host resource failed hash verification", 5)

    @staticmethod
    def _after_commit(_target_db, _manifest) -> None:
        """Narrow test seam for crash-after-commit recovery; no callback is exposed on the API."""
        return None

    def stage_import(self, target_host, bundle_path, headers: dict) -> dict:
        """Stage a verified bundle into the supplied empty Host fixture namespace."""
        return self.restore_backup(target_host, bundle_path, headers)

    def verify(self, bundle_path) -> dict:
        return self.verify_backup(bundle_path)

    def restore(self, target_host, bundle_path, headers: dict) -> dict:
        return self.restore_backup(target_host, bundle_path, headers)

    def switch_primary(self, *_args, **_kwargs) -> dict:
        raise PmtError("migration_primary_switch_unavailable",
                       "F13 preparation does not change the local primary; an F12 verified config receipt is required", 3)


def _meta_from_db(db, key):
    with closing(db.connect()) as conn:
        return _meta(conn, key)


def _validate_business_references(conn: sqlite3.Connection) -> None:
    if "step_specs" in _table_names(conn):
        missing = conn.execute("SELECT COUNT(*) FROM step_specs s LEFT JOIN artifacts a ON a.id=s.directive_id "
                               "WHERE a.id IS NULL").fetchone()[0]
        if missing:
            raise PmtError("migration_reference_invalid", "A Step directive artifact reference is missing", 5)
    if "plans" in _table_names(conn):
        missing = conn.execute("SELECT COUNT(*) FROM plans p LEFT JOIN artifacts a ON a.id=p.artifact_id "
            "WHERE p.artifact_id IS NOT NULL AND a.id IS NULL").fetchone()[0]
        if missing:
            raise PmtError("migration_reference_invalid", "A plan artifact reference is missing", 5)
    if "artifact_refs" in _table_names(conn):
        missing = conn.execute("SELECT COUNT(*) FROM artifact_refs r LEFT JOIN artifacts a ON a.id=r.artifact_id "
                               "WHERE a.id IS NULL").fetchone()[0]
        if missing:
            raise PmtError("migration_reference_invalid", "An artifact reference points to a missing resource", 5)
    if "verifications" in _table_names(conn):
        for row in conn.execute("SELECT evidence_json FROM verifications"):
            try:
                refs = json.loads(row[0])
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise PmtError("migration_source_corrupt", "Verification evidence JSON is invalid", 5) from exc
            if not isinstance(refs, list):
                raise PmtError("migration_source_corrupt", "Verification evidence must be an array", 5)
            for value in refs:
                artifact_id = value.get("artifact_id") if isinstance(value, dict) else value
                if not isinstance(artifact_id, str) or not conn.execute("SELECT 1 FROM artifacts WHERE id=?", (artifact_id,)).fetchone():
                    raise PmtError("migration_reference_invalid", "Verification evidence references a missing resource", 5)


def _verify_imported_rows(target: sqlite3.Connection, staged_path: Path, manifest: dict) -> None:
    with closing(sqlite3.connect(str(staged_path))) as staged:
        staged.row_factory = sqlite3.Row
        for table, expected in manifest["tables"].items():
            if not _table_exists(target, table):
                raise PmtError("migration_schema_incompatible", f"Target table {table} is missing", 3)
            rows = _canonical_rows(target, table)
            actual = {"count": len(rows), "ids_sha256": _id_digest(rows, _primary_key(target, table)),
                      "rows_sha256": _row_digest(rows)}
            if actual != expected:
                raise PmtError("migration_import_corrupt", f"Imported target {table} differs from the verified bundle", 5)
        for item in manifest["resources"]:
            row = target.execute("SELECT sha256,size_bytes,relative_path,state FROM artifacts WHERE id=?",
                                 (item["artifact_id"],)).fetchone()
            if not row or tuple(row) != (item["sha256"], item["size_bytes"],
                    "resources/objects/" + item["sha256"], "ready"):
                raise PmtError("migration_import_corrupt", "Imported artifact metadata differs from the bundle", 5)
            metadata = target.execute("SELECT scope_id,purpose,private_step_id,directive_version FROM host_resource_metadata WHERE artifact_id=?",
                                      (item["artifact_id"],)).fetchone()
            if not metadata or tuple(metadata) != (item["scope_id"], item["purpose"],
                    item.get("private_step_id"), item.get("directive_version")):
                raise PmtError("migration_import_corrupt", "Imported Host resource ownership metadata differs from the verified bundle", 5)
