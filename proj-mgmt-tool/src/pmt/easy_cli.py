"""Short ``pmt`` commands over local or hosted PMT storage.

Every command builds the normal protocol-v1 request and runs it through the
same ``select_store`` path as the JSON CLI.  Callers never write request IDs,
actor names, revisions or claim references by hand.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import easy_setup
from .errors import PmtError
from .storage_config import _read_profile, configure_storage, probe_storage

CHECK_NAMESPACE = uuid.UUID("6f1c7f9e-3c1d-4f5e-9a51-2a4d8c0b7e11")


class EasyError(Exception):
    def __init__(self, code, message, exit_code=3, details=None):
        super().__init__(message)
        self.code, self.message, self.exit_code, self.details = code, message, exit_code, details or {}


def _roots():
    config_root, data_root = easy_setup.default_roots(os.environ)
    profile, config_hash = _read_profile(config_root)
    if profile is None:
        from .client_setup.mode import prepare
        state = prepare(os.environ, os.getcwd())
        if state["status"] != "ready":
            raise EasyError(state.get("error_code", "not_configured"), state.get("message", "PMT setup failed."))
        profile, config_hash = _read_profile(config_root)
    if profile and profile.get("mode") == "local" and not (Path(data_root) / "pmt.sqlite3").is_file():
        from .client_setup.mode import prepare
        state = prepare(os.environ, os.getcwd())
        if state["status"] != "ready":
            raise EasyError(state.get("error_code", "local_setup_failed"), state.get("message", "Local PMT setup failed."))
        profile, config_hash = _read_profile(config_root)
    if profile and profile.get("mode") == "hosted":
        from .client_setup.credentials import load_credential
        try:
            load_credential(config_root, os.environ)
        except PmtError as error:
            raise EasyError(error.code, "PMT credential is unavailable; check the protected credential store.") from error
    if profile is None:
        raise EasyError("not_configured", "PMT storage profile could not be prepared.")
    return config_root, data_root, profile, config_hash


def _session_id():
    native = os.environ.get("CLAUDE_CODE_SESSION_ID")
    return native if native else "pmt-cli-" + hashlib.sha256(str(os.getpid()).encode()).hexdigest()[:12]


def _state_path(data_root):
    return Path(data_root) / "easy-cli" / "claims.json"


def _load_claims(data_root):
    try:
        return json.loads(_state_path(data_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_claims(data_root, claims):
    path = _state_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(claims, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def execute(operation, payload=None, *, record_id=None, expected_revision=None, scope_id=None,
            request_id=None, session_id=None, allow_unscoped=False):
    """Send one request through the selected hosted store; raise on failure."""
    from .service import normalize_request
    from .storage_config import select_store

    config_root, data_root, profile, _ = _roots()
    selected_scope = None if allow_unscoped and operation == "create_scope" and profile["mode"] == "local" else (
        scope_id or os.environ.get("PMT_SCOPE_ID") or _current_scope(profile))
    request = {"protocol_version": 1, "operation": operation,
               "request_id": request_id or str(uuid.uuid4()), "actor": profile.get("actor", "local"),
               "session_id": session_id or _session_id(), "payload": payload or {}}
    if selected_scope is not None:
        request["scope_id"] = selected_scope
    if selected_scope is None and not (allow_unscoped and operation == "create_scope" and profile["mode"] == "local"):
        raise EasyError("project_not_linked", "This checkout is not linked to a PMT project. Run `pmt link`.")
    if record_id is not None:
        request["record_id"] = record_id
    if expected_revision is not None:
        request["expected_revision"] = expected_revision
    request = normalize_request(request)
    store = select_store(str(data_root), str(config_root), request, environ=os.environ)
    try:
        result, exit_code = store.execute(request)
    except PmtError as error:
        if not error.retryable or not callable(getattr(store, "get_request_result", None)):
            raise
        previous = store.get_request_result(request["request_id"], request["actor"], request["session_id"],
                                            expected_request=request)
        if previous is not None:
            result, exit_code = previous
        else:
            result, exit_code = store.execute(request)
    if exit_code != 0 or not result.get("ok"):
        error = result.get("error") or {}
        raise EasyError(error.get("code", "request_failed"), error.get("message", "PMT request failed"),
                        exit_code or 3, error.get("details"))
    return result["result"]


def _records(scope_id=None):
    result = execute("read_context", {"limit": 200}, scope_id=scope_id)
    return result.get("records", [])


def _current_scope(profile):
    root, branch = easy_setup.git_checkout(os.getcwd())
    if root is not None:
        mapping, _reason = easy_setup.select_mapping(profile, root, branch)
        return mapping["project_id"] if mapping else None
    return os.environ.get("PMT_SCOPE_ID")


def _find(record_ref):
    """Resolve a full UUID or a unique ID prefix within the current project."""
    matches = [r for r in _records() if r.get("record_id", r.get("id", "")).startswith(record_ref)]
    if len(matches) != 1:
        raise EasyError("record_not_found" if not matches else "record_ambiguous",
                        f"No unique record matches '{record_ref}'.")
    return matches[0]


def _rid(record):
    return record.get("record_id") or record.get("id")


def _is_uuid(value):
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def cmd_check(args):
    try:
        config_root, data_root, profile, _ = _roots()
    except (EasyError, PmtError) as error:
        _print_rows([("PMT setup", "FAIL", getattr(error, "code", "not_configured"))])
        return 1
    if profile["mode"] == "local":
        from .client_setup.local_commands import check
        try:
            return check(sys.modules[__name__])
        except (EasyError, PmtError) as error:
            _print_rows([("local check", "FAIL", getattr(error, "code", "check_failed"))])
            return 1
    rows = []
    try:
        ca_note = _check_ca(profile)
        probe = probe_storage(str(config_root))
    except (EasyError, PmtError) as error:
        _print_rows([("Host authentication/compatibility", "FAIL", getattr(error, "code", "remote_unavailable"))])
        return 1
    rows.append(("Host CA", "ok", ca_note))
    pre = probe.get("host_preflight") or {}
    rows.append(("Host 인증·호환", "ok", f"core {pre.get('core_version')} / db {pre.get('db_schema')} / "
                 f"graph {pre.get('graph_schema')} / protocol {pre.get('protocol_versions')}"))
    scope = _current_scope(profile)
    if not scope:
        rows.append(("프로젝트 연결", "미연결", "pmt link 필요"))
        _print_rows(rows)
        return 1
    try:
        records = _records()
        rows.append(("조회", "ok", f"레코드 {len(records)}건"))
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        request_id = str(uuid.uuid5(CHECK_NAMESPACE, f"{profile['environment_id']}:{scope}:{day}"))
        payload = {"kind": "fact", "title": f"pmt check {day} ({profile['actor']})",
                   "reason": "pmt check 연결 확인", "body": {}}
        first = execute("save_change", payload, request_id=request_id, session_id="pmt-check")
        again = execute("save_change", payload, request_id=request_id, session_id="pmt-check")
        same = _rid(first) == _rid(again)
        rows.append(("기록·재전송", "ok" if same else "FAIL", f"같은 요청 재전송 시 같은 레코드 {_rid(first)[:8]}"))
    except (EasyError, PmtError) as error:
        rows.append(("Host check", "FAIL", getattr(error, "code", "check_failed")))
        _print_rows(rows)
        return 1
    _print_rows(rows)
    return 0 if same else 1


def _check_ca(profile):
    ca_file = profile.get("ca_file")
    if not ca_file:
        return "system trust store"
    path = Path(ca_file)
    if not path.is_file():
        raise EasyError("host_ca_unavailable", "Configured Host CA file is missing.")
    try:
        import ssl
        ssl.create_default_context(cafile=str(path))
        certificate = ssl._ssl._test_decode_cert(str(path))
        expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(certificate["notAfter"]), timezone.utc)
    except (OSError, ValueError, KeyError, ssl.SSLError) as error:
        raise EasyError("host_ca_invalid", "Configured Host CA file is invalid.") from error
    if expires <= datetime.now(timezone.utc):
        raise EasyError("host_ca_expired", "Configured Host CA certificate has expired.")
    return f"expires {expires.date().isoformat()}"


def cmd_link(args):
    config_root, data_root, profile, config_hash = _roots()
    if profile["mode"] == "local":
        from .client_setup.local_commands import link
        return link(sys.modules[__name__], args)
    root, branch = easy_setup.git_checkout(os.getcwd())
    if root is None or branch is None:
        raise EasyError("not_a_branch_checkout", "Run pmt link inside a Git checkout on a branch.")
    mappings = list(profile.get("workspace_mappings", []))
    same_root = [m for m in mappings if os.path.abspath(m["local_root"]) == root]
    if any(m["branch"] == branch for m in same_root):
        print(f"이미 연결됨: {root} ({branch})")
        return 0
    project = args.project or (same_root[0]["project_id"] if same_root else None)
    repository = args.repository or (same_root[0]["repository_id"] if same_root else None)
    if project is not None and (not _is_uuid(project) or repository is None or not _is_uuid(repository)):
        from .client_setup.local_commands import resolve_project
        project, repository = resolve_project(sys.modules[__name__], config_root, profile, project, repository)
    if project is None or repository is None:
        known = {(m["project_id"], m["repository_id"]) for m in mappings}
        if len(known) == 1 and args.project is None and args.repository is None:
            project, repository = next(iter(known))
        else:
            raise EasyError("link_ids_required", "First link of a repository needs --project and --repository from the Host handoff.")
    mappings.append({"repository_id": repository, "project_id": project, "branch": branch,
                     "branch_key_sha256": hashlib.sha256(branch.encode("utf-8")).hexdigest(),
                     "local_root": root, "relative_graph_path": args.graph_path})
    request = {"mode": "hosted", "expected_config_sha256": config_hash, "endpoint": profile["endpoint"],
               "credential_env": profile["credential_env"], "device_id": profile["device_id"],
               "namespace_id": profile["namespace_id"], "expected_actor": profile["actor"],
               "workspace_mappings": mappings}
    if profile.get("ca_file"):
        request["ca_file"] = profile["ca_file"]
    configure_storage(str(config_root), request)
    print(f"연결함: {root} ({branch}) → project {project}. 새 세션부터 자동으로 사용합니다.")
    return 0


def cmd_status(args):
    _config_root, data_root, profile, _ = _roots()
    if profile["mode"] == "local":
        from .client_setup.local_commands import load_claims
        claims = load_claims(data_root)
    else:
        claims = _load_claims(data_root)
    records = _records()
    rows = [(r.get("kind"), _rid(r)[:8], r.get("state") or r.get("status"), f"rev {r.get('revision')}",
             r.get("title"), "내 점유" if _rid(r) in claims else "") for r in records]
    _print_rows(rows) if rows else print("기록 없음")
    return 0


def cmd_add(args):
    payload = {"kind": args.kind, "title": args.title, "reason": args.reason or "pmt add", "body": {}}
    if args.kind == "item":
        if not args.parent or not args.criteria:
            raise EasyError("item_fields_required", "Items need a parent work and at least one --criteria.")
        payload["parent_id"] = _rid(_find(args.parent))
        payload["body"] = {"criteria": [{"id": f"c{i}", "text": text} for i, text in enumerate(args.criteria, 1)]}
        profile = _roots()[2]
        if profile["mode"] == "local":
            mapping, _reason = easy_setup.select_mapping(profile, *easy_setup.git_checkout(os.getcwd()))
            if mapping:
                payload["body"]["workspace"] = mapping["local_root"]
    elif args.parent:
        payload["parent_id"] = _rid(_find(args.parent))
    result = execute("save_change", payload)
    print(f"생성함: {args.kind} {_rid(result)} (rev {result.get('revision')})")
    return 0


def cmd_start(args):
    record = _find(args.item)
    if _roots()[2]["mode"] == "local":
        from .client_setup.local_commands import start
        return start(sys.modules[__name__], record)
    result = execute("claim_task", {}, record_id=_rid(record), expected_revision=record["revision"])
    data_root = easy_setup.default_roots(os.environ)[1]
    claims = _load_claims(data_root)
    claims[_rid(record)] = {"claim_ref": result["claim_ref"], "session_id": _session_id(),
                            "revision": result["revision"]}
    _save_claims(data_root, claims)
    print(f"점유함: {record.get('title')} (rev {result['revision']})")
    return 0


def _owned(record):
    data_root = easy_setup.default_roots(os.environ)[1]
    claims = _load_claims(data_root)
    claim = claims.get(_rid(record))
    if claim is None:
        raise EasyError("not_claimed_here", "This machine does not hold a claim for that item. Run pmt start first.")
    return data_root, claims, claim


def cmd_pause(args):
    record = _find(args.item)
    if _roots()[2]["mode"] == "local":
        from .client_setup.local_commands import pause
        return pause(sys.modules[__name__], record, args)
    data_root, claims, claim = _owned(record)
    result = execute("release_claim", {"claim_ref": claim["claim_ref"], "status": "Paused",
                                       "reason": args.reason or "pmt pause", "resume": args.next},
                     record_id=_rid(record), expected_revision=record["revision"], session_id=claim["session_id"])
    claims.pop(_rid(record), None)
    _save_claims(data_root, claims)
    print(f"멈춤: {record.get('title')} (rev {result['revision']}). 다음: {args.next}")
    return 0


HELPER_TITLE = "pmt helper: verification run (자동 생성)"
HELPER_DIRECTIVE = {
    "purpose": "Hold an active run so a hosted Item can record its verification", "goal": "Verify Item criteria",
    "non_goal": ["No code change by this Step"],
    "change_scope": {"add": [], "modify": [], "delete": [], "forbidden": ["Host Git access"]},
    "inputs": [{"name": "checkout", "meaning": "client checkout"}],
    "outputs": [{"name": "verification", "meaning": "recorded verification"}],
    "tests": [{"name": "item test", "meaning": "client test command"}],
    "logging": [{"name": "safe trace", "meaning": "IDs and hashes only"}],
    "method": {"steps": ["run test", "record verification"]}, "context_refs": [],
    "autonomy": {"authority": "method", "scope": "current Step"}}


def _mapping_for_scope(profile):
    scope = os.environ.get("PMT_SCOPE_ID")
    root, branch = easy_setup.git_checkout(os.getcwd())
    mapping, reason = easy_setup.select_mapping(profile, root, branch)
    if mapping is None or mapping["project_id"] != scope:
        raise EasyError("project_not_linked", "Run pmt done inside the linked checkout and branch.")
    return mapping


def _helpers_path(data_root):
    return Path(data_root) / "easy-cli" / "helpers.json"


def _helper(data_root, record, mapping, session):
    """Return the reusable helper Item/Step/route for this project, creating it once."""
    path = _helpers_path(data_root)
    try:
        helpers = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        helpers = {}
    key = mapping["project_id"] + ":" + mapping["canonical_workspace"]
    if key in helpers:
        return helpers[key]
    payload = {"kind": "item", "title": HELPER_TITLE, "reason": "hosted 완료 검증용 보조 실행",
               "body": {"criteria": [{"id": "h1", "text": "helper"}]}}
    if record.get("parent_id"):
        payload["parent_id"] = record["parent_id"]
    item = _rid(execute("save_change", payload, session_id=session))
    step = execute("save_step_directive", {
        "item_id": item, "title": "pmt helper verification run", "directive": HELPER_DIRECTIVE,
        "requirements_version": "pmt-helper-v1", "plan_version": "pmt-helper-v1",
        "workspace": mapping["canonical_workspace"], "canonical_workspace": mapping["canonical_workspace"],
        "repository_id": mapping["repository_id"], "relative_graph_path": mapping["relative_graph_path"],
        "kind": "investigate", "exploration_approved": True, "product_stage": "prototype", "role": "lower",
        "scopes": [{"kind": "workspace", "workspace": mapping["canonical_workspace"], "resource": "."}],
        "criteria": [{"id": "h1", "text": "helper"}], "dependencies": []}, session_id=session)["step_id"]
    requirements = {"role": "lower", "needs": ["code"], "active_agent": "claude",
                    "requested_route": "native", "run_state": "not_started"}
    try:
        selection = execute("select_execution_route", {"requirements": requirements}, session_id=session)
    except EasyError:
        capability = {"agent": "claude", "provider": "anthropic", "model": "claude-native", "mode": "native",
                      "support": "supported", "capabilities": ["code"], "capability_ref": "pmt-helper-native",
                      "auth_state": "authenticated", "max_concurrency": 8, "evidence_ref": "pmt:helper",
                      "source_ref": "pmt:helper",
                      "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        execute("register_capabilities", {"capabilities": [capability]}, expected_revision=1, session_id=session)
        selection = execute("select_execution_route", {"requirements": requirements}, session_id=session)
    from .routing.client_config import route_for_enqueue
    helpers[key] = {"item": item, "step": step, "route": route_for_enqueue(selection)}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(helpers, ensure_ascii=False, indent=1), encoding="utf-8")
    return helpers[key]


def _host_store(profile):
    from .http_store import HttpStore
    return HttpStore(profile["endpoint"], profile["credential_env"], profile["device_id"],
                     profile["environment_id"], profile["namespace_id"], profile.get("ca_file"))


def _tracked_files(root):
    import subprocess
    listed = subprocess.run(["git", "-C", root, "ls-files", "-z"], stdout=subprocess.PIPE, check=True).stdout
    paths = sorted(p for p in listed.decode("utf-8").split("\0") if p)
    if len(paths) > 5000:
        raise EasyError("too_many_files", "pmt done supports checkouts up to 5000 tracked files.")
    files = []
    for rel in paths:
        target = Path(root, rel)
        if target.is_file() and not target.is_symlink():
            raw = target.read_bytes()
            files.append({"path": rel, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)})
    return files


def cmd_done(args):
    """Run the test, record hosted verification and finish the claimed Item.

    Hosted completion needs an active run; a reusable helper Step under a
    sibling helper Item provides it and is canceled again afterwards.
    """
    import platform
    import sqlite3
    import subprocess
    from .util import canonical_json, fingerprint

    config_root, data_root, profile, _ = _roots()
    record = _find(args.item)
    if profile["mode"] == "local":
        from .client_setup.local_commands import done
        return done(sys.modules[__name__], args, record)
    item_id = _rid(record)
    _, claims, claim = _owned(record)
    session = claim["session_id"]
    mapping = _mapping_for_scope(profile)
    root = mapping["local_root"]
    dirty = subprocess.run(["git", "-C", root, "status", "--porcelain", "--untracked-files=no"],
                           stdout=subprocess.PIPE, check=True).stdout.strip()
    if dirty:
        raise EasyError("uncommitted_changes", "Commit the work before pmt done; verification is bound to a commit.")
    criteria = (record.get("body") or {}).get("criteria") or []
    if not criteria:
        raise EasyError("criteria_missing", "The item has no completion criteria.")
    from .client_setup.local_commands import _parse_command
    try:
        command = _parse_command(args.test)
    except (OSError, ValueError) as error:
        raise EasyError("test_command_invalid", "The test command could not be parsed.") from error
    if not command:
        raise EasyError("test_required", "Give the test command with --test.")

    helper = _helper(data_root, record, mapping, session)
    run = execute("enqueue_execution", {"step_id": helper["step"], "route": helper["route"]}, session_id=session)
    run_id = run["run_id"]
    prepared = execute("prepare_execution", {"run_id": run_id, "expected_run_revision": run.get("revision", 1)},
                       session_id=session)
    run_revision = prepared.get("revision")
    evidence = None
    try:
        execute("capture_work_basis", {"repository_id": mapping["repository_id"], "workspace": root,
                                       "relative_graph_path": mapping["relative_graph_path"], "run_id": run_id,
                                       "task_id": helper["item"], "inventory_paths": [mapping["relative_graph_path"]]},
                session_id=session)
        pin = execute("read_source_snapshot", {"run_id": run_id}, session_id=session)["source_pin"]
        print(f"테스트 실행: {args.test}")
        completed = subprocess.run(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=args.timeout, check=False)
        output = completed.stdout
        sys.stdout.write(output.decode("utf-8", "replace")[-2000:])
        host = _host_store(profile)
        host.register_session(session)
        evidence = host.publish_resource({"request_id": str(uuid.uuid4()), "scope_id": mapping["project_id"],
                                          "purpose": "evidence"}, output, session_id=session)["artifact_ref"]
        if completed.returncode != 0:
            raise EasyError("test_failed", f"Test failed (exit {completed.returncode}); the item stays in progress.", 1)
        files = _tracked_files(root)
        inputs = {"source_commit": pin["reviewed_commit"], "files": fingerprint(files)}
        definition = {"target_id": item_id, "definition_id": "pmt.item.test", "definition_version": "1",
                      "command": command}
        manifest = {"schema_version": 1, **{k: definition[k] for k in ("target_id", "definition_id", "definition_version")},
                    "environment_id": profile["environment_id"], "canonical_workspace": mapping["canonical_workspace"],
                    "source_pin": pin, "command": command, "inputs_sha256": fingerprint(inputs),
                    "criteria": {c["id"]: fingerprint(c) for c in criteria}, "workspace_files": files,
                    "runtime": {"os": platform.system(), "architecture": platform.machine(),
                                "python": platform.python_version(), "sqlite": sqlite3.sqlite_version, "packages": []},
                    "dependency_manifests": [], "configuration_hashes": [],
                    "evidence_refs": [{"id": evidence["id"], "sha256": evidence["sha256"]}],
                    "inventory_status": "complete", "provenance": "client_snapshot"}
        manifest_ref = host.publish_resource({"request_id": str(uuid.uuid4()), "scope_id": mapping["project_id"],
                                              "purpose": "verification_snapshot"},
                                             canonical_json(manifest).encode("utf-8"), session_id=session)["artifact_ref"]
        common = {"project_id": mapping["project_id"], "repository_id": mapping["repository_id"],
                  "canonical_workspace": mapping["canonical_workspace"],
                  "relative_graph_path": mapping["relative_graph_path"], "run_id": run_id,
                  "expected_run_revision": run_revision, "expected_source": pin}
        def publish(expected):
            return execute("publish_verification_snapshot", common | definition | {
                "inputs_sha256": fingerprint(inputs), "verification_resource_ref": manifest_ref,
                "expected_snapshot_revision": expected}, session_id=session)["snapshot_ref"]
        try:
            snapshot = publish(0)
        except EasyError as error:
            current = error.details.get("current_revision")
            if error.code != "revision_conflict" or not isinstance(current, int):
                raise
            snapshot = publish(current)
        lookup = execute("lookup_verification", common | definition | {"inputs": inputs,
                         "verification_snapshot_ref": snapshot}, session_id=session)
        verification = execute("record_verification", common | definition | {
            "inputs": inputs, "outcome": "pass", "exit_code": 0, "evidence_ids": [evidence["id"]],
            "criterion_ids": [c["id"] for c in criteria], "before_fingerprint": lookup["input_fingerprint"],
            "verification_snapshot_ref": snapshot}, session_id=session)
        latest = _find(item_id)
        finished = execute("finish_task", {"claim_ref": claim["claim_ref"], "result": args.result,
                                           "verification_ids": [verification["verification_id"]], "run_id": run_id,
                                           "verification_snapshot_ref": snapshot, "expected_source": pin},
                           record_id=item_id, expected_revision=latest["revision"], session_id=session)
        claims.pop(item_id, None)
        _save_claims(data_root, claims)
        print(f"완료: {record.get('title')} → {finished.get('state')} (rev {finished.get('revision')}), "
              f"verification {verification['verification_id'][:8]}")
        return 0
    finally:
        _end_helper_run(run_id, session, evidence)


def _end_helper_run(run_id, session, evidence):
    """Cancel the helper run so its scope locks are released; it never launched a process."""
    try:
        current = execute("read_execution", {"run_id": run_id}, session_id=session)["run"]
        if current["state"] in {"canceled", "failed", "blocked", "succeeded"}:
            return
        canceled = execute("request_execution_cancel", {"run_id": run_id,
                                                        "expected_run_revision": current["revision"]}, session_id=session)
        if canceled.get("state") == "cancel_requested":
            refs = [evidence["id"]] if evidence else []
            if not refs:
                print("주의: 보조 run이 cancel_requested 상태로 남았습니다(증거 없음).", file=sys.stderr)
                return
            execute("reconcile_execution", {"run_id": run_id, "expected_run_revision": canceled["revision"],
                                            "stopped": False, "not_started": True, "actual_state": "canceled",
                                            "evidence_refs": refs}, session_id=session)
    except (EasyError, PmtError) as error:
        print(f"주의: 보조 run 정리 실패({getattr(error, 'code', 'error')}). pmt status로 확인하세요.", file=sys.stderr)


def _print_rows(rows):
    for row in rows:
        print(" | ".join("" if cell is None else str(cell) for cell in row))


def build_parser():
    parser = argparse.ArgumentParser(prog="pmt", description="PMT 간편 명령")
    sub = parser.add_subparsers(dest="command", required=True)
    connect = sub.add_parser("connect", help="서버 인계 파일로 Host 연결")
    connect.add_argument("--handoff", required=True)
    credential = connect.add_mutually_exclusive_group()
    credential.add_argument("--credential-file")
    credential.add_argument("--credential-stdin", action="store_true")
    connect.add_argument("--dry-run", action="store_true")
    connect.set_defaults(func=cmd_connect)
    sub.add_parser("disconnect", help="저장된 Host credential만 제거").set_defaults(func=cmd_disconnect)
    storage = sub.add_parser("storage", help="저장 연결 설정 확인")
    storage_sub = storage.add_subparsers(dest="storage_command", required=True)
    storage_sub.add_parser("status", help="저장된 연결 설정을 읽기 전용으로 표시").set_defaults(func=cmd_storage_status)
    storage_sub.add_parser("probe", help="Host 연결과 호환성을 실제 확인").set_defaults(func=cmd_storage_probe)
    sub.add_parser("check", help="연결·인증·기록·재전송 확인").set_defaults(func=cmd_check)
    link = sub.add_parser("link", help="현재 checkout을 PMT project에 연결")
    link.add_argument("project", nargs="?")
    link.add_argument("--new", metavar="TITLE")
    link.add_argument("--yes", action="store_true")
    link.add_argument("--repository")
    link.add_argument("--graph-path", default="docs/pmt-docs/graph.json")
    link.set_defaults(func=cmd_link)
    sub.add_parser("unlink", help="현재 branch의 project 연결 해제").set_defaults(func=cmd_unlink)
    sub.add_parser("projects", help="알려진 PMT project 목록").set_defaults(func=cmd_projects)
    sub.add_parser("mode", help="현재 저장 모드와 경로 표시").set_defaults(func=cmd_mode)
    sub.add_parser("status", help="현재 project 기록 목록").set_defaults(func=cmd_status)
    add = sub.add_parser("add", help="work/item 추가")
    add.add_argument("kind", choices=["work", "item"])
    add.add_argument("title")
    add.add_argument("--parent")
    add.add_argument("--criteria", action="append")
    add.add_argument("--reason")
    add.set_defaults(func=cmd_add)
    start = sub.add_parser("start", help="item 점유")
    start.add_argument("item")
    start.set_defaults(func=cmd_start)
    done = sub.add_parser("done", help="테스트 실행 → 검증 기록 → 완료")
    done.add_argument("item")
    done.add_argument("--test", required=True, help="테스트 명령, 예: \".venv/bin/python -m pytest -q\"")
    done.add_argument("--result", required=True, help="완료 결과 한 줄")
    done.add_argument("--timeout", type=int, default=1800)
    done.set_defaults(func=cmd_done)
    pause = sub.add_parser("pause", help="점유 해제(Paused)")
    pause.add_argument("item")
    pause.add_argument("--next", required=True)
    pause.add_argument("--reason")
    pause.set_defaults(func=cmd_pause)
    return parser


def cmd_unlink(args):
    from .client_setup.local_commands import unlink
    return unlink(sys.modules[__name__])


def cmd_projects(args):
    from .client_setup.local_commands import list_projects
    return list_projects(sys.modules[__name__])


def cmd_mode(args):
    from .client_setup.local_commands import mode
    return mode(sys.modules[__name__])


def _read_credential_input(args):
    if args.credential_file:
        try:
            with Path(args.credential_file).open("rb") as stream:
                raw = stream.read(65537)
        except OSError as error:
            raise PmtError("credential_input_unavailable", "Credential input file could not be read") from error
        if len(raw) > 65536:
            raise PmtError("credential_input_invalid", "Credential input exceeds its size limit")
    elif args.credential_stdin:
        stream = getattr(sys.stdin, "buffer", None)
        raw = stream.read(65537) if stream is not None else sys.stdin.read(65537).encode("utf-8")
        if len(raw) > 65536:
            raise PmtError("credential_input_invalid", "Credential input exceeds its size limit")
    else:
        return None
    try:
        value = raw.decode("utf-8").rstrip("\r\n")
    except UnicodeError as error:
        raise PmtError("credential_input_invalid", "Credential input must be UTF-8 text") from error
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise PmtError("credential_input_invalid", "Credential input must contain one nonempty line")
    return value


def cmd_connect(args):
    config_root, _data_root = easy_setup.default_roots(os.environ)
    from .client_setup.connect import connect
    result = connect(config_root, args.handoff, credential=_read_credential_input(args), dry_run=args.dry_run)
    summary = result["connect_summary"]
    print("Handoff validated only; no files written and no Host contact." if args.dry_run
          else "Connected to PMT Host; protected credential and profile saved.")
    print(f"Endpoint: {summary['endpoint']}")
    print(f"Namespace: {summary['namespace_id']}")
    print(f"Actor: {summary['actor']}")
    print(f"Scopes: {', '.join(summary['scopes'])}")
    print(f"Permissions: {', '.join(summary['permissions'])}")
    compatibility = summary["compatibility"]
    versions = (f"Core {compatibility.get('core_version', compatibility.get('core'))}; "
                f"DB schema {compatibility.get('db_schema')}; "
                f"graph schema {compatibility.get('graph_schema')}; "
                f"protocol {compatibility.get('protocol_versions', compatibility.get('protocol'))}")
    print(f"Compatibility ({summary['compatibility_source']}): {versions}")
    ca_sha256 = summary.get("ca_sha256")
    print(f"Public CA SHA-256: {ca_sha256}" if ca_sha256 else "CA trust: system trust store")
    return 0


def cmd_disconnect(args):
    config_root, _data_root = easy_setup.default_roots(os.environ)
    from .client_setup.connect import disconnect
    removed = disconnect(config_root)
    print("Saved Host credential removed." if removed else "No saved Host credential.")
    return 0


def cmd_storage_status(args):
    from .storage_config import storage_status
    config_root, _data_root = easy_setup.default_roots(os.environ)
    status = storage_status(config_root)
    print(f"mode: {status['mode'] if status['configured'] else 'unconfigured'}")
    print(f"ConfigRoot: {config_root}")
    if status["configured"] and status["mode"] == "hosted":
        for field in ("endpoint", "actor", "namespace_id"):
            print(f"{field}: {status[field]}")
        print(f"CA: {'configured' if status.get('ca_configured') else 'system trust store'}")
        from .client_setup.credentials import has_credential_store
        print(f"saved credential: {'present' if has_credential_store(config_root) else 'absent'}")
    return 0


def cmd_storage_probe(args):
    from .storage_config import probe_storage
    from .client_setup.credentials import ENV_NAME, load_credential
    config_root, _data_root = easy_setup.default_roots(os.environ)
    profile, _digest = _read_profile(config_root)
    original = os.environ.get(ENV_NAME)
    if profile and profile["mode"] == "hosted":
        try:
            load_credential(config_root, os.environ)
        except PmtError as error:
            raise EasyError(error.code, "PMT credential is unavailable; check the protected credential store.") from error
    try:
        result = probe_storage(str(config_root))
    finally:
        if original is None:
            os.environ.pop(ENV_NAME, None)
        elif profile and profile["mode"] == "hosted":
            os.environ[ENV_NAME] = original
    print(f"mode: {result['mode']} | configured: {str(result['configured']).lower()}")
    if result.get("host_preflight"):
        preflight = result["host_preflight"]
        print(f"Host compatible | core {preflight.get('core_version')} | db {preflight.get('db_schema')} | graph {preflight.get('graph_schema')}")
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except EasyError as error:
        print(f"실패({error.code}): {error.message}", file=sys.stderr)
        return error.exit_code
    except PmtError as error:
        print(f"실패({error.code}): {error.message}", file=sys.stderr)
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
