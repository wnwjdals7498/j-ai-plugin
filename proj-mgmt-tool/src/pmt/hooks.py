"""Native lifecycle adapters that forward a minimal event to the PMT CLI."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from .util import canonical_json, new_id, strict_json_loads
from .errors import PmtError

PROTOCOL_VERSION = 1
ADAPTER_VERSION = "0.1.0"
EVENTS = {
    "codex": {
        "SessionStart": "session_started",
        "UserPromptSubmit": "prompt_submitted",
        "Stop": "turn_stopped",
        "SubagentStop": "subagent_stopped",
    },
    "claude": {
        "SessionStart": "session_started",
        "UserPromptSubmit": "prompt_submitted",
        "Stop": "turn_stopped",
        "SessionEnd": "session_ended",
    },
    "opencode": {
        "session.created": "session_started",
        "session.idle": "session_idle",
        "session.error": "hook_error",
        "tool.execute.after": "tool_completed",
    },
}
_NAMESPACE = uuid.UUID("48aa7f38-ecfa-44c5-8ad4-46226e283d4c")


class HookInputError(ValueError):
    pass


def _string(value, limit=256):
    return value[:limit] if isinstance(value, str) and value else None


def _native_parts(product, event, raw):
    """Extract only documented identifiers and small event metadata."""
    if not isinstance(raw, dict):
        raise HookInputError("native input must be a JSON object")
    if product not in EVENTS or event not in EVENTS[product]:
        raise HookInputError("unsupported product event")

    if product in ("codex", "claude"):
        if raw.get("hook_event_name") != event:
            raise HookInputError("hook_event_name does not match configured event")
        session_id = _string(raw.get("session_id"))
        turn_id = _string(raw.get("turn_id"))
        source_occurrence_id = _string(raw.get("tool_use_id")) or turn_id
        if event == "SubagentStop":
            source_occurrence_id = _string(raw.get("agent_id")) or source_occurrence_id
        metadata = {}
        for key in ("source", "reason", "stop_hook_active"):
            val = raw.get(key)
            if isinstance(val, (str, bool)):
                metadata[key] = val
        if event == "SubagentStop":
            for key in ("agent_id", "agent_type"):
                val = _string(raw.get(key))
                if val:
                    metadata[key] = val
        return session_id, turn_id, source_occurrence_id, metadata, raw.get("occurred_at")

    props = raw.get("properties")
    props = props if isinstance(props, dict) else {}
    info = props.get("info")
    info = info if isinstance(info, dict) else {}
    session_id = _string(props.get("sessionID")) or _string(info.get("id"))
    if not session_id:
        raise HookInputError("OpenCode event has no documented session identifier")
    metadata = {}
    for key in ("status", "tool"):
        val = props.get(key)
        if isinstance(val, str):
            metadata[key] = val[:128]
    return session_id, None, _string(props.get("eventID")), metadata, props.get("time")


def _profile_instance_id(env):
    """Use the stable profile UUID already owned by PMT's config root."""
    config_value = env.get("PMT_CONFIG_ROOT")
    if not config_value:
        raise HookInputError("PMT_CONFIG_ROOT is required to identify this installation")
    config_root = Path(config_value).expanduser()
    profile = config_root / "profile.json"

    def read_profile():
        try:
            value = strict_json_loads(profile.read_bytes(), max_bytes=4096)
            profile_id = value.get("environment_id") if isinstance(value, dict) else None
            if isinstance(profile_id, str) and str(uuid.UUID(profile_id)) == profile_id:
                return profile_id
        except (OSError, ValueError, TypeError, AttributeError, PmtError):
            pass
        raise HookInputError("PMT profile configuration is invalid and was left unchanged")

    if profile.exists():
        return read_profile()
    config_root.mkdir(parents=True, exist_ok=True)
    profile_id = new_id()
    fd, temp_name = tempfile.mkstemp(prefix=".profile-", suffix=".tmp", dir=str(config_root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical_json({"environment_id": profile_id}))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp_name, profile)
        except FileExistsError:
            return read_profile()
        return profile_id
    except OSError as exc:
        raise HookInputError("PMT profile could not be created atomically") from exc
    finally:
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def normalize_event(product, event, raw, *, environ=None):
    event_type = EVENTS.get(product, {}).get(event)
    if event_type is None:
        raise HookInputError("unsupported product event")
    session_id, turn_id, occurrence_id, metadata, occurred_at = _native_parts(product, event, raw)
    if not session_id:
        raise HookInputError("native input has no stable session_id")

    env = os.environ if environ is None else environ
    instance_id = _string(env.get("PMT_INSTALLATION_ID")) or _profile_instance_id(env)
    stable_identity = [product, instance_id, session_id, event_type, occurrence_id]
    if occurrence_id:
        event_id = str(uuid.uuid5(_NAMESPACE, canonical_json(stable_identity)))
        request_id = str(uuid.uuid5(_NAMESPACE, canonical_json(stable_identity + ["request"])))
        dedup_scope = "native_occurrence"
    else:
        # The UUID is saved before invoking the CLI. Retries of that pending file
        # preserve this ID; a fresh native invocation cannot be deduplicated.
        event_id, request_id = new_id(), new_id()
        dedup_scope = "adapter_replay_only"
    stamp = occurred_at if isinstance(occurred_at, str) else None
    if stamp is not None and len(stamp) > 64:
        stamp = stamp[:64]
    source = {
        "product": product,
        "version": _string(env.get("PMT_PRODUCT_VERSION")) or "unknown",
        "adapter_version": ADAPTER_VERSION,
        "installation_id": instance_id,
        "native_event": event,
        "native_session_id": session_id,
    }
    normalized = {
        "event_id": event_id,
        "type": event_type,
        "occurred_at": stamp,
        "source_metadata": {"native_event": event, "dedup_scope": dedup_scope, **metadata},
    }
    if turn_id:
        normalized["turn_id"] = turn_id
    if occurrence_id:
        normalized["source_event_id"] = occurrence_id
    return {
        "protocol_version": PROTOCOL_VERSION,
        "operation": "record_event",
        "request_id": request_id,
        "actor": "hook",
        "session_id": session_id,
        "source": source,
        "normalized_event": normalized,
    }


