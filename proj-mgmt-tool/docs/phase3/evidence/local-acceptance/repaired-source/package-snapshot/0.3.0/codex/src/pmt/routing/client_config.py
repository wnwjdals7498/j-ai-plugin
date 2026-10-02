"""Client-local routing settings for hosted mode; never opens the business store."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import sqlite3
from pathlib import Path

from ..db import semantic_request_fingerprint
from ..errors import PmtError
from ..service import normalize_request, response
from ..util import canonical_json
from . import service as routing


_DDL = """
CREATE TABLE IF NOT EXISTS routing_settings (
 id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision >= 1),
 body_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
-- routing.service emits local selection/audit events. This deliberately has
-- no FK to projects or records: the ConfigRoot port has no business scope DB.
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, record_id TEXT, scope_id TEXT,
 actor TEXT, event_type TEXT NOT NULL, reason TEXT, old_revision INTEGER, new_revision INTEGER,
 payload_json TEXT NOT NULL DEFAULT '{}', occurred_at TEXT, recorded_at TEXT NOT NULL,
 correlation_id TEXT, causation_id TEXT
);
CREATE TABLE IF NOT EXISTS client_routing_requests (
 request_id TEXT PRIMARY KEY, actor TEXT NOT NULL, session_id TEXT NOT NULL,
 request_fingerprint TEXT NOT NULL, response_json TEXT NOT NULL, exit_code INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS client_routing_migrations (
 id TEXT PRIMARY KEY, source_fingerprint TEXT NOT NULL, metadata_json TEXT NOT NULL
);
"""


def route_for_enqueue(selection):
    """Return only the existing bounded route identity; never forward local policy snapshots."""
    if not isinstance(selection, dict):
        raise PmtError("invalid_route", "Route selection must be an object", 2)
    if selection.get("blocked") is True or selection.get("waiting") is True:
        raise PmtError(selection.get("selection_reason_code", "route_not_executable"),
                       "Selected route is blocked or waiting", 3)
    fields = {"agent", "provider", "model", "mode", "adapter_kind", "authorization_state",
        "max_concurrency", "selection_reason", "selection_reason_code", "capability_ref",
        "actual_support", "waiting", "blocked", "price_status", "auth_state"}
    route = {key: selection[key] for key in fields if key in selection}
    required = {"agent", "provider", "model", "mode", "adapter_kind", "max_concurrency",
        "selection_reason", "capability_ref", "actual_support"}
    if not required <= set(route):
        raise PmtError("invalid_route", "Selected route identity is incomplete", 2)
    return route


class ClientRoutingConfig:
    """A small ConfigRoot SQLite port reusing Phase 2 routing validation/selection."""

    operations = routing.READ_OPERATIONS | routing.WRITE_OPERATIONS

    def __init__(self, config_root, *, legacy_data_root=None):
        root = Path(config_root).expanduser().absolute()
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or bool(getattr(root, "is_junction", lambda: False)()):
            raise PmtError("routing_config_path_invalid", "Client routing settings root cannot be a link", 3)
        self.path = root / "routing-client.sqlite3"
        if self.path.is_symlink() or bool(getattr(self.path, "is_junction", lambda: False)()):
            raise PmtError("routing_config_path_invalid", "Client routing settings file cannot be a link", 3)
        with closing(self._connect()) as conn:
            conn.executescript(_DDL)
        if legacy_data_root is not None:
            self._migrate_legacy(legacy_data_root)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @staticmethod
    def _read_legacy_rows(legacy_data_root):
        path = Path(legacy_data_root).expanduser().absolute() / "pmt.sqlite3"
        if not path.is_file():
            return path, {}
        if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
            raise PmtError("routing_migration_source_invalid", "Legacy routing source cannot be a link", 3)
        uri = path.resolve().as_uri() + "?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True, timeout=1)) as conn:
                table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='routing_settings'").fetchone()
                if not table:
                    return path, {}
                rows = conn.execute("SELECT id,revision,body_json FROM routing_settings "
                    "WHERE id IN ('routing_policy','routing_capabilities') ORDER BY id").fetchall()
        except sqlite3.Error as exc:
            raise PmtError("routing_migration_read_failed", "Legacy route settings could not be read safely", 3) from exc
        values = {}
        for key, revision, encoded in rows:
            try:
                body = json.loads(encoded)
            except (TypeError, ValueError) as exc:
                raise PmtError("routing_migration_source_invalid", "Legacy routing metadata is corrupt", 3) from exc
            if type(revision) is not int or revision < 1 or not isinstance(body, dict):
                raise PmtError("routing_migration_source_invalid", "Legacy routing metadata shape is invalid", 3)
            values[key] = {"revision": revision, "body": body}
        return path, values

    def _migrate_legacy(self, legacy_data_root):
        path, rows = self._read_legacy_rows(legacy_data_root)
        source_fingerprint = hashlib.sha256(canonical_json(rows).encode("utf-8")).hexdigest()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                done = conn.execute("SELECT 1 FROM client_routing_migrations WHERE id='legacy-routing-v1'").fetchone()
                if done:
                    conn.commit()
                    return
                existing = conn.execute("SELECT count(*) FROM routing_settings").fetchone()[0]
                metadata = {"source": "legacy_local_database", "source_file": path.name,
                    "imported_keys": [], "capabilities_downgraded": 0,
                    "api_policy_forced_off": False, "reason": None}
                if existing:
                    metadata["reason"] = "client_settings_already_exist"
                elif rows:
                    policy_row = rows.get("routing_policy")
                    if policy_row:
                        policy = policy_row["body"]
                        if not isinstance(policy, dict):
                            raise PmtError("routing_migration_source_invalid", "Legacy route policy must be an object", 3)
                        if policy.get("api_allowed") is True:
                            # Preserve every permitted preference; direct API routing
                            # remains disabled by this product's explicit scope.
                            policy = dict(policy, api_allowed=False)
                            metadata["api_policy_forced_off"] = True
                        # Validate with the existing Phase 2 schema before copying.
                        check = sqlite3.connect(":memory:")
                        try:
                            check.executescript(_DDL)
                            routing.handle(None, check, {"operation": "save_routing_policy",
                                "payload": {"policy": policy}, "expected_revision": 1})
                        finally:
                            check.close()
                        conn.execute("INSERT INTO routing_settings VALUES(?,?,?,?)",
                            ("routing_policy", policy_row["revision"], canonical_json(policy), "legacy-import"))
                        metadata["imported_keys"].append("routing_policy")
                    caps_row = rows.get("routing_capabilities")
                    if caps_row:
                        raw_items = caps_row["body"].get("items")
                        if not isinstance(raw_items, list):
                            raise PmtError("routing_migration_source_invalid", "Legacy capabilities must contain an item list", 3)
                        # Environment-specific proof is not carried across stores.
                        # Keep identities, preferences and references, but require
                        # fresh client-side capability observation before selection.
                        items = []
                        for raw in raw_items:
                            if not isinstance(raw, dict):
                                raise PmtError("routing_migration_source_invalid", "Legacy capability entry is invalid", 3)
                            items.append(dict(raw, support="unknown", auth_state="unknown"))
                        check = sqlite3.connect(":memory:")
                        try:
                            check.executescript(_DDL)
                            routing.handle(None, check, {"operation": "register_capabilities",
                                "payload": {"capabilities": items}, "expected_revision": 1})
                        finally:
                            check.close()
                        conn.execute("INSERT INTO routing_settings VALUES(?,?,?,?)",
                            ("routing_capabilities", caps_row["revision"], canonical_json({"items": items}), "legacy-import"))
                        metadata["capabilities_downgraded"] = len(items)
                        metadata["imported_keys"].append("routing_capabilities")
                else:
                    metadata["reason"] = "no_legacy_routing_settings"
                conn.execute("INSERT INTO client_routing_migrations VALUES(?,?,?)",
                    ("legacy-routing-v1", source_fingerprint, canonical_json(metadata)))
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def execute(self, request):
        """Implement the same request/envelope port without LocalStore or Host calls."""
        req = normalize_request(request)
        if req["operation"] not in self.operations:
            error = PmtError("operation_unsupported", "Unsupported client routing operation", 2)
            return response(req.get("request_id"), error=error.as_dict()), error.exit_code
        digest = semantic_request_fingerprint(req)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                prior = conn.execute("SELECT * FROM client_routing_requests WHERE request_id=?",
                                     (req["request_id"],)).fetchone()
                if prior:
                    if (prior["actor"], prior["session_id"], prior["request_fingerprint"]) != (
                            req["actor"], req["session_id"], digest):
                        error = PmtError("request_conflict", "Routing request ID was reused with different inputs", 3)
                        envelope, code = response(req["request_id"], error=error.as_dict()), error.exit_code
                    else:
                        envelope, code = json.loads(prior["response_json"]), prior["exit_code"]
                    conn.commit()
                    return envelope, code

                # Scope/actor values stay in the replay fingerprint, but route policy
                # is client configuration, not a Project-scoped business event.
                local_request = dict(req)
                local_request.pop("scope_id", None)
                try:
                    savepoint = "routing_operation"
                    conn.execute(f"SAVEPOINT {savepoint}")
                    result = routing.handle(None, conn, local_request)
                    conn.execute(f"RELEASE {savepoint}")
                    envelope, code = response(req["request_id"], result=result), 0
                except PmtError as error:
                    conn.execute(f"ROLLBACK TO {savepoint}")
                    conn.execute(f"RELEASE {savepoint}")
                    envelope, code = response(req["request_id"], error=error.as_dict()), error.exit_code
                conn.execute("INSERT INTO client_routing_requests VALUES(?,?,?,?,?,?)",
                    (req["request_id"], req["actor"], req["session_id"], digest,
                     canonical_json(envelope), code))
                conn.commit()
                return envelope, code
            except Exception:
                conn.rollback()
                raise
