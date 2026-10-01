import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import pmt.db as db_module
from pmt.db import Database
from pmt.errors import PmtError


def _schema2(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for name in ("project_baselines", "operation_journal", "run_progress", "scope_locks",
                     "execution_runs", "execution_jobs", "step_specs", "plans", "routing_settings"):
            conn.execute(f"DROP TABLE IF EXISTS {name}")
        conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES('scope','project','p','t','t')")
        conn.execute("INSERT INTO records(id,kind,scope_id,title,created_at,updated_at) VALUES('record','work','scope','W','t','t')")
        conn.execute("INSERT INTO events(id,event_id,record_id,scope_id,event_type,recorded_at) VALUES('event','event','record','scope','created','t')")
        conn.execute("INSERT INTO claims(record_id,owner_session,token_hash,claimed_at,heartbeat_at) VALUES('record','session','hash','t','t')")
        conn.execute("INSERT INTO artifacts(id,scope_id,sha256,size_bytes,relative_path,state,created_at) VALUES('artifact','scope','hash',0,'resource','ready','t')")
        conn.execute("INSERT INTO verifications(id,definition_id,definition_version,target_id,environment_id,input_fingerprint,outcome,completed_at) VALUES('verification','d','1','record','env','fp','pass','t')")
        conn.commit()
    return db


def test_schema3_migration_preserves_schema2_and_is_repeatable(tmp_path):
    old = _schema2(tmp_path)
    migrated = Database(old.root, old.config_root)
    with migrated.connect() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '3'
        assert tuple(conn.execute("SELECT id,scope_id FROM records WHERE id='record'").fetchone()) == ('record', 'scope')
        assert conn.execute("SELECT owner_session FROM claims WHERE record_id='record'").fetchone()[0] == 'session'
        assert conn.execute("SELECT id FROM verifications").fetchone()[0] == 'verification'
        assert conn.execute("SELECT event_id FROM events").fetchone()[0] == 'event'
        assert len(list(old.root.glob('pmt-schema2-*.sqlite3'))) == 1
        assert conn.execute("SELECT value FROM meta WHERE key='maintenance_owner'").fetchone()[0] == ''
    Database(old.root, old.config_root)
    with migrated.connect() as conn:
        assert conn.execute("SELECT count(*) FROM plans").fetchone()[0] == 0
        assert len(list(old.root.glob('pmt-schema2-*.sqlite3'))) == 1


def test_phase2_relationship_checks_active_uniqueness_and_cas_fields(tmp_path):
    db = Database(tmp_path / 'data', tmp_path / 'config')
    with db.connect() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES('s','project','s','t','t')")
        conn.execute("INSERT INTO records(id,kind,scope_id,title,created_at,updated_at) VALUES('step','step','s','S','t','t')")
        conn.execute("INSERT INTO artifacts(id,scope_id,sha256,size_bytes,relative_path,state,created_at) VALUES('a','s','h',0,'a','ready','t')")
        conn.execute("INSERT INTO step_specs(step_id,directive_id,directive_version,requirements_version,plan_version,role,product_stage,workspace,scopes_json,criteria_json,dependencies_json,created_at,updated_at) VALUES('step','a',1,'r','p','worker','build','w','[]','[]','[]','t','t')")
        conn.execute("INSERT INTO execution_jobs VALUES('job','step','running','{}','t','t')")
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES('run','job','step',1,'running',1,'owner',1,'w','[]','{}','{}','t','t')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO execution_jobs VALUES('job2','step','queued','{}','t','t')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES('run2','job','step',1,'completed',1,'owner',1,'w','[]','{}','{}','t','t')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES('bad','job','step',4,'completed',1,'owner',1,'w','[]','{}','{}','t','t')")


def test_future_schema_rejected_without_mutation(tmp_path):
    db = Database(tmp_path / 'data', tmp_path / 'config')
    with db.connect() as conn:
        conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    with pytest.raises(PmtError) as err:
        Database(db.root, db.config_root)
    assert err.value.code == 'schema_version_unsupported'


def test_schema_migration_failure_rolls_back_and_keeps_backup(tmp_path, monkeypatch):
    db = _schema2(tmp_path)
    monkeypatch.setattr(db_module, 'PHASE2_SCHEMA', "CREATE TABLE transient(id TEXT); SELECT invalid_sql;")
    with pytest.raises(sqlite3.OperationalError):
        Database(db.root, db.config_root)
    with sqlite3.connect(db.path) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '2'
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='transient'").fetchone() is None
        assert conn.execute("SELECT value FROM meta WHERE key='maintenance_owner'").fetchone()[0] == ''
    assert len(list(db.root.glob('pmt-schema2-*.sqlite3'))) == 1


def test_process_writer_observes_maintenance_gate(tmp_path):
    db = Database(tmp_path / 'data', tmp_path / 'config')
    with db.write() as conn:
        conn.execute("UPDATE meta SET value='gate' WHERE key='maintenance_owner'")
    code = "from pmt.db import Database; from pmt.errors import PmtError; import sys; d=Database(sys.argv[1],sys.argv[2]);\ntry:\n with d.write(): pass\nexcept PmtError as e:\n print(e.code)"
    proc = subprocess.run([sys.executable, '-c', code, str(db.root), str(db.config_root)],
                          capture_output=True, text=True, check=False,
                          cwd=Path(__file__).resolve().parents[1],
                          env={**__import__('os').environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == 'maintenance_active'
