#!/usr/bin/env python3
"""Run an isolated OpenCode V1 native-plugin lifecycle smoke test.

This runner never builds packages. Supply already-built 0.1.0 and 0.1.1
OpenCode package directories after the P9 gate is approved, then pass
--execute-native explicitly. The test model is a localhost deterministic stub,
not an LLM. Product sessions and hooks are exercised by OpenCode itself.

V1 references used for the runner:
  https://opencode.ai/docs/plugins/
  https://dev.opencode.ai/docs/server/
  https://opencode.ai/docs/providers/
  https://raw.githubusercontent.com/anomalyco/opencode/v1.18.34/packages/opencode/src/plugin/shared.ts
  https://raw.githubusercontent.com/anomalyco/opencode/v1.18.34/packages/opencode/src/plugin/index.ts
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import stat
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = PROJECT_ROOT / ".pmt-test" / "native-opencode"
EXPECTED_OPENCODE_VERSION = "1.18.34"
VERSION_010 = "0.1.0"
VERSION_011 = "0.1.1"
STUB_AUTH = "pmt-test-not-secret"
STUB_ACK = "PMT-LOCAL-STUB-ACK"
API_HOST = "127.0.0.1"
MAX_HTTP_BYTES = 2 * 1024 * 1024


class SmokeFailure(RuntimeError):
    def __init__(self, code: str, *, exit_code: int = 4, details: dict[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code
        self.details = details or {}


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _reject_reparse(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    chain = list(absolute.parents)[::-1] + [absolute]
    for item in chain:
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        attributes = getattr(info, "st_file_attributes", 0)
        if item.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise SmokeFailure("unsafe_reparse_path", exit_code=2)


def _package_info(package: Path, expected_version: str) -> dict[str, Any]:
    package = package.resolve(strict=True)
    _reject_reparse(package)
    manifest_path = package / "pmt-package.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SmokeFailure("package_manifest_invalid", exit_code=2) from exc
    if (manifest.get("manifest_version") != 1 or manifest.get("product") != "opencode"
            or manifest.get("plugin_version") != expected_version):
        raise SmokeFailure("package_version_or_product_mismatch", exit_code=2)
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise SmokeFailure("package_file_manifest_invalid", exit_code=2)
    for relative, expected_hash in files.items():
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            raise SmokeFailure("package_file_manifest_invalid", exit_code=2)
        part = PurePosixPath(relative)
        if part.is_absolute() or ".." in part.parts:
            raise SmokeFailure("package_path_unsafe", exit_code=2)
        target = package.joinpath(*part.parts)
        _reject_reparse(target)
        if not target.is_file() or _sha256(target) != expected_hash:
            raise SmokeFailure("package_hash_mismatch", exit_code=2)
    entry = package / "integrations" / "opencode" / "pmt.js"
    bridge = package / "integrations" / "opencode" / "bridge.py"
    launcher = package / "scripts" / "pmt.py"
    for required in (entry, bridge, launcher):
        if not required.is_file():
            raise SmokeFailure("package_required_file_missing", exit_code=2)
    archive = package.parent / (package.name + ".zip")
    return {"version": expected_version, "package_manifest_sha256": _sha256(manifest_path),
            "package_archive_sha256": _sha256(archive) if archive.is_file() else None,
            "entry_sha256": _sha256(entry), "core_version": manifest.get("core_version"),
            "schema_version": manifest.get("schema_version")}


def _resolve_opencode_executable(wrapper: Path) -> tuple[Path, str]:
    wrapper = wrapper.resolve(strict=True)
    _reject_reparse(wrapper)
    if wrapper.suffix.casefold() == ".cmd":
        expected_parent = (PROJECT_ROOT / ".pmt-test" / "tooling" / "node_modules" / ".bin").resolve()
        if wrapper.parent != expected_parent:
            raise SmokeFailure("opencode_wrapper_outside_test_tooling", exit_code=2)
        binary = wrapper.parent.parent / "opencode-ai" / "bin" / "opencode.exe"
        _reject_reparse(binary)
        if not binary.is_file():
            raise SmokeFailure("opencode_binary_missing", exit_code=4)
        return binary.resolve(), str(wrapper)
    return wrapper, str(wrapper)


def _safe_child_environment(run_root: Path, *, python: Path, data_root: Path, config_root: Path,
                            scope_id: str | None, record_id: str | None, stub_port: int | None = None) -> dict[str, str]:
    # Retain only OS/runtime variables needed to start the trusted local binary.
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
    env = {key: os.environ[key] for key in allowed if os.environ.get(key)}
    config_home = run_root / "xdg-config"
    data_home = run_root / "xdg-data"
    windows_profile = run_root / "windows-profile"
    windows_profile.mkdir(parents=True, exist_ok=True)
    env.update({
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_DATA_HOME": str(data_home),
        "XDG_CACHE_HOME": str(run_root / "xdg-cache"),
        "XDG_STATE_HOME": str(run_root / "xdg-state"),
        "APPDATA": str(config_home),
        "LOCALAPPDATA": str(data_home),
        "USERPROFILE": str(windows_profile),
        "OPENCODE_CONFIG_DIR": str(run_root / "opencode-config"),
        "OPENCODE_CONFIG": str(run_root / "opencode-config" / "opencode.json"),
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_PRUNE": "1",
        "OPENCODE_SERVER_PASSWORD": "",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "PYTHONUTF8": "1",
        "PMT_PYTHON": str(python),
        "PMT_DATA_ROOT": str(data_root),
        "PMT_CONFIG_ROOT": str(config_root),
        "PMT_PRODUCT_VERSION": EXPECTED_OPENCODE_VERSION,
        "PMT_INSTALLATION_ID": str(uuid.uuid5(uuid.NAMESPACE_URL, str(run_root))),
    })
    python_dir = str(python.parent)
    inherited_path = env.get("PATH", "")
    env["PATH"] = python_dir + (os.pathsep + inherited_path if inherited_path else "")
    for key in tuple(env):
        if key.upper().endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")):
            env.pop(key, None)
    if scope_id:
        env["PMT_SCOPE_ID"] = scope_id
    if record_id:
        env["PMT_RECORD_ID"] = record_id
    if stub_port:
        env["PMT_STUB_PORT"] = str(stub_port)
    return env


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _pmt_call(package: Path, python: Path, data_root: Path, config_root: Path,
              request: dict[str, Any]) -> tuple[dict[str, Any], int]:
    launcher = package / "scripts" / "pmt.py"
    command = [str(python), str(launcher), "--data-root", str(data_root), "--config-root", str(config_root)]
    isolated_profile = config_root.parent / "windows-profile"
    isolated_config = config_root.parent / "xdg-config"
    isolated_data = config_root.parent / "xdg-data"
    isolated_profile.mkdir(parents=True, exist_ok=True)
    child_env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP") if os.environ.get(key)}
    child_env.update({"PYTHONUTF8": "1", "PMT_DATA_ROOT": str(data_root), "PMT_CONFIG_ROOT": str(config_root),
                      "USERPROFILE": str(isolated_profile), "APPDATA": str(isolated_config),
                      "LOCALAPPDATA": str(isolated_data)})
    completed = subprocess.run(command, input=json.dumps(request, ensure_ascii=False), text=True, encoding="utf-8",
                               capture_output=True, timeout=20, check=False, shell=False, env=child_env)
    try:
        response = json.loads(completed.stdout)
    except (ValueError, TypeError) as exc:
        raise SmokeFailure("pmt_cli_invalid_json") from exc
    if not isinstance(response, dict):
        raise SmokeFailure("pmt_cli_response_invalid")
    return response, completed.returncode


def _pmt_request(operation: str, payload: dict[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
    return {"protocol_version": 1, "operation": operation, "request_id": str(uuid.uuid4()),
            "actor": "main", "session_id": "opencode-native-smoke", "payload": payload or {}, **fields}


def _bootstrap_pmt(package: Path, python: Path, run_root: Path, workspace: Path) -> dict[str, Any]:
    data_root, config_root = run_root / "pmt-data", run_root / "pmt-config"
    command_codes = []
    response, code = _pmt_call(package, python, data_root, config_root,
                               _pmt_request("setup", {"product": "opencode"}))
    command_codes.append({"operation": "setup", "exit_code": code})
    if code or not response.get("ok"):
        raise SmokeFailure("pmt_setup_failed")
    setup = response["result"]
    project, code = _pmt_call(package, python, data_root, config_root,
                              _pmt_request("create_scope", {"kind": "project", "slug": "OpenCode native E2E",
                                                              "body": {"workspace": str(workspace),
                                                                      "workspace_binding": "isolated OpenCode fixture"}}))
    command_codes.append({"operation": "create_scope", "exit_code": code})
    if code or not project.get("ok"):
        raise SmokeFailure("pmt_scope_create_failed")
    scope_id = project["result"]["scope_id"]
    sentinel = "PMT_NATIVE_CONTEXT_" + uuid.uuid4().hex.upper()
    fact, code = _pmt_call(package, python, data_root, config_root,
                           _pmt_request("save_change", {"kind": "fact", "title": sentinel,
                                                         "reason": "Native OpenCode startup context fixture",
                                                         "body": {"content": sentinel,
                                                                 "workspace": str(workspace)}},
                                        scope_id=scope_id))
    command_codes.append({"operation": "save_context_record", "exit_code": code})
    if code or not fact.get("ok"):
        raise SmokeFailure("pmt_context_record_create_failed")
    item, code = _pmt_call(package, python, data_root, config_root,
                           _pmt_request("save_change", {"kind": "item", "title": "Native response remains uncompleted",
                                                         "reason": "Verify native events do not imply task completion",
                                                         "body": {"criteria": ["native-session-observed"],
                                                                  "content": sentinel,
                                                                  "workspace": str(workspace)}},
                                        scope_id=scope_id))
    command_codes.append({"operation": "save_item", "exit_code": code})
    if code or not item.get("ok"):
        raise SmokeFailure("pmt_item_create_failed")
    allowed = run_root / "evidence-source"
    allowed.mkdir(parents=True, exist_ok=True)
    evidence_file = allowed / "retained.txt"
    evidence_file.write_text("isolated native OpenCode retained evidence\n", encoding="utf-8")
    resource, code = _pmt_call(package, python, data_root, config_root,
                               _pmt_request("register_resource", {"source_path": str(evidence_file),
                                                                   "allowed_root": str(allowed),
                                                                   "retention": "evidence"}, scope_id=scope_id))
    command_codes.append({"operation": "register_resource", "exit_code": code})
    if code or not resource.get("ok"):
        raise SmokeFailure("pmt_evidence_register_failed")
    artifact_id = resource["result"]["artifact_id"]
    with sqlite3.connect(data_root / "pmt.sqlite3") as conn:
        artifact = conn.execute("SELECT sha256,relative_path FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
    if not artifact:
        raise SmokeFailure("pmt_evidence_missing_after_register")
    return {"data_root": data_root, "config_root": config_root, "db_id": setup["db_id"],
            "environment_id": setup["environment_id"], "scope_id": scope_id,
            "context_record_id": fact["result"]["record_id"], "item_record_id": item["result"]["record_id"],
            "retained_record_ids": [fact["result"]["record_id"], item["result"]["record_id"]],
            "sentinel": sentinel, "artifact_id": artifact_id, "artifact_sha256": artifact[0],
            "artifact_relative_path": artifact[1], "pmt_command_exit_codes": command_codes}


def _retained_state(db, pmt: dict[str, Any]) -> dict[str, Any]:
    with sqlite3.connect(pmt["data_root"] / "pmt.sqlite3") as conn:
        conn.row_factory = sqlite3.Row
        db_id = conn.execute("SELECT value FROM meta WHERE key='db_id'").fetchone()
        records = conn.execute("SELECT id,state FROM records WHERE id IN (?,?) ORDER BY id",
                               tuple(sorted(pmt["retained_record_ids"]))).fetchall()
        item = conn.execute("SELECT state FROM records WHERE id=?", (pmt["item_record_id"],)).fetchone()
        artifact = conn.execute("SELECT sha256,relative_path,state FROM artifacts WHERE id=?",
                                (pmt["artifact_id"],)).fetchone()
    file_path = pmt["data_root"] / artifact["relative_path"] if artifact else None
    file_hash = _sha256(file_path) if file_path and file_path.is_file() else None
    return {"db_id": db_id[0] if db_id else None,
            "retained_record_ids": [row["id"] for row in records],
            "item_state": item[0] if item else None,
            "artifact_id": pmt["artifact_id"],
            "artifact_sha256": artifact["sha256"] if artifact else None,
            "artifact_file_sha256": file_hash,
            "artifact_state": artifact["state"] if artifact else None}


class _StubHandler(http.server.BaseHTTPRequestHandler):
    server_version = "PMT-local-stub"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": "stub-model", "object": "model", "created": 0,
                                                              "owned_by": "pmt-test"}]})
            return
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": {"message": "not found"}})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > MAX_HTTP_BYTES:
                self._send(413, {"error": {"message": "request too large"}})
                return
            request = json.loads(self.rfile.read(size))
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": {"message": "invalid request"}})
            return
        messages = request.get("messages", []) if isinstance(request, dict) else []
        system_text, user_text, roles = [], [], set()
        tool_result_text = []
        for message in messages if isinstance(messages, list) else []:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if isinstance(role, str):
                roles.add(role)
            text = _message_text(message.get("content"))
            if role in {"system", "developer"}:
                system_text.append(text)
            elif role == "user":
                user_text.append(text)
            elif role in {"tool", "function"}:
                tool_result_text.append(text)
        # Keep the observation compatible with OpenAI chat and Responses shapes while
        # retaining only counts and booleans, never request text.
        system_text.extend(_string_list(request.get("system")))
        system_text.extend(_string_list(request.get("instructions")))
        input_value = request.get("input")
        if isinstance(input_value, str):
            user_text.append(input_value)
        elif isinstance(input_value, list):
            for item in input_value:
                if not isinstance(item, dict):
                    continue
                role = item.get("role")
                if isinstance(role, str):
                    roles.add(role)
                text = _message_text(item.get("content") or item.get("text"))
                if role in {"system", "developer"}:
                    system_text.append(text)
                elif role == "user":
                    user_text.append(text)
        marker_match = re.search(r"NATIVE_(?:COMMAND_)?MARKER_[A-F0-9]+", "\n".join(user_text))
        marker = marker_match.group(0) if marker_match else None
        context_seen = self.server.sentinel in "\n".join(system_text)
        auth_seen = self.headers.get("Authorization") == f"Bearer {STUB_AUTH}"
        tool_result_seen = _tool_result_seen(messages)
        tool_names = _tool_names(request.get("tools")) if isinstance(request, dict) else []
        command_mode = bool(marker and marker.startswith("NATIVE_COMMAND_MARKER_"))
        command_tool_selected = command_mode and not tool_result_seen and "bash" in tool_names
        if marker:
            with self.server.observation_lock:
                self.server.observations.append({"marker": marker, "context_seen": context_seen,
                                                 "auth_seen": auth_seen, "http_status": 200,
                                                 "request_kind": "chat_messages" if isinstance(messages, list) else "openai_response",
                                                 "message_roles": sorted(roles), "system_instruction_present": bool(system_text),
                                                 "system_instruction_chars": sum(len(value) for value in system_text),
                                                 "user_marker_seen": True, "command_mode": command_mode,
                                                 "tool_names": tool_names, "tool_result_seen": tool_result_seen,
                                                 "command_tool_selected": command_tool_selected,
                                                 "command_result_marker_seen": "PMT_NATIVE_COMMAND_OK" in "\n".join(tool_result_text)})
        completion_id = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())
        tool_call_id = "call_" + uuid.uuid4().hex
        command = "python -c \"print('PMT_NATIVE_COMMAND_OK')\""
        tool_call = {"index": 0, "id": tool_call_id, "type": "function",
                     "function": {"name": "bash", "arguments": json.dumps({"command": command})}}
        if request.get("stream") is True:
            if command_tool_selected:
                chunks = [
                    {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": "stub-model",
                     "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [tool_call]}, "finish_reason": None}]},
                    {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": "stub-model",
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
                ]
            else:
                chunks = [
                    {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": "stub-model",
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": STUB_ACK}, "finish_reason": None}]},
                    {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": "stub-model",
                     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                ]
            payload = "".join("data: " + json.dumps(chunk, separators=(",", ":")) + "\n\n" for chunk in chunks)
            payload += "data: [DONE]\n\n"
            encoded = payload.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        message = ({"role": "assistant", "content": None, "tool_calls": [{key: value for key, value in tool_call.items() if key != "index"}]}
                   if command_tool_selected else {"role": "assistant", "content": STUB_ACK})
        self._send(200, {"id": completion_id, "object": "chat.completion", "created": created,
                         "model": "stub-model", "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if command_tool_selected else "stop"}],
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(item.get("text", "")) for item in value if isinstance(item, dict))
    return ""


def _tool_names(value: Any) -> list[str]:
    names = set()
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            function = item.get("function") if isinstance(item.get("function"), dict) else item
            name = function.get("name") if isinstance(function, dict) else None
            if isinstance(name, str) and len(name) <= 80:
                names.add(name)
    elif isinstance(value, dict):
        names.update(name for name, enabled in value.items() if enabled is True and isinstance(name, str) and len(name) <= 80)
    return sorted(names)


def _tool_result_seen(messages: Any) -> bool:
    """Recognize standard and structured OpenAI tool-result message forms."""
    if not isinstance(messages, list):
        return False
    result_types = {"tool_result", "tool-result", "function_result", "function-result"}
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") in {"tool", "function"}:
            return True
        for field in ("content", "parts"):
            values = message.get(field)
            if isinstance(values, list) and any(isinstance(part, dict) and part.get("type") in result_types
                                                for part in values):
                return True
    return False


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value if (text := _message_text(item))]
    return []


class _StubServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, sentinel: str):
        super().__init__((API_HOST, 0), _StubHandler)
        self.sentinel = sentinel
        self.observation_lock = threading.Lock()
        self.observations: list[dict[str, Any]] = []


def _stub_observation(server: _StubServer, marker: str, timeout: float = 15) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with server.observation_lock:
            found = [row for row in server.observations if row["marker"] == marker]
            if found:
                return found[-1]
        time.sleep(0.05)
    return None


def _stub_observations(server: _StubServer, marker: str, timeout: float = 15) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with server.observation_lock:
            found = [row for row in server.observations if row["marker"] == marker]
            if found and any(row.get("tool_result_seen") for row in found):
                return found
        time.sleep(0.05)
    with server.observation_lock:
        return [row for row in server.observations if row["marker"] == marker]


def _session_by_title(base_url: str, title: str) -> dict[str, Any] | None:
    sessions, status = _http_json(base_url, "GET", "/session", timeout=3)
    if status != 200:
        return None
    values = sessions.get("data", sessions) if isinstance(sessions, dict) else sessions
    if not isinstance(values, list):
        return None
    return next((item for item in values if isinstance(item, dict) and item.get("title") == title), None)


def _create_native_session(base_url: str, label: str, marker: str) -> tuple[str, int]:
    """Create once; reconcile a lost response through the native session list before retrying."""
    title = f"PMT native smoke {label} {marker}"
    last_status = None
    last_details = {}
    for attempt in range(3):
        try:
            created, last_status = _http_json(base_url, "POST", "/session", {"title": title})
            session = created.get("data", created) if isinstance(created, dict) else {}
            session_id = session.get("id") or session.get("sessionID")
            if last_status == 200 and isinstance(session_id, str) and session_id:
                return session_id, last_status
        except SmokeFailure as exc:
            if exc.code != "opencode_http_unavailable":
                raise
            last_details = exc.details
        try:
            existing = _session_by_title(base_url, title)
        except SmokeFailure:
            existing = None
        if existing:
            session_id = existing.get("id") or existing.get("sessionID")
            if isinstance(session_id, str) and session_id:
                return session_id, 200
        if attempt < 2:
            time.sleep(0.25 * (attempt + 1))
    raise SmokeFailure("native_session_create_failed", details=last_details)


def _http_json(base_url: str, method: str, path: str, body: dict[str, Any] | None = None,
               timeout: float = 60) -> tuple[Any, int]:
    started = time.monotonic()
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = Request(base_url + path, data=data, method=method,
                      headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_HTTP_BYTES + 1)
            if len(raw) > MAX_HTTP_BYTES:
                raise SmokeFailure("opencode_api_response_too_large")
            return (json.loads(raw.decode("utf-8")) if raw else None), response.status
    except HTTPError as exc:
        error_fields = {}
        try:
            raw = exc.read(4096)
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
            candidate = parsed.get("error", parsed) if isinstance(parsed, dict) else {}
            if isinstance(candidate, dict):
                for key in ("_tag", "code", "type"):
                    value = candidate.get(key)
                    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
                        error_fields[key] = value
        except Exception:
            pass
        raise SmokeFailure(f"opencode_http_{exc.code}", details={
            "http_status": exc.code,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error_body_fields": error_fields,
        }) from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        reason = exc.reason if isinstance(exc, URLError) else exc
        raise SmokeFailure("opencode_http_unavailable", details={
            "exception_type": type(exc).__name__,
            "reason_type": type(reason).__name__,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }) from exc


def _health(base_url: str, process: subprocess.Popen, timeout: float = 60) -> tuple[str | None, int | None, float, int | None]:
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    last_code = None
    while time.monotonic() < deadline:
        process_exit = process.poll()
        if process_exit is not None:
            return None, last_code, time.monotonic() - started, process_exit
        try:
            result, status = _http_json(base_url, "GET", "/global/health", timeout=1)
            last_code = status
            version = result.get("version") if isinstance(result, dict) else None
            if status == 200 and isinstance(version, str):
                return version, status, time.monotonic() - started, None
        except SmokeFailure:
            time.sleep(0.1)
    return None, last_code, time.monotonic() - started, process.poll()


def _allocate_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((API_HOST, 0))
        return int(sock.getsockname()[1])


def _start_opencode(executable: Path, workspace: Path, env: dict[str, str], port: int) -> subprocess.Popen:
    command = [str(executable), "serve", "--hostname", API_HOST, "--port", str(port)]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.Popen(command, cwd=workspace, env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                shell=False, creationflags=creationflags)
    except OSError as exc:
        raise SmokeFailure("opencode_process_start_failed") from exc


def _stop_opencode(process: subprocess.Popen) -> int | None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    return process.returncode


def _create_local_git_root(workspace: Path) -> None:
    # Keep OpenCode's project discovery inside this fixture, away from the repository/user settings.
    git = workspace / ".git"
    (git / "objects" / "info").mkdir(parents=True)
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = false\n", encoding="utf-8")


def _write_provider_config(config_dir: Path, stub_port: int) -> None:
    config = {"$schema": "https://opencode.ai/config.json", "model": "pmt-loopback/stub-model",
              # Omitting `npm` selects OpenCode 1.18.34's bundled
              # @ai-sdk/openai-compatible provider and avoids package installation/network access.
              "provider": {"pmt-loopback": {"name": "PMT local test stub",
                                              "options": {"baseURL": f"http://{API_HOST}:{stub_port}/v1",
                                                          "apiKey": STUB_AUTH},
                                              "models": {"stub-model": {"name": "Deterministic local test stub",
                                                                          "limit": {"context": 8192, "output": 128}}}}}}
    config["permission"] = {"bash": {"*": "deny",
                                     'python -c "print(\'PMT_NATIVE_COMMAND_OK\')"': "allow"},
                             "edit": "deny", "webfetch": "deny"}
    _write_json(config_dir / "opencode.json", config)


def _install_local_package(package: Path, installed: Path, loader: Path, info: dict[str, Any]) -> str:
    if installed.exists():
        _reject_reparse(installed)
        shutil.rmtree(installed)
    shutil.copytree(package, installed, symlinks=False)
    entry = (installed / "integrations" / "opencode" / "pmt.js").resolve(strict=True)
    loader.parent.mkdir(parents=True, exist_ok=True)
    loader.write_text(f"export {{ PmtPlugin as default }} from {json.dumps(entry.as_uri())};\n", encoding="utf-8")
    if _sha256(installed / "pmt-package.json") != info["package_manifest_sha256"]:
        raise SmokeFailure("installed_package_hash_mismatch")
    return _sha256(loader)


def _write_native_diagnostic_observer(workspace: Path, run_root: Path, sentinel: str) -> Path:
    """Instrument only native hook metadata; never persist prompt/system contents."""
    observer_file = workspace / ".opencode" / "plugins" / "zzzz-pmt-diagnostic-observer.js"
    trace_path = run_root / "diagnostic-observer.jsonl"
    expected_directory = workspace.resolve()
    code = f'''import {{ appendFileSync }} from "node:fs"
import {{ resolve }} from "node:path"

const tracePath = {json.dumps(str(trace_path))}
const expectedDirectory = {json.dumps(str(expected_directory))}
const expectedSentinel = {json.dumps(sentinel)}
const record = (value) => appendFileSync(tracePath, JSON.stringify(value) + "\\n", "utf8")
const normalized = (value) => typeof value === "string" ? resolve(value) : null

export const PmtDiagnosticObserver = async ({{ directory, worktree, project }}) => {{
  record({{
    kind: "plugin_init",
    directory_matches_workspace: normalized(directory) === expectedDirectory,
    worktree_matches_workspace: normalized(worktree) === expectedDirectory,
    project_directory_present: typeof project?.directory === "string",
    project_directory_matches_workspace: normalized(project?.directory) === expectedDirectory,
  }})
  return {{
    event: async ({{ event }}) => {{
      const hookType = typeof event?.type === "string" ? event.type : "unknown"
      if (!["session.created", "session.idle", "session.error"].includes(hookType)) return
      const properties = event?.properties && typeof event.properties === "object" ? event.properties : {{}}
      const info = properties.info && typeof properties.info === "object" ? properties.info : {{}}
      record({{
        kind: "event",
        hook_type: hookType,
        session_id_present: typeof properties.sessionID === "string" || typeof info.id === "string",
        session_id_type: typeof properties.sessionID,
        info_id_type: typeof info.id,
        allowed_property_keys: Object.keys(properties).filter((key) => ["sessionID", "eventID", "status", "tool", "time", "info"].includes(key)).sort(),
      }})
    }},
    "experimental.chat.system.transform": async (input, output) => {{
      const system = Array.isArray(output?.system) ? output.system : []
      const text = system.filter((part) => typeof part === "string").join("\\n")
      record({{
        kind: "system_transform",
        hook_type: "experimental.chat.system.transform",
        session_id_present: typeof input?.sessionID === "string",
        context_chars: text.length,
        sentinel_seen: text.includes(expectedSentinel),
      }})
    }},
  }}
}}

export default PmtDiagnosticObserver
'''
    observer_file.parent.mkdir(parents=True, exist_ok=True)
    observer_file.write_text(code, encoding="utf-8")
    return trace_path


def _read_native_diagnostic_observer(trace_path: Path) -> list[dict[str, Any]]:
    if not trace_path.is_file():
        return []
    results = []
    for line in trace_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("kind") in {"plugin_init", "event", "system_transform"}:
            results.append(item)
    return results


def _remove_local_package(installed: Path, loader: Path, owned_root: Path) -> None:
    root = owned_root.resolve(strict=True)
    for target in (installed, loader):
        if not target.exists() and not target.is_symlink():
            continue
        absolute = target.absolute()
        if not _inside(absolute, root):
            raise SmokeFailure("refusing_unowned_remove", exit_code=2)
        _reject_reparse(target)
    if loader.exists():
        loader.unlink()
    if installed.exists():
        shutil.rmtree(installed)


def _native_session(base_url: str, process: subprocess.Popen, db_file: Path, stub: _StubServer, pmt: dict[str, Any],
                    label: str, *, send_prompt: bool) -> dict[str, Any]:
    marker = "NATIVE_MARKER_" + uuid.uuid4().hex.upper()
    session_id = None
    event_id = None
    status = None
    message_status = None
    event_result = None
    context_seen = None
    response_seen = None
    observation = None
    try:
        session_id, status = _create_native_session(base_url, label, marker)
        event_result = _wait_native_event(db_file, session_id, "session_started", timeout=15)
        if not event_result:
            raise SmokeFailure("native_session_event_missing")
        event_id = event_result["event_id"]
        if send_prompt:
            message, message_status = _http_json(base_url, "POST", f"/session/{quote(session_id, safe='')}/message",
                                                 {"parts": [{"type": "text", "text": marker + " Reply with the fixed short acknowledgement."}]},
                                                 timeout=90)
            if message_status != 200 or not isinstance(message, dict):
                raise SmokeFailure("native_prompt_failed")
            observation = _stub_observation(stub, marker)
            context_seen = bool(observation and observation["context_seen"])
            response_seen = _contains_text(message, STUB_ACK)
            if observation and not observation["auth_seen"]:
                raise SmokeFailure("stub_auth_not_seen")
        state = _item_state(db_file, pmt["item_record_id"])
        if state != "Planned":
            raise SmokeFailure("native_session_marked_item_done")
        if send_prompt and not response_seen:
            raise SmokeFailure("native_response_not_confirmed")
        if send_prompt and not context_seen:
            return {"label": label, "status": "partial", "session_id": session_id,
                    "session_started_event_id": event_id, "context_seen": False,
                    "assistant_response_seen": response_seen, "item_state_after_prompt": state,
                    "session_http_status": status, "prompt_http_status": message_status,
                    "pmt_bridge_exit_code": event_result["pmt_bridge_exit_code"],
                    "system_instruction_present": observation["system_instruction_present"] if observation else False,
                    "system_instruction_chars": observation["system_instruction_chars"] if observation else 0,
                    "message_roles": observation["message_roles"] if observation else [],
                    "request_kind": observation["request_kind"] if observation else None,
                    "error_code": "startup_context_not_observed"}
        return {"label": label, "status": "pass", "session_id": session_id,
                "session_started_event_id": event_id, "context_seen": context_seen,
                "assistant_response_seen": response_seen, "item_state_after_prompt": state,
                "session_http_status": status, "prompt_http_status": message_status,
                "system_instruction_present": observation["system_instruction_present"] if observation else False,
                "system_instruction_chars": observation["system_instruction_chars"] if observation else 0,
                "message_roles": observation["message_roles"] if observation else [],
                "request_kind": observation["request_kind"] if observation else None,
                "pmt_bridge_exit_code": event_result["pmt_bridge_exit_code"]}
    except SmokeFailure as exc:
        server_alive = process.poll() is None
        health_after_failure = False
        if server_alive:
            try:
                health_payload, health_code = _http_json(base_url, "GET", "/global/health", timeout=2)
                health_after_failure = health_code == 200 and isinstance(health_payload, dict)
            except SmokeFailure:
                health_after_failure = False
        return {"label": label, "status": "blocked", "session_id": session_id,
                "session_started_event_id": event_id, "context_seen": False if send_prompt else None,
                "assistant_response_seen": False if send_prompt else None,
                "system_instruction_present": observation["system_instruction_present"] if observation else False,
                "system_instruction_chars": observation["system_instruction_chars"] if observation else 0,
                "message_roles": observation["message_roles"] if observation else [],
                "request_kind": observation["request_kind"] if observation else None,
                "session_http_status": status, "prompt_http_status": message_status,
                "pmt_bridge_exit_code": event_result.get("pmt_bridge_exit_code") if event_result else None,
                "server_alive_after_failure": server_alive,
                "server_health_after_failure": health_after_failure,
                "http_failure_details": exc.details,
                "error_code": exc.code, "item_state_after_prompt": _item_state(db_file, pmt["item_record_id"])}


def _product_tool_exit(message_collection: Any, marker: str) -> dict[str, Any] | None:
    values = message_collection if isinstance(message_collection, list) else [message_collection]
    for message in values:
        if not isinstance(message, dict):
            continue
        for part in message.get("parts", []) if isinstance(message.get("parts"), list) else []:
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            output = state.get("output") if isinstance(state.get("output"), str) else ""
            if marker not in output:
                continue
            metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
            exit_code = next((metadata[key] for key in ("exitCode", "exit_code", "exit", "code")
                              if isinstance(metadata.get(key), int) and not isinstance(metadata.get(key), bool)), None)
            return {"tool_name": part.get("tool"), "state": state.get("status"),
                    "exit_code": exit_code, "command_output_marker_seen": True}
    return None


def _native_command_phase(base_url: str, process: subprocess.Popen, db_file: Path,
                          stub: _StubServer, pmt: dict[str, Any], label: str) -> dict[str, Any]:
    marker = "NATIVE_COMMAND_MARKER_" + uuid.uuid4().hex.upper()
    session_id = None
    session_event = None
    tool_event = None
    idle_event = None
    prompt_status = None
    try:
        session_id, _ = _create_native_session(base_url, label, marker)
        session_event = _wait_native_event(db_file, session_id, "session_started", timeout=20)
        if not session_event:
            raise SmokeFailure("native_session_event_missing")
        final_message, prompt_status = _http_json(base_url, "POST", f"/session/{quote(session_id, safe='')}/message",
                                                  {"agent": "build", "tools": {"bash": True},
                                                   "parts": [{"type": "text", "text": marker +
                                                              " Run exactly one command: python -c \"print('PMT_NATIVE_COMMAND_OK')\". "
                                                              "Do not edit files or run other commands."}]}, timeout=120)
        assistant_ack_seen = _contains_text(final_message, STUB_ACK)
        observations = _stub_observations(stub, marker, timeout=15)
        command_observation = next((row for row in observations if row.get("command_tool_selected")), None)
        reply_observation = next((row for row in observations if row.get("tool_result_seen")), None)
        if (prompt_status != 200 or not assistant_ack_seen or not command_observation or not reply_observation
                or not reply_observation.get("command_result_marker_seen")):
            raise SmokeFailure("local_stub_did_not_select_bash")
        messages, status = _http_json(base_url, "GET", f"/session/{quote(session_id, safe='')}/message?limit=50")
        if status != 200:
            raise SmokeFailure("native_message_history_unavailable")
        command_result = _product_tool_exit(messages, "PMT_NATIVE_COMMAND_OK")
        if not command_result or command_result.get("exit_code") != 0 or command_result.get("state") != "completed":
            raise SmokeFailure("native_command_result_not_zero")
        tool_event = _wait_native_event(db_file, session_id, "tool_completed", timeout=20)
        idle_event = _wait_native_event(db_file, session_id, "session_idle", timeout=20)
        if not tool_event:
            raise SmokeFailure("native_tool_event_missing")
        in_progress = _item_state(db_file, pmt["item_record_id"])
        if in_progress != "In Progress":
            raise SmokeFailure("native_idle_changed_item_state")
        if not idle_event:
            raise SmokeFailure("native_idle_event_missing")
        return {"label": label, "status": "pass", "session_id": session_id,
                "session_http_event_id": session_event["event_id"],
                "tool_completed_event_id": tool_event["event_id"],
                "session_idle_event_id": idle_event["event_id"],
                "pmt_bridge_exit_codes": {"session_started": session_event["pmt_bridge_exit_code"],
                                          "tool_completed": tool_event["pmt_bridge_exit_code"],
                                          "session_idle": idle_event["pmt_bridge_exit_code"]},
                "prompt_http_status": prompt_status, "tool_result": command_result,
                "assistant_ack_seen": assistant_ack_seen,
                "stub_tool_selected": command_observation.get("command_tool_selected"),
                "stub_tool_reply_seen": reply_observation.get("tool_result_seen"),
                "stub_tool_result_marker_seen": reply_observation.get("command_result_marker_seen"),
                "context_seen": command_observation.get("context_seen") and reply_observation.get("context_seen"),
                "item_state_after_native_idle": in_progress}
    except SmokeFailure as exc:
        return {"label": label, "status": "blocked", "session_id": session_id,
                "error_code": exc.code, "http_failure_details": exc.details,
                "session_started_event_id": session_event.get("event_id") if session_event else None,
                "tool_completed_event_id": tool_event.get("event_id") if tool_event else None,
                "session_idle_event_id": idle_event.get("event_id") if idle_event else None,
                "prompt_http_status": prompt_status,
                "assistant_ack_seen": False,
                "item_state_after_native_idle": _item_state(db_file, pmt["item_record_id"])}


def _contains_text(message: Any, needle: str) -> bool:
    if not isinstance(message, dict):
        return False
    message = message.get("data", message)
    if not isinstance(message, dict):
        return False
    parts = message.get("parts", [])
    return any(isinstance(part, dict) and needle in str(part.get("text", "")) for part in parts)


def _item_state(db_file: Path, record_id: str) -> str | None:
    try:
        with sqlite3.connect(db_file, timeout=2) as conn:
            row = conn.execute("SELECT state FROM records WHERE id=?", (record_id,)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _wait_native_event(db_file: Path, session_id: str, event_type: str, timeout: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with sqlite3.connect(db_file, timeout=1) as conn:
                requests = conn.execute("SELECT response_json,exit_code FROM requests WHERE actor='hook' AND session_id=?",
                                        (session_id,)).fetchall()
                for response_text, exit_code in requests:
                    response = json.loads(response_text)
                    event_id = (response.get("result") or {}).get("event_id")
                    if not event_id:
                        continue
                    event = conn.execute("SELECT id FROM events WHERE event_id=? AND event_type=? AND actor='hook'",
                                         (event_id, event_type)).fetchone()
                    if event:
                        return {"event_id": event_id, "pmt_bridge_exit_code": exit_code}
        except (sqlite3.Error, ValueError):
            pass
        time.sleep(0.05)
    return None


def _wait_no_native_event(db_file: Path, session_id: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with sqlite3.connect(db_file, timeout=1) as conn:
                row = conn.execute("SELECT 1 FROM requests WHERE actor='hook' AND session_id=? LIMIT 1",
                                   (session_id,)).fetchone()
            if row:
                return False
        except sqlite3.Error:
            return False
        time.sleep(0.05)
    return True


def _run_product_phase(executable: Path, workspace: Path, env: dict[str, str], db_file: Path,
                       stub: _StubServer, pmt: dict[str, Any], label: str, count: int,
                       *, prompt: bool) -> dict[str, Any]:
    port = _allocate_port()
    base_url = f"http://{API_HOST}:{port}"
    result: dict[str, Any] = {"phase": label, "opencode_version": None,
                              "health_http_status": None, "startup_seconds": None,
                              "server_exit_before_cleanup": None, "sessions": [], "server_exit_code": None}
    process = None
    try:
        process = _start_opencode(executable, workspace, env, port)
        version, health_status, elapsed, early_exit = _health(base_url, process)
        result.update(opencode_version=version, health_http_status=health_status,
                      startup_seconds=round(elapsed, 3), server_exit_before_cleanup=early_exit)
        if version != EXPECTED_OPENCODE_VERSION:
            raise SmokeFailure("opencode_server_exited_before_health" if early_exit is not None
                               else "opencode_health_timeout_or_version_mismatch")
        for index in range(count):
            session = _native_session(base_url, process, db_file, stub, pmt,
                                      f"{label}-{index + 1}", send_prompt=prompt)
            result["sessions"].append(session)
        statuses = [session["status"] for session in result["sessions"]]
        result["status"] = "pass" if statuses and all(value == "pass" for value in statuses) else "partial"
    except SmokeFailure as exc:
        result.update(status="blocked", error_code=exc.code)
    finally:
        if process is not None:
            result["server_exit_before_cleanup"] = process.poll()
            result["server_exit_code"] = _stop_opencode(process)
            result["server_stopped"] = process.poll() is not None
    return result


def _run_removed_phase(executable: Path, workspace: Path, env: dict[str, str], db_file: Path,
                       pmt: dict[str, Any]) -> dict[str, Any]:
    port = _allocate_port()
    base_url = f"http://{API_HOST}:{port}"
    result: dict[str, Any] = {"phase": "remove", "opencode_version": None,
                              "health_http_status": None, "startup_seconds": None,
                              "server_exit_before_cleanup": None, "server_exit_code": None}
    process = None
    try:
        process = _start_opencode(executable, workspace, env, port)
        version, health_status, elapsed, early_exit = _health(base_url, process)
        result.update(opencode_version=version, health_http_status=health_status,
                      startup_seconds=round(elapsed, 3), server_exit_before_cleanup=early_exit)
        if version != EXPECTED_OPENCODE_VERSION:
            raise SmokeFailure("opencode_server_exited_before_health" if early_exit is not None
                               else "opencode_health_timeout_or_version_mismatch")
        session_id, status = _create_native_session(base_url, "remove-no-plugin", "NATIVE_MARKER_REMOVE_" + uuid.uuid4().hex.upper())
        absent = _wait_no_native_event(db_file, session_id, 2.0)
        result.update(status="pass" if absent else "partial", session_id=session_id,
                      pmt_event_absent=absent, context_seen=None,
                      item_state_after_session=_item_state(db_file, pmt["item_record_id"]))
        result["sessions"] = [{"label": "remove-no-plugin-session", "session_id": session_id,
                               "status": result["status"], "pmt_event_absent": absent,
                               "context_seen": None, "assistant_response_seen": None}]
    except SmokeFailure as exc:
        result.update(status="blocked", error_code=exc.code)
    finally:
        if process is not None:
            result["server_exit_before_cleanup"] = process.poll()
            result["server_exit_code"] = _stop_opencode(process)
            result["server_stopped"] = process.poll() is not None
    return result


def _identity_hash(path: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(path.resolve())).encode("utf-8")).hexdigest()


def _collect_safe_network_log_summary(run_root: Path) -> dict[str, Any]:
    log_file = run_root / "xdg-data" / "opencode" / "log" / "opencode.log"
    summary = {"model_stub_calls_target_loopback": True, "external_registry_refused": 0,
               "model_catalog_refused": 0, "unclassified_external_refused": 0,
               "raw_product_logs_retained": False}
    if not log_file.is_file():
        return summary
    try:
        for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if "ECONNREFUSED" not in line:
                continue
            lower = line.lower()
            if "registry.npmjs.org" in lower:
                summary["external_registry_refused"] += 1
            elif "models.dev" in lower:
                summary["model_catalog_refused"] += 1
            else:
                summary["unclassified_external_refused"] += 1
    finally:
        # The evidence keeps only endpoint classes and counts; OpenCode logs are not part of delivery.
        log_file.unlink(missing_ok=True)
    return summary


def run_native_smoke(package_010: Path, package_011: Path, opencode_wrapper: Path,
                     python: Path, *, root: Path = TEST_ROOT, single_phase: bool = False) -> dict[str, Any]:
    started_at = _utc_now()
    root = root.resolve()
    if not _inside(root, PROJECT_ROOT.resolve()):
        raise SmokeFailure("test_root_outside_workspace", exit_code=2)
    root.mkdir(parents=True, exist_ok=True)
    run_root = root / ("run-" + uuid.uuid4().hex)
    run_root.mkdir()
    workspace = run_root / "workspace"
    workspace.mkdir()
    _create_local_git_root(workspace)
    install_root = run_root / "install"
    install_root.mkdir()
    installed = install_root / "active"
    loader = workspace / ".opencode" / "plugins" / "loader.js"
    package010 = _package_info(package_010, VERSION_010)
    package011 = _package_info(package_011, VERSION_011)
    executable, executable_entry = _resolve_opencode_executable(opencode_wrapper)
    pmt = _bootstrap_pmt(package_010, python, run_root, workspace)
    db_file = pmt["data_root"] / "pmt.sqlite3"
    observer_trace = _write_native_diagnostic_observer(workspace, run_root, pmt["sentinel"])
    stub = _StubServer(pmt["sentinel"])
    stub_thread = threading.Thread(target=stub.serve_forever, name="pmt-loopback-stub", daemon=True)
    stub_thread.start()
    config_dir = run_root / "opencode-config"
    _write_provider_config(config_dir, stub.server_address[1])
    env = _safe_child_environment(run_root, python=python, data_root=pmt["data_root"],
                                  config_root=pmt["config_root"], scope_id=pmt["scope_id"],
                                  record_id=pmt["item_record_id"], stub_port=stub.server_address[1])
    phases: list[dict[str, Any]] = []
    try:
        loader_hash = _install_local_package(package_010, installed, loader, package010)
        install010 = {"phase": "install", "package_version": VERSION_010, **package010,
                      "registration": "project-local-loader-imports-bundled-plugin", "loader_sha256": loader_hash}
        initial = _run_product_phase(executable, workspace, env, db_file, stub, pmt, "install-0.1.0", 2, prompt=True)
        install010.update(status=initial["status"], sessions=initial["sessions"],
                          opencode_version=initial["opencode_version"],
                          health_http_status=initial["health_http_status"],
                          startup_seconds=initial["startup_seconds"],
                          server_exit_before_cleanup=initial["server_exit_before_cleanup"],
                          server_exit_code=initial["server_exit_code"], server_stopped=initial.get("server_stopped"))
        phases.append(install010)

        if not single_phase:
            loader_hash = _install_local_package(package_011, installed, loader, package011)
            updated = _run_product_phase(executable, workspace, env, db_file, stub, pmt, "update-0.1.1", 1, prompt=True)
            phases.append({"phase": "update", "package_version": VERSION_011, **package011,
                           "registration": "project-local-loader-imports-bundled-plugin", "loader_sha256": loader_hash,
                           "status": updated["status"], "sessions": updated["sessions"],
                           "opencode_version": updated["opencode_version"], "health_http_status": updated["health_http_status"],
                           "startup_seconds": updated["startup_seconds"],
                           "server_exit_before_cleanup": updated["server_exit_before_cleanup"],
                           "server_exit_code": updated["server_exit_code"], "server_stopped": updated.get("server_stopped")})

            loader_hash = _install_local_package(package_010, installed, loader, package010)
            rollback = _run_product_phase(executable, workspace, env, db_file, stub, pmt, "rollback-0.1.0", 1, prompt=True)
            phases.append({"phase": "rollback", "package_version": VERSION_010, **package010,
                           "registration": "project-local-loader-imports-bundled-plugin", "loader_sha256": loader_hash,
                           "status": rollback["status"], "sessions": rollback["sessions"],
                           "opencode_version": rollback["opencode_version"], "health_http_status": rollback["health_http_status"],
                           "startup_seconds": rollback["startup_seconds"],
                           "server_exit_before_cleanup": rollback["server_exit_before_cleanup"],
                           "server_exit_code": rollback["server_exit_code"], "server_stopped": rollback.get("server_stopped")})

            _remove_local_package(installed, loader, run_root)
            removed = _run_removed_phase(executable, workspace, env, db_file, pmt)
            removed["package_removed"] = not installed.exists()
            removed["loader_removed"] = not loader.exists()
            phases.append(removed)

            loader_hash = _install_local_package(package_010, installed, loader, package010)
            reinstall = _run_product_phase(executable, workspace, env, db_file, stub, pmt, "reinstall-0.1.0", 1, prompt=True)
            phases.append({"phase": "reinstall", "package_version": VERSION_010, **package010,
                           "registration": "project-local-loader-imports-bundled-plugin", "loader_sha256": loader_hash,
                           "status": reinstall["status"], "sessions": reinstall["sessions"],
                           "opencode_version": reinstall["opencode_version"], "health_http_status": reinstall["health_http_status"],
                           "startup_seconds": reinstall["startup_seconds"],
                           "server_exit_before_cleanup": reinstall["server_exit_before_cleanup"],
                           "server_exit_code": reinstall["server_exit_code"], "server_stopped": reinstall.get("server_stopped")})
    finally:
        stub.shutdown()
        stub.server_close()
        stub_thread.join(timeout=5)

    final_state = _retained_state(db_file, pmt)
    preservation = (final_state["db_id"] == pmt["db_id"]
                    and final_state["retained_record_ids"] == sorted(pmt["retained_record_ids"])
                    and final_state["artifact_sha256"] == pmt["artifact_sha256"]
                    and final_state["artifact_file_sha256"] == pmt["artifact_sha256"]
                    and final_state["artifact_state"] == "ready"
                    and final_state["item_state"] == "Planned")
    if not preservation:
        phases.append({"phase": "data-preservation", "status": "partial", "result": final_state})
    else:
        phases.append({"phase": "data-preservation", "status": "pass", "result": final_state})
    marker_sessions = [session for phase in phases for session in phase.get("sessions", [])]
    expected_native = [session for phase in phases if phase.get("phase") != "remove" for session in phase.get("sessions", [])]
    expected_ok = all(session.get("status") == "pass" for session in expected_native)
    removed_ok = single_phase or next((phase.get("status") == "pass" for phase in phases if phase.get("phase") == "remove"), False)
    network_summary = _collect_safe_network_log_summary(run_root)
    overall = "pass" if expected_ok and removed_ok and preservation else "partial"
    return {"evidence_version": 1, "run_mode": "single-phase-diagnostic" if single_phase else "lifecycle-matrix",
            "started_at_utc": started_at, "overall_status": overall,
            "exit_code": 0 if overall == "pass" else 2,
            "opencode": {"requested_version": EXPECTED_OPENCODE_VERSION, "entry_wrapper": Path(executable_entry).name,
                         "launched_binary": executable.name,
                         "version_checked_by_server_health": True},
            "native_product_sessions": True,
            "model_stubbed": True,
            "model_provider_mode": "OpenCode-bundled @ai-sdk/openai-compatible via baseURL override",
            "model_provider_network": "configured loopback only",
            "stub_transport": f"http://{API_HOST}:{stub.server_address[1]}/v1",
            "stub_auth_configured": True,
            "stub_auth_observed": any(row["auth_seen"] for row in stub.observations),
            "network_summary": network_summary,
            "workspace": {"binding": "isolated OpenCode product workspace", "identity_sha256": _identity_hash(workspace)},
            "pmt": {"db_id": pmt["db_id"], "environment_id": pmt["environment_id"],
                    "scope_id": pmt["scope_id"], "workspace_record_id": pmt["context_record_id"],
                    "item_record_id": pmt["item_record_id"], "retained_record_ids": pmt["retained_record_ids"],
                    "retained_artifact_id": pmt["artifact_id"], "retained_artifact_sha256": pmt["artifact_sha256"],
                    "preservation": preservation, "command_exit_codes": pmt["pmt_command_exit_codes"]},
            "phases": phases,
            "native_context_observations": [{"marker": row["marker"], "context_seen": row["context_seen"],
                                              "stub_auth_seen": row["auth_seen"],
                                              "system_instruction_present": row["system_instruction_present"],
                                              "system_instruction_chars": row["system_instruction_chars"],
                                              "message_roles": row["message_roles"],
                                              "request_kind": row["request_kind"]} for row in stub.observations],
            "native_hook_diagnostic_observations": _read_native_diagnostic_observer(observer_trace),
            "session_ids": [session.get("session_id") for session in marker_sessions if session.get("session_id")],
            "event_ids": [session.get("session_started_event_id") for session in marker_sessions
                          if session.get("session_started_event_id")],
            "run_evidence_path": str(run_root / "evidence.json"),
            "diagnostic_logs": "product stdout/stderr and full HTTP messages are not retained"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-010", type=Path, help="Prebuilt OpenCode package directory for 0.1.0")
    parser.add_argument("--package-011", type=Path, help="Prebuilt OpenCode package directory for 0.1.1")
    parser.add_argument("--opencode-cmd", type=Path, default=PROJECT_ROOT / ".pmt-test" / "tooling" / "node_modules" / ".bin" / "opencode.cmd")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--execute-native", action="store_true",
                        help="Required to run OpenCode; do not use before the explicit G1/P9 go-ahead")
    parser.add_argument("--single-phase", action="store_true",
                        help="Run only fresh-profile install 0.1.0 with two native sessions; useful for bounded diagnostics")
    args = parser.parse_args(argv)
    if not args.execute_native:
        print(json.dumps({"ok": False, "status": "not_run", "error_code": "explicit_native_execution_flag_required"}))
        return 2
    if not args.package_010 or not args.package_011:
        print(json.dumps({"ok": False, "status": "blocked", "error_code": "prebuilt_package_paths_required"}))
        return 4
    try:
        python = args.python.resolve(strict=True)
        package010 = _package_info(args.package_010, VERSION_010)
        package011 = _package_info(args.package_011, VERSION_011)
        evidence = run_native_smoke(args.package_010, args.package_011, args.opencode_cmd, python,
                                    single_phase=args.single_phase)
        evidence["package_versions"] = {
            "0.1.0": {"package_archive_sha256": package010["package_archive_sha256"],
                      "package_manifest_sha256": package010["package_manifest_sha256"]},
            "0.1.1": {"package_archive_sha256": package011["package_archive_sha256"],
                      "package_manifest_sha256": package011["package_manifest_sha256"]},
        }
        evidence_path = Path(evidence["run_evidence_path"])
        _write_json(evidence_path, evidence)
        print(json.dumps({"ok": evidence["overall_status"] == "pass", "status": evidence["overall_status"],
                          "exit_code": evidence["exit_code"], "evidence_path": str(evidence_path),
                          "package_hashes": evidence["package_versions"], "session_ids": evidence["session_ids"],
                          "event_ids": evidence["event_ids"]}, ensure_ascii=False))
        return int(evidence["exit_code"])
    except SmokeFailure as exc:
        result = {"ok": False, "status": "blocked", "error_code": exc.code, "exit_code": exc.exit_code}
        print(json.dumps(result, ensure_ascii=False))
        return exc.exit_code
    except Exception as exc:
        result = {"ok": False, "status": "blocked", "error_code": "native_smoke_internal_" + type(exc).__name__,
                  "exit_code": 5}
        print(json.dumps(result, ensure_ascii=False))
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
