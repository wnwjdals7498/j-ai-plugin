"""Owner/source-checked control metadata; no process or model execution here."""
import json
import hashlib
import re

from ..db import semantic_request_fingerprint
from ..efficiency.storage import Phase3Storage
from ..errors import PmtError
from ..phase2_common import require_workspace_claim
from ..service import response
from ..util import canonical_json, utc_now
from .auth import identifier

OPERATIONS = frozenset({"write_execution_control", "read_execution_control"})
_FORBIDDEN = {"prompt", "instruction", "group_prompt", "members", "directive", "transcript", "conversation", "argv", "env", "pid", "credential",
              "authorization", "claim_token", "api_key", "password", "secret", "spool", "cwd", "command",
              "local_workspace", "local_root"}
_ACTION_FIELDS = {"kind", "run_id", "expected_run_revision", "action_nonce", "context_ref",
    "prompt_sha256", "capability_ref", "directive_ref", "agent", "provider", "model",
    "batch_ref", "batch_report_schema", "group_context_refs", "group_prompt_sha256",
    "source_hash", "scope_union_sha256", "physical_slots", "group_prompt_accounting",
    "members", "member_refs", "instruction", "return_contract", "group_prompt", "handle_ref"}


def _bound_action(action, body):
    """Check an original advance response against the action stub in current CAS state."""
    if not isinstance(action, dict) or set(action) - _ACTION_FIELDS:
        return False
    stub = body.get("action")
    if not isinstance(stub, dict):
        return False
    public = {key: value for key, value in action.items()
              if key not in {"instruction", "return_contract", "group_prompt", "members"}}
    if "members" in action:
        public["member_refs"] = [
            {key: member[key] for key in ("step_id", "run_id", "role", "directive_version",
                "directive_ref", "directive_sha256", "context_ref", "criteria") if key in member}
            for member in action["members"] if isinstance(member, dict)]
        if len(public["member_refs"]) != len(action["members"]):
            return False
    if canonical_json(public) != canonical_json(stub):
        return False
    if action.get("action_nonce") != body.get("action_nonce"):
        return False
    if action.get("run_id") != body.get("run_id") or action.get("context_ref") != body.get("context_ref"):
        return False
    if action.get("kind") == "main-native-call":
        if (body.get("stage") != "main_action_pending"
                or not isinstance(action.get("instruction"), str)
                or action.get("instruction") != (
                    "Use only the current bounded F5 context. Invoke exactly one native subagent call. "
                    "Do not read or request a larger private directive. Return its real opaque handle and final result "
                    "through the supplied operations; do not report a test or evidence that was not observed.")
                or action.get("return_contract") != {
                    "native_handle": "attach_execution_handle after actual invocation",
                    "result_operation": "submit_execution_result after actual completion"}):
            return False
        prompt_sha = action.get("prompt_sha256")
        if (not isinstance(prompt_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", prompt_sha)
                or body.get("prompt_sha256") != prompt_sha):
            return False
        if "group_prompt" in action:
            raw = action.get("group_prompt")
            accounting = action.get("group_prompt_accounting")
            members = action.get("members")
            if (not isinstance(raw, str) or not raw
                    or hashlib.sha256(raw.encode("utf-8")).hexdigest() != action.get("group_prompt_sha256")
                    or prompt_sha != action.get("group_prompt_sha256")
                    or not isinstance(accounting, dict)
                    or accounting.get("group_prompt_bytes") != len(raw.encode("utf-8"))
                    or action.get("source_hash") != body.get("source_hash")
                    or not re.fullmatch(r"[0-9a-f]{64}", str(action.get("scope_union_sha256", "")))
                    or not isinstance(members, list) or len(members) < 2
                    or any(not isinstance(item, dict) for item in members)
                    or action.get("group_context_refs") != [item.get("context_ref") for item in members]):
                return False
            for member in members:
                member_context = member.get("context_ref") if isinstance(member, dict) else None
                if (not isinstance(member, dict) or not isinstance(member.get("step_id"), str)
                        or not isinstance(member.get("run_id"), str)
                        or set(member) != {"step_id", "run_id", "role", "directive_version", "directive_ref",
                            "directive_sha256", "context_ref", "criteria"}
                        or not re.fullmatch(r"[0-9a-f]{64}", str(member.get("directive_sha256", "")))
                        or not isinstance(member_context, dict)
                        or member_context.get("scope_id") != body.get("scope_id")
                        or member_context.get("source_hash") != body.get("source_hash")
                        or not isinstance(member.get("criteria"), list)
                        or any(not isinstance(item, dict) or set(item) != {"id", "sha256"}
                            or not isinstance(item.get("id"), str)
                            or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))
                            for item in member.get("criteria", []))):
                    return False
    return True