def _pending_path(data_root, event_id):
    return Path(data_root) / "hook-pending" / (event_id + ".json")


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".pending-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical_json(value))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _read_pending(path):
    try:
        return strict_json_loads(path.read_bytes(), max_bytes=64 * 1024)
    except OSError:
        return None


def _cli_argv(data_root, config_root, environ=None):
    env = os.environ if environ is None else environ
    python = env.get("PMT_PYTHON") or sys.executable
    if not python:
        raise RuntimeError("Python runtime is unavailable")
    return [python, "-m", "pmt", "--data-root", str(data_root), "--config-root", str(config_root)]


def _child_environment(environ=None):
    env = dict(os.environ if environ is None else environ)
    package_src = str(Path(__file__).resolve().parents[1])
    existing = env.get("PYTHONPATH", "")
    entries = existing.split(os.pathsep) if existing else []
    if package_src not in entries:
        env["PYTHONPATH"] = package_src + (os.pathsep + existing if existing else "")
    return env


def process_hook(product, event, raw, *, environ=None, timeout=1.5, run=subprocess.run):
    """Persist a minimal pending envelope before calling the shared CLI."""
    env = os.environ if environ is None else environ
    data_root, config_root = env.get("PMT_DATA_ROOT"), env.get("PMT_CONFIG_ROOT")
    if not data_root or not config_root:
        raise RuntimeError("PMT_DATA_ROOT and PMT_CONFIG_ROOT must be configured")
    envelope = normalize_event(product, event, raw, environ=env)
    pending_path = _pending_path(data_root, envelope["normalized_event"]["event_id"])
    existing = _read_pending(pending_path)
    if existing:
        # Retain the first saved request/event envelope on replay.
        envelope = existing
    else:
        _atomic_json(pending_path, envelope)
    request = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    completed = run(
        _cli_argv(data_root, config_root, env), input=request, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=timeout, check=False, shell=False,
        env=_child_environment(env),
    )
    if completed.returncode == 0:
        try:
            response = strict_json_loads(completed.stdout, max_bytes=1024 * 1024)
        except Exception as exc:
            raise RuntimeError("PMT CLI returned an invalid response") from exc
        if isinstance(response, dict) and response.get("ok") is True:
            pending_path.unlink(missing_ok=True)
            return response
    raise RuntimeError("PMT event was not confirmed; pending event retained")


def replay_pending(*, environ=None, timeout=1.5, run=subprocess.run):
    """Retry saved envelopes with their original request/event UUIDs."""
    env = os.environ if environ is None else environ
    data_root, config_root = env.get("PMT_DATA_ROOT"), env.get("PMT_CONFIG_ROOT")
    if not data_root or not config_root:
        raise RuntimeError("PMT_DATA_ROOT and PMT_CONFIG_ROOT must be configured")
    directory = Path(data_root) / "hook-pending"
    replayed = 0
    if not directory.exists():
        return replayed
    for pending_path in sorted(directory.glob("*.json")):
        envelope = _read_pending(pending_path)
        if not isinstance(envelope, dict):
            continue
        request = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        completed = run(
            _cli_argv(data_root, config_root, env), input=request, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False, shell=False, env=_child_environment(env),
        )
        try:
            response = strict_json_loads(completed.stdout, max_bytes=1024 * 1024)
        except Exception:
            response = None
        if completed.returncode != 0 or not isinstance(response, dict) or response.get("ok") is not True:
            raise RuntimeError("Pending PMT event remains unconfirmed")
        pending_path.unlink(missing_ok=True)
        replayed += 1
    return replayed


