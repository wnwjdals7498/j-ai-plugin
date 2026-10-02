"""Registered device/session authority, checked on the transaction connection.

Only the local Host administrator issues, rotates and revokes device credentials.
Device credentials are independent of environment profiles and claim locators.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from contextlib import closing
from dataclasses import dataclass

from ..errors import PmtError
from ..util import canonical_json, new_id, utc_now

HOST_SCHEMA_VERSION = 1
PERMISSIONS = frozenset({"read", "write", "runtime", "review", "admin"})
DDL = """
CREATE TABLE IF NOT EXISTS host_devices (
 id TEXT PRIMARY KEY, actor TEXT NOT NULL, credential_hash TEXT NOT NULL UNIQUE,
 scopes_json TEXT NOT NULL, permissions_json TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS host_sessions (
 id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES host_devices(id),
 environment_id TEXT NOT NULL, state TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS host_environments (
 id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES host_devices(id),
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS host_claim_leases (
 id TEXT PRIMARY KEY, record_id TEXT NOT NULL REFERENCES records(id),
 scope_id TEXT NOT NULL REFERENCES scopes(id), device_id TEXT NOT NULL REFERENCES host_devices(id),
 actor TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES host_sessions(id),
 key_id TEXT NOT NULL, fingerprint TEXT NOT NULL, generation INTEGER NOT NULL,
 state TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS host_active_claim_idx
 ON host_claim_leases(record_id) WHERE state='active';
"""


def identifier(value, label):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise PmtError("host_input_invalid", f"{label} must be a canonical UUID")
    return value


def short_text(value, label, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise PmtError("host_input_invalid", f"{label} must be bounded text")
    return value


@dataclass(frozen=True)
class Principal:
    device_id: str
    actor: str
    scopes: tuple[str, ...]
    permissions: frozenset[str]
    revision: int
    session_id: str | None = None
    environment_id: str | None = None

    def require(self, permission):
        if permission not in self.permissions:
            raise PmtError("scope_forbidden", "Device lacks the required permission", 3)


class AuthRegistry:
    def __init__(self, db):
        self.db = db
        with closing(db.connect()) as conn:
            version = db._meta_value(conn, "host_schema_version")
            namespace_id = db._meta_value(conn, "host_namespace_id")
            if version == str(HOST_SCHEMA_VERSION) and namespace_id:
                self.namespace_id = identifier(namespace_id, "namespace_id")
                return
        with db.write() as conn:
            version = db._meta_value(conn, "host_schema_version")
            if version not in {None, str(HOST_SCHEMA_VERSION)}:
                raise PmtError("host_schema_unsupported", "Host schema is incompatible")
            # Keep DDL inside the existing transaction; executescript would commit it.
            for statement in DDL.split(";"):
                if statement.strip():
                    conn.execute(statement)
            db._put_meta(conn, "host_schema_version", str(HOST_SCHEMA_VERSION))
            if not db._meta_value(conn, "host_namespace_id"):
                db._put_meta(conn, "host_namespace_id", new_id())
            self.namespace_id = db._meta_value(conn, "host_namespace_id")

    @staticmethod
    def _grants(scopes, permissions):
        if not isinstance(scopes, (list, tuple)) or not scopes or len(scopes) > 100:
            raise PmtError("host_input_invalid", "Device needs a bounded scope grant list")
        values = sorted(set(s if s == "*" else identifier(s, "scope_id") for s in scopes))
        if not isinstance(permissions, (list, tuple, set, frozenset)) or not permissions or set(permissions) - PERMISSIONS:
            raise PmtError("host_input_invalid", "Device permissions are invalid")
        return values, sorted(set(permissions))

    def issue_device(self, actor, scopes, permissions, *, device_id=None):
        """Trusted local administration only; plaintext is returned exactly once."""
        actor = short_text(actor, "actor")
        device_id = identifier(device_id, "device_id") if device_id else new_id()
        scopes, permissions = self._grants(scopes, permissions)
        credential = secrets.token_urlsafe(48)
        now = utc_now()
        with self.db.write() as conn:
            for scope_id in scopes:
                if scope_id != "*" and conn.execute("SELECT 1 FROM scopes WHERE id=?", (scope_id,)).fetchone() is None:
                    raise PmtError("scope_forbidden", "Granted scope does not exist", 3)
            if conn.execute("SELECT 1 FROM host_devices WHERE id=?", (device_id,)).fetchone():
                raise PmtError("device_exists", "Device is already registered", 3)
            conn.execute("INSERT INTO host_devices VALUES(?,?,?,?,?,1,'active',?,?)",
                         (device_id, actor, hashlib.sha256(credential.encode()).hexdigest(), canonical_json(scopes),
                          canonical_json(permissions), now, now))
        return {"device_id": device_id, "actor": actor, "namespace_id": self.namespace_id,
                "scopes": scopes, "permissions": permissions, "credential": credential, "revision": 1}

    def rotate_device(self, device_id, expected_revision):
        identifier(device_id, "device_id")
        credential = secrets.token_urlsafe(48)
        with self.db.write() as conn:
            row = conn.execute("SELECT revision,state FROM host_devices WHERE id=?", (device_id,)).fetchone()
            if not row or row["state"] != "active" or type(expected_revision) is not int or row["revision"] != expected_revision:
                raise PmtError("device_revision_conflict", "Device is absent, revoked or changed", 3)
            conn.execute("UPDATE host_devices SET credential_hash=?,revision=revision+1,updated_at=? WHERE id=?",
                         (hashlib.sha256(credential.encode()).hexdigest(), utc_now(), device_id))
        return {"device_id": device_id, "credential": credential, "revision": expected_revision + 1}

    def update_grants(self, device_id, expected_revision, scopes, permissions):
        identifier(device_id, "device_id")
        scopes, permissions = self._grants(scopes, permissions)
        if type(expected_revision) is not int or expected_revision < 1:
            raise PmtError("device_revision_conflict", "Device revision must be positive", 3)
        with self.db.write() as conn:
            for scope_id in scopes:
                if scope_id != "*" and conn.execute("SELECT 1 FROM scopes WHERE id=?", (scope_id,)).fetchone() is None:
                    raise PmtError("scope_forbidden", "Granted scope does not exist", 3)
            count = conn.execute("UPDATE host_devices SET scopes_json=?,permissions_json=?,revision=revision+1,updated_at=? "
                                 "WHERE id=? AND revision=? AND state='active'",
                                 (canonical_json(scopes), canonical_json(permissions), utc_now(), device_id, expected_revision)).rowcount
            if count != 1:
                raise PmtError("device_revision_conflict", "Device is absent, revoked or changed", 3)
        return {"device_id": device_id, "revision": expected_revision + 1, "scopes": scopes, "permissions": permissions}

    def revoke_device(self, device_id, expected_revision):
        identifier(device_id, "device_id")
        if type(expected_revision) is not int or expected_revision < 1:
            raise PmtError("device_revision_conflict", "Device revision must be positive", 3)
        with self.db.write() as conn:
            count = conn.execute("UPDATE host_devices SET state='revoked',revision=revision+1,updated_at=? "
                                 "WHERE id=? AND revision=? AND state='active'",
                                 (utc_now(), device_id, expected_revision)).rowcount
            if count != 1:
                raise PmtError("device_revision_conflict", "Device is absent, revoked or changed", 3)
            conn.execute("UPDATE host_sessions SET state='revoked',updated_at=? WHERE device_id=?", (utc_now(), device_id))
        return {"device_id": device_id, "state": "revoked", "revision": expected_revision + 1}

    def authenticate(self, conn, credential, device_id, namespace_id, *, session_id=None, environment_id=None):
        if namespace_id != self.namespace_id or not isinstance(credential, str) or not 32 <= len(credential) <= 512:
            raise PmtError("unauthenticated", "Device authentication failed", 3)
        row = conn.execute("SELECT * FROM host_devices WHERE id=? AND state='active'", (device_id,)).fetchone()
        digest = hashlib.sha256(credential.encode()).hexdigest()
        if row is None or not hmac.compare_digest(row["credential_hash"], digest):
            raise PmtError("unauthenticated", "Device authentication failed", 3)
        if session_id is not None:
            session = conn.execute("SELECT * FROM host_sessions WHERE id=? AND state='active'", (session_id,)).fetchone()
            if not session or session["device_id"] != device_id or session["environment_id"] != environment_id:
                raise PmtError("unauthenticated", "Session is not registered to this device and environment", 3)
        return Principal(device_id, row["actor"], tuple(json.loads(row["scopes_json"])),
                         frozenset(json.loads(row["permissions_json"])), row["revision"], session_id, environment_id)

    def register_session(self, credential, device_id, namespace_id, session_id, environment_id):
        short_text(session_id, "session_id")
        identifier(environment_id, "environment_id")
        with self.db.write() as conn:
            principal = self.authenticate(conn, credential, device_id, namespace_id)
            env = conn.execute("SELECT device_id FROM host_environments WHERE id=?", (environment_id,)).fetchone()
            if env and env[0] != device_id:
                raise PmtError("environment_device_conflict", "Environment profile is already registered to another device", 3)
            old = conn.execute("SELECT * FROM host_sessions WHERE id=?", (session_id,)).fetchone()
            if old and (old["device_id"] != device_id or old["environment_id"] != environment_id or old["state"] != "active"):
                raise PmtError("session_device_conflict", "Session identity cannot be reassigned", 3)
            now = utc_now()
            conn.execute("INSERT OR IGNORE INTO host_environments VALUES(?,?,?)", (environment_id, device_id, now))
            conn.execute("INSERT INTO host_sessions VALUES(?,?,?,'active',?,?) ON CONFLICT(id) "
                         "DO UPDATE SET updated_at=excluded.updated_at", (session_id, device_id, environment_id, now, now))
        return {"session_id": session_id, "environment_id": environment_id, "device_id": principal.device_id}

    @staticmethod
    def authorize_scope(conn, principal, scope_id):
        identifier(scope_id, "scope_id")
        seen = set()
        candidate = scope_id
        while candidate is not None and len(seen) < 1000:
            if candidate in seen:
                raise PmtError("scope_forbidden", "Scope ancestry is invalid", 3)
            seen.add(candidate)
            row = conn.execute("SELECT parent_id FROM scopes WHERE id=?", (candidate,)).fetchone()
            if row is None:
                raise PmtError("scope_forbidden", "Scope is absent or inaccessible", 3)
            if "*" in principal.scopes or candidate in principal.scopes:
                return scope_id
            candidate = row[0]
        raise PmtError("scope_forbidden", "Scope is outside this device's grants", 3)

    def principal(self, credential, device_id, namespace_id, **kw):
        with closing(self.db.connect()) as conn:
            return self.authenticate(conn, credential, device_id, namespace_id, **kw)
