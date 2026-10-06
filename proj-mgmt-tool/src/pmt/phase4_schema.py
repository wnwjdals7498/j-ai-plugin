"""Additive storage for source-bound continuity metadata and recoverable effects."""

SCHEMA_VERSION = 5
SCHEMA = """
CREATE TABLE IF NOT EXISTS continuity_objects (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope_id TEXT NOT NULL REFERENCES scopes(id),
 contract_version TEXT NOT NULL, body_hash TEXT NOT NULL, basis_hash TEXT,
 visibility TEXT NOT NULL CHECK(visibility IN ('shared','private')),
 owner_actor TEXT NOT NULL, owner_session TEXT NOT NULL, body_json TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision=1), created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS continuity_objects_scope_idx ON continuity_objects(scope_id,kind,created_at);
CREATE TABLE IF NOT EXISTS continuity_pointers (
 scope_id TEXT NOT NULL REFERENCES scopes(id), pointer_key TEXT NOT NULL, selector_json TEXT NOT NULL,
 object_id TEXT NOT NULL REFERENCES continuity_objects(id), revision INTEGER NOT NULL CHECK(revision>=1),
 updated_at TEXT NOT NULL, PRIMARY KEY(scope_id,pointer_key));
CREATE TABLE IF NOT EXISTS continuity_events (
 scope_id TEXT NOT NULL REFERENCES scopes(id), kind TEXT NOT NULL, event_id TEXT NOT NULL,
 body_hash TEXT NOT NULL, object_id TEXT NOT NULL REFERENCES continuity_objects(id),
 created_at TEXT NOT NULL, PRIMARY KEY(scope_id,kind,event_id));
CREATE TABLE IF NOT EXISTS continuity_journal (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL, kind TEXT NOT NULL,
 scope_id TEXT NOT NULL REFERENCES scopes(id), owner_actor TEXT NOT NULL, owner_session TEXT NOT NULL,
 basis_hash TEXT, body_hash TEXT NOT NULL, body_json TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('prepared','applying','partial','unknown','conflict','completed')),
 outcome_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(scope_id,request_id,kind));
CREATE INDEX IF NOT EXISTS continuity_journal_pending_idx ON continuity_journal(state,scope_id);
"""
