"""Read-only Host and local-database status."""
from __future__ import annotations

import sqlite3
import json
from pathlib import Path

from .config import load_config_snapshot
from .doctor import _health
from .serve import instance_available


def read_status(config_root):
    config, _ = load_config_snapshot(Path(config_root) / "host-config.json")
    data_root = Path(config["paths"]["data_root"])
    db_path = data_root / "pmt.sqlite3"
    meta, counts = {}, {"devices_active": None, "devices_revoked": None, "sessions_active": None, "claim_leases_active": None}
    if db_path.is_file():
        with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=1) as conn:
            meta = dict(conn.execute("SELECT key,value FROM meta WHERE key IN ('host_namespace_id','host_schema_version')"))
            for key, table, predicate in (("devices_active", "host_devices", "state='active'"),
                                          ("devices_revoked", "host_devices", "state='revoked'"),
                                          ("sessions_active", "host_sessions", "state='active'"),
                                          ("claim_leases_active", "host_claim_leases", "state='active'")):
                try: counts[key] = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {predicate}").fetchone()[0]
                except sqlite3.Error: pass
    lock_path = data_root / ".pmt-server.lock"
    running = lock_path.exists() and not instance_available(lock_path)
    lock_meta = {}
    if running:
        marker = lock_path.with_name(".pmt-server.status.json")
        try: lock_meta = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError): lock_meta = {}
    backup_dir = Path(config["paths"]["backup_dir"])
    backups = [entry for entry in backup_dir.iterdir() if entry.is_dir()] if backup_dir.is_dir() else []
    latest_backup = max(backups, key=lambda entry: entry.stat().st_mtime).stat().st_mtime if backups else None
    return {"ok": True, "service": {"running": running, "pid": lock_meta.get("pid"), "started_at": lock_meta.get("started_at"),
                                    "kind": config["service"]["kind"], "automatic_start": "unknown"},
            "health": "ok" if _health(config) is True else "unavailable_or_not_probed",
            "compatibility": "not_checked", "namespace_id": meta.get("host_namespace_id"),
            **counts, "last_backup_mtime": latest_backup, "log_path": str(Path(config["paths"]["log_dir"]) / "host.log"),
            "config_path": str(Path(config_root) / "host-config.json")}
