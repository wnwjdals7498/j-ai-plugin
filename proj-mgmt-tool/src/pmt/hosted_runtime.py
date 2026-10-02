"""Client-side controller adapters for authenticated Host state and local effects.

The Host owns execution/control state. This module never opens a local PMT DB;
local files and process handles are resolved only through injected adapters.
"""
from __future__ import annotations

import uuid
import copy
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
import time
from collections.abc import Mapping

from .errors import PmtError
from .util import canonical_json, fingerprint, utc_now
from .efficiency.source import pin_source, verify_source_pin
from .workspace import ClientWorkspaceResolver, canonical_workspace
_EVENT_ALLOWED = {"control.state_observed", "control.reconcile_required", "control.review_needed",
                  "control.action_requested", "control.notice", "control.retry_requested",
                  "control.observation_started", "control.ai_notified", "control.retry_decided"}


def _operation(req, name, payload, *, suffix, request_id=None):
    request_id = request_id or str(uuid.uuid5(uuid.UUID(req["request_id"]), "pmt-hosted-control:" + suffix))
    return {"protocol_version": 1, "operation": name, "request_id": request_id,
            "actor": req["actor"], "session_id": req["session_id"],
            "scope_id": req.get("scope_id"), "source": req.get("source", {}),
            "context_refs": [], "payload": payload}


def _call(port, request, operation):
    if port is None or not callable(getattr(port, "execute", None)):
        raise PmtError("controller_port_unavailable", "Hosted control state port is unavailable", 3)
    envelope, code = port.execute(request)
    if code or not isinstance(envelope, dict) or not envelope.get("ok"):
        error = envelope.get("error") if isinstance(envelope, dict) else None
        if isinstance(error, dict):
            raise PmtError(error.get("code", "control_store_failed"),
                f"{operation} operation failed", code or 3, bool(error.get("retryable")), error.get("details"))
        raise PmtError("control_store_failed", f"{operation} operation failed", code or 3)
    return envelope.get("result") or {}


def _write_spool_json(spool_root, path, value):
    """Bounded, hash-CAS JSON write for this adapter's private local spool only."""
    from .resources import _reject_links, _hash
    root = Path(os.path.abspath(spool_root)).resolve(strict=True)
    target = Path(os.path.abspath(path))
    try:
        target.parent.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise PmtError("runner_path_invalid", "Hosted spool write escaped its configured root", 3) from exc
    _reject_links(target.parent)
    raw = canonical_json(value).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    stage = target.with_name("." + target.name + "." + digest + ".stage")
    if stage.exists():
        if _hash(stage) != (digest, len(raw)):
            raise PmtError("runner_stage_conflict", "A prior local spool stage has different bytes", 4)
    else:
        with stage.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    original = _hash(target) if target.exists() else None
    delays = (0.05, 0.15, 0.30)
    for attempt in range(len(delays) + 1):
        _reject_links(target.parent)
        current = _hash(target) if target.exists() else None
        if current != original:
            if current == (digest, len(raw)):
                try:
                    stage.unlink(missing_ok=True)
                except OSError:
                    pass
                return digest
            raise PmtError("runner_journal_conflict", "Local spool JSON changed during publication", 3)
        try:
            if original is None:
                os.link(stage, target)
            else:
                os.replace(stage, target)
            break
        except FileExistsError:
            current = _hash(target) if target.is_file() else None
            if current == (digest, len(raw)):
                break
            raise PmtError("runner_journal_conflict", "A different local spool value won the create race", 3)
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {32, 33}:
                raise PmtError("runner_io_error", "Local spool publication failed safely", 4, True,
                    {"operation": "spool_json_publish", "exception_type": type(exc).__name__,
                     "winerror": getattr(exc, "winerror", None)}) from exc
            if attempt >= len(delays):
                raise PmtError("runner_io_unknown", "Local spool journal remains locked after bounded retry", 4, True,
                    {"operation": "spool_json_publish", "attempts": len(delays) + 1,
                     "sha256": digest, "stage_preserved": stage.exists()}) from exc
            time.sleep(delays[attempt])
    if not target.is_file() or _hash(target) != (digest, len(raw)):
        raise PmtError("runner_hash_mismatch", "Local spool JSON failed post-publication verification", 4)
    if original is None:
        try:
            stage.unlink(missing_ok=True)
        except OSError:
            pass
    return digest


def _control_body_for_host(req, body):
    """Keep a native tool payload only in its private original-response cache."""
    safe = copy.deepcopy(body)
    safe.pop("pending_action_unavailable", None)
    action = safe.get("action")
    if isinstance(action, dict) and any(key in action for key in ("instruction", "group_prompt", "members")):
        if not isinstance(req.get("request_id"), str):
            raise PmtError("control_response_invalid", "Pending action needs its original request identity", 3)
        safe.setdefault("action_response_request_id", req["request_id"])
        safe["action"] = {key: action[key] for key in
            ("kind", "run_id", "expected_run_revision", "action_nonce", "context_ref",
             "prompt_sha256", "capability_ref", "directive_ref", "agent", "provider", "model",
             "batch_ref", "batch_report_schema", "group_context_refs", "group_prompt_sha256",
             "source_hash", "scope_union_sha256", "physical_slots", "group_prompt_accounting")
            if key in action}
        if "members" in action:
            safe["action"]["member_refs"] = [{key: member[key] for key in
                ("step_id", "run_id", "role", "directive_version", "directive_ref",
                 "directive_sha256", "context_ref", "criteria") if key in member}
                for member in action["members"] if isinstance(member, dict)]
    return safe


class HostedControlStateRepository:
    """ControlStateRepository implementation backed only by Host RPCs."""

    def __init__(self, state_port):
        if not callable(getattr(state_port, "execute", None)) or not callable(
                getattr(state_port, "get_request_result", None)):
            raise PmtError("controller_port_unavailable", "Hosted control needs the full StorePort", 3)
        self.state_port = state_port

    def get(self, req, run_id, *, conn=None):
        if conn is not None:
            raise PmtError("hosted_sql_forbidden", "Hosted control state cannot read a local SQL connection", 3)
        result = _call(self.state_port, _operation(req, "read_execution_control",
            {"run_id": run_id, "include_pending_action": True},
            suffix="read:" + run_id), "read_execution_control")
        if result is None or result == {}:
            return None
        required = {
                "kind", "id", "scope_id", "source_hash", "revision", "state", "body",
                "owner_actor", "owner_session"}
        if not isinstance(result, dict) or not required <= set(result):
            raise PmtError("control_response_invalid", "Host returned an invalid control state", 3)
        if (result["kind"] != "execution_control" or result["id"] != run_id
                or result["scope_id"] != req.get("scope_id")
                or result["owner_actor"] != req.get("actor")
                or result["owner_session"] != req.get("session_id")
                or type(result["revision"]) is not int or result["revision"] < 1
                or not isinstance(result["body"], dict)):
            raise PmtError("execution_owner_mismatch", "Host control state does not match the current owner", 3)
        body = result["body"]
        pending_action = result.get("pending_action")
        if (isinstance(body, dict) and body.get("stage") in {"main_action_pending", "cancel_action_pending"}
                and isinstance(pending_action, dict)):
            body = dict(body) | {"action": pending_action}
        elif isinstance(body, dict) and body.get("action_response_request_id"):
            body = dict(body) | {"pending_action_unavailable": True}
        return {"kind": result["kind"], "id": result["id"], "scope_id": result["scope_id"],
                "source_hash": result["source_hash"], "revision": result["revision"],
                "state": result["state"], "body": body}

    def compare_and_set(self, req, run_id, scope_id, source_hash, body, *,
                        event_name=None, expected_revision=None, request_suffix="state"):
        if not isinstance(body, dict):
            raise PmtError("execution_control_corrupt", "Control state must be an object", 4)
        old = self.get(req, run_id)
        current_revision = old["revision"] if old else 0
        expected = current_revision if expected_revision is None else expected_revision
        if type(expected) is not int or expected < 0:
            raise PmtError("execution_control_revision_invalid", "Control CAS revision must be nonnegative", 2)
        mapped_event = event_name
        if mapped_event is not None and mapped_event not in _EVENT_ALLOWED:
            raise PmtError("control_event_invalid", "Hosted control event is not supported", 2)
        safe_body = _control_body_for_host(req, body)
        payload = {"run_id": run_id, "expected_control_revision": expected,
                   "source_hash": source_hash, "body": safe_body}
        if mapped_event:
            payload["event_name"] = mapped_event
            payload["event_id"] = str(uuid.uuid5(uuid.UUID(req["request_id"]),
                                                  "pmt-control-event:" + request_suffix))
        result = _call(self.state_port, _operation(req, "write_execution_control", payload,
            suffix="cas:" + request_suffix), "write_execution_control")
        if (not isinstance(result, dict) or set(result) != {"control_ref", "revision"}
                or type(result.get("revision")) is not int
                or not isinstance(result.get("control_ref"), dict)
                or result["control_ref"].get("kind") != "execution_control"
                or result["control_ref"].get("id") != run_id
                or result["control_ref"].get("scope_id") != scope_id
                or result["control_ref"].get("revision") != result["revision"]):
            raise PmtError("control_response_invalid", "Host control CAS receipt is invalid", 3)
        return result

    def complete_response(self, req, result, *, run_id, scope_id, stage, operation, body):
        if operation not in {"advance_execution_control", "acknowledge_execution_action"}:
            raise PmtError("control_response_invalid", "Original control operation is unsupported", 2)
        result = dict(result)
        result.setdefault("run_id", run_id)
        state = self.get(req, run_id)
        safe_body = _control_body_for_host(req, body)
        if (not state or state["scope_id"] != scope_id or state["source_hash"] != safe_body.get("source_hash")
                or state["body"].get("stage") != stage):
            raise PmtError("execution_control_conflict", "Host control state changed before response caching", 3)
        expected_ref = {"kind": "execution_control", "id": run_id,
                        "scope_id": scope_id, "revision": state["revision"]}
        if result.get("control_ref") != expected_ref:
            raise PmtError("control_response_conflict", "Controller response does not identify the current CAS revision", 3,
                details={"expected_revision": state["revision"],
                         "response_revision": (result.get("control_ref") or {}).get("revision")})
        payload = {"run_id": run_id, "expected_control_revision": state["revision"],
                   "source_hash": state["source_hash"], "body": safe_body,
                   "original_request": req,
                   "original_response": {"result": result, "exit_code": 0}}
        try:
            stored = _call(self.state_port, _operation(req, "write_execution_control", payload,
                suffix="response:" + req["request_id"]), "write_execution_control")
        except PmtError as error:
            if error.code == "control_response_conflict":
                raise PmtError(error.code, "Host rejected original control response binding", error.exit_code,
                    error.retryable, {"state_revision": state["revision"],
                        "state_scope_id": state["scope_id"],
                        "result_control_ref": result.get("control_ref"),
                        "result_run_id": result.get("run_id"),
                        "body_sha256": fingerprint(safe_body)}) from error
            raise
        if (not isinstance(stored, dict) or set(stored) != {
                "control_ref", "revision", "original_response", "original_exit_code"}
                or stored["revision"] != state["revision"] or stored["original_exit_code"] != 0
                or not isinstance(stored["original_response"], dict)):
            raise PmtError("control_response_invalid", "Host original response receipt is invalid", 3)
        return stored["original_response"], stored["original_exit_code"]