def _control_response_bound(original, result, run, body, scope_id, revision):
    if (not isinstance(result, dict) or result.get("run_id") != run["id"]
            or result.get("control_ref") != {"kind": "execution_control", "id": run["id"],
                "scope_id": scope_id, "revision": revision}
            or result.get("run_state") != run["state"] or type(result.get("locks_retained")) is not bool):
        return "response_ref_run_or_lock_mismatch"
    payload = original["payload"]
    context = body.get("context_ref")
    if not isinstance(context, dict) or context.get("source_hash") != body.get("source_hash"):
        return "persisted_context_source_mismatch"
    source = original.get("source")
    if isinstance(source, dict) and source.get("source_hash") is not None and source.get("source_hash") != body.get("source_hash"):
        return "request_source_mismatch"
    if original["operation"] == "advance_execution_control":
        if (payload.get("context_ref", context) != context
                or body.get("stage") not in {"main_action_pending", "waiting", "dispatch_pending",
                    "running", "observed", "cancel_wait", "cancelled_before_dispatch", "waiting_context",
                    "reuse_review_pending", "review_required", "reconcile_required", "retry_queued",
                    "review_pending", "result_ready", "succeeded", "failed", "blocked", "canceled"}):
            return "advance_context_or_stage_mismatch"
        action = result.get("action")
        if not isinstance(action, dict) or action.get("run_id") != run["id"]:
            return "advance_action_missing_or_wrong_run"
        if action.get("kind") == "main-native-call":
            return (None if body.get("action_response_request_id") == original["request_id"]
                    and _bound_action(action, body) else "native_action_stub_nonce_or_prompt_mismatch")
        if set(action) - {"kind", "run_id", "reason", "nonce", "next_poll_at", "refs", "locks_retained"}:
            return "advance_action_schema_mismatch"
        if action.get("nonce") is not None and action.get("nonce") != body.get("action_nonce"):
            return "advance_action_nonce_mismatch"
        return None
    if original["operation"] == "acknowledge_execution_action":
        nonce = payload.get("action_nonce")
        ack = body.get("action_acks", {}).get(nonce) if isinstance(body.get("action_acks"), dict) else None
        if (not isinstance(nonce, str) or nonce != body.get("action_ack", {}).get("nonce")
                or not isinstance(ack, dict)
                or ack.get("request_fingerprint") != semantic_request_fingerprint(original)
                or body.get("action", {}).get("action_nonce") != nonce
                or payload.get("expected_run_revision") != body.get("action", {}).get("expected_run_revision")
                or payload.get("control_ref") != {"kind": "execution_control", "id": run["id"],
                    "scope_id": scope_id, "revision": revision - 1}
                or result.get("handle_ref") != ack.get("handle_ref")
                or result.get("run_state") != ack.get("run_state")
                or ack.get("outcome") not in {"started", "not_started", "unknown", "cancel_requested"}
                or (ack.get("outcome") == "started" and run["state"] != "running")
                or (ack.get("outcome") == "unknown" and run["state"] != "reconciling")
                or (ack.get("outcome") == "cancel_requested" and run["state"] != "cancel_requested")
                or result.get("acknowledged") is not (ack.get("outcome") in {"started", "cancel_requested"})
                or result.get("reconcile_required") is not (ack.get("outcome") == "unknown")):
            return "ack_nonce_fingerprint_ref_or_outcome_mismatch"
        action = result.get("action")
        if (isinstance(action, dict) and action.get("run_id") == run["id"]
                and action.get("kind") == ("wait" if ack.get("outcome") in {"started", "cancel_requested"} else "review-needed")
                and not (set(action) - {"kind", "run_id", "reason", "nonce", "next_poll_at", "refs", "locks_retained"})):
            return None
        return "ack_action_schema_or_outcome_mismatch"
    return "original_operation_unsupported"


