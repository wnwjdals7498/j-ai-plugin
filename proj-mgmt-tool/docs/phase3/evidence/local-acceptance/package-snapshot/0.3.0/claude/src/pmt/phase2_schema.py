"""Schema additions for the phase two execution and planning foundation."""

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
 id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES scopes(id), artifact_id TEXT REFERENCES artifacts(id),
 graph_version INTEGER NOT NULL, requirements_version TEXT NOT NULL, plan_version TEXT NOT NULL,
 state TEXT NOT NULL, workspace TEXT NOT NULL, relative_path TEXT NOT NULL, sha256 TEXT NOT NULL,
 baseline_commit TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS step_specs (
 step_id TEXT PRIMARY KEY REFERENCES records(id), directive_id TEXT NOT NULL REFERENCES artifacts(id),
 directive_version INTEGER NOT NULL, requirements_version TEXT NOT NULL, plan_version TEXT NOT NULL,
 plan_id TEXT REFERENCES plans(id), role TEXT NOT NULL, product_stage TEXT NOT NULL, workspace TEXT NOT NULL,
 scopes_json TEXT NOT NULL, criteria_json TEXT NOT NULL, dependencies_json TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS execution_jobs (
 id TEXT PRIMARY KEY, step_id TEXT NOT NULL REFERENCES records(id), state TEXT NOT NULL,
 policy_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS execution_jobs_one_active_step_idx ON execution_jobs(step_id)
 WHERE state IN ('queued','starting','running','review_pending','reconciling','cancel_requested');
CREATE TABLE IF NOT EXISTS execution_runs (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES execution_jobs(id), step_id TEXT NOT NULL REFERENCES records(id),
 attempt INTEGER NOT NULL CHECK(attempt BETWEEN 1 AND 3), state TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
 owner_session TEXT NOT NULL, directive_version INTEGER NOT NULL, workspace TEXT NOT NULL,
 scopes_json TEXT NOT NULL, route_json TEXT NOT NULL, intent_json TEXT NOT NULL, handle_json TEXT, result_json TEXT,
 stop_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(stop_confirmed IN (0,1)), started_at TEXT, completed_at TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(job_id,attempt));
CREATE UNIQUE INDEX IF NOT EXISTS execution_runs_one_active_step_idx ON execution_runs(step_id)
 WHERE state IN ('queued','starting','running','review_pending','reconciling','cancel_requested');
CREATE TABLE IF NOT EXISTS scope_locks (
 lock_key TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES execution_runs(id), owner_session TEXT NOT NULL,
 kind TEXT NOT NULL, workspace TEXT NOT NULL, resource TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS routing_settings (
 id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK(revision >= 1), body_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_progress (
 run_id TEXT PRIMARY KEY REFERENCES execution_runs(id), last_seen TEXT NOT NULL, last_changed TEXT NOT NULL,
 body_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS project_baselines (
 scope_id TEXT PRIMARY KEY REFERENCES scopes(id), workspace TEXT NOT NULL, selected_ref TEXT NOT NULL,
 reviewed_commit TEXT, fingerprint TEXT NOT NULL, body_json TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision >= 1), updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operation_journal (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, body_json TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""

ACTIVE_RUN_STATES = ("queued", "starting", "running", "review_pending", "reconciling", "cancel_requested")