def _overview_selector(config_root, scope_id, record_id):
    """Resolve only a unique configured mapping for the user's explicit scope."""
    try:
        from .storage_config import _read_profile
        from .workspace import canonical_workspace
        profile, _digest = _read_profile(config_root)
    except Exception:
        return None
    if not isinstance(profile, dict):
        return None
    matches = [item for item in profile.get("workspace_mappings", [])
               if item.get("project_id") == scope_id]
    if len(matches) != 1:
        return None
    mapping = matches[0]
    branch = mapping.get("branch")
    if not isinstance(branch, str) or not branch:
        return None
    try:
        workspace_ref = canonical_workspace(mapping["repository_id"], branch)
    except Exception:
        return None
    return {"repository_id": mapping["repository_id"], "branch": branch,
            "workspace_ref": workspace_ref, "task_id": record_id,
            # Current checkpoints intentionally use a shared environment=None
            # selector; Host authentication still binds the registered caller's
            # actual environment in the request headers.
            "purpose": "current", "environment_id": None}


def lookup_context(session_id, *, product, environ=None, timeout=1.5, run=subprocess.run,
                   native_event=None):
    """Read a bounded metadata-only resume overview through the public PMT CLI."""
    env = os.environ if environ is None else environ
    scope_id = env.get("PMT_SCOPE_ID")
    if scope_id is None or not scope_id.strip():
        return {"status": "not_configured", "context_markdown": None, "error_code": None}
    try:
        if str(uuid.UUID(scope_id)) != scope_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        return {"status": "invalid_configuration", "context_markdown": None,
                "error_code": "invalid_scope_id"}
    record_id = env.get("PMT_RECORD_ID")
    if record_id:
        try:
            if str(uuid.UUID(record_id)) != record_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            return {"status": "invalid_configuration", "context_markdown": None,
                    "error_code": "invalid_record_id"}
    if not isinstance(session_id, str) or not session_id:
        return {"status": "invalid_input", "context_markdown": None, "error_code": "session_id_required"}
    data_root, config_root = env.get("PMT_DATA_ROOT"), env.get("PMT_CONFIG_ROOT")
    if not data_root or not config_root:
        return {"status": "unavailable", "context_markdown": None, "error_code": "pmt_roots_unconfigured"}
    installation_id = _string(env.get("PMT_INSTALLATION_ID"))
    if installation_id is None:
        try:
            installation_id = _profile_instance_id(env)
        except Exception:
            return {"status": "unavailable", "context_markdown": None,
                    "error_code": "profile_config_invalid"}
    source = {"product": product, "adapter_version": ADAPTER_VERSION,
        "installation_id": installation_id, "native_event": native_event or
        ("session.created" if product == "opencode" else "SessionStart"),
        "native_session_id": session_id}
    payload = {"role": "main", "budget": {"max_bytes": 8192, "max_lines": 96}}
    selector = _overview_selector(config_root, scope_id, record_id)
    if selector is not None:
        payload["selector"] = selector
    if record_id:
        payload["task_id"] = record_id
    request = {
        "protocol_version": PROTOCOL_VERSION,
        "operation": "compose_resume_overview",
        "request_id": new_id(),
        "actor": "hook",
        "session_id": session_id,
        "scope_id": scope_id,
        "payload": payload,
        "source": source,
    }
    wire = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        completed = run(_cli_argv(data_root, config_root, env), input=wire,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                        check=False, shell=False, env=_child_environment(env))
        response = strict_json_loads(completed.stdout, max_bytes=1024 * 1024)
    except subprocess.TimeoutExpired:
        return {"status": "unavailable", "context_markdown": None, "error_code": "timeout"}
    except Exception:
        return {"status": "unavailable", "context_markdown": None, "error_code": "cli_unavailable"}
    if completed.returncode != 0 or not isinstance(response, dict) or response.get("ok") is not True:
        error = response.get("error") if isinstance(response, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        return {"status": "unavailable", "context_markdown": None,
                "error_code": code if isinstance(code, str) else "context_read_failed"}
    result = response.get("result")
    overview = result.get("overview") if isinstance(result, dict) else None
    if isinstance(result, dict) and result.get("selection_required") is True:
        overview = {"scope_id": scope_id, "selection_required": True,
                    "candidate_work_refs": result.get("candidate_work_refs", []),
                    "unknown": [result.get("reason", "explicit repository selection is required")],
                    "complete": False}
    if not isinstance(overview, dict):
        return {"status": "unavailable", "context_markdown": None,
                "error_code": "overview_unavailable"}
    summary = "PMT metadata overview; reference only. Recheck current authority, source, and work ownership before acting.\n"
    context = summary + canonical_json(overview)
    if len(context.encode("utf-8")) > 12 * 1024:
        return {"status": "unavailable", "context_markdown": None,
                "error_code": "overview_output_exceeded"}
    return {"status": "ok", "context_markdown": context, "error_code": None,
            "overview": overview, "complete": result.get("complete") is True}


def _session_context_output(context_result, event_ok):
    output = {}
    if context_result["status"] == "ok":
        output["hookSpecificOutput"] = {
            "hookEventName": "SessionStart",
            "additionalContext": context_result["context_markdown"],
        }
    elif context_result["status"] not in {"not_configured"}:
        output["systemMessage"] = f"PMT context unavailable ({context_result['error_code']})."
    if not event_ok:
        message = "PMT could not confirm session event storage; pending event retained."
        output["systemMessage"] = (output.get("systemMessage", "") + " " + message).strip()
    if not output:
        return None
    return output


def process_session_start(product, raw, *, environ=None, timeout=1.5, run=subprocess.run):
    """Store SessionStart and optionally fetch explicitly scoped PMT context in parallel."""
    if product not in {"codex", "claude"}:
        raise HookInputError("native SessionStart context output is supported only for Codex and Claude")
    env = os.environ if environ is None else environ
    parts = _native_parts(product, "SessionStart", raw)
    session_id = parts[0]
    if not session_id:
        raise HookInputError("native input has no stable session_id")
    with ThreadPoolExecutor(max_workers=2) as pool:
        event_future = pool.submit(process_hook, product, "SessionStart", raw,
                                   environ=env, timeout=timeout, run=run)
        context_future = pool.submit(lookup_context, session_id, product=product,
                                     environ=env, timeout=timeout, run=run,
                                     native_event="SessionStart")
        try:
            event_future.result()
            event_ok = True
        except Exception:
            event_ok = False
        try:
            context_result = context_future.result()
        except Exception:
            context_result = {"status": "unavailable", "context_markdown": None,
                              "error_code": "context_read_failed"}
    return _session_context_output(context_result, event_ok)


def _native_warning(product, event, message):
    # Only product-documented warning channels; otherwise use concise stderr.
    if product == "claude" and event != "SessionEnd":
        sys.stdout.write(json.dumps({"systemMessage": message}, ensure_ascii=False) + "\n")
    elif product == "codex":
        sys.stdout.write(json.dumps({"systemMessage": message}, ensure_ascii=False) + "\n")
    else:
        print(message, file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description="PMT native hook bridge")
    parser.add_argument("--product", choices=sorted(EVENTS))
    parser.add_argument("--event")
    parser.add_argument("--replay-pending", action="store_true")
    parser.add_argument("--with-context", action="store_true")
    parser.add_argument("--read-context", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.replay_pending:
            replay_pending()
            return 0
        if args.read_context:
            if not args.product:
                raise HookInputError("--product is required for context lookup")
            raw = strict_json_loads(sys.stdin.buffer.read(64 * 1024 + 1), max_bytes=64 * 1024)
            session_id = raw.get("session_id") if isinstance(raw, dict) else None
            native_event = raw.get("native_event") if isinstance(raw, dict) else None
            expected_event = "session.created" if args.product == "opencode" else "SessionStart"
            if native_event != expected_event:
                raise HookInputError("context lookup requires the product's native session-start event")
            result = lookup_context(session_id, product=args.product, native_event=native_event)
            sys.stdout.write(canonical_json(result) + "\n")
            return 0
        if not args.product or not args.event:
            raise HookInputError("--product and --event are required")
        raw = strict_json_loads(sys.stdin.buffer.read(64 * 1024 + 1), max_bytes=64 * 1024)
        if args.with_context:
            if args.event != "SessionStart":
                raise HookInputError("--with-context is valid only for SessionStart")
            output = process_session_start(args.product, raw)
            if output is not None:
                sys.stdout.write(canonical_json(output) + "\n")
        else:
            process_hook(args.product, args.event, raw)
    except Exception:
        _native_warning(args.product or "", args.event or "", "PMT could not confirm event storage; see local diagnostics.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
