#!/usr/bin/env python3
"""P9 Claude Code native lifecycle smoke test using an isolated local marketplace.

The product is real; model responses come from scripts/native_stream_stub.py and
are not evidence of a real LLM. The script never reads or copies user settings or
credentials. Running installation/session steps requires --execute --g1-approved
and explicit package paths.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = PROJECT_ROOT / ".pmt-test" / "native-claude" / "runs"
PLUGIN_NAME = "pmt-lifecycle"
MARKETPLACE_NAME = "pmt-local"
CLAUDE_VERSION = "2.1.283"
CORE_VERSIONS = ("0.1.0", "0.1.1")
MODEL = "claude-haiku-4-5"
PROMPT = "Summarize the saved PMT task context in one short sentence. Do not edit files."


class SmokeFailure(RuntimeError):
    def __init__(self, step: str, code: str, exit_code: int | None = None, detail: str | None = None):
        super().__init__(code)
        self.step = step
        self.code = code
        self.exit_code = exit_code
        self.detail = detail


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SmokeFailure("package_validation", "package_manifest_invalid") from exc
    if not isinstance(value, dict):
        raise SmokeFailure("package_validation", "package_manifest_invalid")
    return value


def _validate_package(directory: Path, version: str, claude_exe: Path | None = None) -> dict[str, Any]:
    try:
        root = directory.resolve(strict=True)
    except OSError as exc:
        raise SmokeFailure("package_validation", "package_directory_missing") from exc
    if not root.is_dir():
        raise SmokeFailure("package_validation", "package_directory_missing")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SmokeFailure("package_validation", "package_symlink_rejected")
    manifest_path = root / "pmt-package.json"
    manifest = _json_file(manifest_path)
    if manifest.get("product") != "claude" or manifest.get("plugin_name") != PLUGIN_NAME:
        raise SmokeFailure("package_validation", "wrong_package_identity")
    if manifest.get("plugin_version") != version:
        raise SmokeFailure("package_validation", "wrong_package_version")
    hashes = manifest.get("files")
    if not isinstance(hashes, dict) or not hashes:
        raise SmokeFailure("package_validation", "package_file_manifest_missing")
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual != set(hashes) | {"pmt-package.json"}:
        raise SmokeFailure("package_validation", "package_file_set_mismatch")
    for relative, digest in hashes.items():
        rel = PurePosixPath(relative)
        if rel.is_absolute() or ".." in rel.parts or not isinstance(digest, str):
            raise SmokeFailure("package_validation", "unsafe_package_manifest_path")
        try:
            candidate = root.joinpath(*rel.parts).resolve(strict=True)
            candidate.relative_to(root)
        except (OSError, ValueError) as exc:
            raise SmokeFailure("package_validation", "package_path_escape") from exc
        if not candidate.is_file() or _sha256(candidate) != digest:
            raise SmokeFailure("package_validation", "package_hash_mismatch")
    plugin = _json_file(root / ".claude-plugin" / "plugin.json")
    market = _json_file(root / ".claude-plugin" / "marketplace.json")
    if plugin.get("name") != PLUGIN_NAME or plugin.get("version") != version:
        raise SmokeFailure("package_validation", "plugin_manifest_mismatch")
    if market.get("name") != MARKETPLACE_NAME:
        raise SmokeFailure("package_validation", "marketplace_manifest_mismatch")
    listings = market.get("plugins")
    entry = next((item for item in listings or [] if isinstance(item, dict) and item.get("name") == PLUGIN_NAME), None)
    if not entry or entry.get("source") != "./" or entry.get("version") != version:
        raise SmokeFailure("package_validation", "marketplace_plugin_entry_mismatch")
    return {"version": version, "manifest_sha256": _sha256(manifest_path),
            "file_count": len(hashes), "core_version": manifest.get("core_version"),
            "schema_version": manifest.get("schema_version"), "product": "claude"}


def _validate_plugins(claude: Path, package: Path, env: dict[str, str], cwd: Path,
                      version: str, report: dict[str, Any]) -> dict[str, Any]:
    try:
        completed = subprocess.run([str(claude), "plugin", "validate", str(package), "--json", "--strict"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", cwd=cwd, env=env, timeout=30, check=False)
    except subprocess.TimeoutExpired as exc:
        raise SmokeFailure(f"validate_{version}", "plugin_validation_timeout") from exc
    try:
        result = json.loads(completed.stdout)
    except ValueError as exc:
        raise SmokeFailure(f"validate_{version}", "plugin_validation_invalid_response", completed.returncode) from exc
    manifest = result.get("manifest") if isinstance(result, dict) else None
    errors = manifest.get("errors") if isinstance(manifest, dict) else None
    report["steps"].append({"step": f"validate_{version}",
                            "status": "passed" if completed.returncode == 0 and result.get("success") is True else "failed",
                            "exit_code": completed.returncode,
                            "warning_count": len(manifest.get("warnings", [])) if isinstance(manifest, dict) else 0})
    if completed.returncode != 0 or result.get("success") is not True or errors:
        raise SmokeFailure(f"validate_{version}", "plugin_validation_failed", completed.returncode)
    return {"status": "passed", "exit_code": completed.returncode,
            "warning_count": len(manifest.get("warnings", []))}


def _base_environment(run_root: Path) -> dict[str, str]:
    home = run_root / "isolated-home"
    for path in (home, home / "AppData" / "Roaming", home / "AppData" / "Local", run_root / "tmp"):
        path.mkdir(parents=True, exist_ok=True)
    base_path = os.environ.get("PATH", "")
    python_bin = str(Path(sys.executable).resolve().parent)
    isolated_path = python_bin + (os.pathsep + base_path if base_path else "")
    env = {"PATH": isolated_path, "HOME": str(home), "USERPROFILE": str(home),
           "APPDATA": str(home / "AppData" / "Roaming"), "LOCALAPPDATA": str(home / "AppData" / "Local"),
           "TEMP": str(run_root / "tmp"), "TMP": str(run_root / "tmp"),
           "CLAUDE_CONFIG_DIR": str(run_root / "claude-config"),
           "CLAUDE_CODE_PLUGIN_CACHE_DIR": str(run_root / "plugin-cache"),
           "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_SYNC_PLUGIN_INSTALL": "1",
           "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1", "DISABLE_AUTOUPDATER": "1",
           "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
           "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    for name in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "PROCESSOR_ARCHITECTURE"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    for path in (Path(env["CLAUDE_CONFIG_DIR"]), Path(env["CLAUDE_CODE_PLUGIN_CACHE_DIR"])):
        path.mkdir(parents=True, exist_ok=True)
    return env


def _invoke(argv: list[str], env: dict[str, str], cwd: Path, timeout: float, step: str,
            report: dict[str, Any], *, stdin: str | None = None, must_succeed=True) -> subprocess.CompletedProcess[str]:
    try:
        options = {"text": True, "encoding": "utf-8", "stdout": subprocess.PIPE,
                   "stderr": subprocess.PIPE, "cwd": cwd, "env": env,
                   "timeout": timeout, "check": False}
        if stdin is None:
            options["stdin"] = subprocess.DEVNULL
        else:
            options["input"] = stdin
        completed = subprocess.run(argv, **options)
    except subprocess.TimeoutExpired as exc:
        report["steps"].append({"step": step, "status": "timeout", "exit_code": None})
        if must_succeed:
            raise SmokeFailure(step, "timeout") from exc
        return subprocess.CompletedProcess(argv, None, "", "")
    report["steps"].append({"step": step, "status": "passed" if completed.returncode == 0 else "failed",
                            "exit_code": completed.returncode})
    if must_succeed and completed.returncode != 0:
        raise SmokeFailure(step, "command_failed", completed.returncode)
    return completed


def _base_environment_for_pmt(data_root: Path, config_root: Path) -> dict[str, str]:
    source = str(PROJECT_ROOT / "src")
    base_path = os.environ.get("PATH", "")
    python_bin = str(Path(sys.executable).resolve().parent)
    isolated_home = data_root.parent / "isolated-home"
    temp_root = data_root.parent / "tmp"
    for path in (isolated_home, isolated_home / "AppData" / "Roaming",
                 isolated_home / "AppData" / "Local", temp_root):
        path.mkdir(parents=True, exist_ok=True)
    env = {"PATH": python_bin + (os.pathsep + base_path if base_path else ""),
           "PMT_DATA_ROOT": str(data_root), "PMT_CONFIG_ROOT": str(config_root),
           "PYTHONPATH": source, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "HOME": str(isolated_home), "USERPROFILE": str(isolated_home),
           "APPDATA": str(isolated_home / "AppData" / "Roaming"),
           "LOCALAPPDATA": str(isolated_home / "AppData" / "Local"),
           "TEMP": str(temp_root), "TMP": str(temp_root)}
    for name in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def _read_plugin_state(claude: Path, env: dict[str, str], cwd: Path, report: dict[str, Any],
                       expected_version: str) -> dict[str, Any]:
    completed = _invoke([str(claude), "plugin", "list", "--json"], env, cwd, 30,
                        f"plugin_list_{expected_version}", report)
    try:
        value = json.loads(completed.stdout)
    except ValueError as exc:
        raise SmokeFailure("plugin_inventory", "invalid_plugin_list_json", completed.returncode) from exc
    matches = []

    def visit(node, key_hint=""):
        if isinstance(node, dict):
            names = [node.get(k) for k in ("name", "id", "plugin", "plugin_id")]
            if key_hint:
                names.append(key_hint)
            matched = any(isinstance(name, str) and
                          (name == PLUGIN_NAME or name.startswith(PLUGIN_NAME + "@")) for name in names)
            if matched:
                version = node.get("version")
                scope = node.get("scope")
                status = node.get("status")
                enabled = node.get("enabled")
                if version is not None:
                    matches.append({"version": str(version), "scope": scope,
                                    "enabled": enabled, "status": status})
            for key, child in node.items():
                visit(child, key if isinstance(key, str) else "")
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(value)
    state = next((item for item in matches if item.get("scope") in (None, "user")), None)
    if state is None or state["version"] != expected_version:
        raise SmokeFailure("plugin_version_check", "installed_plugin_version_mismatch")
    if state.get("enabled") is False or state.get("status") in {"disabled", "inactive"}:
        raise SmokeFailure("plugin_version_check", "installed_plugin_not_enabled")
    return {"version": state["version"], "scope": state.get("scope") or "unknown",
            "enabled": state.get("enabled") is not False and state.get("status") not in {"disabled", "inactive"}}


def _copy_exact_package(source: Path, destination: Path) -> None:
    if destination.exists():
        raise SmokeFailure("marketplace_stage", "staging_directory_exists")
    shutil.copytree(source, destination, symlinks=False)


def _sync_staged_package(source: Path, destination: Path) -> None:
    source_files: dict[str, Path] = {}
    for path in source.rglob("*"):
        if path.is_symlink():
            raise SmokeFailure("marketplace_stage", "source_symlink_rejected")
        if path.is_file():
            source_files[path.relative_to(source).as_posix()] = path
    staged_files: dict[str, Path] = {}
    for path in destination.rglob("*"):
        if path.is_symlink():
            raise SmokeFailure("marketplace_stage", "staging_symlink_rejected")
        if path.is_file():
            staged_files[path.relative_to(destination).as_posix()] = path
    for relative in set(staged_files) - set(source_files):
        staged_files[relative].unlink()
    for relative, source_file in source_files.items():
        target = destination / Path(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, target)
    for directory in sorted((path for path in destination.rglob("*") if path.is_dir()),
                            key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def _start_stub(run_root: Path, sentinel: str) -> tuple[subprocess.Popen, int, Path]:
    stats = run_root / "stub-stats.json"
    command = [sys.executable, str(PROJECT_ROOT / "scripts" / "native_stream_stub.py"),
               "--sentinel", sentinel, "--stats", str(stats), "--port", "0"]
    env = {"PATH": os.environ.get("PATH", ""), "TEMP": str(run_root / "tmp"), "TMP": str(run_root / "tmp")}
    for name in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    try:
        process = subprocess.Popen(command, cwd=PROJECT_ROOT, env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as exc:
        raise SmokeFailure("stub_start", "loopback_stub_start_failed") from exc
    first_line: queue.Queue[str] = queue.Queue(maxsize=1)
    def read_line():
        if process.stdout is not None:
            first_line.put(process.stdout.readline())
    threading.Thread(target=read_line, daemon=True).start()
    try:
        line = first_line.get(timeout=8)
        message = json.loads(line)
        if message.get("host") != "127.0.0.1" or message.get("real_llm") is not False:
            raise ValueError("invalid stub announcement")
        return process, int(message["port"]), stats
    except Exception as exc:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
        raise SmokeFailure("stub_start", "loopback_stub_ready_failed") from exc


def _stop_stub(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _stub_stats(path: Path) -> list[bool]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("real_llm") is not False or not isinstance(value.get("requests"), list):
            return []
        return [item.get("context_seen") is True for item in value["requests"] if isinstance(item, dict)]
    except (OSError, ValueError):
        return []


def _run_native_session(claude: Path, env: dict[str, str], cwd: Path, mcp_config: Path,
                        stub_stats: Path, ordinal: int, report: dict[str, Any]) -> dict[str, Any]:
    before = _stub_stats(stub_stats)
    argv = [str(claude), "--print", "--output-format", "json", "--model", MODEL,
            "--max-turns", "1", "--permission-mode", "plan", "--permission-prompts", "none",
            "--setting-sources", "user", "--strict-mcp-config", "--mcp-config", str(mcp_config),
            "--", PROMPT]
    completed = _invoke(argv, env, cwd, 90, f"native_session_{ordinal}", report,
                        stdin=None, must_succeed=False)
    if completed.returncode != 0:
        output = (completed.stdout + "\n" + completed.stderr).casefold()
        if any(term in output for term in ("api key", "authentication", "unauthorized", "401")):
            detail = "authentication_rejected"
        elif any(term in output for term in ("hook", "sessionstart", "command not found", "enoent")):
            detail = "hook_or_command_failure"
        elif any(term in output for term in ("connection refused", "connecterror", "network")):
            detail = "local_transport_failure"
        elif "mcp" in output:
            detail = "mcp_configuration_failure"
        else:
            detail = "product_command_failure"
        report["sessions"].append({"ordinal": ordinal, "exit_code": completed.returncode,
                                   "failure_kind": detail,
                                   "stub_request_count": len(_stub_stats(stub_stats))})
        raise SmokeFailure(f"native_session_{ordinal}", "claude_session_failed", completed.returncode, detail)
    try:
        response = json.loads(completed.stdout)
    except ValueError as exc:
        raise SmokeFailure(f"native_session_{ordinal}", "invalid_claude_json_response", completed.returncode) from exc
    if not isinstance(response, dict) or response.get("is_error") is True:
        raise SmokeFailure(f"native_session_{ordinal}", "claude_session_failed", completed.returncode)
    after = _stub_stats(stub_stats)
    observed = after[len(before):]
    context_seen = any(observed)
    response_text = response.get("result")
    response_confirmed = isinstance(response_text, str) and "PMT_NATIVE_CONTEXT_OK" in response_text
    if not context_seen or not response_confirmed:
        raise SmokeFailure(f"native_session_{ordinal}", "startup_context_not_confirmed", completed.returncode)
    return {"ordinal": ordinal, "exit_code": completed.returncode,
            "stub_context_seen": True, "response_confirmed": True,
            "stub_request_count": len(observed)}


def _pmt_binary(launcher: Path, data_root: Path, config_root: Path, request: dict[str, Any],
                report: dict[str, Any], label: str) -> dict[str, Any]:
    env = _base_environment_for_pmt(data_root, config_root)
    completed = _invoke([sys.executable, str(launcher), "--data-root", str(data_root),
                         "--config-root", str(config_root)], env, PROJECT_ROOT, 30,
                        label, report, stdin=json.dumps(request, ensure_ascii=False))
    try:
        response = json.loads(completed.stdout)
    except ValueError as exc:
        raise SmokeFailure(label, "invalid_pmt_response", completed.returncode) from exc
    if not isinstance(response, dict) or response.get("ok") is not True:
        error = response.get("error") if isinstance(response, dict) else None
        code = error.get("code") if isinstance(error, dict) else "pmt_request_failed"
        raise SmokeFailure(label, code, completed.returncode)
    result = response.get("result")
    return result if isinstance(result, dict) else {}


def _request(operation: str, payload: dict[str, Any], *, scope_id=None, record_id=None,
             expected_revision=None, actor="main", session_id="p9-claude-smoke"):
    value = {"protocol_version": 1, "operation": operation, "request_id": str(uuid.uuid4()),
             "actor": actor, "session_id": session_id, "payload": payload}
    if scope_id:
        value["scope_id"] = scope_id
    if record_id:
        value["record_id"] = record_id
    if expected_revision is not None:
        value["expected_revision"] = expected_revision
    return value


def _logical_data_signature(data_root: Path) -> dict[str, Any]:
    db_path = data_root / "pmt.sqlite3"
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint and checkpoint[0] != 0:
            raise SmokeFailure("data_signature", "database_checkpoint_busy")
        meta = {row["key"]: row["value"] for row in conn.execute(
            "SELECT key,value FROM meta WHERE key IN ('db_id','environment_id','installation:claude')")}
        records = [dict(row) for row in conn.execute("SELECT id,revision,state FROM records ORDER BY id")]
        artifacts = [dict(row) for row in conn.execute(
            "SELECT id,sha256,size_bytes,relative_path FROM artifacts WHERE state='ready' ORDER BY id")]
        conn.close()
    except (OSError, sqlite3.Error) as exc:
        raise SmokeFailure("data_signature", "database_read_failed") from exc
    artifact_hashes = []
    for item in artifacts:
        relative = PurePosixPath(item["relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise SmokeFailure("data_signature", "artifact_path_unsafe")
        path = data_root.joinpath(*relative.parts)
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise SmokeFailure("data_signature", "artifact_missing") from exc
        if digest != item["sha256"]:
            raise SmokeFailure("data_signature", "artifact_hash_mismatch")
        artifact_hashes.append({"artifact_id": item["id"], "sha256": digest})
    logical = {"meta": meta, "records": records, "artifacts": artifact_hashes}
    logical_hash = hashlib.sha256(json.dumps(logical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"db_id": meta.get("db_id"), "environment_id": meta.get("environment_id"),
            "installation_id": meta.get("installation:claude"), "logical_sha256": logical_hash,
            "db_file_sha256": _sha256(db_path), "artifact_hashes": artifact_hashes,
            "record_count": len(records)}


def _record_json(path: Path, value: dict[str, Any]) -> str:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return _sha256(path)


def _native_session_ids(db_path: Path) -> list[str]:
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            values = [row[0] for row in conn.execute(
                "SELECT session_id FROM requests WHERE actor='hook' AND session_id IS NOT NULL "
                "GROUP BY session_id ORDER BY MIN(created_at)")]
    except sqlite3.Error as exc:
        raise SmokeFailure("session_ids", "native_session_ids_unavailable") from exc
    if len(values) != 2:
        raise SmokeFailure("session_ids", "expected_two_native_hook_sessions")
    for value in values:
        try:
            if str(uuid.UUID(value)) != value:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise SmokeFailure("session_ids", "native_session_id_invalid") from exc
    return values


def _preservation_signature(data_root: Path) -> dict[str, Any]:
    db_path = data_root / "pmt.sqlite3"
    try:
        with sqlite3.connect(db_path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise SmokeFailure("preservation_integrity", "database_integrity_check_failed")
            meta = {row["key"]: row["value"] for row in conn.execute(
                "SELECT key,value FROM meta WHERE key IN ('db_id','schema_version','environment_id','installation:claude')")}
            records = [dict(row) for row in conn.execute("SELECT * FROM records ORDER BY id")]
            event_rows = [dict(row) for row in conn.execute("SELECT * FROM events ORDER BY id")]
            artifact_rows = [dict(row) for row in conn.execute(
                "SELECT id,sha256,size_bytes,relative_path FROM artifacts WHERE state='ready' ORDER BY id")]
    except (OSError, sqlite3.Error) as exc:
        raise SmokeFailure("preservation_signature", "database_read_failed") from exc
    digest_json = lambda value: hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    artifact_hashes = []
    for row in artifact_rows:
        relative = PurePosixPath(row["relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise SmokeFailure("preservation_signature", "artifact_path_unsafe")
        artifact_path = data_root.joinpath(*relative.parts)
        if not artifact_path.is_file() or artifact_path.is_symlink():
            raise SmokeFailure("preservation_signature", "artifact_missing_or_unsafe")
        actual = _sha256(artifact_path)
        if actual != row["sha256"]:
            raise SmokeFailure("preservation_signature", "artifact_hash_mismatch")
        artifact_hashes.append({"artifact_id": row["id"], "sha256": actual,
                                "size_bytes": row["size_bytes"], "relative_path": row["relative_path"]})
    return {"db_id": meta.get("db_id"), "schema_version": meta.get("schema_version"),
            "environment_id": meta.get("environment_id"),
            "installation_id": meta.get("installation:claude"),
            "record_count": len(records), "records_sha256": digest_json(records),
            "event_count": len(event_rows), "events_sha256": digest_json(event_rows),
            "ready_artifacts": artifact_hashes, "database_integrity": "ok"}


def _find_plugin_state(node, expected_name: str, key_hint=""):
    if isinstance(node, list):
        for child in node:
            found = _find_plugin_state(child, expected_name)
            if found:
                return found
        return None
    if not isinstance(node, dict):
        return None
    names = [node.get(key) for key in ("name", "id", "plugin", "plugin_id")]
    if key_hint:
        names.append(key_hint)
    match = any(isinstance(name, str) and (name == expected_name or name.startswith(expected_name + "@"))
                for name in names)
    if match and isinstance(node.get("version"), (str, int, float)):
        return {"version": str(node["version"]), "scope": node.get("scope"),
                "enabled": node.get("enabled"), "status": node.get("status")}
    for key, child in node.items():
        found = _find_plugin_state(child, expected_name, key if isinstance(key, str) else "")
        if found:
            return found
    return None


def _assert_installed(claude: Path, env: dict[str, str], cwd: Path,
                      expected_version: str, report: dict[str, Any], label: str) -> dict[str, Any]:
    completed = _invoke([str(claude), "plugin", "list", "--json"], env, cwd, 30,
                        label, report)
    try:
        inventory = json.loads(completed.stdout)
    except ValueError as exc:
        raise SmokeFailure(label, "invalid_plugin_inventory") from exc
    state = _find_plugin_state(inventory, PLUGIN_NAME)
    if state is None or state["version"] != expected_version:
        raise SmokeFailure(label, "installed_plugin_version_mismatch")
    if state.get("enabled") is False or state.get("status") in {"disabled", "inactive"}:
        raise SmokeFailure(label, "installed_plugin_disabled")
    return {"version": state["version"], "scope": state.get("scope") or "unknown", "enabled": True}


def _cached_package_hash(cache_root: Path, expected_version: str, expected_hash: str) -> bool:
    for manifest_path in cache_root.rglob("pmt-package.json"):
        if manifest_path.is_symlink():
            continue
        try:
            manifest = _json_file(manifest_path)
        except SmokeFailure:
            continue
        if (manifest.get("product") == "claude" and manifest.get("plugin_version") == expected_version
                and _sha256(manifest_path) == expected_hash):
            return True
    return False


def _cached_package_root(cache_root: Path, expected_version: str, expected_hash: str) -> Path:
    for manifest_path in cache_root.rglob("pmt-package.json"):
        if manifest_path.is_symlink():
            continue
        try:
            manifest = _json_file(manifest_path)
        except SmokeFailure:
            continue
        if (manifest.get("product") == "claude" and manifest.get("plugin_version") == expected_version
                and _sha256(manifest_path) == expected_hash):
            root = manifest_path.parent
            launcher = root / "scripts" / "pmt.py"
            if launcher.is_file() and not launcher.is_symlink():
                return root
    raise SmokeFailure("plugin_cache_core", "installed_package_launcher_unavailable")


def _verification_input(claude_exe, manifest_hashes):
    return {
        "product": "claude", "product_version": CLAUDE_VERSION,
        "model": MODEL, "real_llm": False, "plugin_manifest_hashes": manifest_hashes,
        "session_count": 2, "session_start_context_required": True,
        "prompt_sha256": hashlib.sha256(PROMPT.encode("utf-8")).hexdigest(),
        "mcp_config": "isolated-empty", "settings_source": "isolated-user-only",
        "claude_executable_sha256": _sha256(claude_exe),
    }


def _verification_command(empty_mcp: Path) -> list[str]:
    return ["claude", "--print", "--output-format", "json", "--model", MODEL,
            "--max-turns", "1", "--permission-mode", "plan", "--permission-prompts", "none",
            "--setting-sources", "user", "--strict-mcp-config", "--mcp-config", str(empty_mcp)]


def _register_and_finish(launcher, data_root, config_root, run_root, state, verify_target,
                         before_fingerprint, command, inputs, session_results, report):
    evidence_dir = run_root / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / "native-session-context.json"
    evidence = {"evidence_type": "claude_native_session_and_package_lifecycle", "real_llm": False,
                "product_version": CLAUDE_VERSION, "model": MODEL,
                "sessions": session_results, "required_context_seen": True,
                "stop_did_not_finish_item": True, "package_manifest_hashes": inputs["plugin_manifest_hashes"]}
    evidence["plugin_versions"] = report.get("plugin_versions", {})
    evidence["data_preserved"] = report.get("data_preserved") is True
    evidence["data_signatures"] = {
        name: {key: value for key, value in signature.items()
               if key in {"db_id", "environment_id", "installation_id", "logical_sha256", "db_file_sha256"}}
        for name, signature in (("before", report.get("data_signature_before_plugin_reinstall", {})),
                                ("after", report.get("data_signature_after_plugin_reinstall", {})))
    }
    _record_json(evidence_path, evidence)
    artifact = _pmt_binary(launcher, data_root, config_root,
                           _request("register_resource", {"source_path": str(evidence_path),
                                                           "allowed_root": str(evidence_dir),
                                                           "retention": "evidence",
                                                           "owner_record_id": state["item_id"]},
                                    scope_id=state["scope_id"], record_id=state["item_id"]),
                           report, "pmt_register_native_evidence")
    artifact_id = artifact.get("artifact_id")
    if not isinstance(artifact_id, str):
        raise SmokeFailure("pmt_register_native_evidence", "artifact_id_missing")
    verification = _pmt_binary(launcher, data_root, config_root,
                               _request("record_verification", {
                                   **verify_target, "command": command, "inputs": inputs,
                                   "outcome": "pass", "exit_code": 0,
                                   "criterion_ids": ["C1", "C2"], "evidence_ids": [artifact_id],
                                   "before_fingerprint": before_fingerprint,
                               }, record_id=state["item_id"]), report, "pmt_record_verification")
    verification_id = verification.get("verification_id")
    if not isinstance(verification_id, str):
        raise SmokeFailure("pmt_record_verification", "verification_id_missing")
    finished = _pmt_binary(launcher, data_root, config_root,
                           _request("finish_task", {"claim_token": state["claim_token"],
                                                     "result": "Two fresh Claude sessions received saved PMT context; plugin update, rollback, removal/reinstallation, and PMT data preservation passed.",
                                                     "verification_ids": [verification_id]},
                                    record_id=state["item_id"], expected_revision=state["revision"],
                                    session_id="p9-claude-main"), report, "pmt_finish_item")
    if finished.get("state") != "Done":
        raise SmokeFailure("pmt_finish_item", "finish_not_done")
    state["claim_token"] = None
    state["revision"] = int(finished["revision"])
    report["pmt_evidence"] = {"artifact_id": artifact_id, "sha256": artifact.get("sha256"),
                              "verification_id": verification_id, "record_id": state["item_id"],
                              "scope_id": state["scope_id"], "finished_state": finished["state"]}


def _release_claim_if_needed(launcher, data_root, config_root, state, report):
    if not state or not state.get("claim_token"):
        return
    try:
        result = _pmt_binary(launcher, data_root, config_root,
                             _request("release_claim", {"claim_token": state["claim_token"],
                                                         "status": "Blocked", "reason": "P9 smoke did not complete",
                                                         "next_action": "Inspect native smoke failure and rerun."},
                                      record_id=state["item_id"], expected_revision=state["revision"],
                                      session_id="p9-claude-main"), report, "pmt_release_failed_smoke")
        report["claim_released_after_failure"] = result.get("state") == "Blocked"
    except Exception:
        report["claim_released_after_failure"] = False


def _execute(args, claude: Path, preflight: dict[str, Any]) -> int:
    package010 = _validate_package(args.package_010, "0.1.0")
    package011 = _validate_package(args.package_011, "0.1.1")
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:16]
    run_root = RUNS_ROOT / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    for name in ("tmp", "workspace", "data", "config", "claude-config", "plugin-cache", "evidence"):
        (run_root / name).mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {**preflight, "status": "running", "run_id": run_id,
                              "package_manifest_hashes": preflight["package_manifest_hashes"],
                              "steps": [], "sessions": [], "evidence_file": "evidence.json"}
    product_env = _base_environment(run_root)
    data_root, config_root = run_root / "data", run_root / "config"
    workspace = run_root / "workspace"
    empty_mcp = run_root / "empty-mcp.json"
    empty_mcp.write_text(json.dumps({"mcpServers": {}}, separators=(",", ":")), encoding="utf-8")
    staged_marketplace = run_root / "marketplace"
    stub_process = None
    state = None
    try:
        version_result = _invoke([str(claude), "--version"], product_env, workspace, 15,
                                 "claude_version", report)
        version_match = re.search(r"\b\d+\.\d+\.\d+\b", version_result.stdout)
        actual_version = version_match.group(0) if version_match else None
        report["runtime"] = {"claude_version": actual_version, "python": sys.version.split()[0],
                             "os": sys.platform, "sqlite": sqlite3.sqlite_version}
        if actual_version != CLAUDE_VERSION:
            raise SmokeFailure("claude_version", "target_product_version_mismatch", version_result.returncode)
        report["package_validation"] = {
            "0.1.0": _validate_plugins(claude, args.package_010.resolve(), product_env, workspace, "0.1.0", report),
            "0.1.1": _validate_plugins(claude, args.package_011.resolve(), product_env, workspace, "0.1.1", report),
        }
        _copy_exact_package(args.package_010.resolve(), staged_marketplace)
        marketplace = _json_file(staged_marketplace / ".claude-plugin" / "marketplace.json")
        if marketplace.get("name") != MARKETPLACE_NAME:
            raise SmokeFailure("marketplace_stage", "unexpected_marketplace_name")
        launcher = args.package_010.resolve() / "scripts" / "pmt.py"
        if not launcher.is_file():
            raise SmokeFailure("pmt_launcher", "standalone_launcher_missing")
        setup = _pmt_binary(launcher, data_root, config_root,
                            {"protocol_version": 1, "operation": "setup", "request_id": str(uuid.uuid4()),
                             "actor": "main", "session_id": "p9-claude-setup", "payload": {"product": "claude"}},
                            report, "pmt_setup")
        report["pmt_ids"] = {key: setup[key] for key in ("db_id", "environment_id", "installation_id", "schema_version")
                             if key in setup}
        scope = _pmt_binary(launcher, data_root, config_root,
                            _request("create_scope", {"kind": "project", "slug": "native-claude-p9"}),
                            report, "pmt_create_scope")
        scope_id = scope.get("scope_id") or scope.get("id")
        if not isinstance(scope_id, str):
            raise SmokeFailure("pmt_create_scope", "scope_id_missing")
        work = _pmt_binary(launcher, data_root, config_root,
                           _request("save_change", {"kind": "work", "title": "Claude native P9 smoke",
                                                     "reason": "Isolated product integration check",
                                                     "body": {"goal": "Verify native SessionStart context and package lifecycle."}},
                                    scope_id=scope_id), report, "pmt_create_work")
        work_id = work.get("record_id") or work.get("id")
        if not isinstance(work_id, str):
            raise SmokeFailure("pmt_create_work", "work_record_id_missing")
        sentinel = "PMT_NATIVE_CONTEXT_SENTINEL_" + uuid.uuid4().hex
        item = _pmt_binary(launcher, data_root, config_root,
                           _request("save_change", {"kind": "item", "title": "Restore saved startup context",
                                                     "reason": "Verify actual SessionStart hook delivery",
                                                     "parent_id": work_id,
                                                     "body": {"criteria": ["C1", "C2"], "workspace": str(workspace),
                                                              "content": f"Required startup marker: {sentinel}",
                                                              "next": "Verify two new Claude sessions receive the saved PMT context."}},
                                    scope_id=scope_id), report, "pmt_create_item")
        item_id = item.get("record_id") or item.get("id")
        if not isinstance(item_id, str):
            raise SmokeFailure("pmt_create_item", "item_record_id_missing")
        verify_target = {"definition_id": "p9-claude-native-context", "definition_version": "1",
                         "target_id": item_id}
        decision = _pmt_binary(launcher, data_root, config_root,
                               _request("save_decision", {"decision_kind": "select", "option_id": "loopback-stub",
                                                           "decider": "user", "reason": "Use the isolated local test server",
                                                           "confirmation_source": "explicit P9 test configuration"},
                                        record_id=item_id, expected_revision=int(item["revision"])),
                               report, "pmt_save_decision")
        claim = _pmt_binary(launcher, data_root, config_root,
                            _request("claim_task", {}, record_id=item_id,
                                     expected_revision=int(decision["revision"]), session_id="p9-claude-main"),
                            report, "pmt_claim_item")
        if not isinstance(claim.get("claim_token"), str):
            raise SmokeFailure("pmt_claim_item", "claim_token_missing")
        state = {"scope_id": scope_id, "work_id": work_id, "item_id": item_id,
                 "revision": int(claim["revision"]), "claim_token": claim["claim_token"]}

        _invoke([str(claude), "plugin", "marketplace", "add", str(staged_marketplace), "--scope", "user"],
                product_env, workspace, 45, "marketplace_add_v010", report)
        _invoke([str(claude), "plugin", "install", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
                 "--scope", "user", "--json"], product_env, workspace, 60, "plugin_install_v010", report)
        installed010 = _assert_installed(claude, product_env, workspace, "0.1.0", report, "plugin_list_v010")
        if not _cached_package_hash(Path(product_env["CLAUDE_CODE_PLUGIN_CACHE_DIR"]),
                                    "0.1.0", package010["manifest_sha256"]):
            raise SmokeFailure("plugin_cache_v010", "installed_package_hash_mismatch")
        launcher = (_cached_package_root(Path(product_env["CLAUDE_CODE_PLUGIN_CACHE_DIR"]),
                                         "0.1.0", package010["manifest_sha256"]) / "scripts" / "pmt.py")
        report["pmt_core_source"] = "installed_plugin_cache_0.1.0"
        report["plugin_versions"] = {"installed_initial": installed010["version"]}

        stub_stats = run_root / "stub-stats.json"
        stub_process, port, stub_stats = _start_stub(run_root, sentinel)
        product_env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
        product_env["ANTHROPIC_API_KEY"] = "pmt-test-not-secret"
        product_env["PMT_PYTHON"] = sys.executable
        product_env["PMT_DATA_ROOT"] = str(data_root)
        product_env["PMT_CONFIG_ROOT"] = str(config_root)
        product_env["PMT_SCOPE_ID"] = scope_id
        product_env["PMT_RECORD_ID"] = item_id

        command = _verification_command(empty_mcp)
        inputs = _verification_input(claude, {"0.1.0": package010["manifest_sha256"],
                                             "0.1.1": package011["manifest_sha256"]})
        before = _pmt_binary(launcher, data_root, config_root,
                             _request("lookup_verification", {**verify_target, "command": command,
                                                               "inputs": inputs, "criterion_ids": ["C1", "C2"]},
                                      record_id=item_id),
                             report, "pmt_verification_before_snapshot")
        before_fingerprint = before.get("input_fingerprint")
        if not isinstance(before_fingerprint, str) or len(before_fingerprint) != 64:
            raise SmokeFailure("pmt_verification_before_snapshot", "before_fingerprint_missing")

        product_env["PMT_SCOPE_ID"] = scope_id
        product_env["PMT_RECORD_ID"] = item_id
        first_session = _run_native_session(claude, product_env, workspace, empty_mcp, stub_stats, 1, report)
        report["sessions"].append(first_session)
        db_first = _logical_data_signature(data_root)
        if db_first["db_id"] != setup.get("db_id"):
            raise SmokeFailure("session_one_state", "pmt_database_id_changed")
        if _item_state(data_root, item_id) != "In Progress":
            raise SmokeFailure("session_one_state", "native_stop_changed_task_state")

        _sync_staged_package(args.package_011.resolve(), staged_marketplace)
        _invoke([str(claude), "plugin", "marketplace", "update", MARKETPLACE_NAME],
                product_env, workspace, 45, "marketplace_update_v011", report)
        _invoke([str(claude), "plugin", "update", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
                 "--scope", "user", "--json"], product_env, workspace, 60, "plugin_update_v011", report)
        installed011 = _assert_installed(claude, product_env, workspace, "0.1.1", report, "plugin_list_v011")
        if not _cached_package_hash(Path(product_env["CLAUDE_CODE_PLUGIN_CACHE_DIR"]),
                                    "0.1.1", package011["manifest_sha256"]):
            raise SmokeFailure("plugin_cache_v011", "installed_package_hash_mismatch")
        report["plugin_versions"]["updated"] = installed011["version"]

        second_session = _run_native_session(claude, product_env, workspace, empty_mcp, stub_stats, 2, report)
        report["sessions"].append(second_session)
        with sqlite3.connect(data_root / "pmt.sqlite3") as conn:
            stop_count = conn.execute("SELECT count(*) FROM events WHERE event_type='turn_stopped'").fetchone()[0]
            start_count = conn.execute("SELECT count(*) FROM events WHERE event_type='session_started'").fetchone()[0]
            finish_events = conn.execute("SELECT count(*) FROM events WHERE event_type='task_finished' AND record_id=?",
                                         (item_id,)).fetchone()[0]
        if stop_count < 2 or start_count < 2 or finish_events != 0 or _item_state(data_root, item_id) != "In Progress":
            raise SmokeFailure("native_stop_state", "stop_event_changed_task_state")
        report["native_events"] = {"session_started": start_count, "turn_stopped": stop_count,
                                   "task_finished_before_explicit_finish": finish_events}

        baseline = _logical_data_signature(data_root)
        report["data_signature_before_plugin_reinstall"] = baseline

        # Roll the local marketplace package back to 0.1.0 using Claude's own
        # uninstall/install path; no direct cache edits or versionless copy.
        _sync_staged_package(args.package_010.resolve(), staged_marketplace)
        _invoke([str(claude), "plugin", "marketplace", "update", MARKETPLACE_NAME],
                product_env, workspace, 45, "marketplace_rollback_v010", report)
        _invoke([str(claude), "plugin", "uninstall", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
                 "--scope", "user", "--yes"], product_env, workspace, 60, "plugin_uninstall_for_rollback", report)
        _invoke([str(claude), "plugin", "install", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
                 "--scope", "user", "--json"], product_env, workspace, 60, "plugin_reinstall_rollback_v010", report)
        rollback = _assert_installed(claude, product_env, workspace, "0.1.0", report, "plugin_list_rollback_v010")
        report["plugin_versions"]["rollback"] = rollback["version"]

        _invoke([str(claude), "plugin", "uninstall", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
                 "--scope", "user", "--yes"], product_env, workspace, 60, "plugin_uninstall_final", report)
        _invoke([str(claude), "plugin", "marketplace", "remove", MARKETPLACE_NAME, "--scope", "user"],
                product_env, workspace, 45, "marketplace_remove", report)
        _invoke([str(claude), "plugin", "marketplace", "add", str(staged_marketplace), "--scope", "user"],
                product_env, workspace, 45, "marketplace_readd", report)
        _invoke([str(claude), "plugin", "install", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
                 "--scope", "user", "--json"], product_env, workspace, 60, "plugin_reinstall_final_v010", report)
        reinstalled = _assert_installed(claude, product_env, workspace, "0.1.0", report, "plugin_list_final_v010")
        report["plugin_versions"]["reinstalled"] = reinstalled["version"]
        after = _logical_data_signature(data_root)
        report["data_signature_after_plugin_reinstall"] = after
        if after != baseline:
            raise SmokeFailure("data_preservation", "pmt_data_changed_during_plugin_lifecycle")
        report["data_preserved"] = True
        _register_and_finish(launcher, data_root, config_root, run_root, state, verify_target,
                             before_fingerprint, command, inputs, report["sessions"], report)
        report["status"] = "pass"
        report["model_fixture"] = {"real_llm": False, "server": "127.0.0.1 loopback Anthropic Messages SSE",
                                   "model": MODEL}
    except SmokeFailure as exc:
        report["status"] = "fail"
        report["failure"] = {"step": exc.step, "error_code": exc.code, "exit_code": exc.exit_code}
        if exc.detail:
            report["failure"]["detail"] = exc.detail
        _release_claim_if_needed(launcher if "launcher" in locals() else args.package_010 / "scripts" / "pmt.py",
                                 data_root if "data_root" in locals() else run_root / "data",
                                 config_root if "config_root" in locals() else run_root / "config",
                                 state, report)
    except Exception as exc:
        report["status"] = "fail"
        report["failure"] = {"step": "internal", "error_code": type(exc).__name__}
        _release_claim_if_needed(launcher if "launcher" in locals() else args.package_010 / "scripts" / "pmt.py",
                                 data_root if "data_root" in locals() else run_root / "data",
                                 config_root if "config_root" in locals() else run_root / "config",
                                 state, report)
    finally:
        _stop_stub(stub_process)
    evidence_file = run_root / "evidence.json"
    _record_json(evidence_file, report)
    report["evidence_file"] = f"runs/{run_id}/evidence.json"
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "pass" else 1


def _item_state(data_root: Path, record_id: str) -> str | None:
    try:
        with sqlite3.connect(data_root / "pmt.sqlite3", timeout=5) as conn:
            row = conn.execute("SELECT state FROM records WHERE id=?", (record_id,)).fetchone()
            return row[0] if row else None
    except sqlite3.Error as exc:
        raise SmokeFailure("item_state", "item_state_unavailable") from exc


def _preserve_existing_run(args, claude: Path, preflight: dict[str, Any]) -> int:
    run_id = args.preserve_run_id
    if not re.fullmatch(r"[0-9a-f]{16}", run_id):
        raise SmokeFailure("preservation_input", "invalid_run_id")
    run_root = RUNS_ROOT / run_id
    evidence_path = run_root / "evidence.json"
    try:
        report = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SmokeFailure("preservation_input", "completed_run_evidence_missing") from exc
    if report.get("status") != "pass" or report.get("run_id") != run_id or report.get("product") != "claude":
        raise SmokeFailure("preservation_input", "completed_claude_run_required")
    data_root = run_root / "data"
    db_path = data_root / "pmt.sqlite3"
    config_root = run_root / "config"
    workspace = run_root / "workspace"
    staged_marketplace = run_root / "marketplace"
    product_env = _base_environment(run_root)
    if not db_path.is_file() or not staged_marketplace.is_dir() or not workspace.is_dir():
        raise SmokeFailure("preservation_input", "completed_run_state_missing")
    pmt_ids = report.get("pmt_ids") or {}
    pmt_evidence = report.get("pmt_evidence") or {}
    if not pmt_ids.get("db_id") or not pmt_evidence.get("artifact_id"):
        raise SmokeFailure("preservation_input", "registered_finish_evidence_required")
    session_ids = _native_session_ids(db_path)
    sessions = report.get("sessions")
    if not isinstance(sessions, list) or len(sessions) != 2:
        raise SmokeFailure("preservation_input", "two_native_session_summaries_required")
    for result, session_id in zip(sessions, session_ids):
        result["session_id"] = session_id
    _record_json(evidence_path, report)

    package010 = _validate_package(args.package_010, "0.1.0")
    package011 = _validate_package(args.package_011, "0.1.1")
    # The pre-existing run owns a distinct cache root; retain it and use the exact
    # installed launcher that was validated during the original run.
    product_env["CLAUDE_CODE_PLUGIN_CACHE_DIR"] = str(run_root / "plugin-cache")
    launcher_path = _cached_package_root(Path(product_env["CLAUDE_CODE_PLUGIN_CACHE_DIR"]),
                                         "0.1.0", package010["manifest_sha256"]) / "scripts" / "pmt.py"
    report["pmt_core_source"] = "installed_plugin_cache_0.1.0"
    steps: list[dict[str, Any]] = []
    preserve_root = run_root / "preservation"
    if preserve_root.exists():
        raise SmokeFailure("preservation_input", "preservation_run_already_exists")
    preserve_root.mkdir(parents=True, exist_ok=False)
    backup_data = preserve_root / "backup-data"
    backup_data.mkdir()
    backup_db = backup_data / "pmt.sqlite3"
    try:
        source = sqlite3.connect(db_path, timeout=5)
        target = sqlite3.connect(backup_db)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
            source.close()
    except sqlite3.Error as exc:
        raise SmokeFailure("preservation_backup", "sqlite_backup_failed") from exc
    resources = data_root / "resources"
    if resources.exists():
        shutil.copytree(resources, backup_data / "resources")

    before = _preservation_signature(data_root)
    backup_signature = _preservation_signature(backup_data)
    if before != backup_signature or before["db_id"] != pmt_ids["db_id"]:
        raise SmokeFailure("preservation_backup", "backup_signature_mismatch")
    ready_ids = {item["artifact_id"] for item in before["ready_artifacts"]}
    if pmt_evidence["artifact_id"] not in ready_ids:
        raise SmokeFailure("preservation_backup", "registered_evidence_artifact_missing")

    try:
        version = _invoke([str(claude), "--version"], product_env, workspace, 15,
                          "preservation_claude_version", report)
        matched = re.search(r"\b\d+\.\d+\.\d+\b", version.stdout)
        if not matched or matched.group(0) != CLAUDE_VERSION:
            raise SmokeFailure("preservation_claude_version", "target_product_version_mismatch",
                               version.returncode)
        installed = _assert_installed(claude, product_env, workspace, "0.1.0", report,
                                      "preservation_plugin_list_initial")
        steps.append({"action": "initial_plugin_state", "version": installed["version"], "exit_code": 0})

        _sync_staged_package(args.package_011.resolve(), staged_marketplace)
        _invoke([str(claude), "plugin", "marketplace", "update", MARKETPLACE_NAME],
                product_env, workspace, 45, "preservation_marketplace_update_v011", report)
        _invoke([str(claude), "plugin", "update", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
                 "--scope", "user", "--json"], product_env, workspace, 60,
                "preservation_plugin_update_v011", report)
        updated = _assert_installed(claude, product_env, workspace, "0.1.1", report,
                                   "preservation_plugin_list_v011")
        if not _cached_package_hash(Path(product_env["CLAUDE_CODE_PLUGIN_CACHE_DIR"]),
                                    "0.1.1", package011["manifest_sha256"]):
            raise SmokeFailure("preservation_cache_v011", "installed_package_hash_mismatch")
        steps.extend([{"action": "marketplace_update_and_plugin_update", "version": updated["version"],
                       "exit_code": 0}])

        _sync_staged_package(args.package_010.resolve(), staged_marketplace)
        _invoke([str(claude), "plugin", "marketplace", "update", MARKETPLACE_NAME],
                product_env, workspace, 45, "preservation_marketplace_rollback_v010", report)
        _invoke([str(claude), "plugin", "uninstall", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
                 "--scope", "user", "--yes"], product_env, workspace, 60,
                "preservation_uninstall_for_rollback", report)
        _invoke([str(claude), "plugin", "install", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
                 "--scope", "user", "--json"], product_env, workspace, 60,
                "preservation_reinstall_rollback_v010", report)
        rollback = _assert_installed(claude, product_env, workspace, "0.1.0", report,
                                     "preservation_plugin_list_rollback_v010")
        steps.append({"action": "rollback_by_uninstall_reinstall", "version": rollback["version"],
                      "exit_code": 0})

        _invoke([str(claude), "plugin", "uninstall", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
                 "--scope", "user", "--yes"], product_env, workspace, 60,
                "preservation_uninstall_final", report)
        _invoke([str(claude), "plugin", "marketplace", "remove", MARKETPLACE_NAME, "--scope", "user"],
                product_env, workspace, 45, "preservation_marketplace_remove", report)
        _invoke([str(claude), "plugin", "marketplace", "add", str(staged_marketplace), "--scope", "user"],
                product_env, workspace, 45, "preservation_marketplace_readd", report)
        _invoke([str(claude), "plugin", "install", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
                 "--scope", "user", "--json"], product_env, workspace, 60,
                "preservation_reinstall_final_v010", report)
        reinstalled = _assert_installed(claude, product_env, workspace, "0.1.0", report,
                                        "preservation_plugin_list_final_v010")
        if not _cached_package_hash(Path(product_env["CLAUDE_CODE_PLUGIN_CACHE_DIR"]),
                                    "0.1.0", package010["manifest_sha256"]):
            raise SmokeFailure("preservation_cache_v010", "installed_package_hash_mismatch")
        steps.append({"action": "remove_readd_reinstall", "version": reinstalled["version"],
                      "exit_code": 0})
    except Exception:
        # Leave the isolated marketplace restored to 0.1.0 where possible.
        try:
            _sync_staged_package(args.package_010.resolve(), staged_marketplace)
            subprocess.run([str(claude), "plugin", "marketplace", "update", MARKETPLACE_NAME],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           cwd=workspace, env=product_env, timeout=45, check=False)
            subprocess.run([str(claude), "plugin", "uninstall", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
                            "--scope", "user", "--yes"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           cwd=workspace, env=product_env, timeout=60, check=False)
            subprocess.run([str(claude), "plugin", "install", f"{PLUGIN_NAME}@{MARKETPLACE_NAME}",
                            "--scope", "user", "--json"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           cwd=workspace, env=product_env, timeout=60, check=False)
        except Exception:
            pass
        raise

    after = _preservation_signature(data_root)
    if after != before:
        raise SmokeFailure("preservation_compare", "database_changed_during_plugin_lifecycle")

    restored_root = preserve_root / "restored-data"
    restored_root.mkdir()
    shutil.copy2(backup_db, restored_root / "pmt.sqlite3")
    if (backup_data / "resources").exists():
        shutil.copytree(backup_data / "resources", restored_root / "resources")
    restored = _preservation_signature(restored_root)
    if restored != before:
        raise SmokeFailure("preservation_restore", "restored_data_signature_mismatch")

    preservation = {"status": "pass", "run_id": run_id, "runtime": report.get("runtime"),
                    "native_session_ids": session_ids, "record_id": pmt_evidence.get("record_id"),
                    "scope_id": pmt_evidence.get("scope_id"),
                    "artifact_id": pmt_evidence.get("artifact_id"),
                    "artifact_sha256": pmt_evidence.get("sha256"),
                    "verification_id": pmt_evidence.get("verification_id"),
                    "finished_state": pmt_evidence.get("finished_state"),
                    "pmt_core_source": str(launcher_path.relative_to(run_root).as_posix()),
                    "package_manifest_hashes": {"0.1.0": package010["manifest_sha256"],
                                                "0.1.1": package011["manifest_sha256"]},
                    "commands": steps,
                    "command_results": [{"command": item["step"], "status": item["status"],
                                         "exit_code": item.get("exit_code")}
                                        for item in report.get("steps", [])
                                        if item.get("step", "").startswith("preservation_")],
                    "database_before": before, "backup": backup_signature,
                    "database_after": after, "restored": restored,
                    "restore_root": "preservation/restored-data",
                    "real_llm": False}
    preservation_path = run_root / "preservation.json"
    preservation_sha256 = _record_json(preservation_path, preservation)
    report["preservation_file"] = f"runs/{run_id}/preservation.json"
    report["preservation_sha256"] = preservation_sha256
    report["preservation_status"] = "pass"
    report["sessions"] = sessions
    _record_json(evidence_path, report)
    print(json.dumps(preservation, ensure_ascii=False, sort_keys=True))
    return 0


def _plan(args) -> dict[str, Any]:
    p0 = _validate_package(args.package_010, "0.1.0")
    p1 = _validate_package(args.package_011, "0.1.1")
    return {"status": "packages_ready", "product": "claude", "target_version": CLAUDE_VERSION,
            "model": MODEL, "model_role": "loopback_fixture_only", "real_llm": False,
            "package_manifest_hashes": {"0.1.0": p0["manifest_sha256"], "0.1.1": p1["manifest_sha256"]}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-010", required=True, type=Path)
    parser.add_argument("--package-011", required=True, type=Path)
    parser.add_argument("--claude-exe", type=Path, help="Target Claude Code CLI executable")
    parser.add_argument("--execute", action="store_true", help="Run isolated native install/session workflow")
    parser.add_argument("--g1-approved", action="store_true", help="Confirm the integrated G1 gate has passed")
    parser.add_argument("--preserve-run-id", help="Preserve package/data state for a completed native run")
    args = parser.parse_args(argv)
    try:
        prepared = _plan(args)
        if not args.execute:
            print(json.dumps({**prepared, "native_execution": "not_run"}, ensure_ascii=False))
            return 0
        if not args.g1_approved:
            print(json.dumps({"status": "blocked", "error_code": "g1_approval_required",
                              "native_execution": "not_run"}, ensure_ascii=False))
            return 2
        if prepared["status"] != "packages_ready":
            print(json.dumps({"status": "blocked", "error_code": "packages_not_ready"}, ensure_ascii=False))
            return 2
        claude = args.claude_exe.resolve() if args.claude_exe else Path(shutil.which("claude") or "")
        if not claude.is_file():
            print(json.dumps({"status": "blocked", "error_code": "claude_executable_unavailable"}, ensure_ascii=False))
            return 3
        if args.preserve_run_id:
            return _preserve_existing_run(args, claude, prepared)
        return _execute(args, claude, prepared)
    except SmokeFailure as exc:
        print(json.dumps({"status": "blocked", "step": exc.step, "error_code": exc.code,
                          "exit_code": exc.exit_code, "native_execution": "not_run"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
