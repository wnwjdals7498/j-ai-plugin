"""Isolated Codex plugin installation and native-session smoke test.

Use after G1 and a built bundle. The local model server is a deterministic
fixture, not a real LLM. Review the exact plugin hooks with `/hooks` in the
reported isolated CODEX_HOME before `--session`; no trust bypass is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import uuid

SENTINEL = "PMT_NATIVE_CODEX_SENTINEL_7F42"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--model-port", type=int, required=True)
    parser.add_argument("--session", action="store_true")
    args = parser.parse_args()
    root, package = args.root.resolve(), args.package.resolve(strict=True)
    try:
        root.relative_to((Path(__file__).resolve().parents[1] / ".pmt-test").resolve())
    except ValueError:
        raise SystemExit("Native test root must stay inside this project's .pmt-test directory")
    root.mkdir(parents=True, exist_ok=True)
    profile, workspace = root / "profile", root / "workspace"
    profile.mkdir(exist_ok=True)
    workspace.mkdir(exist_ok=True)
    (workspace / "app.py").write_text("assert 1 + 1 == 2\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(CODEX_HOME=str(profile), PMT_DATA_ROOT=str(root / "data"), PMT_CONFIG_ROOT=str(root / "config"),
               PMT_PYTHON=sys.executable, PMT_PRODUCT_VERSION="0.156.1", PYTHONUTF8="1")
    state_file = root / "state.json"
    codex = shutil.which("codex.cmd") or shutil.which("codex")
    if not codex:
        raise SystemExit("Codex executable unavailable")
    config = profile / "config.toml"
    if not config.exists():
        config.write_text(f'''model = "pmt-native-fixture"
model_provider = "pmt_native_fixture"
project_root_markers = []
web_search = "disabled"
[model_providers.pmt_native_fixture]
name = "PMT deterministic local test fixture"
base_url = "http://127.0.0.1:{args.model_port}/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 5000
''', encoding="utf-8")

    def command(argv, timeout=30):
        return subprocess.run(argv, env=env, cwd=workspace, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, check=False)

    def pmt(operation, payload=None, **fields):
        installed_launchers = list((profile / "plugins" / "cache").glob("**/scripts/pmt.py"))
        if len(installed_launchers) != 1:
            raise RuntimeError("A unique installed Codex PMT launcher was not found")
        request = {"protocol_version": 1, "operation": operation, "request_id": str(uuid.uuid4()),
                   "actor": "main", "session_id": "codex-native-test-main", "payload": payload or {}, **fields}
        process = subprocess.run([sys.executable, str(installed_launchers[0])], env=env, cwd=workspace,
                                 input=json.dumps(request), text=True, capture_output=True, timeout=10)
        result = json.loads(process.stdout)
        if process.returncode != 0 or not result["ok"]:
            raise RuntimeError(f"PMT {operation}: {result.get('error', {}).get('code')}")
        return result["result"]

    if not args.session:
        registration = command([codex, "plugin", "marketplace", "add", str(package), "--json"])
        installed = command([codex, "plugin", "add", "pmt-lifecycle@pmt-local", "--json"])
        listing = command([codex, "plugin", "list", "--json"])
        for name, result in (("marketplace", registration), ("install", installed), ("list", listing)):
            (root / f"{name}-result.json").write_text(json.dumps({"exit_code": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr}, indent=2) + "\n", encoding="utf-8")
        if registration.returncode or installed.returncode:
            print(json.dumps({"result": "blocked", "reason": "native_plugin_registration_failed"}))
            return 2
        setup = pmt("setup", {"product": "codex"})
        scope = pmt("create_scope", {"kind": "project", "slug": "native-codex"})["scope_id"]
        item = pmt("save_change", {"kind": "item", "title": "Native Codex preserves unfinished work",
                   "reason": "isolated native integration test", "body": {"criteria": ["NATIVE-01"],
                   "workspace": str(workspace), "next": SENTINEL}}, scope_id=scope)
        decision = pmt("save_decision", {"decision_kind": "custom", "decider": "test-user",
                       "content": SENTINEL, "reason": "explicit test fixture choice",
                       "confirmation_source": "native_integration_fixture"},
                       record_id=item["id"], expected_revision=item["revision"])
        state = {"db_id": setup["db_id"], "scope_id": scope, "record_id": item["id"],
                 "revision": decision["revision"], "profile": str(profile), "workspace": str(workspace),
                 "real_llm": False, "package_sha256": hashlib.sha256((package.parent / "codex.zip").read_bytes()).hexdigest()}
        state_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"result": "installed_pending_hook_review", "profile": str(profile),
                          "workspace": str(workspace), "scope_id": scope, "record_id": item["id"]}))
        return 0
    state = json.loads(state_file.read_text(encoding="utf-8"))
    env["PMT_SCOPE_ID"] = state["scope_id"]
    env["PMT_RECORD_ID"] = state["record_id"]
    session = command([codex, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--json",
                       "Return the available project memory summary. Do not call tools or change files."], timeout=45)
    rows = []
    for line in session.stdout.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        # Only event types, native session IDs and sentinel confirmation are retained.
        rows.append({"type": value.get("type"), "thread_id": value.get("thread_id"),
                     "context_confirmed": "PMT_NATIVE_CONTEXT_OK" in line})
    with sqlite3.connect(root / "data" / "pmt.sqlite3") as connection:
        status = connection.execute("SELECT state FROM records WHERE id=?", (state["record_id"],)).fetchone()[0]
        native_events = [row[0] for row in connection.execute("SELECT event_type FROM events WHERE actor='hook'")]
    result = {"exit_code": session.returncode, "real_llm": False, "events": rows,
              "context_confirmed": any(row["context_confirmed"] for row in rows),
              "native_events": native_events, "automatic_done": status == "Done",
              "hook_review_needed": "review" in session.stderr.lower() and "hook" in session.stderr.lower()}
    count = len(list(root.glob("session-*.json"))) + 1
    (root / f"session-{count}.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if session.returncode == 0 and result["context_confirmed"] and not result["automatic_done"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
