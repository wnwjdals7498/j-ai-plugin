"""Schema additions for phase three's derived and durable local state."""

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS phase3_objects (
 kind TEXT NOT NULL, id TEXT NOT NULL, scope_id TEXT NOT NULL,
 owner_actor TEXT NOT NULL, owner_session TEXT NOT NULL,
 source_hash TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 body_json TEXT NOT NULL, state TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(kind,id));
CREATE INDEX IF NOT EXISTS phase3_objects_scope_idx ON phase3_objects(scope_id,kind,state);
CREATE INDEX IF NOT EXISTS phase3_objects_source_idx ON phase3_objects(source_hash,kind);
CREATE TABLE IF NOT EXISTS phase3_journal (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL, event_id TEXT,
 kind TEXT NOT NULL, scope_id TEXT NOT NULL, owner_actor TEXT NOT NULL, owner_session TEXT NOT NULL,
 body_json TEXT NOT NULL, outcome_json TEXT, created_at TEXT NOT NULL, completed_at TEXT);
CREATE INDEX IF NOT EXISTS phase3_journal_request_idx ON phase3_journal(request_id);
CREATE UNIQUE INDEX IF NOT EXISTS phase3_journal_kind_request_idx ON phase3_journal(kind,request_id);
CREATE TABLE IF NOT EXISTS phase3_outbox (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, request_id TEXT NOT NULL,
 scope_id TEXT NOT NULL, owner_actor TEXT NOT NULL, owner_session TEXT NOT NULL,
 body_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','leased','done','failed')),
 owner TEXT, lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
 result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(kind,request_id));
CREATE INDEX IF NOT EXISTS phase3_outbox_pending_idx ON phase3_outbox(state,created_at);
"""
