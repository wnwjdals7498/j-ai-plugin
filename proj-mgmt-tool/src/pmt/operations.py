"""Progress observation, bounded diagnostics and reference-safe retention."""
from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta, timezone
import json

from .errors import PmtError
from .phase2_common import event, identifier, replay
from .resources import _artifact_path
from .util import canonical_json, new_id, utc_now

READ_OPERATIONS = {"read_progress", "plan_retention", "diagnose_execution"}
WRITE_OPERATIONS = {"observe_progress", "configure_diagnostics"}
FILE_OPERATIONS = {"execute_retention"}
PROGRESS_FIELDS = {"stage", "state", "model", "route", "artifact_refs", "wait_reason", "next_action", "user_decision_needed"}
TERMINAL = {"succeeded", "failed", "canceled", "blocked"}
ACTIVE_RUNS = {"queued", "starting", "running", "review_pending", "reconciling", "cancel_requested"}
TEMP_RETENTION_DAYS = 7
HISTORY_RETENTION_MONTHS = 3
DIAGNOSTICS_RETENTION_DAYS = 7


def _now():
    """Clock boundary for deterministic retention tests; requests cannot override it."""
    return datetime.now(timezone.utc)


def _utc_text(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _months_before(value, months):
    year, month = value.year, value.month - months
    while month <= 0:
        year -= 1; month += 12
    return value.replace(year=year, month=month, day=min(value.day, monthrange(year, month)[1]))


def _contains(value, target):
    if value == target:
        return True
    if isinstance(value, str) and isinstance(target, str) and len(target) >= 36:
        return target in value
    if isinstance(value, dict):
        return any(_contains(k, target) or _contains(v, target) for k, v in value.items())
    if isinstance(value, list):
        return any(_contains(item, target) for item in value)
    return False


def _load_json(text, label):
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PmtError("stored_record_invalid", f"Stored {label} is invalid; retention stopped safely", 5) from exc


def _run(conn, run_id):
    identifier(run_id, "run_id")
    row = conn.execute("SELECT * FROM execution_runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise PmtError("run_not_found", "Execution does not exist")
    return row


def _referenced(conn, artifact_id, now=None):
    now = now or _now()
    history_cutoff = _utc_text(_months_before(now, HISTORY_RETENTION_MONTHS))
    if conn.execute("SELECT 1 FROM artifact_refs WHERE artifact_id=?", (artifact_id,)).fetchone():
        return True
    if conn.execute("SELECT 1 FROM step_specs WHERE directive_id=?", (artifact_id,)).fetchone():
        return True
    if conn.execute("SELECT 1 FROM plans WHERE artifact_id=?", (artifact_id,)).fetchone():
        return True
    # Current records and plans may contain structured artifact references in their canonical bodies.
    for row in conn.execute("SELECT body_json FROM records UNION ALL SELECT body_json FROM scopes UNION ALL SELECT body_json FROM project_baselines"):
        if _contains(_load_json(row[0], "current resource references"), artifact_id):
            return True
    for row in conn.execute("SELECT evidence_json,includes_json,command_json FROM verifications WHERE state='valid'"):
        if any(_contains(_load_json(text, "valid verification references"), artifact_id) for text in row):
            return True
    # Active or unreviewed execution receipts and fresh observations remain current references.
    for row in conn.execute("SELECT result_json,intent_json FROM execution_runs WHERE state IN ('queued','starting','running','review_pending','reconciling','cancel_requested')"):
        for text in row:
            if text and _contains(_load_json(text, "active execution references"), artifact_id):
                return True
    for row in conn.execute("SELECT body_json FROM run_progress p JOIN execution_runs r ON r.id=p.run_id WHERE r.state IN ('queued','starting','running','review_pending','reconciling','cancel_requested')"):
        if _contains(_load_json(row[0], "active progress references"), artifact_id):
            return True
    for row in conn.execute("SELECT body_json FROM operation_journal WHERE state NOT IN ('completed','committed','rolled_back') AND kind<>'resource_cleanup'"):
        if _contains(_load_json(row[0], "unfinished operation references"), artifact_id):
            return True
    return False


def _candidates(conn, now):
    now_text = _utc_text(now)
    temp_cutoff = _utc_text(now - timedelta(days=TEMP_RETENTION_DAYS))
    candidates = []
    for row in conn.execute("SELECT id,created_at,retention_until FROM artifacts WHERE state='ready' AND retention_until IS NOT NULL AND retention_until<=? AND created_at<=?", (now_text, temp_cutoff)):
        if not _referenced(conn, row["id"], now):
            candidates.append(row["id"])
    return candidates


def handle(db, conn, req):
    op, p = req["operation"], req["payload"]
    if op == "configure_diagnostics":
        config = {"enabled": p.get("enabled", True), "retention_days": p.get("retention_days", 7),
                  "max_bytes": p.get("max_bytes", 10 * 1024 * 1024), "file_count": p.get("file_count", 10)}
        if type(config["enabled"]) is not bool or any(type(config[k]) is not int or config[k] < 1
                for k in ("retention_days", "max_bytes", "file_count")):
            raise PmtError("invalid_diagnostics_config", "Diagnostics bounds must be positive integers")
        if not 2 <= config["file_count"] <= 100 or config["max_bytes"] > 100 * 1024 * 1024:
            raise PmtError("invalid_diagnostics_config", "Diagnostics bounds exceed supported limits")
        row = conn.execute("SELECT revision FROM routing_settings WHERE id='diagnostics'").fetchone()
        if row and req.get("expected_revision") != row[0]:
            raise PmtError("revision_conflict", "Diagnostics configuration changed", 3)
        revision = row[0] + 1 if row else 1
        conn.execute("INSERT INTO routing_settings VALUES('diagnostics',?,?,?) ON CONFLICT(id) DO UPDATE SET "
                     "revision=excluded.revision,body_json=excluded.body_json,updated_at=excluded.updated_at",
                     (revision, canonical_json(config), utc_now()))
        event(conn, req, "diagnostic.configured", payload={"revision": revision})
        return {"configuration": config, "revision": revision, "applies": "next_invocation"}
    if op == "plan_retention":
        now = _now()
        try:
            candidates = _candidates(conn, now)
        except PmtError as error:
            db.diagnostics.emit("retention.reference_check_failed", level=40, outcome="error", error_code=error.code)
            raise
        cutoff = _utc_text(_months_before(now, HISTORY_RETENTION_MONTHS))
        old_runs = conn.execute("SELECT count(*) FROM execution_runs r LEFT JOIN scope_locks l ON l.run_id=r.id WHERE r.state IN ('succeeded','failed','blocked','canceled') AND r.completed_at<=? AND l.run_id IS NULL", (cutoff,)).fetchone()[0]
        old_requests = conn.execute("SELECT count(*) FROM requests WHERE created_at<=?", (cutoff,)).fetchone()[0]
        return {"temporary_artifact_ids": candidates, "history_retention_months": HISTORY_RETENTION_MONTHS,
                "diagnostics_retention_days": DIAGNOSTICS_RETENTION_DAYS, "history_cutoff": cutoff,
                "aged_terminal_runs": old_runs, "aged_requests": old_requests,
                "referenced_resources_protected": True, "automatic_deletion": False}
    if op == "diagnose_execution":
        active = [dict(row) for row in conn.execute("SELECT id,step_id,state,updated_at FROM execution_runs WHERE state IN "
                "('starting','running','cancel_requested','reconciling','review_pending')")]
        journals = [dict(row) for row in conn.execute("SELECT id,kind,state FROM operation_journal WHERE state NOT IN "
                "('completed','committed','rolled_back')")]
        return {"active_runs": active, "unfinished_journals": journals,
                "automatic_claim_recovery": False, "next_action": "reconcile original execution or unfinished file operation"}
    run = _run(conn, p.get("run_id"))
    stored = conn.execute("SELECT * FROM run_progress WHERE run_id=?", (run["id"],)).fetchone()
    if op == "read_progress":
        invalid = False
        try:
            snapshot = _load_json(stored["body_json"], "progress snapshot") if stored else {}
            if not isinstance(snapshot, dict) or set(snapshot) - PROGRESS_FIELDS:
                raise PmtError("invalid_observation", "Stored progress snapshot has unsupported fields", 5)
        except PmtError as error:
            snapshot, invalid = {}, True
            db.diagnostics.emit("run.observation_invalid", level=40, run_id=run["id"], outcome="error", error_code=error.code)
        last_seen = stored["last_seen"] if stored else None
        try:
            stale = not last_seen or (_now() - datetime.fromisoformat(last_seen.replace("Z", "+00:00"))).total_seconds() > 60
        except (ValueError, TypeError):
            stale, invalid = True, True
            db.diagnostics.emit("run.observation_invalid", level=40, run_id=run["id"], outcome="error", error_code="invalid_timestamp")
        try:
            route = _load_json(run["route_json"], "execution route")
            if not isinstance(route, dict):
                raise PmtError("stored_record_invalid", "Stored execution route is invalid", 5)
        except PmtError as error:
            route, invalid = {}, True
            db.diagnostics.emit("run.observation_invalid", level=40, run_id=run["id"], outcome="error", error_code=error.code)
        if invalid:
            stale = True
        lineage, current = {}, run["step_id"]
        for kind in ("step", "item", "work"):
            parent = conn.execute("SELECT id,parent_id FROM records WHERE id=?", (current,)).fetchone() if current else None
            if not parent:
                break
            lineage[kind + "_id"] = parent["id"]
            current = parent["parent_id"]
        elapsed = None
        if run["started_at"]:
            try:
                stop = datetime.fromisoformat(run["completed_at"].replace("Z", "+00:00")) if run["completed_at"] else _now()
                elapsed = max(0, int((stop - datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))).total_seconds()))
            except (ValueError, TypeError):
                invalid = True
        return {"run_id": run["id"], "step_id": run["step_id"], "state": run["state"],
                "model": route.get("model"), "route": route.get("mode"), "observation": snapshot,
                "last_seen": last_seen, "last_changed": stored["last_changed"] if stored else None,
                "observation_stale": stale, "observation_unavailable": invalid,
                "elapsed_seconds": elapsed, **lineage,
                "next_action": "check runner status" if stale else snapshot.get("next_action")}
    if op == "observe_progress":
        if req["session_id"] != run["owner_session"]:
            raise PmtError("ownership_conflict", "Only the owning session may publish observations", 3)
        raw = p.get("observation", {})
        if not isinstance(raw, dict) or set(raw) - PROGRESS_FIELDS:
            raise PmtError("invalid_observation", "Observation contains unsupported fields")
        for key in ("stage", "state", "model", "route", "wait_reason", "next_action"):
            if key in raw and (not isinstance(raw[key], str) or len(raw[key]) > 500):
                raise PmtError("invalid_observation", "Observation text is bounded")
        if "artifact_refs" in raw:
            if not isinstance(raw["artifact_refs"], list) or len(raw["artifact_refs"]) > 100:
                raise PmtError("invalid_observation", "artifact_refs must be a bounded array")
            for artifact_id in raw["artifact_refs"]:
                identifier(artifact_id, "artifact_ref")
                if conn.execute("SELECT 1 FROM artifacts WHERE id=? AND state='ready'", (artifact_id,)).fetchone() is None:
                    raise PmtError("invalid_observation", "artifact_refs must identify ready resources")
        if "user_decision_needed" in raw and type(raw["user_decision_needed"]) is not bool:
            raise PmtError("invalid_observation", "user_decision_needed must be boolean")
        snapshot = canonical_json(raw)
        now = utc_now()
        changed = stored["last_changed"] if stored and stored["body_json"] == snapshot else now
        conn.execute("INSERT INTO run_progress VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
                     "last_seen=excluded.last_seen,last_changed=excluded.last_changed,body_json=excluded.body_json",
                     (run["id"], now, changed, snapshot))
        db.diagnostics.emit("run.observed", run_id=run["id"], step_id=run["step_id"], outcome="observed")
        return {"run_id": run["id"], "last_seen": now, "last_changed": changed,
                "changed": not stored or stored["body_json"] != snapshot}
    raise PmtError("operation_unsupported", "Unsupported operational request")


