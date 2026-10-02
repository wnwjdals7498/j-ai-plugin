"""Q7 Git baseline reconciliation with scope ownership and explicit review gates."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

from ..errors import PmtError
from ..resources import _reject_links
from ..util import canonical_json, fingerprint, strict_json_loads, utc_now

READ_OPERATIONS = {"read_project_baseline"}
WRITE_OPERATIONS = set()
FILE_OPERATIONS = {"sync_project_baseline"}
_ACTIVE_RUN_STATES = ("queued", "starting", "running", "review_pending", "reconciling", "cancel_requested")
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def _common():
    try:
        return importlib.import_module("pmt.phase2_common")
    except ImportError as exc:
        raise PmtError("phase2_common_unavailable", "Shared phase two ownership helpers are not available", 5, True) from exc


def _payload(req):
    payload = req.get("payload", {})
    if not isinstance(payload, dict):
        raise PmtError("invalid_payload", "payload must be an object")
    return payload


def _project_scope(db, conn, req):
    scope_id = req.get("scope_id") or _payload(req).get("scope_id")
    common = _common()
    scope = common.validate_scope(db, conn, scope_id)
    if scope["kind"] != "project":
        raise PmtError("project_scope_required", "Git baseline operations require a project scope")
    return scope_id


def handle(db, conn, req):
    if req.get("operation") != "read_project_baseline":
        raise PmtError("operation_unsupported", "Operation is not a database read operation")
    scope_id = _project_scope(db, conn, req)
    row = conn.execute("SELECT * FROM project_baselines WHERE scope_id=?", (scope_id,)).fetchone()
    if row is None:
        return {"found": False, "scope_id": scope_id}
    result = dict(row)
    try:
        result["details"] = json.loads(result.pop("body_json"))
    except (ValueError, TypeError):
        raise PmtError("baseline_invalid", "Stored baseline metadata is invalid", 5)
    result["found"] = True
    return result


def _git(workspace: Path, *args: str, repo_root: Path | None = None,
         input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    trusted_root = (repo_root or workspace).resolve()
    command = ["git", "-c", f"safe.directory={trusted_root}", "-c", "core.fsmonitor=false",
               "-c", "core.untrackedCache=false", "-C", str(workspace), *args]
    environment = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        environment.pop(key, None)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    # Treat this checkout as the repository boundary; never discover a parent repository.
    environment["GIT_CEILING_DIRECTORIES"] = str(trusted_root.parent)
    return subprocess.run(command, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          check=False, shell=False, env=environment)


def _git_text(workspace, *args, input_bytes=None, allow_failure=False, repo_root=None):
    result = _git(workspace, *args, input_bytes=input_bytes, repo_root=repo_root)
    if result.returncode and not allow_failure:
        raise PmtError("git_inspection_failed", "Git could not inspect the claimed workspace", 4, True,
                       {"git_exit_code": result.returncode})
    try:
        return result.returncode, result.stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PmtError("git_output_invalid", "Git returned non-UTF-8 metadata", 4) from exc


def _find_git_anchor(workspace):
    """Find the nearest repository marker without letting Git search unrelated parents."""
    current = workspace.resolve()
    while True:
        marker = current / ".git"
        _reject_links(marker)
        if marker.exists():
            if not marker.is_dir() and not marker.is_file():
                raise PmtError("git_repository_invalid", "Nearest .git marker is not a regular file or directory", 4)
            return current
        if current.parent == current:
            return None
        current = current.parent


def _git_context(workspace):
    root = _find_git_anchor(workspace)
    if root is None:
        return None, None
    code, top = _git_text(workspace, "rev-parse", "--show-toplevel", repo_root=root)
    actual_root = Path(top.strip()).resolve()
    if code or actual_root != root:
        raise PmtError("git_repository_invalid", "Git root does not match the nearest repository marker", 4)
    relative = workspace.resolve().relative_to(root).as_posix()
    return root, relative if relative != "." else "."


def _selected_ref(payload, branch):
    ref = payload.get("selected_ref")
    if ref is None:
        return branch or "HEAD"
    if not isinstance(ref, str) or not ref.strip() or len(ref) > 256 or ref.startswith("-") or "\x00" in ref:
        raise PmtError("invalid_git_ref", "selected_ref must be a valid Git ref name")
    return ref


def _validate_ack(payload, changed_paths, mapped_nodes, mapped_paths, graph_node_ids=None):
    if payload.get("reviewed_changes") is not True or payload.get("impact_set_reconciled") is not True:
        return False, "reviewed_changes and impact_set_reconciled must both be confirmed"
    reviewed_paths = payload.get("reviewed_paths")
    if (not isinstance(reviewed_paths, list) or any(not isinstance(path, str) for path in reviewed_paths) or
            set(reviewed_paths) != set(changed_paths)):
        return False, "reviewed_paths must exactly identify the changed paths"
    node_ids = payload.get("impact_node_ids")
    if not isinstance(node_ids, list) or any(not isinstance(value, str) for value in node_ids):
        return False, "impact_node_ids must explicitly state the reviewed impact set"
    if not set(mapped_nodes).issubset(set(node_ids)):
        return False, "impact_node_ids omits graph nodes mapped to changed paths"
    if graph_node_ids is not None and not set(node_ids).issubset(graph_node_ids):
        return False, "impact_node_ids references an unknown graph node"
    unmapped = sorted(set(changed_paths) - set(mapped_paths))
    if unmapped:
        reviewed_unmapped = payload.get("unmapped_paths_reviewed")
        if (not isinstance(reviewed_unmapped, list) or any(not isinstance(path, str) for path in reviewed_unmapped) or
                not set(unmapped).issubset(reviewed_unmapped)):
            return False, "unmapped_paths_reviewed must cover paths with unknown semantic impact"
    return True, None


def _hash_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _workspace_docs_fingerprint(workspace):
    docs = workspace / "docs" / "pmt-docs"
    _reject_links(workspace)
    if not docs.exists():
        return fingerprint([]), []
    _reject_links(docs)
    entries = []
    for path in sorted(docs.rglob("*"), key=lambda p: p.as_posix().casefold()):
        _reject_links(path)
        if not path.is_file():
            continue
        rel = path.relative_to(workspace).as_posix()
        entries.append({"path": rel, "sha256": _hash_bytes(path.read_bytes())})
    return fingerprint(entries), entries


def _repo_path(project_relative, relative):
    return relative if project_relative == "." else f"{project_relative.rstrip('/')}/{relative}"


def _project_path(project_relative, repository_path):
    value = Path(repository_path).as_posix()
    if project_relative == ".":
        return value
    prefix = project_relative.rstrip("/") + "/"
    return value[len(prefix):] if value.casefold().startswith(prefix.casefold()) else None


def _status_paths(workspace, repo_root, project_relative):
    args = ["status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all"]
    if project_relative != ".":
        args.extend(["--", project_relative])
    _, status = _git_text(repo_root, *args, repo_root=repo_root)
    paths = []
    for item in status.split("\x00"):
        if len(item) >= 4:
            relative = _project_path(project_relative, item[3:])
            if relative is not None:
                paths.append(relative)
    return status, sorted(set(paths), key=str.casefold)


def _status_and_dirty(workspace, repo_root, project_relative, status, paths):
    # This reads only paths whose run scope was checked by the caller.
    observed = []
    for relative in paths:
        candidate = workspace / Path(relative)
        _reject_links(candidate)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(workspace.resolve())
        except ValueError as exc:
            raise PmtError("git_path_outside_workspace", "Git reported a path outside its workspace", 4) from exc
        if resolved.is_file():
            worktree_hash = _hash_bytes(resolved.read_bytes())
        elif resolved.is_dir():
            submodule = _git(resolved, "rev-parse", "--verify", "HEAD", repo_root=resolved)
            worktree_hash = _hash_bytes(submodule.stdout.strip()) if submodule.returncode == 0 else "directory"
        else:
            worktree_hash = "deleted"
        index_result = _git(repo_root, "show", ":" + _repo_path(project_relative, relative), repo_root=repo_root)
        index_hash = _hash_bytes(index_result.stdout) if index_result.returncode == 0 else None
        observed.append({"path": relative, "worktree": worktree_hash, "index": index_hash,
                         "index_state": "available" if index_hash else "unavailable"})
    return bool(paths), fingerprint({"status_sha256": _hash_bytes(status.encode("utf-8")), "paths": observed}), status


def _resource_covers(resource, relative):
    prefix = os.path.normcase(str(resource).replace("\\", "/")).replace("\\", "/").strip("/") or "."
    path = os.path.normcase(str(relative).replace("\\", "/")).replace("\\", "/").strip("/") or "."
    return prefix == "." or path == prefix or path.startswith(prefix + "/")


def _lock_rows(conn, workspace):
    target = os.path.normcase(str(workspace.resolve()))
    rows = conn.execute("SELECT l.run_id,l.owner_session,l.kind,l.workspace,l.resource,r.state "
                        "FROM scope_locks l JOIN execution_runs r ON r.id=l.run_id").fetchall()
    return [dict(row) for row in rows if row["kind"] in {"path", "workspace"} and
            os.path.normcase(str(Path(row["workspace"]).resolve())) == target and row["state"] in _ACTIVE_RUN_STATES]


def _read_scope_status(conn, workspace, run_id, relative):
    locks = _lock_rows(conn, workspace)
    own = [lock for lock in locks if lock["run_id"] == run_id]
    if not any(_resource_covers(lock["resource"], relative) for lock in own):
        return False, "the current run does not own a read scope for this path"
    for lock in locks:
        if lock["run_id"] != run_id and (_resource_covers(lock["resource"], relative) or
                                          _resource_covers(relative, lock["resource"])):
            return False, "another active run owns an overlapping path"
    return True, None


def _require_any_run_scope(conn, db, req, workspace):
    locks = _lock_rows(conn, workspace)
    own = [lock for lock in locks if lock["owner_session"] == req.get("session_id") and
           lock["run_id"] == _payload(req).get("run_id")]
    if not own:
        # Delegate owner, run-state and workspace mismatch errors to the shared checker.
        return _common().require_workspace_claim(db, conn, req, str(workspace), ["."])
    # Prove the task owns one real path lock; detailed path reads are checked separately below.
    return _common().require_workspace_claim(db, conn, req, str(workspace), [own[0]["resource"]])


def _step_belongs_to_project(conn, step_id, project_id):
    row = conn.execute("SELECT id,scope_id FROM records WHERE id=? AND kind='step'", (step_id,)).fetchone()
    if row is None:
        return None
    common = _common()
    resolver = getattr(common, "project_scope_id", None)
    if resolver is not None:
        return dict(row) if resolver(conn, row["scope_id"]) == project_id else None
    found = conn.execute("WITH RECURSIVE project_tree(id,parent_id,kind) AS ("
                         "SELECT id,parent_id,kind FROM scopes WHERE id=? "
                         "UNION SELECT s.id,s.parent_id,s.kind FROM scopes s JOIN project_tree p ON s.parent_id=p.id) "
                         "SELECT 1 FROM project_tree WHERE id=? AND kind IN ('project','classification') LIMIT 1",
                         (project_id, row["scope_id"])).fetchone()
    return dict(row) if found else None


def _changed_paths(workspace, repo_root, project_relative, old_commit, new_commit):
    args = ["diff", "--name-only", "-z", "--no-renames", f"{old_commit}..{new_commit}"]
    if project_relative != ".":
        args.extend(["--", project_relative])
    code, raw = _git_text(repo_root, *args, allow_failure=True, repo_root=repo_root)
    if code:
        raise PmtError("git_diff_unavailable", "Commit changes could not be inspected", 4, True)
    paths = {_project_path(project_relative, value) for value in raw.split("\x00") if value}
    return sorted({value for value in paths if value is not None}, key=str.casefold)


def _graph_snapshot(workspace, scope_id):
    path = workspace / "docs" / "pmt-docs" / "plan.graph.json"
    _reject_links(path)
    if not path.exists():
        return None, None, None
    try:
        raw = path.read_bytes()
        graph = strict_json_loads(raw, max_bytes=8 * 1024 * 1024)
    except PmtError:
        raise
    except OSError as exc:
        raise PmtError("project_graph_unreadable", "Project graph could not be read", 4, True) from exc
    except Exception as exc:
        raise PmtError("project_graph_invalid", "Project graph is not valid JSON", 2) from exc
    planning = importlib.import_module("pmt.planning.graph")
    report = planning.validate_graph(graph, scope_id)
    return graph, report, _hash_bytes(raw)


def _committed_graph(workspace, repo_root, project_relative, commit, scope_id):
    if not commit:
        return None
    path = _repo_path(project_relative, "docs/pmt-docs/plan.graph.json")
    result = _git(repo_root, "show", f"{commit}:{path}", repo_root=repo_root)
    if result.returncode:
        return None
    graph = strict_json_loads(result.stdout, max_bytes=8 * 1024 * 1024)
    importlib.import_module("pmt.planning.graph").validate_graph(graph, scope_id)
    return graph


def _impact(graph, changed_paths):
    if graph is None:
        return [], []
    normalized = {path.replace("\\", "/").strip("/").casefold() for path in changed_paths}
    affected, mapped_paths = set(), set()
    for node in graph["nodes"]:
        refs = node.get("file_refs", [])
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, str):
                continue
            candidate = ref.replace("\\", "/").strip("/").casefold()
            if not candidate or ".." in Path(candidate).parts:
                continue
            for changed in normalized:
                if changed == candidate or changed.startswith(candidate.rstrip("/") + "/"):
                    affected.add(node["id"])
                    mapped_paths.add(next(p for p in changed_paths if p.replace("\\", "/").strip("/").casefold() == changed))
    graph_path = "docs/pmt-docs/plan.graph.json"
    if graph_path in changed_paths:
        affected.update(node["id"] for node in graph["nodes"])
        mapped_paths.add(graph_path)
    return sorted(affected), sorted(mapped_paths)


def _journal_start(db, req, workspace, run, scope_id):
    journal_id = str(uuid.uuid5(uuid.UUID(req["request_id"]), "git.sync_project_baseline"))
    now = utc_now()
    body = {"scope_id": scope_id, "workspace": str(workspace), "run_id": run["id"],
            "owner_session": req["session_id"], "request_id": req["request_id"], "state": "read_intent"}
    with db.write() as conn:
        old = conn.execute("SELECT state FROM operation_journal WHERE id=?", (journal_id,)).fetchone()
        if old:
            conn.execute("UPDATE operation_journal SET state='resumed',body_json=?,updated_at=? WHERE id=?",
                         (canonical_json(body), now, journal_id))
        else:
            conn.execute("INSERT INTO operation_journal(id,kind,state,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                         (journal_id, "git.sync_project_baseline", "reading", canonical_json(body), now, now))
    return journal_id


def _fail_journal(db, req, code):
    try:
        journal_id = str(uuid.uuid5(uuid.UUID(req["request_id"]), "git.sync_project_baseline"))
    except (KeyError, ValueError, TypeError, AttributeError):
        return
    with db.write() as conn:
        row = conn.execute("SELECT body_json FROM operation_journal WHERE id=?", (journal_id,)).fetchone()
        if row is None:
            return
        try:
            body = json.loads(row["body_json"])
        except (ValueError, TypeError):
            body = {}
        body["failure_code"] = code
        conn.execute("UPDATE operation_journal SET state='failed',body_json=?,updated_at=? WHERE id=?",
                     (canonical_json(body), utc_now(), journal_id))


def _record_result(db, req, scope_id, workspace, selected_ref, result, journal_id,
                   *, reviewed_commit, workspace_fingerprint, body, plan_update=None, cancel_steps=(),
                   advance_baseline=False, required_read_paths=()):
    def save(conn, request):
        common = _common()
        common.validate_scope(db, conn, scope_id)
        _require_any_run_scope(conn, db, request, workspace)
        for relative in required_read_paths:
            allowed, reason = _read_scope_status(conn, workspace, _payload(request)["run_id"], relative)
            if not allowed:
                raise PmtError("ownership_conflict", "Workspace scope changed during Git review", 3, False,
                               {"path": relative, "reason": reason})
        repo_root = _find_git_anchor(workspace)
        project_relative = (workspace.relative_to(repo_root).as_posix() if repo_root else None)
        body_with_anchor = {**body, "repo_root": str(repo_root) if repo_root else None,
                            "project_relative": project_relative or "."}
        now = utc_now()
        prior = conn.execute("SELECT * FROM project_baselines WHERE scope_id=?", (scope_id,)).fetchone()
        revision = (prior["revision"] + 1) if prior else 1
        stored_workspace, stored_ref, stored_commit, stored_fingerprint = (
            str(workspace), selected_ref, reviewed_commit, workspace_fingerprint)
        stored_body = body_with_anchor
        if prior and not advance_baseline:
            try:
                old_body = json.loads(prior["body_json"])
            except (ValueError, TypeError):
                old_body = {}
            stored_workspace, stored_ref = prior["workspace"], prior["selected_ref"]
            stored_commit, stored_fingerprint = prior["reviewed_commit"], prior["fingerprint"]
            stored_body = {**old_body, "pending_observation": body_with_anchor}
        elif not advance_baseline:
            stored_commit = None
        conn.execute("INSERT INTO project_baselines(scope_id,workspace,selected_ref,reviewed_commit,fingerprint,body_json,revision,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(scope_id) DO UPDATE SET workspace=excluded.workspace,"
                     "selected_ref=excluded.selected_ref,reviewed_commit=excluded.reviewed_commit,fingerprint=excluded.fingerprint,"
                     "body_json=excluded.body_json,revision=excluded.revision,updated_at=excluded.updated_at",
                     (scope_id, stored_workspace, stored_ref, stored_commit, stored_fingerprint,
                      canonical_json(stored_body), revision, now))
        if plan_update:
            for item in plan_update:
                row = conn.execute("SELECT id,plan_version,graph_version FROM plans WHERE id=? AND scope_id=?",
                                   (item["plan_id"], scope_id)).fetchone()
                if row is None:
                    continue
                old_version = str(row["plan_version"])
                new_version = str(int(old_version) + 1) if old_version.isdigit() else old_version + ".1"
                conn.execute("UPDATE plans SET graph_version=?,plan_version=?,sha256=?,baseline_commit=?,updated_at=? WHERE id=?",
                             (item.get("graph_version", row["graph_version"]), new_version, item["sha256"],
                              reviewed_commit, now, item["plan_id"]))
        for step_id in cancel_steps:
            owned_step = _step_belongs_to_project(conn, step_id, scope_id)
            if owned_step is None:
                continue
            step_body_row = conn.execute("SELECT body_json,revision,scope_id FROM records WHERE id=?", (step_id,)).fetchone()
            try:
                step_body = json.loads(step_body_row["body_json"])
            except (ValueError, TypeError):
                raise PmtError("step_metadata_invalid", "Impacted Step metadata is invalid", 5)
            reason = "git_baseline_changed"
            changed = (step_body.get("invalidated") is not True or
                       step_body.get("invalidation_reason") != reason or
                       step_body.get("invalidated_by_commit") != reviewed_commit)
            if changed:
                step_body.update(invalidated=True, invalidation_reason=reason,
                                 invalidated_by_commit=reviewed_commit,
                                 invalidated_graph_version=result.get("graph_validation", {}).get("graph_version"))
                conn.execute("UPDATE records SET body_json=?,revision=revision+1,updated_at=? WHERE id=?",
                             (canonical_json(step_body), now, step_id))
            rows = conn.execute("SELECT id,job_id,revision,state FROM execution_runs WHERE step_id=? AND state IN "
                                "('queued','starting','running','review_pending','reconciling','cancel_requested')", (step_id,)).fetchall()
            for run in rows:
                if run["state"] == "queued":
                    conn.execute("UPDATE execution_runs SET state='canceled',revision=revision+1,stop_confirmed=1,completed_at=?,updated_at=? WHERE id=?",
                                 (now, now, run["id"]))
                    conn.execute("UPDATE execution_jobs SET state='canceled',updated_at=? WHERE id=?", (now, run["job_id"]))
                    conn.execute("DELETE FROM scope_locks WHERE run_id=?", (run["id"],))
                elif run["state"] == "cancel_requested":
                    conn.execute("UPDATE execution_jobs SET state='cancel_requested',updated_at=? WHERE id=?",
                                 (now, run["job_id"]))
                    continue
                else:
                    conn.execute("UPDATE execution_runs SET state='cancel_requested',revision=revision+1,updated_at=? WHERE id=?",
                                 (now, run["id"]))
                    conn.execute("UPDATE execution_jobs SET state='cancel_requested',updated_at=? WHERE id=?",
                                 (now, run["job_id"]))
            if changed:
                common.event(conn, request, "reconciliation.premise_impact_detected",
                             scope_id=step_body_row["scope_id"], record_id=step_id,
                             payload={"plan_id": result.get("plan_id"), "reason": reason,
                                      "reviewed_commit": reviewed_commit})
        event_type = ("git.dirty_state.pinned" if result.get("status") == "dirty_owner_pinned" else
                      "git.sync.unchanged" if result.get("status") == "unchanged" else
                      "git.baseline.updated" if advance_baseline else "git.sync.review_required")
        common.event(conn, request, event_type,
                     scope_id=scope_id, payload={"reviewed_commit": reviewed_commit,
                                                 "status": result.get("status"), "revision": revision,
                                                 "changed_path_count": len(result.get("changed_paths", [])),
                                                 "impact_node_ids": result.get("impact_node_ids", []),
                                                 "affected_step_ids": result.get("invalidated_step_ids", []),
                                                 "dirty_fingerprint": result.get("dirty_fingerprint"),
                                                 "state_fingerprint": result.get("execution_basis", {}).get("state_fingerprint"),
                                                 "run_id": body_with_anchor.get("run_id")})
        conn.execute("UPDATE operation_journal SET state=?,body_json=?,updated_at=? WHERE id=?",
                     ("completed" if advance_baseline or result.get("status") in {"unchanged", "dirty_owner_pinned"} else "review_required",
                      canonical_json({"scope_id": scope_id, "workspace": str(workspace), "request_id": request["request_id"],
                                      "run_id": body_with_anchor.get("run_id"), "reviewed_commit": reviewed_commit,
                                      "result_status": result.get("status"), "fingerprint": workspace_fingerprint,
                                      "execution_basis": result.get("execution_basis")}),
                      now, journal_id))
        result["revision"] = revision
        return result
    response, exit_code = db.run_request(req, save)
    return response, exit_code


def _sync(db, req):
    payload = _payload(req)
    scope_id = req.get("scope_id") or payload.get("scope_id")
    workspace_value = payload.get("workspace")
    if not isinstance(workspace_value, str) or not Path(workspace_value).is_absolute():
        raise PmtError("invalid_workspace", "workspace must be an absolute path")
    workspace_input = Path(workspace_value)
    workspace = workspace_input.resolve()
    activity = payload.get("activity")
    if activity not in {"active", "nonactive"}:
        raise PmtError("invalid_activity", "activity must be active or nonactive")

    # Ownership must be proven before Git status, commit metadata, graph or document paths are read.
    common = _common()
    with db.connect() as conn:
        scope = common.validate_scope(db, conn, scope_id)
        if scope["kind"] != "project":
            raise PmtError("project_scope_required", "Git baseline operations require a project scope")
        run = _require_any_run_scope(conn, db, req, workspace)
        if _step_belongs_to_project(conn, run["step_id"], scope_id) is None:
            raise PmtError("ownership_conflict", "Run Step does not belong to the requested project", 3)
        previous = conn.execute("SELECT * FROM project_baselines WHERE scope_id=?", (scope_id,)).fetchone()
        previous = dict(previous) if previous else None

    journal_id = _journal_start(db, req, workspace, run, scope_id)
    _reject_links(workspace_input)
    _reject_links(workspace)
    repo_root, project_relative = _git_context(workspace)
    git_repo = repo_root is not None
    if git_repo:
        branch_code, branch = _git_text(workspace, "symbolic-ref", "--short", "--quiet", "HEAD",
                                        allow_failure=True, repo_root=repo_root)
        branch = branch.strip() if branch_code == 0 else None
    else:
        branch = None
    selected_ref = _selected_ref(payload, branch)
    if git_repo:
        code, head = _git_text(workspace, "rev-parse", "--verify", "--end-of-options",
                               selected_ref + "^{commit}", allow_failure=True, repo_root=repo_root)
    else:
        code, head = 1, ""
    is_git = git_repo and code == 0 and bool(_SHA.fullmatch(head.strip()))
    dirty = False
    dirty_fingerprint = None
    status = None
    graph = None
    graph_report = None
    graph_hash = None
    changed_paths = []
    commit_count = 0
    if is_git:
        head = head.strip()
        try:
            prior_details = json.loads(previous["body_json"]) if previous else {}
        except (ValueError, TypeError):
            raise PmtError("baseline_invalid", "Stored project baseline metadata is invalid", 5)
        if previous and (prior_details.get("repo_root") != str(repo_root) or
                         prior_details.get("project_relative") != project_relative):
            result = {"scope_id": scope_id, "status": "reconciliation_required",
                      "reason": "repository_anchor_changed", "previous_commit": previous.get("reviewed_commit"),
                      "head_commit": head, "repository_root": str(repo_root),
                      "project_relative": project_relative, "baseline_advanced": False,
                      "main_action": "reestablish the project baseline against the verified nearest Git root"}
            response, exit_code = _record_result(db, req, scope_id, workspace, selected_ref, result, journal_id,
                                                 reviewed_commit=previous.get("reviewed_commit"),
                                                 workspace_fingerprint=previous["fingerprint"],
                                                 body={"run_id": run["id"], "status": result["status"]})
            return response, exit_code
        status, dirty_paths = _status_paths(workspace, repo_root, project_relative)
        with db.connect() as conn:
            blocked_dirty = [(path, reason) for path in dirty_paths
                             for allowed, reason in [_read_scope_status(conn, workspace, run["id"], path)] if not allowed]
        if blocked_dirty:
            result = {"scope_id": scope_id, "status": "main_action_required", "reason": "dirty_paths_outside_owned_scope",
                      "dirty_paths": dirty_paths, "baseline_advanced": False,
                      "main_action": "obtain read scopes for dirty paths or wait for their active owners"}
            response, exit_code = _record_result(db, req, scope_id, workspace,
                                                 previous.get("selected_ref", selected_ref) if previous else selected_ref,
                                                 result, journal_id,
                                                 reviewed_commit=previous.get("reviewed_commit") if previous else None,
                                                 workspace_fingerprint=previous.get("fingerprint") if previous else fingerprint({"workspace": str(workspace)}),
                                                 body={"run_id": run["id"], "status": result["status"], "dirty_paths": dirty_paths})
            return response, exit_code
        dirty, dirty_fingerprint, status = _status_and_dirty(workspace, repo_root, project_relative,
                                                             status, dirty_paths)
        prior_commit = previous.get("reviewed_commit") if previous else None
        ref_changed = bool(previous and previous.get("selected_ref") != selected_ref)
        if dirty:
            owner = payload.get("dirty_owner_session")
            state_pin = payload.get("reviewed_dirty_fingerprint")
            if owner != req.get("session_id"):
                reason = "dirty_owner_unconfirmed"
            elif not isinstance(state_pin, str) or state_pin != dirty_fingerprint:
                reason = "dirty_fingerprint_unconfirmed"
            elif not previous or prior_commit != head or ref_changed:
                reason = "known_baseline_required"
            else:
                reason = None
            if reason is None:
                document_paths = ["docs/pmt-docs/plan.graph.json", "docs/pmt-docs/plan.md"]
                with db.connect() as conn:
                    read_status = [_read_scope_status(conn, workspace, run["id"], path) for path in document_paths]
                if not all(allowed for allowed, _ in read_status):
                    reason = next(message for allowed, message in read_status if not allowed)
                else:
                    graph, graph_report, graph_hash = _graph_snapshot(workspace, scope_id)
                    if graph is None:
                        reason = "published graph is unavailable"
                    else:
                        second_status, second_paths = _status_paths(workspace, repo_root, project_relative)
                        if second_paths != dirty_paths:
                            reason = "dirty_state_changed_during_review"
                        else:
                            _, second_dirty_fingerprint, _ = _status_and_dirty(workspace, repo_root, project_relative,
                                                                                second_status, second_paths)
                            _, current_head = _git_text(workspace, "rev-parse", "--verify", "--end-of-options",
                                                        selected_ref + "^{commit}", repo_root=repo_root)
                            if second_dirty_fingerprint != dirty_fingerprint or current_head.strip() != head:
                                reason = "dirty_state_changed_during_review"
                        if reason is None:
                            graph_path = workspace / "docs" / "pmt-docs" / "plan.graph.json"
                            _reject_links(graph_path)
                            if _hash_bytes(graph_path.read_bytes()) != graph_hash:
                                reason = "dirty_state_changed_during_review"
                        if reason is None:
                            plan_path = workspace / "docs" / "pmt-docs" / "plan.md"
                            _reject_links(plan_path)
                            document_hashes = {"docs/pmt-docs/plan.graph.json": graph_hash,
                                               "docs/pmt-docs/plan.md": (_hash_bytes(plan_path.read_bytes())
                                                                         if plan_path.is_file() else None)}
                            state_fingerprint = fingerprint({"commit": head, "dirty_fingerprint": dirty_fingerprint,
                                                             "documents": document_hashes})
                            basis = {"repository_root": str(repo_root), "project_relative": project_relative,
                                     "commit": head, "dirty_fingerprint": dirty_fingerprint,
                                     "state_fingerprint": state_fingerprint, "documents": document_hashes,
                                     "graph_version": graph_report["graph_version"]}
                            result = {"scope_id": scope_id, "status": "dirty_owner_pinned",
                                      "reviewed_commit": head, "selected_ref": selected_ref,
                                      "dirty": True, "dirty_owner": owner,
                                      "dirty_fingerprint": dirty_fingerprint, "dirty_paths": dirty_paths,
                                      "execution_basis": basis, "baseline_advanced": False, "state_pinned": True}
                            response, exit_code = _record_result(db, req, scope_id, workspace, selected_ref, result,
                                                                 journal_id, reviewed_commit=prior_commit,
                                                                 workspace_fingerprint=previous["fingerprint"],
                                                                 body={"run_id": run["id"], "dirty": True,
                                                                       "dirty_owner": owner,
                                                                       "dirty_fingerprint": dirty_fingerprint,
                                                                       "execution_basis": basis},
                                                                 required_read_paths=dirty_paths + document_paths)
                            return response, exit_code
            result = {"scope_id": scope_id, "status": "main_action_required", "reason": reason,
                      "head_commit": head, "repository_root": str(repo_root),
                      "project_relative": project_relative, "dirty": True,
                      "dirty_fingerprint": dirty_fingerprint,
                      "dirty_owner": owner if isinstance(owner, str) else "unknown",
                      "dirty_paths": dirty_paths, "baseline_advanced": False,
                      "main_action": "review the current dirty state, owner, and known commit before continuing"}
            response, exit_code = _record_result(db, req, scope_id, workspace, selected_ref, result, journal_id,
                                                 reviewed_commit=prior_commit,
                                                 workspace_fingerprint=previous["fingerprint"] if previous else fingerprint({"head": head}),
                                                 body={"run_id": run["id"], "status": result["status"],
                                                       "dirty_owner": owner if isinstance(owner, str) else "unknown",
                                                       "dirty_fingerprint": dirty_fingerprint})
            return response, exit_code
        diverged = False
        if previous and prior_commit:
            if not _SHA.fullmatch(prior_commit):
                raise PmtError("baseline_invalid", "Stored reviewed_commit is not a valid Git commit ID", 5)
            ancestor_code, _ = _git_text(workspace, "merge-base", "--is-ancestor", prior_commit, head,
                                         allow_failure=True, repo_root=repo_root)
            if ancestor_code == 1:
                diverged = True
            elif ancestor_code != 0:
                raise PmtError("git_history_inspection_failed", "Git could not compare baseline history", 4, True)
            if prior_commit == head and not ref_changed:
                result = {"scope_id": scope_id, "status": "unchanged", "selected_ref": selected_ref,
                          "reviewed_commit": head, "dirty": False, "changed_paths": [], "baseline_advanced": False,
                          "analysis_skipped": True}
                response, exit_code = _record_result(db, req, scope_id, workspace, selected_ref, result, journal_id,
                                                     reviewed_commit=head, workspace_fingerprint=fingerprint({"head": head, "dirty": dirty_fingerprint}),
                                                     body={"run_id": run["id"], "branch": branch, "status": "unchanged"})
                return response, exit_code
            changed_paths = _changed_paths(workspace, repo_root, project_relative, prior_commit, head)
            rev_range = head if diverged else f"{prior_commit}..{head}"
            _, count_text = _git_text(workspace, "rev-list", "--count", rev_range, repo_root=repo_root)
            commit_count = int(count_text.strip())
        else:
            changed_paths = []
        with db.connect() as conn:
            graph_read_allowed, graph_read_reason = _read_scope_status(
                conn, workspace, run["id"], "docs/pmt-docs/plan.graph.json")
        if graph_read_allowed:
            graph, graph_report, graph_hash = _graph_snapshot(workspace, scope_id)
        else:
            graph, graph_report, graph_hash = None, None, None
        mapped_nodes, mapped_paths = _impact(graph, changed_paths)
        old_graph = (_committed_graph(workspace, repo_root, project_relative, prior_commit, scope_id)
                     if graph_read_allowed and prior_commit and changed_paths else None)
        old_mapped_nodes, old_mapped_paths = _impact(old_graph, changed_paths)
        mapped_nodes = sorted(set(mapped_nodes) | set(old_mapped_nodes))
        mapped_paths = sorted(set(mapped_paths) | set(old_mapped_paths))
        reconciliation_reason = ("history_diverged" if diverged else "selected_ref_changed" if ref_changed else None)
        result = {"scope_id": scope_id, "repository_root": str(repo_root),
                  "project_relative": project_relative,
                  "status": "reconciliation_required" if reconciliation_reason else "review_required",
                  "reason": reconciliation_reason, "previous_ref": previous.get("selected_ref") if previous else None,
                  "selected_ref": selected_ref,
                  "previous_commit": prior_commit, "head_commit": head, "commit_count": commit_count,
                  "changed_paths": changed_paths, "impact_node_ids": mapped_nodes,
                  "mapped_paths": mapped_paths, "graph_validation": graph_report,
                  "dirty": False, "baseline_advanced": False, "activity": activity}
        if previous is None:
            result["reason"] = "initial_baseline_requires_explicit_review"
        elif not changed_paths:
            result["reason"] = "commit_range_has_no_changed_paths"
        graph_node_ids = ({node["id"] for node in graph["nodes"]} if graph else set())
        if old_graph:
            graph_node_ids.update(node["id"] for node in old_graph["nodes"])
        ack, reason = _validate_ack(payload, changed_paths, mapped_nodes, mapped_paths, graph_node_ids)
        if reconciliation_reason and payload.get("reestablish_baseline") is not True:
            ack = False
            reason = "reestablish_baseline must explicitly approve the changed ref or divergent history"
        result["main_action"] = None if ack else reason
        if graph is None and changed_paths:
            ack = False
            result["main_action"] = ("published graph is unavailable; semantic impact must be reviewed"
                                      if graph_read_allowed else graph_read_reason)
        if "docs/pmt-docs/plan.graph.json" in changed_paths and prior_commit and old_graph is None:
            ack = False
            result["main_action"] = "previous graph is unavailable; review possible removed node and Step impact"
        if ack:
            second_status, second_paths = _status_paths(workspace, repo_root, project_relative)
            if second_paths:
                result.update(status="main_action_required", baseline_advanced=False,
                              main_action="workspace changed during Git review; preserve changes and retry after ownership review")
                response, exit_code = _record_result(db, req, scope_id, workspace,
                                                     previous.get("selected_ref", selected_ref) if previous else selected_ref,
                                                     result, journal_id,
                                                     reviewed_commit=prior_commit,
                                                     workspace_fingerprint=previous.get("fingerprint") if previous else fingerprint({"head": head}),
                                                     body={"run_id": run["id"], "status": result["status"],
                                                           "changed_during_review": second_paths})
                return response, exit_code
            _, current_head = _git_text(workspace, "rev-parse", "--verify", "--end-of-options",
                                        selected_ref + "^{commit}", repo_root=repo_root)
            if current_head.strip() != head:
                result.update(status="reconciliation_required", baseline_advanced=False,
                              main_action="selected ref moved during review; rescan the new commit")
                response, exit_code = _record_result(db, req, scope_id, workspace,
                                                     previous.get("selected_ref", selected_ref) if previous else selected_ref,
                                                     result, journal_id,
                                                     reviewed_commit=prior_commit,
                                                     workspace_fingerprint=previous.get("fingerprint") if previous else fingerprint({"head": head}),
                                                     body={"run_id": run["id"], "status": result["status"],
                                                           "observed_head": head, "current_head": current_head.strip()})
                return response, exit_code
        if not ack:
            effective_fingerprint = fingerprint({"head": head, "dirty": dirty_fingerprint})
            response, exit_code = _record_result(db, req, scope_id, workspace, selected_ref, result, journal_id,
                                                 reviewed_commit=prior_commit, workspace_fingerprint=effective_fingerprint,
                                                 body={"run_id": run["id"], "branch": branch, "status": "review_required",
                                                       "head_commit": head, "dirty": False})
            return response, exit_code
        plan_update = []
        cancel_steps = []
        if graph:
            for node in graph["nodes"]:
                if node["id"] not in set(payload["impact_node_ids"]):
                    continue
                refs = node.get("work_item_step_refs", {})
                cancel_steps.extend(refs.get("step", []))
        if old_graph:
            for node in old_graph["nodes"]:
                if node["id"] not in set(payload["impact_node_ids"]):
                    continue
                refs = node.get("work_item_step_refs", {})
                cancel_steps.extend(refs.get("step", []))
        with db.connect() as conn:
            cancel_steps = sorted({step_id for step_id in cancel_steps
                                   if _step_belongs_to_project(conn, step_id, scope_id) is not None})
        plan_read_paths = []
        if graph_hash and (set(changed_paths).intersection({"docs/pmt-docs/plan.graph.json", "docs/pmt-docs/plan.md"}) or
                           payload.get("impact_node_ids")):
            with db.connect() as conn:
                for row in conn.execute("SELECT id,relative_path,workspace FROM plans WHERE scope_id=? AND state='published'",
                                        (scope_id,)).fetchall():
                    if Path(row["workspace"]).resolve() != workspace:
                        continue
                    relative = row["relative_path"]
                    allowed, reason = _read_scope_status(conn, workspace, run["id"], relative)
                    if not allowed:
                        result.update(status="main_action_required", baseline_advanced=False,
                                      main_action=reason)
                        response, exit_code = _record_result(db, req, scope_id, workspace, selected_ref, result,
                                                             journal_id, reviewed_commit=prior_commit,
                                                             workspace_fingerprint=fingerprint({"head": head}),
                                                             body={"run_id": run["id"], "status": result["status"]})
                        return response, exit_code
                    path = workspace / Path(relative)
                    _reject_links(path)
                    if path.is_file():
                        plan_update.append({"plan_id": row["id"], "sha256": _hash_bytes(path.read_bytes()),
                                            "graph_version": graph["graph_version"]})
                        plan_read_paths.append(relative)
        result = {**result, "status": "updated", "reviewed_commit": head, "baseline_advanced": True,
                  "impact_node_ids": payload["impact_node_ids"], "affected_step_ids": cancel_steps,
                  "invalidated_step_ids": cancel_steps}
        effective_fingerprint = fingerprint({"head": head, "dirty": dirty_fingerprint})
        response, exit_code = _record_result(db, req, scope_id, workspace, selected_ref, result, journal_id,
                                             reviewed_commit=head, workspace_fingerprint=effective_fingerprint,
                                             body={"run_id": run["id"], "branch": branch, "dirty": False,
                                                   "graph_sha256": graph_hash, "graph_version": graph["graph_version"] if graph else None,
                                                   "impact_node_ids": sorted(set(payload["impact_node_ids"]))},
                                             plan_update=plan_update, cancel_steps=sorted(set(cancel_steps)),
                                             advance_baseline=True,
                                             required_read_paths=(["docs/pmt-docs/plan.graph.json"] if graph_read_allowed else []) + plan_read_paths)
        return response, exit_code

    if git_repo:
        result = {"scope_id": scope_id, "status": "initial_commit_required", "selected_ref": selected_ref,
                  "baseline_advanced": False, "main_action": "create or select an existing Git commit before setting a baseline"}
        response, exit_code = _record_result(db, req, scope_id, workspace, selected_ref, result, journal_id,
                                             reviewed_commit=previous.get("reviewed_commit") if previous else None,
                                             workspace_fingerprint=previous.get("fingerprint") if previous else fingerprint({"workspace": str(workspace)}),
                                             body={"run_id": run["id"], "branch": branch, "status": result["status"]})
        return response, exit_code

    # A Git-less project uses a read-only fingerprint of the managed project-document tree.
    with db.connect() as conn:
        docs_read_allowed, docs_read_reason = _read_scope_status(conn, workspace, run["id"], "docs/pmt-docs")
    if not docs_read_allowed:
        result = {"scope_id": scope_id, "status": "main_action_required", "reason": "project_docs_read_scope_missing",
                  "baseline_advanced": False, "main_action": docs_read_reason}
        response, exit_code = _record_result(db, req, scope_id, workspace,
                                             previous.get("selected_ref", selected_ref) if previous else selected_ref,
                                             result, journal_id,
                                             reviewed_commit=previous.get("reviewed_commit") if previous else None,
                                             workspace_fingerprint=previous.get("fingerprint") if previous else fingerprint({"workspace": str(workspace)}),
                                             body={"run_id": run["id"], "status": result["status"]})
        return response, exit_code
    docs_fingerprint, entries = _workspace_docs_fingerprint(workspace)
    graph, graph_report, graph_hash = _graph_snapshot(workspace, scope_id)
    if graph:
        graph_ids = {node["id"] for node in graph["nodes"]}
    else:
        graph_ids = set()
    prior_commit = previous.get("reviewed_commit") if previous else None
    result = {"scope_id": scope_id, "status": "git_unavailable", "selected_ref": selected_ref,
              "reviewed_commit": None, "document_fingerprint": docs_fingerprint, "documents": entries,
              "baseline_advanced": False, "main_action": "explicitly review the non-Git document baseline"}
    reviewed_docs = payload.get("reviewed_paths")
    expected_docs = [entry["path"] for entry in entries]
    approved = (payload.get("reviewed_changes") is True and payload.get("impact_set_reconciled") is True
                and isinstance(reviewed_docs, list) and set(reviewed_docs) == set(expected_docs)
                and isinstance(payload.get("impact_node_ids"), list)
                and set(payload["impact_node_ids"]).issubset(graph_ids))
    second_fingerprint, second_entries = _workspace_docs_fingerprint(workspace)
    if second_fingerprint != docs_fingerprint:
        approved = False
        result["status"] = "workspace_changed"
        result["main_action"] = "project documents changed during snapshot; retry after ownership review"
    if approved:
        result["status"] = "updated_without_git"
        result["baseline_advanced"] = True
        result["main_action"] = None
        result["graph_validation"] = graph_report
    response, exit_code = _record_result(db, req, scope_id, workspace, selected_ref, result, journal_id,
                                         reviewed_commit=None, workspace_fingerprint=docs_fingerprint,
                                         body={"run_id": run["id"], "git_available": False,
                                               "document_fingerprint": docs_fingerprint,
                                               "documents": [{"path": entry["path"], "sha256": entry["sha256"]} for entry in entries]},
                                         advance_baseline=approved, required_read_paths=["docs/pmt-docs"])
    return response, exit_code


def execute_file(db, req):
    try:
        if req.get("operation") != "sync_project_baseline":
            raise PmtError("operation_unsupported", "Unsupported reconciliation file operation")
        replay = _common().replay(db, req)
        if replay is not None:
            return replay
        return _sync(db, req)
    except PmtError as error:
        _fail_journal(db, req, error.code)
        return db._response(req.get("request_id"), False, None, error.as_dict(), []), error.exit_code
    except OSError as error:
        _fail_journal(db, req, "git_workspace_io_error")
        issue = PmtError("git_workspace_io_error", "Claimed workspace could not be inspected", 4, True)
        return db._response(req.get("request_id"), False, None, issue.as_dict(), []), issue.exit_code
    except subprocess.SubprocessError:
        _fail_journal(db, req, "git_inspection_failed")
        issue = PmtError("git_inspection_failed", "Git inspection could not be completed", 4, True)
        return db._response(req.get("request_id"), False, None, issue.as_dict(), []), issue.exit_code