class HostedLocalRuntime:
    """Run an approved local CLI/native action using Host-owned execution state.

    The class accepts a StorePort for all shared state and a local workspace
    mapping provider. It never opens a PMT Database or forwards process details
    to the Host.
    """

    def __init__(self, state_port, workspace_mapping_provider, spool_root, *,
                 process_launcher=None, clock=None, diagnostics=None, pending_outbox=None):
        if (not callable(getattr(state_port, "execute", None))
                or not callable(getattr(state_port, "get_request_result", None))):
            raise PmtError("controller_port_unavailable", "Hosted runtime requires the Host StorePort", 3)
        if not callable(workspace_mapping_provider):
            raise PmtError("workspace_mapping_invalid", "A local workspace mapping provider is required", 3)
        root = Path(spool_root)
        if not root.is_absolute():
            raise PmtError("runner_path_invalid", "Hosted runner spool root must be absolute", 3)
        self.state_port = state_port
        self.workspace_mapping_provider = workspace_mapping_provider
        self.spool_root = root
        self.process_launcher = process_launcher
        self.clock = clock
        self.diagnostics = diagnostics
        self._profile_identity = {"device_id": getattr(state_port, "device_id", None),
            "environment_id": getattr(state_port, "environment_id", None),
            "namespace_id": getattr(state_port, "namespace_id", None)}
        self._pending_outbox_source = pending_outbox
        self._pending_outbox_instance = None

    def _pending_outbox(self):
        source = self._pending_outbox_source
        if source is None:
            return None
        if self._pending_outbox_instance is not None:
            return self._pending_outbox_instance
        if callable(getattr(source, "stage_resource", None)):
            self._pending_outbox_instance = source
        elif callable(source):
            self._pending_outbox_instance = source()
        else:
            raise PmtError("pending_outbox_invalid", "Pending outbox provider is invalid", 3)
        required = ("stage_resource", "publish_staged_resource", "enqueue_result", "reconcile")
        if not all(callable(getattr(self._pending_outbox_instance, name, None)) for name in required):
            raise PmtError("pending_outbox_invalid", "Pending outbox does not implement the result protocol", 3)
        return self._pending_outbox_instance

    def _pending_current_facts(self, request, run_id, context_ref, immutable=None):
        """Read actual Host owner, source, run, and authorization facts for F14."""
        current = dict(request) | {"operation": "dispatch_execution",
            "payload": dict(request.get("payload", {})) | {"run_id": run_id, "context_ref": context_ref}}
        run, _snapshot, context, _f5, pin, _resolved = self._prepare(current)
        return self._pending_facts_from_run(request, run, pin)

    def _pending_facts_from_run(self, request, run, pin):
        compatibility = self.state_port.check_compatibility()
        run_id = run["id"]
        identity = {"namespace_id": self.state_port.namespace_id, "actor": request["actor"],
            "device_id": self.state_port.device_id, "environment_id": self.state_port.environment_id,
            "session_id": request["session_id"]}
        auth = {"schema": "pmt-host-auth-facts-v1", "ref": "host-auth:" + identity["device_id"] + ":" + identity["session_id"],
            **identity, "scope_id": request["scope_id"], "scopes": compatibility["scopes"],
            "permissions": compatibility["permissions"]}
        auth["sha256"] = fingerprint(auth)
        owner = {"actor": identity["actor"], "device_id": identity["device_id"],
            "session_id": identity["session_id"]}
        run_ref = {"schema": "pmt-host-run-read-v1", "ref": "host-run:" + run_id + ":" + str(run["revision"]),
            "run_id": run_id, "scope_id": request["scope_id"], "revision": run["revision"],
            "state": run["state"], "owner": owner, "workspace": run["workspace"],
            "actual_route": run.get("route") if isinstance(run.get("route"), dict) else {}}
        run_ref["sha256"] = fingerprint(run_ref)
        return {"schema": "pmt-pending-current-facts-v1", **identity,
            "scope_id": request["scope_id"], "source_fingerprint": pin.source_hash,
            "source_ref": {"schema": "pmt-source-pin-ref-v1",
                "ref": "host-source:" + pin.repository_id + ":" + pin.source_hash,
                "pin": pin.to_dict()}, "authorization_ref": auth,
            "run_ref": run_ref, "run_id": run_id, "run_revision": run["revision"],
            "run_state": run["state"], "run_owner": owner}

    def capabilities(self, run):
        route = run.get("route") if isinstance(run, dict) else None
        if not isinstance(route, dict):
            return {"state": "unknown", "reason": "runner_route_unavailable"}
        mode, agent = route.get("mode"), route.get("agent")
        if mode in {"native", "subagent"}:
            if route.get("actual_support") != "verified_supported":
                return {"state": "unsupported", "reason": "native_capability_unverified"}
            return {"state": "supported", "kind": "main_native_call",
                    "capability_ref": route.get("capability_ref")}
        if (mode == "cli" and agent in {"codex", "claude"}
                and route.get("auth_state") == "authenticated"):
            return {"state": "supported", "kind": "local_cli", "agent": agent,
                    "capability_ref": route.get("capability_ref")}
        return {"state": "unsupported", "reason": "local_cli_capability_unverified"}

    def _run(self, req, run_id):
        from .efficiency.control import _snapshot
        result = _call(self.state_port, _operation(req, "read_execution", {"run_id": run_id},
            suffix="runtime-run:" + run_id), "read_execution")
        run = result.get("run") if isinstance(result, dict) else None
        snapshot = _snapshot(run, run_id, req.get("session_id"))
        if run.get("scope_id") not in {None, req.get("scope_id")}:
            raise PmtError("execution_scope_mismatch", "Host run does not match the selected Project", 3)
        return run, snapshot

    def _f5(self, req, run, context_ref):
        from .efficiency.control import ExecutionController, _snapshot
        run_id = run["id"]
        payload = {"context_ref": context_ref, "run_id": run_id,
                   "project_id": req.get("scope_id"), "canonical_workspace": run.get("workspace"),
                   "expected_run_revision": run.get("revision")}
        raw = _call(self.state_port, _operation(req, "read_task_context", payload,
            suffix="runtime-context:" + context_ref["id"]), "read_task_context")
        snapshot = _snapshot(run, run_id, req.get("session_id"))
        checked = ExecutionController(self.state_port, self, hosted_context_binding=True).read_context(
            req, snapshot, context_ref)
        if not checked.get("valid"):
            reason = checked.get("reason", "context_unverified")
            raise PmtError("context_" + reason, "Current F5 context is incomplete or unavailable", 3)
        source = raw.get("source") if isinstance(raw, dict) else None
        if (not isinstance(source, dict) or source.get("source_hash") != context_ref.get("source_hash")
                or raw.get("current_authority", {}).get("run_id") != run_id
                or raw.get("current_authority", {}).get("owner_checked") is not True):
            raise PmtError("context_source_mismatch", "Current F5 source/owner binding is invalid", 3)
        return raw, checked

    @staticmethod
    def _branch_key(pin):
        if pin.source_kind == "git":
            return pin.selected_ref if pin.selected_ref is not None else "detached:" + pin.reviewed_commit
        if pin.source_kind == "non_git":
            return "non-git"
        raise PmtError("source_kind_unknown", "Unknown SourcePin cannot authorize local execution", 3)

    def _workspace_authority(self, req, run, pin, mapping):
        branch_key = self._branch_key(pin)
        if mapping.get("branch") != pin.selected_ref:
            raise PmtError("workspace_mapping_invalid", "Local mapping branch does not match the SourcePin", 3)
        if mapping.get("branch_key_sha256") is not None:
            branch_digest = hashlib.sha256(branch_key.encode("utf-8")).hexdigest()
            if mapping["branch_key_sha256"] != branch_digest:
                raise PmtError("workspace_mapping_invalid", "Local mapping branch fingerprint is inconsistent", 3)
        workspace_uri = run.get("workspace")
        if canonical_workspace(pin.repository_id, branch_key) != workspace_uri:
            raise PmtError("workspace_mapping_invalid", "Host run URI does not match the pinned branch", 3)
        project_id = req.get("scope_id")
        if pin.project_id != project_id:
            raise PmtError("workspace_scope_denied", "SourcePin Project differs from the current Host run", 3)
        relative = mapping.get("relative_graph_path")
        branch = pin.selected_ref

        def fetch_current():
            payload = {"mode": "execute", "project_id": project_id,
                "repository_id": pin.repository_id, "canonical_workspace": workspace_uri,
                "relative_graph_path": relative, "branch": branch, "branch_key": branch_key,
                "run_id": run["id"], "expected_run_revision": run["revision"],
                "expected_source": pin.to_dict()}
            result = _call(self.state_port, _operation(req, "authorize_workspace", payload,
                suffix="workspace-authorize:" + run["id"] + ":" + str(run["revision"])),
                "authorize_workspace")
            locks = result.get("scope_locks") if isinstance(result, dict) else None
            actual_scopes = [{"kind": item.get("kind"), "resource": item.get("resource")}
                for item in locks if isinstance(item, dict)] if isinstance(locks, list) else None
            owner = result.get("owner") if isinstance(result, dict) else None
            grant = {"repository_id": result.get("repository_id"),
                "project_id": result.get("project_id"), "canonical_workspace": result.get("canonical_workspace"),
                "branch": result.get("branch"), "relative_graph_path": result.get("relative_graph_path"),
                "run_id": result.get("run_id"), "run_revision": result.get("run_revision"),
                "owner": owner, "scope_locks": actual_scopes, "source_pin": result.get("source_pin")}
            if (result.get("status") != "authorized" or grant["repository_id"] != pin.repository_id
                    or grant["project_id"] != project_id or grant["canonical_workspace"] != workspace_uri
                    or grant["branch"] != branch or grant["relative_graph_path"] != relative
                    or grant["run_id"] != run["id"] or grant["run_revision"] != run["revision"]
                    or not isinstance(owner, dict) or owner.get("actor") != req.get("actor")
                    or owner.get("session_id") != req.get("session_id")
                    or owner.get("device_id") != getattr(self.state_port, "device_id", None)
                    or owner.get("environment_id") != getattr(self.state_port, "environment_id", None)
                    or actual_scopes is None
                    or result.get("context_hash") != fingerprint(grant)):
                raise PmtError("workspace_authority_stale", "Host workspace authorization changed or is incomplete", 3)
            verify_source_pin(pin, result.get("source_pin"))
            return result, actual_scopes

        _, scopes = fetch_current()

        def authorize(current_request):
            result, actual_scopes = fetch_current()
            if (canonical_json(actual_scopes) != canonical_json(current_request.get("scopes"))
                    or current_request.get("repository_id") != pin.repository_id
                    or current_request.get("project_id") != project_id
                    or current_request.get("branch") != branch
                    or current_request.get("canonical_workspace") != workspace_uri
                    or current_request.get("run_id") != run["id"]
                    or current_request.get("run_revision") != run["revision"]
                    or current_request.get("expected_source_hash") != pin.source_hash
                    or current_request.get("owner") != {"actor": req["actor"],
                                                        "session_id": req["session_id"]}):
                raise PmtError("workspace_authority_stale", "Current Host run/scope/source differs from local mapping", 3)
            return {"status": "authorized", "repository_id": pin.repository_id,
                "project_id": project_id, "branch": branch, "canonical_workspace": workspace_uri,
                "scopes": actual_scopes, "run_id": run["id"], "run_revision": run["revision"],
                "owner": {"actor": req["actor"], "session_id": req["session_id"]},
                "source_pin": result["source_pin"], "context_hash": current_request["context_hash"]}

        resolved_mapping = {"repository_id": pin.repository_id, "project_id": project_id,
            "branch": branch, "local_workspace": mapping.get("local_root", mapping.get("local_workspace")),
            "relative_graph_path": relative, "canonical_workspace": workspace_uri,
            "remote": mapping.get("remote")}
        runtime_context = {"expected_source_pin": pin.to_dict(), "repository_id": pin.repository_id,
            "project_id": project_id, "branch": branch, "canonical_workspace": workspace_uri,
            "scopes": scopes, "run_id": run["id"], "current_owner": {"actor": req["actor"],
            "session_id": req["session_id"]}, "revision": run["revision"]}
        resolver = ClientWorkspaceResolver(authorize)
        return ClientWorkspaceResolver(authorize).resolve(resolved_mapping, runtime_context)

    def _prepare(self, req):
        from .efficiency.control import ExecutionController, _snapshot
        payload = req.get("payload", {})
        run_id, context_ref = payload.get("run_id"), payload.get("context_ref")
        if not isinstance(run_id, str) or not isinstance(context_ref, dict):
            raise PmtError("runtime_input_invalid", "Hosted runtime needs run_id and the current F5 context ref", 2)
        result = _call(self.state_port, _operation(req, "read_execution", {"run_id": run_id},
            suffix="runtime-run:" + run_id), "read_execution")
        run = result.get("run")
        snapshot = _snapshot(run, run_id, req.get("session_id"))
        controller = ExecutionController(self.state_port, self, hosted_context_binding=True)
        context = controller.read_context(req, snapshot, context_ref)
        if not context.get("valid"):
            raise PmtError("context_" + context.get("reason", "unverified"),
                           "Current Host F5 context is incomplete or unavailable", 3)
        f5_payload = {"context_ref": context_ref, "run_id": run_id,
            "project_id": req.get("scope_id"), "canonical_workspace": run.get("workspace"),
            "expected_run_revision": run.get("revision")}
        f5 = _call(self.state_port, _operation(req, "read_task_context", f5_payload,
            suffix="runtime-f5:" + context_ref["id"]), "read_task_context")
        source = f5.get("source") if isinstance(f5, dict) else None
        if not isinstance(source, dict) or source.get("source_hash") != context_ref.get("source_hash"):
            raise PmtError("context_source_mismatch", "Current F5 SourcePin does not match the supplied reference", 3)
        pin = pin_source(source)
        mapping = self.workspace_mapping_provider(run, pin.to_dict())
        if not isinstance(mapping, Mapping):
            raise PmtError("workspace_mapping_invalid", "No local workspace mapping matches this run", 3)
        resolved = self._workspace_authority(req, run, pin, mapping)
        return run, snapshot, context, f5, pin, resolved

    def _spool(self, run_id):
        from .resources import _reject_links
        if not isinstance(run_id, str) or str(uuid.UUID(run_id)) != run_id:
            raise PmtError("runner_path_invalid", "Hosted local spool requires a canonical run UUID", 3)
        root = Path(os.path.abspath(self.spool_root))
        root.mkdir(parents=True, exist_ok=True)
        _reject_links(root)
        root = root.resolve(strict=True)
        spool = root / run_id
        spool.mkdir(parents=True, exist_ok=True)
        _reject_links(spool)
        try:
            spool.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise PmtError("runner_path_invalid", "Hosted local spool escaped its configured root", 3) from exc
        return spool

    @staticmethod
    def _json_file(path, *, max_bytes=1024 * 1024):
        from .resources import _reject_links
        path = Path(path)
        _reject_links(path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PmtError("runner_io_error", "Local runner receipt could not be read", 4, True) from exc
        if len(raw) > max_bytes:
            raise PmtError("runner_receipt_too_large", "Local runner receipt exceeds its bound", 3)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PmtError("runner_receipt_invalid", "Local runner receipt is invalid JSON", 4) from exc
        if not isinstance(value, dict):
            raise PmtError("runner_receipt_invalid", "Local runner receipt must be an object", 4)
        return value

    def observe(self, request):
        from .runners.service import _read_receipt, _read_state
        run, snapshot, context, f5, pin, resolved = self._prepare(request)
        route = snapshot.route
        if route.get("mode") in {"native", "subagent"}:
            control = HostedControlStateRepository(self.state_port).get(request, run["id"])
            pending = control and control["body"].get("stage") in {"main_action_pending", "cancel_action_pending"}
            if pending:
                return {"run_id": run["id"], "status": "awaiting_main_action",
                        "run_state": run["state"], "runner_kind": "native",
                        "handle_ref": (control["body"].get("handle_ref") or {}).get("id"),
                        "receipt_ref": None, "stop_confirmed": False}
            if run["state"] in {"running", "review_pending", "cancel_requested", "reconciling"}:
                return {"run_id": run["id"], "status": "running" if run["state"] == "running" else "unknown",
                        "run_state": run["state"], "runner_kind": "native",
                        "receipt_ref": None, "stop_confirmed": False,
                        "reason": "native_result_pending" if run["state"] == "running" else "native_state_reconciliation_required"}
            return {"run_id": run["id"], "status": "not_dispatched", "run_state": run["state"],
                    "runner_kind": "native", "receipt_ref": None, "stop_confirmed": run["state"] == "starting"}

        spool = self._spool(run["id"])
        journal = self._json_file(spool / "dispatch.json")
        if journal is None:
            if run["state"] in {"starting", "queued"}:
                return {"run_id": run["id"], "status": "not_dispatched", "run_state": run["state"],
                        "runner_kind": route.get("agent"), "receipt_ref": None, "stop_confirmed": False}
            return {"run_id": run["id"], "status": "unknown", "run_state": run["state"],
                    "runner_kind": route.get("agent"), "receipt_ref": None,
                    "reason": "host_run_has_no_local_dispatch_journal", "stop_confirmed": False}
        self._match_journal(journal, run, context, pin)
        if journal.get("state") == "dispatch_intent":
            return {"run_id": run["id"], "status": "unknown", "run_state": run["state"],
                    "runner_kind": journal.get("runner_kind"), "receipt_ref": None,
                    "reason": "local_dispatch_checkpoint_incomplete", "stop_confirmed": False}
        if journal.get("state") == "started" and run["state"] == "starting":
            self._attach_handle(request, run, journal)
            journal = self._json_file(spool / "dispatch.json") or journal
        receipt = _read_receipt(spool)
        if receipt is None:
            state = _read_state(spool)
            if not isinstance(state, dict):
                return {"run_id": run["id"], "status": "unknown", "run_state": run["state"],
                        "runner_kind": journal.get("runner_kind"), "receipt_ref": journal.get("local_receipt_ref"),
                        "reason": "runner_state_unavailable", "stop_confirmed": False}
            if state.get("state") in {"starting", "running"}:
                return {"run_id": run["id"], "status": "cancel_requested" if run["state"] == "cancel_requested" else "running",
                        "run_state": run["state"], "runner_kind": journal.get("runner_kind"),
                        "receipt_ref": journal.get("local_receipt_ref"),
                        "last_observed_at": state.get("heartbeat_at"), "stop_confirmed": False}
            return {"run_id": run["id"], "status": "unknown", "run_state": run["state"],
                    "runner_kind": journal.get("runner_kind"), "receipt_ref": journal.get("local_receipt_ref"),
                    "reason": "runner_receipt_missing", "stop_confirmed": False}
        if receipt.get("run_id") != run["id"] or receipt.get("state") not in {"completed", "failed", "canceled", "unknown"}:
            return {"run_id": run["id"], "status": "unknown", "run_state": run["state"],
                    "runner_kind": journal.get("runner_kind"), "receipt_ref": journal.get("local_receipt_ref"),
                    "reason": "runner_receipt_binding_invalid", "stop_confirmed": False}
        from .resources import _reject_links
        receipt_path = spool / "receipt.json"
        _reject_links(receipt_path)
        receipt_hash = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        safe_receipt = {key: receipt.get(key) for key in
            ("run_id", "runner_kind", "state", "exit_code", "started_at", "completed_at", "error_code")}
        local_ref = "local-runner-receipt:" + receipt_hash
        journal["local_receipt_ref"] = local_ref
        _write_spool_json(self.spool_root, spool / "dispatch.json", journal)
        terminal = receipt["state"] in {"completed", "failed", "canceled"}
        return {"run_id": run["id"], "status": "terminal" if terminal else "unknown",
                "run_state": run["state"], "runner_kind": journal.get("runner_kind"),
                "receipt_ref": local_ref, "receipt_sha256": receipt_hash,
                "exit_code": receipt.get("exit_code"), "stop_confirmed": terminal,
                "reason": None if terminal else "runner_receipt_outcome_unknown"}

    @staticmethod
    def _match_journal(journal, run, context, pin):
        if (journal.get("run_id") != run["id"]
                or journal.get("context_ref") != context.get("context_ref")
                or journal.get("source_hash") != pin.source_hash
                or not isinstance(journal.get("dispatch_request_id"), str)):
            raise PmtError("runner_journal_conflict", "Local runner journal does not match current Host refs", 3)

    def _attach_handle(self, request, run, journal):
        run_id = run["id"]
        handle_id = journal.get("handle_id")
        if not isinstance(handle_id, str):
            raise PmtError("runner_handle_unavailable", "Local dispatch has no opaque handle reference", 4)
        handle = {"id": handle_id, "runner_kind": journal.get("runner_kind"),
                  "provider_ref": journal.get("provider_ref"),
                  "context_ref": journal.get("context_ref"), "source_hash": journal.get("source_hash"),
                  "prompt_sha256": journal.get("prompt_sha256")}
        fresh, _snapshot, _context, _f5, _pin, _resolved = self._prepare(request)
        if fresh["state"] == "running":
            journal["state"] = "attached"
            journal["attached_run_revision"] = fresh["revision"]
            _write_spool_json(self.spool_root, self._spool(run_id) / "dispatch.json", journal)
            return {"run_id": run_id, "state": "running", "revision": fresh["revision"],
                    "handle_ref": handle_id, "replayed": True}
        if fresh["state"] != "starting":
            raise PmtError("execution_state_conflict", "Host run left starting before local handle attach", 3)
        result = _call(self.state_port, _operation(request, "attach_execution_handle", {
            "run_id": run_id, "expected_run_revision": fresh["revision"], "handle": handle},
            suffix="attach:" + journal["dispatch_request_id"]), "attach_execution_handle")
        journal["state"] = "attached"
        journal["attached_run_revision"] = result.get("revision")
        _write_spool_json(self.spool_root, self._spool(run_id) / "dispatch.json", journal)
        return result

    def dispatch(self, request):
        from .service import response
        try:
            run, _snapshot, context, f5, pin, resolved = self._prepare(request)
            route = run.get("route")
            capability = self.capabilities({"route": route})
            if capability.get("state") != "supported":
                raise PmtError(capability.get("reason", "runner_unsupported"),
                               "Current local runner capability is not verified", 3)
            if capability["kind"] == "main_native_call":
                action, _member_count = self._native_action(request, run, context, f5, pin, capability)
                return response(request.get("request_id"), result={"run_id": run["id"],
                    "job_id": run["job_id"], "state": run["state"], "main_action": action,
                    "main_action_pending": True}), 0

            from .runners import service as runners
            if route.get("mode") != "cli" or route.get("adapter_kind") == "sdk" or route.get("agent") == "opencode":
                raise PmtError("runner_unsupported", "Only verified local Codex/Claude CLI routes are supported", 3)
            prompt, prompt_hash = self._private_context(run, context, f5)
            spool = self._spool(run["id"])
            journal_path = spool / "dispatch.json"
            prior = self._json_file(journal_path)
            dispatch_id = request["request_id"]
            if prior:
                self._match_journal(prior, run, context, pin)
                if prior.get("dispatch_request_id") != dispatch_id:
                    raise PmtError("runner_dispatch_conflict", "This Host run already has a different local dispatch identity", 3)
                if prior.get("state") == "dispatch_intent":
                    raise PmtError("runner_dispatch_outcome_unknown", "A prior local launch checkpoint is ambiguous; do not launch again", 4, True)
                if prior.get("state") in {"started", "attached"}:
                    if run["state"] == "starting" and prior.get("state") == "started":
                        self._attach_handle(request, run, prior)
                    return response(request.get("request_id"), result={"run_id": run["id"],
                        "job_id": run["job_id"], "state": "running", "runner_kind": prior.get("runner_kind"),
                        "handle_ref": prior.get("handle_id"), "already_dispatched": True}), 0
                raise PmtError("runner_dispatch_state_invalid", "Local dispatch journal is not launchable", 3)
            if run.get("state") != "starting":
                raise PmtError("execution_state_conflict", "Local CLI can launch only a Host-owned starting run", 3)
            try:
                runner_kind, command = runners._command(route)
                executable = shutil.which(runner_kind) if self.process_launcher is None else "test-fixture-process"
                if not executable:
                    raise PmtError("cli_not_installed", "Selected CLI executable is not installed", 3)
            except PmtError:
                raise
            handle_id = str(uuid.uuid5(uuid.UUID(run["id"]), "pmt-hosted-handle:" + dispatch_id))
            provider_ref = "hosted-local:" + hashlib.sha256((run["id"] + "\0" + dispatch_id).encode()).hexdigest()
            journal = {"schema_version": 1, "run_id": run["id"], "dispatch_request_id": dispatch_id,
                "step_id": run["step_id"], "scope_id": request.get("scope_id"),
                "canonical_workspace": run.get("workspace"),
                "repository_id": pin.repository_id, "relative_graph_path": resolved["relative_graph_path"],
                "source_pin": pin.to_dict(),
                "context_ref": context["context_ref"], "source_hash": pin.source_hash,
                "prompt_sha256": prompt_hash, "runner_kind": runner_kind,
                "route_sha256": fingerprint(run.get("route", {})),
                "owner": {"actor": request["actor"], "session_id": request["session_id"],
                    "device_id": getattr(self.state_port, "device_id", None),
                    "environment_id": getattr(self.state_port, "environment_id", None),
                    "namespace_id": getattr(self.state_port, "namespace_id", None)},
                "handle_id": handle_id, "provider_ref": provider_ref, "state": "dispatch_intent",
                "intent_at": utc_now()}
            _write_spool_json(self.spool_root, journal_path, journal)
            config = {"run_id": run["id"], "runner_kind": runner_kind, "adapter": runner_kind,
                "executable": executable, "command": command, "workspace": str(resolved["workspace"]),
                "spool_root": str(spool), "model": route["model"], "provider": route["provider"],
                "agent": route["agent"], "state_path": str(spool / "state.json"),
                "receipt_path": str(spool / "receipt.json"), "stdout_path": str(spool / "stdout.raw"),
                "stderr_path": str(spool / "stderr.raw"), "output_path": str(spool / "output.json"),
                "control_path": str(spool / "control.json"),
                "criteria_ids": [item if isinstance(item, str) else item.get("id")
                                 for item in run["intent"].get("criteria", [])]}
            _write_spool_json(self.spool_root, spool / "config.json", config)
            launcher = self.process_launcher or runners._launch_helper
            pid = launcher(spool / "config.json", prompt)
            if type(pid) is not int or pid < 1:
                raise PmtError("runner_handoff_unknown", "Local supervisor start was not confirmed", 4, True)
            journal.update(state="started", started_at=utc_now())
            _write_spool_json(self.spool_root, journal_path, journal)
            attached = self._attach_handle(request, run, journal)
            return response(request.get("request_id"), result={"run_id": run["id"],
                "job_id": run["job_id"], "state": attached.get("state", "running"),
                "revision": attached.get("revision"), "handle_ref": handle_id,
                "runner_kind": runner_kind}), 0
        except PmtError as error:
            return response(request.get("request_id"), error=error.as_dict()), error.exit_code
        except Exception as error:
            if self.diagnostics is not None:
                self.diagnostics.emit("hosted_runner.internal_error", request_id=request.get("request_id"),
                    operation=request.get("operation"), scope_id=request.get("scope_id"),
                    outcome="error", reason_code=type(error).__name__)
            issue = PmtError("runner_internal_error", "Hosted local runner failed safely", 5)
            return response(request.get("request_id"), error=issue.as_dict()), issue.exit_code

    def _private_context(self, run, context, f5):
        from .runners.service import _bounded_prompt
        safe_projection = {"included": f5.get("projection", {}).get("included", []),
            "aliases": f5.get("projection", {}).get("aliases", {}),
            "completeness": f5.get("projection", {}).get("completeness"),
            "mandatory_omissions": f5.get("mandatory_omissions", []),
            "unknown": f5.get("unknown", []), "budget": f5.get("budget")}
        prompt = _bounded_prompt(run["intent"], context["context_ref"], context["source_hash"], safe_projection)
        data = prompt.encode("utf-8")
        if len(data) > 1024 * 1024:
            raise PmtError("runner_prompt_too_large", "Verified bounded F5 prompt exceeds 1 MiB", 3)
        return prompt, hashlib.sha256(data).hexdigest()

    def _native_action(self, request, run, context, f5, pin, capability):
        instruction = ("Use only the current bounded F5 context. Invoke exactly one native subagent call. "
            "Do not read or request a larger private directive. Return its real opaque handle and final result "
            "through the supplied operations; do not report a test or evidence that was not observed.")
        return_contract = {"native_handle": "attach_execution_handle after actual invocation",
            "result_operation": "submit_execution_result after actual completion"}
        action = {"kind": "invoke_native_subagent", "job_id": run["job_id"], "run_id": run["id"],
            "step_id": run["step_id"], "agent": run["route"]["agent"],
            "provider": run["route"]["provider"], "model": run["route"]["model"],
            "directive_ref": run["intent"].get("directive_ref"), "instruction": instruction,
            "return_contract": return_contract, "context_ref": context["context_ref"],
            "context_source_hash": pin.source_hash,
            "capability_ref": capability.get("capability_ref")}
        batch_id = run.get("intent", {}).get("batch_ref")
        if not batch_id:
            prompt, prompt_hash = self._private_context(run, context, f5)
            return action | {"prompt_sha256": prompt_hash}, 0

        batch = _call(self.state_port, _operation(request, "read_step_batch", {
            "batch_ref": batch_id, "parent_run_id": run["id"],
            "expected_run_revision": run["revision"]}, suffix="runtime-batch:" + str(batch_id)),
            "read_step_batch")
        source = batch.get("source") if isinstance(batch, dict) else None
        batch_ref = batch.get("batch_ref") if isinstance(batch, dict) else None
        batch_issues = []
        if not isinstance(source, dict) or not isinstance(batch_ref, dict):
            batch_issues.append("metadata_missing")
        else:
            if batch_ref.get("id") != batch_id or batch_ref.get("scope_id") != request.get("scope_id"):
                batch_issues.append("batch_ref_mismatch")
            if (not isinstance(batch_ref.get("source_hash"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", batch_ref["source_hash"])
                    or source.get("source_hash") != pin.source_hash):
                batch_issues.append("source_hash_mismatch")
            if (source.get("project_id") != request.get("scope_id")
                    or source.get("repository_id") != pin.repository_id
                    or source.get("canonical_workspace") != run.get("workspace")):
                batch_issues.append("source_scope_mismatch")
            try:
                if pin_source(source.get("source_pin")).source_hash != pin.source_hash:
                    batch_issues.append("source_pin_mismatch")
            except PmtError:
                batch_issues.append("source_pin_invalid")
            if batch.get("execution_enabled") is not True or batch.get("status") not in {"prepared", "running"}:
                batch_issues.append("batch_not_executable")
            if batch.get("physical_slots") != 1:
                batch_issues.append("physical_slots_invalid")
            if batch.get("unknown"):
                batch_issues.append("batch_unknown")
            if (not isinstance(batch.get("scope_union_sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", batch["scope_union_sha256"])):
                batch_issues.append("scope_union_invalid")
        if batch_issues:
            raise PmtError("batch_host_binding_invalid", "Current Host batch binding is incomplete or stale", 3,
                           details={"reason_codes": batch_issues})
        members, prompt_members, refs = [], [], []
        seen_runs, seen_steps = set(), set()
        for index, item in enumerate(batch.get("members", [])):
            if not isinstance(item, dict):
                raise PmtError("batch_host_binding_invalid", "Host batch member metadata is invalid", 3)
            child_id, step_id = item.get("run_id"), item.get("step_id")
            child_ref = item.get("context_ref")
            if (not isinstance(child_id, str) or not isinstance(step_id, str)
                    or child_id in seen_runs or step_id in seen_steps
                    or item.get("context_current") is not True or item.get("directive_current") is not True
                    or not isinstance(child_ref, dict) or child_ref.get("source_hash") != pin.source_hash
                    or not isinstance(item.get("criteria"), list)):
                raise PmtError("batch_host_binding_invalid", "Host batch member is stale or incomplete", 3)
            seen_runs.add(child_id)
            seen_steps.add(step_id)
            if index == 0:
                child_run, child_context, child_f5 = run, context, f5
            else:
                child_run, _child_snapshot = self._run(request, child_id)
                if (child_run.get("revision") != item.get("run_revision")
                        or child_run.get("step_id") != step_id
                        or child_run.get("workspace") != run.get("workspace")
                        or child_run.get("state") not in {"queued", "starting", "running"}
                        or child_run.get("route") != run.get("route")):
                    raise PmtError("batch_host_binding_invalid", "Host child run differs from its current binding", 3)
                child_f5, child_context = self._f5(request, child_run, child_ref)
            if (child_run.get("id") != child_id or child_run.get("step_id") != step_id
                    or child_ref != child_context.get("context_ref")
                    or child_f5.get("source", {}).get("source_hash") != pin.source_hash):
                raise PmtError("batch_host_binding_invalid", "Host child context differs from its current source", 3)
            directive = child_run.get("intent", {}).get("directive_ref")
            directive_id = directive.get("artifact_id") if isinstance(directive, dict) else directive
            criteria = item["criteria"]
            directive_ref_matches = (item.get("directive_ref") == directive_id
                or canonical_json(item.get("directive_ref")) == canonical_json(directive))
            if (str(item.get("directive_version")) != str(child_run.get("intent", {}).get("directive_version"))
                    or not directive_ref_matches
                    or not all(isinstance(entry, dict) and set(entry) == {"id", "sha256"}
                               and isinstance(entry.get("id"), str)
                               and re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", "")))
                               for entry in criteria)):
                raise PmtError("batch_host_binding_invalid", "Host child directive or criteria binding differs", 3)
            prompt, prompt_hash = self._private_context(child_run, child_context, child_f5)
            refs.append(child_ref)
            members.append({"step_id": step_id, "run_id": child_id, "role": item.get("role"),
                "directive_version": item["directive_version"], "directive_ref": item["directive_ref"],
                "directive_sha256": item["directive_sha256"], "context_ref": child_ref,
                "criteria": criteria})
            prompt_members.append({"step_id": step_id, "run_id": child_id,
                "prompt_sha256": prompt_hash, "prompt": prompt})
        if (len(members) < 2 or members[0]["run_id"] != run["id"]
                or len(members) != len(batch.get("members", []))):
            raise PmtError("batch_host_binding_invalid", "Host batch must bind the parent and every current child", 3)
        group_prompt = canonical_json({"schema": "pmt-hosted-group-context-v1",
            "batch_ref": batch_id, "source_hash": pin.source_hash, "members": prompt_members})
        group_bytes = group_prompt.encode("utf-8")
        if len(group_bytes) > 1024 * 1024:
            raise PmtError("runner_prompt_too_large", "Verified bounded group context exceeds 1 MiB", 3)
        group_hash = hashlib.sha256(group_bytes).hexdigest()
        return action | {"prompt_sha256": group_hash, "batch_ref": batch_id,
            "batch_report_schema": "pmt-batch-report-v1", "members": members,
            "group_context_refs": refs, "group_prompt": group_prompt,
            "group_prompt_sha256": group_hash, "source_hash": pin.source_hash,
            "scope_union_sha256": batch["scope_union_sha256"], "physical_slots": 1,
            "group_prompt_accounting": {"group_prompt_bytes": len(group_bytes),
                "member_count": len(members), "member_context_bytes": sum(
                    len(entry["prompt"].encode("utf-8")) for entry in prompt_members)}}, len(members)

    @staticmethod
    def _terminal_manifest(journal, receipt, output, receipt_bytes, output_bytes):
        if receipt.get("state") not in {"completed", "failed", "canceled"}:
            raise PmtError("runner_stop_unconfirmed", "Local supervisor did not confirm physical termination", 3)
        report = output.get("model_report") if isinstance(output.get("model_report"), dict) else None
        return {"schema": "pmt-hosted-runner-receipt-v1", "run_id": journal["run_id"],
            "step_id": journal["step_id"], "scope_id": journal["scope_id"],
            "canonical_workspace": journal["canonical_workspace"],
            "attached_run_revision": journal.get("attached_run_revision"),
            "owner": journal["owner"], "source_hash": journal["source_hash"],
            "context_ref": journal["context_ref"], "runner_kind": journal["runner_kind"],
            "prompt_sha256": journal["prompt_sha256"],
            "route_sha256": journal.get("route_sha256"),
            "receipt": {"run_id": receipt.get("run_id"), "runner_kind": receipt.get("runner_kind"),
                "state": receipt.get("state"), "exit_code": receipt.get("exit_code"),
                "started_at": receipt.get("started_at"), "completed_at": receipt.get("completed_at"),
                "error_code": receipt.get("error_code"), "stop_confirmed": True},
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
            "output_sha256_basis": "exact local supervisor output.json bytes",
            "output_available": bool(output_bytes), "report_valid": output.get("report_valid") is True,
            "model_report_sha256": fingerprint(report) if report is not None else None,
            "provenance": "local_runner_supervisor",
            "fixture": receipt.get("contract_fixture") is True}

    def collect_terminal(self, request, observation):
        from .service import response
        from .runners.service import _result_for
        try:
            if not isinstance(observation, dict) or observation.get("status") != "terminal" \
                    or observation.get("stop_confirmed") is not True:
                raise PmtError("runner_not_terminal", "Only a current terminal supervisor receipt can be collected", 3)
            run, snapshot, context, f5, pin, resolved = self._prepare(request)
            if snapshot.route.get("mode") in {"native", "subagent"}:
                raise PmtError("native_result_requires_ack", "Native result must arrive through its action acknowledgement", 3)
            spool = self._spool(run["id"])
            journal = self._json_file(spool / "dispatch.json")
            if not isinstance(journal, dict) or journal.get("state") != "attached":
                raise PmtError("runner_handle_unconfirmed", "Host has not confirmed this local runner handle", 3)
            self._match_journal(journal, run, context, pin)
            from .resources import _reject_links
            receipt_path, output_path = spool / "receipt.json", spool / "output.json"
            _reject_links(receipt_path)
            _reject_links(output_path)
            raw_receipt = receipt_path.read_bytes()
            raw_output = output_path.read_bytes() if output_path.exists() else b""
            if len(raw_receipt) > 1024 * 1024 or len(raw_output) > 8 * 1024 * 1024:
                raise PmtError("runner_receipt_too_large", "Local result receipt exceeds its storage bound", 3)
            try:
                receipt = json.loads(raw_receipt.decode("utf-8"))
                output = json.loads(raw_output.decode("utf-8")) if raw_output else {}
            except (ValueError, UnicodeDecodeError) as exc:
                raise PmtError("runner_receipt_invalid", "Local terminal receipt or output is invalid JSON", 4) from exc
            if (not isinstance(receipt, dict) or receipt.get("run_id") != run["id"]
                    or receipt.get("state") not in {"completed", "failed", "canceled"}
                    or not isinstance(output, dict)):
                raise PmtError("runner_receipt_invalid", "Local terminal receipt does not match this run", 4)
            receipt_hash = hashlib.sha256(raw_receipt).hexdigest()
            manifest = self._terminal_manifest(journal, receipt, output, raw_receipt, raw_output)
            output_hash = manifest["output_sha256"]
            safe_receipt = manifest["receipt"]
            report = output.get("model_report") if isinstance(output.get("model_report"), dict) else None
            stop_confirmed = manifest["receipt"]["stop_confirmed"]
            if type(manifest["attached_run_revision"]) is not int or manifest["attached_run_revision"] < 1:
                raise PmtError("runner_handle_unconfirmed", "Opaque handle lacks an attached Host run revision", 3)
            manifest_bytes = canonical_json(manifest).encode("utf-8")
            manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
            _write_spool_json(self.spool_root, spool / "receipt_manifest.json", manifest)
            upload_id = str(uuid.uuid5(uuid.UUID(run["id"]), "pmt-hosted-result:" + receipt_hash))
            outbox = self._pending_outbox()
            facts_reader = lambda immutable: self._pending_current_facts(request, run["id"], context["context_ref"], immutable)
            if outbox is not None:
                outbox.stage_resource(upload_request_id=upload_id, scope_id=request["scope_id"],
                    purpose="result", source_fingerprint=pin.source_hash,
                    output_ref="local-runner-manifest:" + run["id"] + ":" + manifest_hash,
                    output_reader=lambda ref: manifest_bytes if ref == "local-runner-manifest:" + run["id"] + ":" + manifest_hash else None)
                upload = outbox.publish_staged_resource(upload_id, self.state_port, facts_reader)
                receipt_resource = upload.get("artifact_ref") if isinstance(upload, dict) else None
                if upload.get("state") != "published" or not isinstance(receipt_resource, dict):
                    raise PmtError("pending_resource_preserved", "Terminal receipt is safely staged for retry", 4, True,
                        {"run_id": run["id"], "upload_request_id": upload_id,
                         "runtime_receipt_ref": "local-runner-receipt:" + run["id"] + ":" + manifest_hash,
                         "state": upload.get("state"), "reason_code": upload.get("reason_code")})
            else:
                publisher = getattr(self.state_port, "publish_resource", None)
                if not callable(publisher):
                    raise PmtError("runtime_resource_unavailable", "Hosted StorePort cannot publish a terminal receipt", 3)
                receipt_resource = publisher({"request_id": upload_id, "scope_id": request["scope_id"], "purpose": "result"},
                    manifest_bytes, session_id=request["session_id"])["artifact_ref"]
            runner_result = _result_for(run, safe_receipt, report, receipt_resource["id"])
            local_receipt_ref = "local-runner-receipt:" + run["id"] + ":" + manifest_hash
            runner_result.update(runtime_receipt_ref=local_receipt_ref,
                runtime_receipt_sha256=manifest_hash, runtime_supervisor_receipt_sha256=receipt_hash,
                runtime_output_sha256=output_hash,
                runtime_receipt_resource_ref=receipt_resource)
            submit_id = str(uuid.uuid5(uuid.UUID(run["id"]), "pmt-hosted-submit-result:" + manifest_hash))
            submit_request = _operation(request, "submit_execution_result", {
                "run_id": run["id"], "expected_run_revision": manifest["attached_run_revision"],
                "result": runner_result}, suffix="submit-terminal:" + receipt_hash, request_id=submit_id)
            if outbox is not None:
                runtime_ref = "local-runner-receipt:" + run["id"] + ":" + manifest_hash
                outbox.enqueue_result(submit_request, base_run_revision=manifest["attached_run_revision"],
                    source_fingerprint=pin.source_hash, runtime_receipt_ref=runtime_ref,
                    runtime_receipt_sha256=manifest_hash,
                    receipt_reader=lambda ref: {key: self.read_terminal_receipt(
                        request, context["context_ref"], ref)[key]
                        for key in ("manifest_bytes", "receipt_bytes", "output_bytes")},
                    run_id=run["id"])
            submitted = _call(self.state_port, submit_request, "submit_execution_result")
            pending_result = None
            if outbox is not None:
                pending_result = outbox.reconcile(submit_id, self.state_port, facts_reader)
            if receipt["state"] == "canceled":
                fresh, _snapshot = self._run(request, run["id"])
                submitted = _call(self.state_port, _operation(request, "reconcile_execution", {
                    "run_id": run["id"], "expected_run_revision": fresh["revision"],
                    "stopped": True, "not_started": False, "actual_state": "canceled",
                    "evidence_refs": [receipt_resource["id"]]},
                    suffix="reconcile-canceled:" + receipt_hash), "reconcile_execution")
            result = {**submitted,
                "receipt_ref": receipt_resource["id"], "runtime_receipt_ref": local_receipt_ref,
                "stop_confirmed": True}
            if pending_result is not None:
                result["pending_result_state"] = pending_result.state
            return response(request.get("request_id"), result=result), 0
        except PmtError as error:
            return response(request.get("request_id"), error=error.as_dict()), error.exit_code

    def cancel(self, request):
        from .service import response
        try:
            run_id = request.get("payload", {}).get("run_id")
            control = HostedControlStateRepository(self.state_port).get(request, run_id)
            context_ref = control.get("body", {}).get("context_ref") if control else None
            if not isinstance(context_ref, dict):
                raise PmtError("context_ref_required", "Current F5 ref is required before local cancellation", 3)
            request_with_context = dict(request) | {"payload": dict(request.get("payload", {})) | {
                "context_ref": context_ref}}
            run, snapshot, context, f5, pin, resolved = self._prepare(request_with_context)
            if snapshot.route.get("mode") in {"native", "subagent"}:
                handle_ref = (control or {}).get("body", {}).get("handle_ref")
                if not isinstance(handle_ref, dict) or not isinstance(handle_ref.get("id"), str):
                    return response(request.get("request_id"), result={"run_id": run_id,
                        "state": run["state"], "awaiting_native_cancellation": True,
                        "main_action_pending": True}), 0
                return response(request.get("request_id"), result={"run_id": run_id,
                    "state": "cancel_requested", "main_action": {"kind": "cancel_native_subagent",
                    "run_id": run_id, "native_handle": handle_ref}}), 0
            spool = self._spool(run_id)
            journal = self._json_file(spool / "dispatch.json")
            if journal is None:
                return response(request.get("request_id"), result={"run_id": run_id,
                    "state": run["state"], "awaiting_stop_confirmation": False,
                    "not_dispatched": run["state"] == "starting"}), 0
            self._match_journal(journal, run, context, pin)
            receipt = self._json_file(spool / "receipt.json")
            if receipt and receipt.get("state") in {"completed", "failed", "canceled"}:
                return response(request.get("request_id"), result={"run_id": run_id,
                    "state": receipt["state"], "already_stopped": True}), 0
            _write_spool_json(self.spool_root, spool / "control.json", {"cancel_requested": True, "requested_at": utc_now()})
            return response(request.get("request_id"), result={"run_id": run_id,
                "state": "cancel_requested", "awaiting_stop_confirmation": True,
                "scope_locks_retained": True}), 0
        except PmtError as error:
            return response(request.get("request_id"), error=error.as_dict()), error.exit_code

    def read_terminal_receipt(self, request, context_ref, runtime_receipt_ref):
        """Return exact local manifest/receipt/output bytes after live Host reauthorization."""
        if (not isinstance(runtime_receipt_ref, str)
                or not runtime_receipt_ref.startswith("local-runner-receipt:")):
            raise PmtError("runner_receipt_ref_invalid", "Runtime receipt ref is invalid", 2)
        parts = runtime_receipt_ref.split(":")
        if len(parts) != 3:
            raise PmtError("runner_receipt_ref_invalid", "Runtime receipt ref is invalid", 2)
        run_id, expected_manifest_hash = parts[1], parts[2]
        if not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_hash):
            raise PmtError("runner_receipt_ref_invalid", "Runtime receipt hash is invalid", 2)
        authorized = dict(request) | {"payload": dict(request.get("payload", {})) | {
            "run_id": run_id, "context_ref": context_ref}}
        run, snapshot, context, f5, pin, resolved = self._prepare(authorized)
        if (run_id != run["id"] or context["context_ref"] != context_ref
                or run.get("state") not in {"starting", "running", "reconciling", "cancel_requested",
                    "review_pending", "succeeded", "failed", "blocked", "canceled"}):
            raise PmtError("runner_receipt_owner_conflict", "Terminal receipt is not bound to the current Host run", 3)
        from .resources import _reject_links
        root = Path(os.path.abspath(self.spool_root))
        if not root.is_dir():
            raise PmtError("runner_spool_unavailable", "Previously configured local spool is unavailable", 3)
        _reject_links(root)
        root = root.resolve(strict=True)
        spool = root / run_id
        if not spool.is_dir():
            raise PmtError("runner_spool_unavailable", "Previously attached local run spool is unavailable", 3)
        _reject_links(spool)
        try:
            spool.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise PmtError("runner_path_invalid", "Local receipt spool escaped its managed root", 3) from exc
        journal = self._json_file(spool / "dispatch.json")
        if not isinstance(journal, dict) or journal.get("state") != "attached":
            raise PmtError("runner_receipt_owner_conflict", "No verified local dispatch journal matches this run", 3)
        self._match_journal(journal, run, context, pin)
        from .resources import _reject_links
        paths = {"manifest_bytes": spool / "receipt_manifest.json", "receipt_bytes": spool / "receipt.json",
                 "output_bytes": spool / "output.json"}
        data = {}
        for name, path in paths.items():
            _reject_links(path)
            try:
                data[name] = path.read_bytes() if path.exists() else b""
            except OSError as exc:
                raise PmtError("runner_receipt_unavailable", "A local terminal receipt component is unavailable", 3) from exc
        if (not data["manifest_bytes"] or hashlib.sha256(data["manifest_bytes"]).hexdigest() != expected_manifest_hash):
            raise PmtError("runner_receipt_hash_mismatch", "Local receipt manifest bytes do not match the reference", 3)
        try:
            manifest = json.loads(data["manifest_bytes"].decode("utf-8"))
            receipt = json.loads(data["receipt_bytes"].decode("utf-8")) if data["receipt_bytes"] else None
        except (ValueError, UnicodeDecodeError) as exc:
            raise PmtError("runner_receipt_invalid", "Local terminal receipt bytes are invalid JSON", 4) from exc
        expected_owner = {"actor": request["actor"], "session_id": request["session_id"],
            "device_id": getattr(self.state_port, "device_id", None),
            "environment_id": getattr(self.state_port, "environment_id", None),
            "namespace_id": getattr(self.state_port, "namespace_id", None)}
        manifest_receipt = manifest.get("receipt") if isinstance(manifest, dict) else None
        if (not isinstance(manifest, dict) or manifest.get("schema") != "pmt-hosted-runner-receipt-v1"
                or manifest.get("run_id") != run_id or manifest.get("step_id") != run["step_id"]
                or manifest.get("owner") != expected_owner or manifest.get("source_hash") != pin.source_hash
                or manifest.get("context_ref") != context_ref or manifest.get("attached_run_revision") != journal.get("attached_run_revision")
                or not isinstance(receipt, dict) or receipt.get("run_id") != run_id
                or receipt.get("state") not in {"completed", "failed", "canceled"}
                or not isinstance(manifest_receipt, dict)
                or type(manifest_receipt.get("stop_confirmed")) is not bool
                or manifest_receipt["stop_confirmed"] is not True
                or hashlib.sha256(data["receipt_bytes"]).hexdigest() != manifest.get("receipt_sha256")
                or hashlib.sha256(data["output_bytes"]).hexdigest() != manifest.get("output_sha256")):
            raise PmtError("runner_receipt_binding_invalid", "Manifest, local files and current Host refs do not agree", 3)
        return {**data, "manifest": manifest,
            "receipt_resource_ref": (run.get("result") or {}).get("runtime_receipt_resource_ref"),
            "receipt_sha256": manifest["receipt_sha256"],
            "output_sha256": manifest["output_sha256"], "run_id": run_id,
            "source_hash": pin.source_hash, "context_ref": context_ref,
            "owner": expected_owner, "attached_run_revision": manifest["attached_run_revision"]}

    def capture_pending_terminal_receipt(self, request):
        """Stage an already attached terminal local spool while Host is offline."""
        payload = request.get("payload", {}) if isinstance(request, dict) else {}
        if set(payload) != {"run_id", "context_ref", "source_hash", "dispatch_ref"}:
            raise PmtError("pending_capture_invalid", "Offline capture needs run, context, source and dispatch refs", 2)
        outbox = self._pending_outbox()
        if outbox is None:
            raise PmtError("pending_outbox_unavailable", "Local pending storage is not configured", 3)
        if (request.get("scope_id") is None or request.get("actor") is None
                or request.get("session_id") is None
                or request.get("actor") != outbox.identity.get("actor")
                or request.get("session_id") != outbox.identity.get("session_id")
                or self._profile_identity["device_id"] != outbox.identity.get("device_id")
                or self._profile_identity["environment_id"] != outbox.identity.get("environment_id")
                or self._profile_identity["namespace_id"] != outbox.identity.get("namespace_id")):
            raise PmtError("pending_capture_owner_mismatch", "Offline capture profile does not match the request owner", 3)
        owner = {"actor": request["actor"], "session_id": request["session_id"],
            **self._profile_identity}
        captured = self.capture_terminal_receipt_offline(payload["run_id"], payload["context_ref"],
            payload["source_hash"], owner, payload["dispatch_ref"])
        if (payload["context_ref"].get("scope_id") != request["scope_id"]
                or captured["manifest"].get("scope_id") != request["scope_id"]
                or captured["manifest"].get("context_ref") != payload["context_ref"]
                or captured["manifest"].get("source_hash") != payload["source_hash"]):
            raise PmtError("pending_capture_scope_mismatch", "Local terminal receipt differs from the selected scope/source", 3)
        upload_id = str(uuid.uuid5(uuid.UUID(payload["run_id"]),
            "pmt-hosted-result:" + captured["receipt_sha256"]))
        output_ref = "local-runner-manifest:" + payload["run_id"] + ":" + captured["runtime_receipt_sha256"]
        staged = outbox.stage_resource(upload_request_id=upload_id, scope_id=request["scope_id"],
            purpose="result", source_fingerprint=payload["source_hash"], output_ref=output_ref,
            output_reader=lambda ref: captured["manifest_bytes"] if ref == output_ref else None)
        return {"run_id": payload["run_id"], "state": staged.state,
            "upload_request_id": upload_id, "runtime_receipt_ref": captured["runtime_receipt_ref"],
            "runtime_receipt_sha256": captured["runtime_receipt_sha256"],
            "receipt_sha256": captured["receipt_sha256"], "output_sha256": captured["output_sha256"],
            "size": staged.size, "scope_id": staged.scope_id}

    def reconcile_pending_result(self, request):
        """Reauthorize, publish one staged receipt, then reconcile its immutable P2 request."""
        from .service import response
        try:
            payload = request.get("payload", {}) if isinstance(request, dict) else {}
            allowed = {"run_id", "context_ref", "source_hash", "dispatch_ref", "runtime_receipt_ref",
                "pending_request_id"}
            if set(payload) - allowed or not {"run_id", "context_ref", "source_hash"} <= set(payload):
                raise PmtError("pending_reconcile_invalid", "Pending reconcile needs current run, context and source refs", 2)
            outbox = self._pending_outbox()
            if outbox is None:
                raise PmtError("pending_outbox_unavailable", "Local pending storage is not configured", 3)
            if payload.get("pending_request_id"):
                # First ask the authenticated Host request cache whether this exact
                # immutable effect already committed. This path must not require a
                # current checkout or still-held run lock to report an applied result.
                _row, immutable, original_template = outbox._load(payload["pending_request_id"])
                cached_run, _ = self._run(request, immutable["run_id"])
                spool = self._spool(immutable["run_id"])
                journal = self._json_file(spool / "dispatch.json")
                if (not isinstance(journal, dict) or journal.get("run_id") != cached_run["id"]
                        or journal.get("scope_id") != immutable["scope_id"]
                        or journal.get("source_hash") != immutable["source_fingerprint"]
                        or not isinstance(journal.get("source_pin"), dict)
                        or not isinstance(journal.get("repository_id"), str)
                        or not isinstance(journal.get("relative_graph_path"), str)):
                    raise PmtError("pending_journal_binding_invalid", "Pending request has no matching original local dispatch pin", 3)
                source_result = _call(self.state_port, _operation(request, "read_source_metadata", {
                    "project_id": request["scope_id"], "repository_id": journal["repository_id"],
                    "canonical_workspace": cached_run["workspace"],
                    "relative_graph_path": journal["relative_graph_path"]},
                    suffix="pending-cached-source:" + cached_run["id"]), "read_source_metadata")
                current_pin = pin_source(source_result.get("source_pin"))
                source_ref = source_result.get("snapshot_ref")
                if (current_pin.project_id != request["scope_id"]
                        or current_pin.repository_id != journal["repository_id"]
                        or source_result.get("source_hash") != current_pin.source_hash
                        or source_result.get("execution_authorized") is not False
                        or source_result.get("provenance") != "client_snapshot"
                        or source_result.get("host_git_verified") is not False
                        or not isinstance(source_ref, dict)
                        or source_ref.get("kind") != "host_source_current"
                        or source_ref.get("scope_id") != request["scope_id"]
                        or source_ref.get("source_hash") != current_pin.source_hash):
                    raise PmtError("pending_current_source_conflict", "Host source metadata differs from the attached run", 3)
                metadata_facts = self._pending_facts_from_run(request, cached_run, current_pin)
                original_request = outbox._rehydrate_request(original_template, immutable, metadata_facts)
                prior = self.state_port.get_request_result(payload["pending_request_id"],
                    request["actor"], request["session_id"], expected_request=original_request)
                if prior is not None:
                    receipt = outbox.reconcile(payload["pending_request_id"], self.state_port,
                        lambda _immutable: metadata_facts)
                    return response(request.get("request_id"), result={"run_id": cached_run["id"],
                        "state": receipt.state, "request_id": receipt.request_id,
                        "reason_code": receipt.reason_code,
                        "changed_dimensions": list(receipt.changed_dimensions)}), 0
            current = dict(request) | {"operation": "dispatch_execution",
                "payload": dict(payload) | {"context_ref": payload["context_ref"]}}
            run, _snapshot, context, _f5, pin, _resolved = self._prepare(current)
            if (run["id"] != payload["run_id"] or context["context_ref"] != payload["context_ref"]
                    or pin.source_hash != payload["source_hash"]):
                raise PmtError("pending_current_source_conflict", "Pending result no longer matches the current Host source", 3)
            if payload.get("pending_request_id"):
                receipt = outbox.reconcile(payload["pending_request_id"], self.state_port,
                    lambda immutable: self._pending_current_facts(request, run["id"], context["context_ref"], immutable))
                return response(request.get("request_id"), result={"run_id": run["id"],
                    "state": receipt.state, "request_id": receipt.request_id,
                    "reason_code": receipt.reason_code, "changed_dimensions": list(receipt.changed_dimensions)}), 0
            runtime_ref = payload.get("runtime_receipt_ref")
            if not isinstance(runtime_ref, str) and isinstance(payload.get("dispatch_ref"), str):
                owner = {"actor": request["actor"], "session_id": request["session_id"],
                    "device_id": getattr(self.state_port, "device_id", None),
                    "environment_id": getattr(self.state_port, "environment_id", None),
                    "namespace_id": getattr(self.state_port, "namespace_id", None)}
                captured = self.capture_terminal_receipt_offline(run["id"], context["context_ref"],
                    pin.source_hash, owner, payload["dispatch_ref"])
                runtime_ref = captured["runtime_receipt_ref"]
            if not isinstance(runtime_ref, str) or not runtime_ref.startswith("local-runner-receipt:"):
                raise PmtError("pending_receipt_ref_invalid", "A staged local terminal receipt is required", 3)
            receipt_parts = self.read_terminal_receipt(request, context["context_ref"], runtime_ref)
            manifest = receipt_parts["manifest"]
            upload_id = str(uuid.uuid5(uuid.UUID(run["id"]), "pmt-hosted-result:" + manifest["receipt_sha256"]))
            upload = outbox.publish_staged_resource(upload_id, self.state_port,
                lambda immutable: self._pending_current_facts(request, run["id"], context["context_ref"], immutable))
            if upload.get("state") != "published" or not isinstance(upload.get("artifact_ref"), dict):
                return response(request.get("request_id"), result={"run_id": run["id"],
                    "state": "pending_resource_" + str(upload.get("state")),
                    "upload_request_id": upload_id, "runtime_receipt_ref": runtime_ref,
                    "reason_code": upload.get("reason_code")}), 0
            from .runners.service import _result_for
            physical = manifest["receipt"]
            raw_output = receipt_parts["output_bytes"]
            output = json.loads(raw_output.decode("utf-8")) if raw_output else {}
            report = output.get("model_report") if isinstance(output.get("model_report"), dict) else None
            result = _result_for(run, physical, report, upload["artifact_ref"]["id"])
            result.update(runtime_receipt_ref=runtime_ref,
                runtime_receipt_sha256=hashlib.sha256(receipt_parts["manifest_bytes"]).hexdigest(),
                runtime_supervisor_receipt_sha256=manifest["receipt_sha256"],
                runtime_output_sha256=manifest["output_sha256"],
                runtime_receipt_resource_ref=upload["artifact_ref"])
            request_id = str(uuid.uuid5(uuid.UUID(run["id"]),
                "pmt-hosted-submit-result:" + result["runtime_receipt_sha256"]))
            submit = _operation(request, "submit_execution_result", {
                "run_id": run["id"], "expected_run_revision": manifest["attached_run_revision"],
                "result": result}, suffix="submit-terminal:" + result["runtime_supervisor_receipt_sha256"],
                request_id=request_id)
            outbox.enqueue_result(submit, base_run_revision=manifest["attached_run_revision"],
                source_fingerprint=pin.source_hash, runtime_receipt_ref=runtime_ref,
                runtime_receipt_sha256=result["runtime_receipt_sha256"],
                receipt_reader=lambda ref: {key: self.read_terminal_receipt(
                    request, context["context_ref"], ref)[key]
                    for key in ("manifest_bytes", "receipt_bytes", "output_bytes")},
                run_id=run["id"])
            applied = outbox.reconcile(request_id, self.state_port,
                lambda immutable: self._pending_current_facts(request, run["id"], context["context_ref"], immutable))
            return response(request.get("request_id"), result={"run_id": run["id"],
                "state": applied.state, "request_id": request_id,
                "receipt_resource_ref": upload["artifact_ref"],
                "runtime_receipt_ref": runtime_ref, "reason_code": applied.reason_code,
                "changed_dimensions": list(applied.changed_dimensions)}), 0
        except PmtError as error:
            return response(request.get("request_id"), error=error.as_dict()), error.exit_code

    def capture_terminal_receipt_offline(self, run_id, context_ref, source_hash, owner,
                                         runtime_receipt_ref):
        """Preserve a prior local terminal receipt without new Host authority claims.

        This only reads the immutable spool of an already-dispatched handle. It
        cannot launch, alter the workspace, write shared state, or call a model.
        A caller must reconnect and reauthorize before uploading/submitting it.
        """
        if (not isinstance(owner, Mapping) or set(owner) != {
                "actor", "session_id", "device_id", "environment_id", "namespace_id"}
                or not isinstance(context_ref, dict)):
            raise PmtError("runner_offline_capture_invalid", "Offline receipt identity refs are incomplete", 2)
        if (not isinstance(runtime_receipt_ref, str)
                or not runtime_receipt_ref.startswith(("local-runner-receipt:", "local-runner-dispatch:"))):
            raise PmtError("runner_receipt_ref_invalid", "Runtime receipt ref is invalid", 2)
        parts = runtime_receipt_ref.split(":")
        is_manifest_ref = parts[0] == "local-runner-receipt"
        if (len(parts) != 3 or parts[1] != run_id
                or (is_manifest_ref and not re.fullmatch(r"[0-9a-f]{64}", parts[2]))
                or (not is_manifest_ref and not re.fullmatch(r"[0-9a-f-]{36}", parts[2]))):
            raise PmtError("runner_receipt_ref_invalid", "Runtime receipt ref is invalid", 2)
        manifest_expected = parts[2] if is_manifest_ref else None
        from .resources import _reject_links
        root = Path(os.path.abspath(self.spool_root))
        if not root.is_dir():
            raise PmtError("runner_spool_unavailable", "Previously configured local spool is unavailable", 3)
        _reject_links(root)
        root = root.resolve(strict=True)
        spool = root / run_id
        if not spool.is_dir():
            raise PmtError("runner_spool_unavailable", "Previously attached local run spool is unavailable", 3)
        _reject_links(spool)
        try:
            spool.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise PmtError("runner_path_invalid", "Local receipt spool escaped its managed root", 3) from exc
        journal = self._json_file(spool / "dispatch.json")
        if (not isinstance(journal, dict) or journal.get("state") != "attached"
                or journal.get("run_id") != run_id or journal.get("owner") != dict(owner)
                or journal.get("context_ref") != context_ref or journal.get("source_hash") != source_hash
                or not isinstance(journal.get("handle_id"), str)
                or type(journal.get("attached_run_revision")) is not int
                or (not is_manifest_ref and journal.get("dispatch_request_id") != parts[2])):
            raise PmtError("runner_offline_capture_unverified", "No matching previously attached local handle exists", 3)
        filemap = {"manifest_bytes": spool / "receipt_manifest.json",
                   "receipt_bytes": spool / "receipt.json", "output_bytes": spool / "output.json"}
        values = {}
        for name, path in filemap.items():
            _reject_links(path)
            try:
                values[name] = path.read_bytes() if path.exists() else b""
            except OSError as exc:
                raise PmtError("runner_receipt_unavailable", "Local terminal receipt is unavailable", 3, True) from exc
        try:
            receipt = json.loads(values["receipt_bytes"].decode("utf-8")) if values["receipt_bytes"] else None
            output = json.loads(values["output_bytes"].decode("utf-8")) if values["output_bytes"] else {}
            manifest = json.loads(values["manifest_bytes"].decode("utf-8")) if values["manifest_bytes"] else None
        except (ValueError, UnicodeDecodeError) as exc:
            raise PmtError("runner_receipt_invalid", "Offline terminal receipt is invalid JSON", 4) from exc
        if not isinstance(receipt, dict) or not isinstance(output, dict):
            raise PmtError("runner_receipt_unavailable", "Physical terminal receipt and output are required", 3)
        if manifest is None:
            manifest = self._terminal_manifest(journal, receipt, output,
                                               values["receipt_bytes"], values["output_bytes"])
            _write_spool_json(self.spool_root, spool / "receipt_manifest.json", manifest)
            values["manifest_bytes"] = canonical_json(manifest).encode("utf-8")
        actual_manifest_hash = hashlib.sha256(values["manifest_bytes"]).hexdigest()
        if manifest_expected is not None and actual_manifest_hash != manifest_expected:
            raise PmtError("runner_receipt_hash_mismatch", "Offline manifest bytes do not match their opaque ref", 3)
        inner = manifest.get("receipt") if isinstance(manifest, dict) else None
        if (not isinstance(receipt, dict) or receipt.get("run_id") != run_id
                or receipt.get("state") not in {"completed", "failed", "canceled"}
                or not isinstance(manifest, dict) or manifest.get("schema") != "pmt-hosted-runner-receipt-v1"
                or manifest.get("run_id") != run_id or manifest.get("owner") != dict(owner)
                or manifest.get("source_hash") != source_hash or manifest.get("context_ref") != context_ref
                or manifest.get("attached_run_revision") != journal["attached_run_revision"]
                or not isinstance(inner, dict) or type(inner.get("stop_confirmed")) is not bool
                or inner["stop_confirmed"] is not True
                or hashlib.sha256(values["receipt_bytes"]).hexdigest() != manifest.get("receipt_sha256")
                or hashlib.sha256(values["output_bytes"]).hexdigest() != manifest.get("output_sha256")):
            raise PmtError("runner_receipt_binding_invalid", "Offline receipt files do not match the prior dispatch refs", 3)
        actual_ref = "local-runner-receipt:" + run_id + ":" + actual_manifest_hash
        return {**values, "runtime_receipt_ref": actual_ref,
            "runtime_receipt_sha256": actual_manifest_hash,
            "receipt_sha256": manifest["receipt_sha256"], "output_sha256": manifest["output_sha256"],
            "manifest": manifest, "owner": dict(owner), "run_id": run_id,
            "source_hash": source_hash, "context_ref": context_ref,
            "attached_run_revision": manifest["attached_run_revision"]}
