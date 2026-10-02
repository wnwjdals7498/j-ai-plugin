"""Q2 operations. Database handlers are pure reads or transaction-local writes."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import uuid
from pathlib import Path

from ..errors import PmtError
from ..resources import _reject_links
from ..util import canonical_json, new_id, utc_now
from .graph import render_docs, validate_graph

READ_OPERATIONS = {"validate_plan_graph", "read_plan"}
WRITE_OPERATIONS = set()
FILE_OPERATIONS = {"save_plan_draft", "publish_project_docs"}
_OPS = READ_OPERATIONS | FILE_OPERATIONS
_BEGIN = "<!-- PMT:PLAN:BEGIN -->"
_END = "<!-- PMT:PLAN:END -->"


def _payload(req):
    value = req.get("payload", {})
    if not isinstance(value, dict):
        raise PmtError("invalid_payload", "payload must be an object")
    return value


def _common():
    try:
        return importlib.import_module("pmt.phase2_common")
    except ImportError as exc:
        raise PmtError("phase2_common_unavailable", "Shared phase two resource helpers are not available", 5, True) from exc


def _project_scope(db, conn, req):
    scope_id = req.get("scope_id") or _payload(req).get("scope_id")
    if not isinstance(scope_id, str):
        raise PmtError("scope_required", "scope_id is required")
    common = _common()
    common.validate_scope(db, conn, scope_id)
    row = conn.execute("SELECT id,kind FROM scopes WHERE id=?", (scope_id,)).fetchone()
    if row is None or row["kind"] != "project":
        raise PmtError("project_scope_required", "Planning operations require a project scope")
    return scope_id


def _artifact_id(payload):
    artifact_id = payload.get("artifact_id")
    try:
        if not isinstance(artifact_id, str) or str(uuid.UUID(artifact_id)) != artifact_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise PmtError("invalid_artifact_id", "artifact_id must be a canonical UUID")
    return artifact_id


def handle(db, conn, req):
    op = req.get("operation")
    if op not in READ_OPERATIONS:
        raise PmtError("operation_unsupported", "Operation is not a read-only planning operation")
    payload = _payload(req)
    if op == "validate_plan_graph":
        graph = payload.get("graph")
        if graph is None:
            common = _common()
            graph = common.load_json_resource(db, conn, _artifact_id(payload))
        return validate_graph(graph, req.get("scope_id") or payload.get("scope_id"), complete=payload.get("complete", True))
    scope_id = _project_scope(db, conn, req)
    plan_id = payload.get("plan_id")
    if not isinstance(plan_id, str):
        raise PmtError("plan_id_required", "plan_id is required")
    row = conn.execute("SELECT * FROM plans WHERE id=? AND scope_id=?", (plan_id, scope_id)).fetchone()
    if row is None:
        raise PmtError("plan_not_found", "Plan was not found in this project", 3)
    result = dict(row)
    result["graph_ref"] = {"workspace": result["workspace"], "relative_path": result["relative_path"],
                           "sha256": result["sha256"], "graph_version": result["graph_version"]}
    if payload.get("include_graph") and result["state"] == "draft":
        result["graph"] = _common().load_json_resource(db, conn, result["artifact_id"])
    elif payload.get("include_graph") and result["state"] == "published":
        result["graph_source"] = "project_git_document"
    return result


def _call_resource(db, req, graph, scope_id):
    common = _common()
    return common.persist_json_resource(db, req, graph, scope_id, "plan_draft", owner_id=scope_id)


def _save_metadata(db, req, scope_id, artifact, graph):
    def write(conn, request):
        _project_scope(db, conn, request)
        plan_id = request.get("payload", {}).get("plan_id") or new_id()
        try:
            if str(uuid.UUID(plan_id)) != plan_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise PmtError("invalid_plan_id", "plan_id must be a canonical UUID")
        now = utc_now()
        row = conn.execute("SELECT id,graph_version,plan_version FROM plans WHERE id=? AND scope_id=?",
                           (plan_id, scope_id)).fetchone()
        prior = row["plan_version"] if row else None
        requested_version = request.get("payload", {}).get("expected_plan_version")
        if requested_version is not None and requested_version != prior and prior is not None:
            raise PmtError("plan_version_conflict", "The plan draft changed since it was read", 3)
        plan_version = str(int(prior) + 1) if prior and str(prior).isdigit() else ("1" if prior is None else str(prior) + ".1")
        graph_version = graph["graph_version"]
        doc_path = "docs/pmt-docs/plan.graph.json"
        if row:
            conn.execute("UPDATE plans SET artifact_id=?,graph_version=?,requirements_version=?,plan_version=?,state='draft',workspace=?,relative_path=?,sha256=?,baseline_commit=?,updated_at=? WHERE id=?",
                         (artifact["artifact_id"], graph_version, str(request.get("payload", {}).get("requirements_version", "1")),
                          plan_version, str(request.get("payload", {}).get("workspace", "")), doc_path,
                          validate_graph(graph, complete=False)["sha256"], request.get("payload", {}).get("baseline_commit"), now, plan_id))
        else:
            conn.execute("INSERT INTO plans(id,scope_id,artifact_id,graph_version,requirements_version,plan_version,state,workspace,relative_path,sha256,baseline_commit,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (plan_id, scope_id, artifact["artifact_id"], graph_version,
                          str(request.get("payload", {}).get("requirements_version", "1")), plan_version, "draft",
                          str(request.get("payload", {}).get("workspace", "")), doc_path, validate_graph(graph, complete=False)["sha256"],
                          request.get("payload", {}).get("baseline_commit"), now, now))
        _common().event(conn, request, "planning.graph_validated", scope_id=scope_id, record_id=None,
                        payload={"plan_id": plan_id, "graph_version": graph_version, "state": "draft",
                                 "node_count": len(graph["nodes"])})
        return {"plan_id": plan_id, "scope_id": scope_id, "artifact_id": artifact["artifact_id"],
                "graph_version": graph_version, "plan_version": plan_version, "state": "draft",
                "sha256": validate_graph(graph, complete=False)["sha256"]}
    response, exit_code = db.run_request(req, write)
    if exit_code:
        error = response.get("error") or {}
        raise PmtError(error.get("code", "plan_save_failed"), error.get("message", "Plan draft could not be saved"), exit_code,
                       error.get("retryable", False), error.get("details"))
    return response.get("result")


def execute_file(db, req):
    op = req.get("operation")
    try:
        payload = _payload(req)
        if op == "save_plan_draft":
            graph = payload.get("graph")
            scope_id = req.get("scope_id") or payload.get("scope_id")
            if graph is None:
                raise PmtError("plan_graph_required", "payload.graph is required")
            report = validate_graph(graph, scope_id, complete=False)
            with db.connect() as conn:
                _project_scope(db, conn, req)
            replay = _common().replay(db, req)
            if replay is not None:
                return replay
            artifact = _call_resource(db, req, graph, scope_id)
            result = _save_metadata(db, req, scope_id, artifact, graph)
            return db._response(req.get("request_id"), True, result, None, []), 0
        if op == "publish_project_docs":
            replay = _common().replay(db, req)
            if replay is not None:
                return replay
            return _publish(db, req)
        raise PmtError("operation_unsupported", "Unsupported planning file operation")
    except PmtError as error:
        return db._response(req.get("request_id"), False, None, error.as_dict(), []), error.exit_code
    except OSError as error:
        issue = PmtError("plan_docs_io_error", "Project plan documents could not be written", 4, True)
        return db._response(req.get("request_id"), False, None, issue.as_dict(), []), issue.exit_code


def _file_hash(path):
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise PmtError("plan_docs_path_invalid", "Project plan target must be a regular file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_stage(target, content, temp):
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_links(temp)
    with temp.open("xb") as stream:
        stream.write(content.encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    return temp


def _publish(db, req):
    payload = _payload(req)
    scope_id = req.get("scope_id") or payload.get("scope_id")
    plan_id = payload.get("plan_id")
    if not isinstance(plan_id, str):
        raise PmtError("plan_id_required", "plan_id is required")
    with db.connect() as conn:
        _project_scope(db, conn, req)
        plan = conn.execute("SELECT * FROM plans WHERE id=? AND scope_id=?", (plan_id, scope_id)).fetchone()
        if plan is None:
            raise PmtError("plan_not_found", "Plan was not found in this project", 3)
        if plan["state"] == "published":
            raise PmtError("plan_already_published", "This plan version is already published", 3)
        graph = _common().load_json_resource(db, conn, plan["artifact_id"])
    report = validate_graph(graph, scope_id)
    markdown, graph_json, _ = render_docs(graph)
    workspace = Path(payload.get("workspace") or plan["workspace"]).expanduser().resolve()
    docs_dir = workspace / "docs" / "pmt-docs"
    graph_path = docs_dir / "plan.graph.json"
    plan_path = docs_dir / "plan.md"
    agents_path = workspace / "AGENTS.md"
    relative_paths = ["docs/pmt-docs/plan.graph.json", "docs/pmt-docs/plan.md", "AGENTS.md"]
    common = _common()
    # No project document is read before the publishing run owns the workspace paths.
    with db.connect() as conn:
        run = common.require_workspace_claim(db, conn, req, str(workspace), relative_paths)
    _reject_links(workspace)
    for relative in relative_paths:
        _reject_links(workspace / Path(relative))
    agents_hash = _file_hash(agents_path)
    agents_text = agents_path.read_text(encoding="utf-8") if agents_hash else ""
    managed = f"{_BEGIN}\nPlan: docs/pmt-docs/plan.md\nGraph: docs/pmt-docs/plan.graph.json\n{_END}"
    if _BEGIN in agents_text and _END not in agents_text:
        raise PmtError("managed_reference_invalid", "AGENTS.md contains an incomplete PMT reference block")
    if _BEGIN in agents_text:
        left, rest = agents_text.split(_BEGIN, 1)
        _, right = rest.split(_END, 1)
        agents_text = left + managed + right
    else:
        agents_text = agents_text.rstrip() + ("\n\n" if agents_text.strip() else "") + managed + "\n"
    files = dict(zip(relative_paths, (graph_json, markdown, agents_text)))
    expected = payload.get("expected_file_hashes", {})
    if not isinstance(expected, dict):
        raise PmtError("invalid_expected_hashes", "expected_file_hashes must be an object")
    observed = {rel: _file_hash(workspace / Path(rel)) for rel in files}
    for rel, actual in observed.items():
        if actual is not None and expected.get(rel) != actual:
            raise PmtError("project_doc_conflict", "An existing project document changed or has no matching expected hash",
                           3, False, {"path": rel, "actual_sha256": actual})
    journal_id = new_id()
    staged = {}
    backups = {}
    installed = set()
    committed = False
    now = utc_now()
    temp_paths = {rel: workspace / Path(rel).with_name(Path(rel).name + ".pmt-stage-" + journal_id)
                  for rel in files}
    backup_paths = {rel: workspace / Path(rel).with_name(Path(rel).name + ".pmt-backup-" + journal_id)
                    for rel in files if observed[rel] is not None}
    journal_body = {"scope_id": scope_id, "plan_id": plan_id, "workspace": str(workspace),
                    "paths": list(files), "owner_session": req.get("session_id"), "run_id": run["id"],
                    "request_id": req.get("request_id"), "expected_hashes": observed,
                    "temporary_paths": {key: path.relative_to(workspace).as_posix() for key, path in temp_paths.items()},
                    "backup_paths": {key: path.relative_to(workspace).as_posix() for key, path in backup_paths.items()}}
    with db.write() as conn:
        conn.execute("INSERT INTO operation_journal(id,kind,state,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (journal_id, "publish_project_docs", "staging",
                      canonical_json(journal_body), now, now))
    try:
        for rel, content in files.items():
            target = workspace / Path(rel)
            staged[rel] = temp_paths[rel]
            _atomic_stage(target, content, staged[rel])
            if observed[rel] is not None:
                if _file_hash(target) != observed[rel]:
                    raise PmtError("project_doc_conflict", "An existing project document changed during publish", 3,
                                   False, {"path": rel})
                backup = backup_paths[rel]
                with backup.open("xb") as stream:
                    stream.write(target.read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                backups[rel] = backup
        for rel, temp in staged.items():
            if _file_hash(workspace / Path(rel)) != observed[rel]:
                raise PmtError("project_doc_conflict", "A project document changed during publish", 3,
                               False, {"path": rel})
            os.replace(temp, workspace / Path(rel))
            installed.add(rel)
        hashes = {rel: _file_hash(workspace / Path(rel)) for rel in files}
        def commit_publish(conn, request):
            common.require_workspace_claim(db, conn, request, str(workspace), relative_paths)
            current = conn.execute("SELECT state,artifact_id FROM plans WHERE id=? AND scope_id=?", (plan_id, scope_id)).fetchone()
            if current is None or current["state"] != "draft" or current["artifact_id"] != plan["artifact_id"]:
                raise PmtError("plan_version_conflict", "Plan changed while documents were being published", 3)
            conn.execute("UPDATE plans SET state='published',workspace=?,relative_path=?,sha256=?,baseline_commit=?,updated_at=? WHERE id=?",
                         (str(workspace), "docs/pmt-docs/plan.graph.json", hashes["docs/pmt-docs/plan.graph.json"],
                          payload.get("baseline_commit"), utc_now(), plan_id))
            common.event(conn, req, "planning.project_docs_published", scope_id=scope_id, record_id=None,
                         payload={"plan_id": plan_id, "graph_version": graph["graph_version"], "run_id": run["id"],
                                  "baseline_commit": payload.get("baseline_commit"), "sha256": hashes["docs/pmt-docs/plan.graph.json"]})
            conn.execute("UPDATE operation_journal SET state='committed',body_json=?,updated_at=? WHERE id=?",
                         (canonical_json({"scope_id": scope_id, "plan_id": plan_id, "paths": list(files),
                                          "hashes": hashes, "run_id": run["id"]}), utc_now(), journal_id))
            return {"plan_id": plan_id, "state": "published", "graph_version": graph["graph_version"],
                    "sha256": hashes["docs/pmt-docs/plan.graph.json"], "files": hashes,
                    "baseline_commit": payload.get("baseline_commit"), "validation": report}
        response, exit_code = db.run_request(req, commit_publish)
        if exit_code:
            error = response.get("error") or {}
            raise PmtError(error.get("code", "plan_publish_failed"), error.get("message", "Plan publication failed"),
                           exit_code, error.get("retryable", False), error.get("details"))
        committed = True
        for backup in backups.values():
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass
        return db._response(req.get("request_id"), True,
                            response.get("result"), None, []), 0
    except Exception as error:
        if not committed:
            for rel in installed:
                if rel not in backups:
                    (workspace / Path(rel)).unlink(missing_ok=True)
            for rel, backup in backups.items():
                if backup.exists():
                    os.replace(backup, workspace / Path(rel))
            with db.write() as conn:
                conn.execute("UPDATE operation_journal SET state='failed',body_json=?,updated_at=? WHERE id=?",
                             (canonical_json({"scope_id": scope_id, "plan_id": plan_id,
                                              "error_code": error.code if isinstance(error, PmtError) else "io_error"}),
                              utc_now(), journal_id))
        raise
    finally:
        for temp in staged.values():
            temp.unlink(missing_ok=True)
        for backup in backups.values():
            backup.unlink(missing_ok=True)