def _metadata(value, depth=0):
    if depth > 20:
        raise PmtError("control_body_invalid", "Control metadata is too deeply nested")
    if isinstance(value, dict):
        if any(not isinstance(key, str) or key.casefold() in _FORBIDDEN for key in value):
            raise PmtError("control_body_invalid", "Control metadata cannot contain private execution inputs")
        for item in value.values():
            _metadata(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 500:
            raise PmtError("control_body_invalid", "Control metadata array exceeds its bound")
        for item in value:
            _metadata(item, depth + 1)
    elif isinstance(value, str):
        if len(value.encode()) > 4096 or re.search(r"(?:^[A-Za-z]:[\\/]|^\\\\|(?i:bearer)\s+|\bsk-[A-Za-z0-9_-]{8,})", value):
            raise PmtError("control_body_invalid", "Control metadata contains an unsupported value")
    elif value is not None and type(value) not in {int, bool, float}:
        raise PmtError("control_body_invalid", "Control metadata must be JSON scalars and references")


def authorize(application, conn, req, principal):
    principal.require("runtime")
    run_id = identifier(req["payload"].get("run_id"), "run_id")
    row = conn.execute("SELECT e.*,r.scope_id FROM execution_runs e JOIN records r ON r.id=e.step_id WHERE e.id=?", (run_id,)).fetchone()
    if not row or row["owner_session"] != principal.session_id or row["scope_id"] != req.get("scope_id"):
        raise PmtError("ownership_conflict", "Control state requires this session's run and selected scope", 3)
    if row["state"] in {"starting", "running", "review_pending", "reconciling", "cancel_requested"}:
        require_workspace_claim(application.db, conn, req, row["workspace"], [])
    elif row["state"] not in {"queued", "succeeded", "failed", "canceled", "blocked"}:
        raise PmtError("ownership_conflict", "Run state cannot authorize control metadata", 3)
    if req["operation"] == "write_execution_control":
        source_hash = req["payload"].get("source_hash")
        if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise PmtError("source_pin_invalid", "Control source hash must be SHA-256")
        pointers = conn.execute("SELECT body_json FROM phase3_objects WHERE kind='host_source_current' AND scope_id=?",
                                (row["scope_id"],)).fetchall()
        matched = [json.loads(p[0]) for p in pointers if json.loads(p[0]).get("canonical_workspace") == row["workspace"]]
        if len(matched) != 1 or matched[0].get("source_hash") != source_hash:
            raise PmtError("source_conflict", "Control source no longer matches the current workspace snapshot", 3)
    return dict(row)


def handle(application, conn, req, principal):
    run = authorize(application, conn, req, principal)
    storage = Phase3Storage(application.db)
    p = req["payload"]
    old = storage.get_object("execution_control", run["id"], run["scope_id"], principal.actor, principal.session_id, conn=conn)
    if req["operation"] == "read_execution_control":
        if set(p) - {"run_id", "include_pending_action"} or type(p.get("include_pending_action", False)) is not bool:
            raise PmtError("unknown_fields", "Control read fields are invalid")
        if old and p.get("include_pending_action"):
            check = {**req, "operation": "write_execution_control", "payload": {"run_id": run["id"], "source_hash": old["source_hash"]}}
            authorize(application, conn, check, principal)
            old = {**old, "pending_action": None, "pending_action_unavailable": True}
            response_id = old["body"].get("action_response_request_id")
            cached = conn.execute("SELECT * FROM requests WHERE request_id=?", (response_id,)).fetchone() if isinstance(response_id, str) else None
            authority = conn.execute("SELECT device_id FROM host_request_scopes WHERE request_id=? AND scope_id=?",
                                     (response_id, req["scope_id"])).fetchone() if cached else None
            if cached and authority:
                envelope = json.loads(cached["response_json"])
                result = envelope.get("result") or {}
                action = result.get("action")
                ref = {"kind": "execution_control", "id": run["id"], "scope_id": req["scope_id"], "revision": old["revision"]}
                if (cached["actor"] == principal.actor and cached["session_id"] == principal.session_id
                        and authority[0] == principal.device_id and envelope.get("ok") is True
                        and cached["fingerprint_version"] == 1 and cached["exit_code"] == 0
                        and envelope.get("request_id") == response_id
                        and result.get("control_ref") == ref and result.get("run_id") == run["id"]
                        and result.get("run_state") == run["state"]
                        and result.get("source_hash", old["source_hash"]) == old["source_hash"]
                        and isinstance(action, dict) and action.get("kind") == "main-native-call"
                        and action.get("action_nonce") == old["body"].get("action_nonce")
                        and _bound_action(action, old["body"])):
                    old.update(pending_action=action, pending_action_unavailable=False)
        return old
    allowed = {"run_id", "expected_control_revision", "source_hash", "body", "event_name", "event_id",
               "original_request", "original_response"}
    if set(p) - allowed or not isinstance(p.get("body"), dict):
        raise PmtError("control_body_invalid", "Control write fields are invalid")
    _metadata(p["body"])
    if len(canonical_json(p["body"]).encode()) > 128 * 1024:
        raise PmtError("control_body_invalid", "Control metadata exceeds 128 KiB")
    expected = p.get("expected_control_revision")
    actual = old["revision"] if old else 0
    if type(expected) is not int or expected != actual:
        raise PmtError("revision_conflict", "Control revision changed", 3, details={"current_revision": actual})
    has_original = "original_request" in p or "original_response" in p
    if has_original:
        if not old or old["body"] != p["body"] or old["source_hash"] != p["source_hash"]:
            raise PmtError("control_response_conflict", "Response cache requires the unchanged persisted control state", 3)
        from ..service import normalize_request
        original = normalize_request(p.get("original_request"))
        cached = p.get("original_response")
        payload_fields = ({"run_id", "context_ref", "reuse_body_ref", "retry", "cancel", "previous_run_id"}
                          if isinstance(original, dict) and original.get("operation") == "advance_execution_control"
                          else {"run_id", "control_ref", "action_nonce", "outcome", "handle_ref",
                                "expected_run_revision", "verified_trace_ref"})
        if (original["operation"] not in {"advance_execution_control", "acknowledge_execution_action"}
                or set(original["payload"]) - payload_fields
                or original["actor"] != principal.actor or original["session_id"] != principal.session_id
                or original.get("scope_id") != req["scope_id"] or original["payload"].get("run_id") != run["id"]
                or original["request_id"] == req["request_id"] or not isinstance(cached, dict)
                or set(cached) != {"result", "exit_code"} or type(cached["exit_code"]) is not int or cached["exit_code"] != 0
                or not isinstance(cached["result"], dict)):
            raise PmtError("control_response_invalid", "Original control request/response binding is invalid")
        result = cached["result"]
        ref = {"kind": "execution_control", "id": run["id"], "scope_id": req["scope_id"], "revision": actual}
        if result.get("control_ref") != ref or result.get("run_id") != run["id"]:
            raise PmtError("control_response_conflict", "Original response does not match the persisted control revision", 3)
        binding_failure = _control_response_bound(original, result, run, old["body"], req["scope_id"], actual)
        if binding_failure:
            raise PmtError("control_response_conflict", "Original response is not bound to the current source, action and run state", 3,
                           details={"binding_failure": binding_failure})
        digest = semantic_request_fingerprint(original)
        prior = conn.execute("SELECT * FROM requests WHERE request_id=?", (original["request_id"],)).fetchone()
        envelope = response(original["request_id"], result=result)
        if prior:
            if prior["actor"] != principal.actor or prior["session_id"] != principal.session_id or prior["request_fingerprint"] != digest:
                raise PmtError("request_conflict", "Original request ID was used with different inputs or owner", 3)
            if json.loads(prior["response_json"]) != envelope:
                raise PmtError("control_response_conflict", "A different original response is already committed", 3)
        else:
            conn.execute("INSERT INTO requests(request_id,fingerprint_version,request_fingerprint,response_json,exit_code,"
                         "deterministic,actor,session_id,created_at) VALUES(?,1,?,?,0,1,?,?,?)",
                         (original["request_id"], digest, canonical_json(envelope), principal.actor, principal.session_id, utc_now()))
            conn.execute("INSERT INTO host_request_scopes VALUES(?,?,?)", (original["request_id"], req["scope_id"], principal.device_id))
        return {"control_ref": ref, "revision": actual, "original_response": envelope, "original_exit_code": 0}
    receipt = storage.put_object("execution_control", run["id"], req["scope_id"], principal.actor, principal.session_id,
        p["source_hash"], actual, p["body"], state=p["body"].get("stage", "active"), request_id=req["request_id"], conn=conn)
    if p.get("event_name"):
        from ..lifecycle import _event
        if p["event_name"] not in {"control.state_observed", "control.reconcile_required", "control.review_needed",
                                   "control.action_requested", "control.notice", "control.retry_requested",
                                   "control.observation_started", "control.ai_notified", "control.retry_decided"}:
            raise PmtError("control_event_invalid", "Control event name is unsupported")
        _event(conn, req, event_id=p.get("event_id"), event_type=p["event_name"], scope_id=req["scope_id"],
               record_id=run["step_id"], payload={key: p["body"].get(key) for key in
                   ("stage", "source_hash", "context_ref", "action_nonce", "notice_id", "reason_code")})
    return {"control_ref": {"kind": "execution_control", "id": run["id"], "scope_id": req["scope_id"],
                            "revision": receipt["revision"]}, "revision": receipt["revision"]}