def _journal_payload(row):
    body = _load_json(row["body_json"], "retention journal")
    if not isinstance(body, dict) or not isinstance(body.get("artifact_id"), str):
        raise PmtError("stored_record_invalid", "Retention journal is invalid; cleanup stopped safely", 5)
    return body


def _request_cleanup_journals(db, request_id):
    with db.connect() as conn:
        rows = conn.execute("SELECT id,state,body_json FROM operation_journal WHERE kind='resource_cleanup'").fetchall()
    found = []
    for row in rows:
        body = _journal_payload(row)
        if body.get("request_id") == request_id:
            found.append({"id": row["id"], "state": row["state"], "body_json": row["body_json"], **body})
    return found


def _resume_cleanup(db, request_id, journals):
    failed = []
    for job in journals:
        artifact_id, journal_id = job["artifact_id"], job["id"]
        if job["state"] == "completed":
            continue
        if job["state"] == "blocked_reference":
            failed.append(artifact_id)
            continue
        try:
            with db.write() as conn:
                artifact = conn.execute("SELECT state,relative_path FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
                journal = conn.execute("SELECT state FROM operation_journal WHERE id=?", (journal_id,)).fetchone()
                if not artifact or not journal:
                    raise PmtError("retention_recovery_incomplete", "Reserved resource or journal is missing", 4)
                if artifact["state"] == "deleted" or journal["state"] == "completed":
                    conn.execute("UPDATE operation_journal SET state='completed',updated_at=? WHERE id=?", (_utc_text(_now()), journal_id))
                    continue
                if artifact["state"] != "deleting":
                    raise PmtError("retention_recovery_incomplete", "Resource is no longer reserved for deletion", 4)
                # Recheck in the same transaction that confirms the reservation. The resource writer
                # rejects new refs while state=deleting, closing the candidate/delete race.
                if _referenced(conn, artifact_id, _now()):
                    conn.execute("UPDATE operation_journal SET state='blocked_reference',updated_at=? WHERE id=?", (_utc_text(_now()), journal_id))
                    failed.append(artifact_id)
                    continue
                relative = artifact["relative_path"]
            _artifact_path(db, relative).unlink(missing_ok=True)
            with db.write() as conn:
                conn.execute("UPDATE artifacts SET state='deleted' WHERE id=? AND state='deleting'", (artifact_id,))
                conn.execute("UPDATE operation_journal SET state='completed',updated_at=? WHERE id=? AND state IN ('reserved','failed')",
                             (_utc_text(_now()), journal_id))
        except (OSError, PmtError) as exc:
            failed.append(artifact_id)
            code = getattr(exc, "code", "resource_io_error")
            with db.write() as conn:
                conn.execute("UPDATE operation_journal SET state='failed',body_json=?,updated_at=? WHERE id=?",
                             (canonical_json({"artifact_id": artifact_id, "request_id": request_id,
                                              "last_error_code": code}), _utc_text(_now()), journal_id))
            db.diagnostics.emit("resource.cleanup.failed", level=40, resource_id=artifact_id,
                                outcome="error", error_code=code)
    return sorted(set(failed))


def _cleanup_history(conn, now):
    cutoff = _utc_text(_months_before(now, HISTORY_RETENTION_MONTHS))
    def protected_reference(token):
        for ref in conn.execute("SELECT body_json FROM records UNION ALL SELECT body_json FROM scopes "
                                "UNION ALL SELECT body_json FROM project_baselines UNION ALL SELECT command_json FROM verifications "
                                "WHERE state='valid' UNION ALL SELECT body_json FROM operation_journal "
                                "WHERE state NOT IN ('completed','committed','rolled_back')"):
            if _contains(_load_json(ref[0], "history references"), token):
                return True
        return False
    old_runs = conn.execute("SELECT r.id,r.step_id,r.job_id,r.attempt,r.directive_version,r.intent_json FROM execution_runs r "
        "LEFT JOIN scope_locks l ON l.run_id=r.id WHERE r.state IN ('succeeded','failed','blocked','canceled') "
        "AND r.completed_at IS NOT NULL AND r.completed_at<=? AND l.run_id IS NULL", (cutoff,)).fetchall()
    cleaned_run_ids = []
    deleted_progress = 0
    for run in old_runs:
        old_intent = _load_json(run["intent_json"], "execution intent")
        if isinstance(old_intent, dict) and old_intent.get("tombstone") is True:
            continue
        run_id = run["id"]
        if protected_reference(run_id):
            continue
        # Keep the run/job/attempt identity as a tombstone for callback and retry deduplication.
        tombstone = {"tombstone": True, "run_id": run_id, "job_id": run["job_id"],
                    "step_id": run["step_id"], "attempt": run["attempt"],
                    "directive_version": run["directive_version"]}
        conn.execute("UPDATE execution_runs SET handle_json=NULL,result_json=NULL,intent_json=? WHERE id=?",
                     (canonical_json(tombstone), run_id))
        deleted_progress += conn.execute("DELETE FROM run_progress WHERE run_id=?", (run_id,)).rowcount
        cleaned_run_ids.append(run_id)

    claimed_events = {row[0] for row in conn.execute("SELECT claim_event_id FROM claims WHERE claim_event_id IS NOT NULL")}
    rows = conn.execute("SELECT id,event_type,record_id,payload_json,recorded_at FROM events WHERE recorded_at<=?", (cutoff,)).fetchall()
    event_ids = []
    cleaned_set = set(cleaned_run_ids)
    for row in rows:
        if row["id"] in claimed_events or protected_reference(row["id"]):
            continue
        event_type = row["event_type"]
        execution_event = event_type.startswith(("execution.", "scope_lock.", "runner."))
        if execution_event:
            payload = _load_json(row["payload_json"], "execution event")
            if any(_contains(payload, run_id) for run_id in cleaned_set):
                event_ids.append(row["id"])
                continue
        if row["record_id"] and execution_event:
            terminal = conn.execute("SELECT state FROM records WHERE id=?", (row["record_id"],)).fetchone()
            if terminal and terminal[0] in {"Done", "Canceled"}:
                event_ids.append(row["id"])
    for event_id in event_ids:
        conn.execute("DELETE FROM events WHERE id=?", (event_id,))

    tombstoned_requests = 0
    for row in conn.execute("SELECT request_id,response_json,exit_code FROM requests WHERE created_at<=?", (cutoff,)).fetchall():
        response = _load_json(row["response_json"], "request replay response")
        result = response.get("result") if isinstance(response, dict) else None
        if protected_reference(row["request_id"]):
            continue
        if isinstance(result, dict):
            run_id = result.get("run_id")
            record_id = result.get("record_id") or result.get("step_id")
            run = conn.execute("SELECT state FROM execution_runs WHERE id=?", (run_id,)).fetchone() if run_id else None
            record = conn.execute("SELECT kind,state FROM records WHERE id=?", (record_id,)).fetchone() if record_id else None
            if run and run[0] in ACTIVE_RUNS:
                continue
            if record and (record[0] not in {"work", "item", "step"} or record[1] not in {"Done", "Canceled"}):
                continue
        if isinstance(result, dict) and result.get("tombstone") is True:
            continue
        tombstone = {"protocol_version": 1, "request_id": row["request_id"],
                     "ok": row["exit_code"] == 0,
                     "result": {"tombstone": True, "request_id": row["request_id"]} if row["exit_code"] == 0 else None,
                     "error": None if row["exit_code"] == 0 else {"code": "request_result_expired",
                         "message": "The saved response expired; this request ID remains reserved", "retryable": False},
                     "warnings": ["request_result_expired"]}
        conn.execute("UPDATE requests SET response_json=? WHERE request_id=?", (canonical_json(tombstone), row["request_id"]))
        tombstoned_requests += 1
    return {"history_cutoff": cutoff, "runs_tombstoned": len(cleaned_run_ids),
            "execution_events_removed": len(event_ids), "progress_removed": deleted_progress,
            "requests_tombstoned": tombstoned_requests}


def execute_file(db, req):
    prior = replay(db, req)
    if prior:
        if prior[0].get("ok"):
            pending = _request_cleanup_journals(db, req["request_id"])
            _resume_cleanup(db, req["request_id"], pending)
        return prior
    p = req["payload"]
    artifact_ids = p.get("artifact_ids", [])
    if req["actor"] != "main":
        raise PmtError("invalid_retention_request", "Only main may execute retention")
    if not isinstance(artifact_ids, list) or any(not isinstance(aid, str) for aid in artifact_ids):
        raise PmtError("invalid_retention_request", "artifact_ids must be an array of IDs")
    if len(set(artifact_ids)) != len(artifact_ids):
        raise PmtError("invalid_retention_request", "Main must specify distinct candidate artifact IDs")
    for artifact_id in artifact_ids:
        identifier(artifact_id, "artifact_id")
    instant = _now()
    now = _utc_text(instant)
    retention_cutoff = _utc_text(instant - timedelta(days=TEMP_RETENTION_DAYS))
    existing = _request_cleanup_journals(db, req["request_id"])
    existing_ids = {job["artifact_id"] for job in existing}
    with db.write() as conn:
        for artifact_id in artifact_ids:
            if artifact_id in existing_ids:
                continue
            artifact = conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            if not artifact or artifact["state"] != "ready" or _referenced(conn, artifact_id, instant):
                raise PmtError("retention_protected", "Resource is missing, reserved or referenced", 3)
            if (not artifact["retention_until"] or artifact["retention_until"] > now
                    or artifact["created_at"] > retention_cutoff):
                raise PmtError("retention_not_expired", "Resource retention has not expired", 3)
        for artifact_id in artifact_ids:
            if artifact_id in existing_ids:
                continue
            journal_id = new_id()
            conn.execute("UPDATE artifacts SET state='deleting' WHERE id=?", (artifact_id,))
            conn.execute("INSERT INTO operation_journal VALUES(?,?,'reserved',?,?,?)", (journal_id, "resource_cleanup",
                         canonical_json({"artifact_id": artifact_id, "request_id": req["request_id"]}), now, now))
    all_jobs = _request_cleanup_journals(db, req["request_id"])
    failed = _resume_cleanup(db, req["request_id"], all_jobs)
    removed = sorted(job["artifact_id"] for job in _request_cleanup_journals(db, req["request_id"])
                     if job["state"] == "completed")
    def finish(conn, request):
        history = _cleanup_history(conn, _now())
        event(conn, request, "resource.cleanup.completed", payload={"removed_count": len(removed), "failed_count": len(failed),
              "runs_tombstoned": history["runs_tombstoned"], "requests_tombstoned": history["requests_tombstoned"]})
        return {"removed_artifact_ids": removed, "failed_artifact_ids": failed,
                "warnings": ["retention_partial_failure"] if failed else [],
                **history, "identifier_tombstones_preserved": True}
    response, code = db.run_request(req, finish)
    if code == 0 and failed:
        warnings = response.setdefault("warnings", [])
        if "retention_partial_failure" not in warnings:
            warnings.append("retention_partial_failure")
        try:
            with db.write() as conn:
                conn.execute("UPDATE requests SET response_json=? WHERE request_id=?",
                             (canonical_json(response), req["request_id"]))
        except (PmtError, OSError) as error:
            db.diagnostics.emit("request.warning_persistence_failed", level=40,
                                outcome="warning", error_code=getattr(error, "code", "database_error"))
    return response, code


def configure_logger(db):
    """Optional per-invocation log sink; stderr remains the fallback."""
    from logging.handlers import RotatingFileHandler
    from .diagnostics import JsonFormatter
    from .resources import _reject_links
    with db.connect() as conn:
        row = conn.execute("SELECT body_json FROM routing_settings WHERE id='diagnostics'").fetchone()
    if not row:
        return
    try:
        config = _load_json(row[0], "diagnostics configuration")
        if not isinstance(config, dict) or type(config.get("enabled")) is not bool:
            raise PmtError("diagnostics_config_invalid", "Stored diagnostics configuration is invalid", 5)
        if any(type(config.get(key)) is not int for key in ("max_bytes", "file_count", "retention_days")):
            raise PmtError("diagnostics_config_invalid", "Stored diagnostics bounds are invalid", 5)
    except PmtError as error:
        db.diagnostics.emit("diagnostic.configuration_invalid", level=40, outcome="error", error_code=error.code)
        raise
    if not config.get("enabled"):
        return
    root = db.root / "logs"
    _reject_links(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "pmt.jsonl"
    _reject_links(path)
    class Handler(RotatingFileHandler):
        def handleError(self, record):
            raise OSError("Diagnostic file unavailable")
    for handler in list(db.diagnostics.logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            db.diagnostics.logger.removeHandler(handler)
            handler.close()
    sink = Handler(path, maxBytes=config["max_bytes"], backupCount=config["file_count"] - 1, encoding="utf-8")
    sink.setFormatter(JsonFormatter())
    db.diagnostics.logger.addHandler(sink)
    cutoff = datetime.now(timezone.utc).timestamp() - config["retention_days"] * 86400
    for file in root.glob("pmt.jsonl.*"):
        _reject_links(file)
        if file.is_file() and file.stat().st_mtime < cutoff:
            file.unlink()
