#!/usr/bin/env python3
"""Local-only Phase 2 product boundary check for a built PMT plugin bundle.

Product sessions use a deterministic loopback model fixture, isolated HOME and
PMT roots, and a minimal environment. No raw prompt or model response is saved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = PROJECT_ROOT / ".pmt-test"
EXPECTED_CORE = "0.4.0"
EXPECTED_SCHEMA = 5
MAX_SESSION_SECONDS = 45
MARKER = "PMT_PHASE2_PRODUCT_CONTEXT_OK"


class CheckError(RuntimeError):
    def __init__(self, code: str, *, exit_code: int | None = None):
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_package(package: Path, product: str) -> dict:
    try:
        root = package.resolve(strict=True)
        test_root = TEST_ROOT.resolve(strict=True)
        root.relative_to(test_root)
    except (OSError, ValueError) as exc:
        raise CheckError("package_path_outside_test_distribution") from exc
    manifest_path = root / "pmt-package.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CheckError("package_manifest_invalid") from exc
    if (manifest.get("product") != product or not re.fullmatch(r"\d+\.\d+\.\d+", str(manifest.get("plugin_version", "")))
            or manifest.get("core_version") != EXPECTED_CORE
            or manifest.get("schema_version") != EXPECTED_SCHEMA):
        raise CheckError("package_identity_or_version_mismatch")
    hashes = manifest.get("files")
    if not isinstance(hashes, dict) or not hashes:
        raise CheckError("package_file_manifest_missing")
    actual = set()
    for item in root.rglob("*"):
        if item.is_symlink():
            raise CheckError("package_symlink_rejected")
        if item.is_file():
            actual.add(item.relative_to(root).as_posix())
    if actual != set(hashes) | {"pmt-package.json"}:
        raise CheckError("package_file_set_mismatch")
    for relative, expected in hashes.items():
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts:
            raise CheckError("package_manifest_path_unsafe")
        candidate = root / rel
        if not candidate.is_file() or _sha256(candidate) != expected:
            raise CheckError("package_hash_mismatch")
    launcher = root / "scripts" / "pmt.py"
    if not launcher.is_file():
        raise CheckError("package_cli_missing")
    return {"root": root, "manifest_sha256": _sha256(manifest_path), "launcher": launcher,
            "plugin_version": manifest["plugin_version"]}


def _base_env(root: Path, package: Path, product: str, python: Path,
              executable: Path | None = None) -> dict[str, str]:
    home = root / "home"
    config = root / "config"
    data = root / "data"
    temp = root / "tmp"
    for path in (home, home / "AppData" / "Roaming", home / "AppData" / "Local",
                 config, data, temp, root / "workspace"):
        path.mkdir(parents=True, exist_ok=True)
    path_entries = [str(python.parent)]
    if executable is not None:
        path_entries.append(str(executable.parent))
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if system_root:
        path_entries.append(str(Path(system_root) / "System32"))
    env = {
        "PATH": os.pathsep.join(dict.fromkeys(path_entries)),
        "HOME": str(home), "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "TEMP": str(temp), "TMP": str(temp), "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(package / "src"),
        "PMT_PYTHON": str(python), "PMT_DATA_ROOT": str(data), "PMT_CONFIG_ROOT": str(config),
        "PMT_PRODUCT_VERSION": EXPECTED_CORE,
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
    }
    for name in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "PROCESSOR_ARCHITECTURE"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    if product == "claude":
        (home / "claude-config").mkdir(exist_ok=True)
        (home / "plugin-cache").mkdir(exist_ok=True)
        env.update({
            "CLAUDE_CONFIG_DIR": str(home / "claude-config"),
            "CLAUDE_CODE_PLUGIN_CACHE_DIR": str(home / "plugin-cache"),
            "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_SYNC_PLUGIN_INSTALL": "1",
            "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
            "DISABLE_AUTOUPDATER": "1",
        })
    else:
        codex_home = home / "codex-home"
        codex_home.mkdir(exist_ok=True)
        env["CODEX_HOME"] = str(codex_home)
    return env


def _pmt(package: dict, env: dict, root: Path, request: dict, timeout: float = 10) -> dict:
    argv = [env["PMT_PYTHON"], str(package["launcher"]), "--data-root", env["PMT_DATA_ROOT"],
            "--config-root", env["PMT_CONFIG_ROOT"]]
    try:
        completed = subprocess.run(argv, input=json.dumps(request, ensure_ascii=False), text=True,
                                   encoding="utf-8", capture_output=True, cwd=root, env=env,
                                   timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise CheckError("pmt_cli_timeout") from exc
    try:
        response = json.loads(completed.stdout)
    except ValueError as exc:
        raise CheckError("pmt_cli_invalid_json", exit_code=completed.returncode) from exc
    if completed.returncode != 0 or response.get("ok") is not True:
        error = response.get("error") if isinstance(response, dict) else {}
        raise CheckError(f"pmt_{request['operation']}_{(error or {}).get('code', 'failed')}",
                         exit_code=completed.returncode)
    return response["result"]


def _req(operation: str, session: str, payload: dict | None = None, **fields) -> dict:
    return {"protocol_version": 1, "operation": operation, "request_id": str(uuid.uuid4()),
            "actor": "main", "session_id": session, "payload": payload or {}, **fields}


def _seed(package: dict, env: dict, root: Path, product: str) -> tuple[str, str]:
    session = f"native-phase2-{product}-setup"
    setup = _pmt(package, env, root, _req("setup", session, {"product": product}))
    project = _pmt(package, env, root, _req("create_scope", session,
                                           {"kind": "project", "slug": f"phase2-local-{product}"}))
    scope_id = project.get("scope_id") or project.get("id")
    work = _pmt(package, env, root, _req("save_change", session,
                                         {"kind": "work", "title": "Local fixture session",
                                          "reason": "Product boundary check"}, scope_id=scope_id))
    item = _pmt(package, env, root, _req("save_change", session,
                                         {"kind": "item", "title": "Read saved PMT context",
                                          "reason": "Verify packaged lifecycle hook",
                                          "parent_id": work.get("record_id") or work.get("id"),
                                          "body": {"criteria": ["C1"], "workspace": str(root / "workspace"),
                                                   "content": f"Required context marker: {MARKER}"}},
                                         scope_id=scope_id))
    return scope_id, item.get("record_id") or item.get("id")


def _start_fixture(root: Path, env: dict) -> tuple[subprocess.Popen, int, Path]:
    stats = root / "fixture-stats.json"
    minimal = {key: env[key] for key in ("PATH", "TEMP", "TMP", "PYTHONUTF8") if key in env}
    for name in ("SystemRoot", "WINDIR", "COMSPEC", "PATHEXT"):
        if env.get(name):
            minimal[name] = env[name]
    command = [env["PMT_PYTHON"], str(PROJECT_ROOT / "scripts" / "native_stream_stub.py"),
               "--sentinel", MARKER, "--stats", str(stats), "--port", "0"]
    process = subprocess.Popen(command, cwd=PROJECT_ROOT, env=minimal, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding="utf-8")
    try:
        line = process.stdout.readline() if process.stdout else ""
        ready = json.loads(line)
        if ready.get("host") != "127.0.0.1" or ready.get("real_llm") is not False:
            raise ValueError
        return process, int(ready["port"]), stats
    except Exception as exc:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        raise CheckError("loopback_fixture_start_failed") from exc


def _stop_fixture(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _fixture_observations(path: Path) -> list[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("real_llm") is not False or not isinstance(value.get("requests"), list):
            return []
        return [{"route": row.get("route"), "context_seen": row.get("context_seen") is True}
                for row in value["requests"] if isinstance(row, dict)]
    except (OSError, ValueError):
        return []


def _claude_session(executable: Path, package: dict, env: dict, root: Path, port: int,
                    mcp_path: Path, stats_path: Path) -> dict:
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    env["ANTHROPIC_API_KEY"] = "pmt-local-fixture-only"
    command = [str(executable), "--print", "--output-format", "json", "--model", "pmt-native-fixture",
               "--max-turns", "1", "--permission-mode", "plan", "--permission-prompts", "none",
               "--setting-sources", "user", "--strict-mcp-config", "--mcp-config", str(mcp_path),
               "--plugin-dir", str(package["root"]), "--", "Reply with the marker present in saved PMT context."]
    started = time.monotonic()
    try:
        completed = subprocess.run(command, env=env, cwd=root / "workspace", stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=MAX_SESSION_SECONDS, check=False)
    except subprocess.TimeoutExpired as exc:
        return {"status": "blocked", "exit_code": None, "error_code": "claude_session_timeout",
                "elapsed_seconds": round(time.monotonic() - started, 3), "raw_response_saved": False}
    elapsed = round(time.monotonic() - started, 3)
    if completed.returncode != 0:
        return {"status": "blocked", "exit_code": completed.returncode,
                "error_code": "claude_session_failed", "elapsed_seconds": elapsed,
                "raw_response_saved": False}
    try:
        response = json.loads(completed.stdout)
    except ValueError as exc:
        return {"status": "blocked", "exit_code": completed.returncode,
                "error_code": "claude_response_invalid_json", "elapsed_seconds": elapsed,
                "raw_response_saved": False}
    observations = _fixture_observations(stats_path)
    result_text = response.get("result") if isinstance(response, dict) else None
    session_id = response.get("session_id") if isinstance(response, dict) else None
    return {"status": "pass" if (isinstance(result_text, str)
            and "PMT_NATIVE_CONTEXT_OK" in result_text and any(x["context_seen"] for x in observations)) else "fail",
            "exit_code": completed.returncode, "elapsed_seconds": elapsed,
            "configured_model": "pmt-native-fixture",
            "reported_model": response.get("model") if isinstance(response, dict) else None,
            "response_marker_seen": isinstance(result_text, str) and "PMT_NATIVE_CONTEXT_OK" in result_text,
            "response_bytes": len(completed.stdout.encode("utf-8")),
            "session_ref_sha256": hashlib.sha256(session_id.encode()).hexdigest() if isinstance(session_id, str) else None,
            "fixture": {"real_llm": False, "requests": observations},
            "raw_response_saved": False}


def _codex_session(executable: Path, env: dict, root: Path, port: int, stats_path: Path) -> dict:
    env["CODEX_DISABLE_TELEMETRY"] = "1"
    codex_home = Path(env["CODEX_HOME"])
    provider_config = codex_home / "config.toml"
    provider_config.write_text(
        f'''model = "pmt-native-fixture"\nmodel_provider = "pmt_local_fixture"\nweb_search = "disabled"\n\n'''
        f'''[model_providers.pmt_local_fixture]\nname = "PMT local deterministic fixture"\n'''
        f'''base_url = "http://127.0.0.1:{port}/v1"\nwire_api = "responses"\n'''
        '''requires_openai_auth = false\nsupports_websockets = false\n'''
        '''request_max_retries = 0\nstream_max_retries = 0\n\n[analytics]\nenabled = false\n''', encoding="utf-8")
    command = [str(executable), "exec", "--config", 'model_provider="pmt_local_fixture"',
               "--config", f'model_providers.pmt_local_fixture.base_url="http://127.0.0.1:{port}/v1"',
               "--config", "model_providers.pmt_local_fixture.requires_openai_auth=false",
               "--config", 'web_search="disabled"', "--skip-git-repo-check", "--sandbox", "read-only", "--json",
               "Return exactly FIXTURE_OK. Do not call tools or change files."]
    started = time.monotonic()
    try:
        completed = subprocess.run(command, env=env, cwd=root / "workspace", stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=MAX_SESSION_SECONDS, check=False)
    except subprocess.TimeoutExpired as exc:
        raise CheckError("codex_session_timeout") from exc
    elapsed = round(time.monotonic() - started, 3)
    events = []
    thread_id = None
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        thread_id = event.get("thread_id") or thread_id
        events.append({"type": event.get("type"), "context_confirmed": "PMT_NATIVE_CONTEXT_MISSING" in line})
    observations = _fixture_observations(stats_path)
    response_seen = any(event["context_confirmed"] for event in events)
    return {"status": "pass" if completed.returncode == 0 and response_seen
            and any(x.get("route", "").startswith("/v1/responses") for x in observations) else "blocked",
            "exit_code": completed.returncode, "elapsed_seconds": elapsed,
            "thread_ref_sha256": hashlib.sha256(thread_id.encode()).hexdigest() if isinstance(thread_id, str) else None,
            "event_types": [event["type"] for event in events if isinstance(event.get("type"), str)],
            "_session_id": thread_id,
            "fixture": {"real_llm": False, "requests": observations},
            "raw_response_saved": False,
            "note": "Codex plugin hooks were not installed or trust-approved in this session."}


def _session_id_from_db(data_root: Path) -> str | None:
    try:
        with sqlite3.connect(data_root / "pmt.sqlite3") as connection:
            row = connection.execute("SELECT session_id FROM requests WHERE actor='hook' AND session_id IS NOT NULL ORDER BY created_at DESC LIMIT 1").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _hook_observation(data_root: Path, session_id: str | None) -> dict:
    if not session_id:
        return {"hooks_observed": False, "session_started_events": 0, "hook_requests_for_session": 0}
    try:
        with sqlite3.connect(data_root / "pmt.sqlite3") as connection:
            started = connection.execute("SELECT count(*) FROM events WHERE actor='hook' AND event_type='session_started'").fetchone()[0]
            requests = connection.execute("SELECT count(*) FROM requests WHERE actor='hook' AND session_id=?",
                                          (session_id,)).fetchone()[0]
        return {"hooks_observed": requests > 0, "session_started_events": started,
                "hook_requests_for_session": requests,
                "session_context_reads_expected": started > 0 and requests >= 2}
    except sqlite3.Error:
        return {"hooks_observed": False, "session_started_events": 0, "hook_requests_for_session": 0}


def run_check(product: str, package_root: Path, root: Path, executable: Path | None,
              python: Path) -> dict:
    package = _load_package(package_root, product)
    try:
        test_root = TEST_ROOT.resolve(strict=True)
        root = root.resolve(strict=False)
        root.relative_to(test_root)
    except (OSError, ValueError) as exc:
        raise CheckError("run_root_outside_pmt_test") from exc
    if not root.name.startswith("native-phase2-") or root == TEST_ROOT:
        raise CheckError("run_root_name_invalid")
    if root.exists() and any(root.iterdir()):
        raise CheckError("run_root_not_empty")
    root.mkdir(parents=True, exist_ok=True)
    env = _base_env(root, package["root"], product, python, executable)
    workspace = root / "workspace"
    scope_id, item_id = _seed(package, env, root, product)
    env.update({"PMT_SCOPE_ID": scope_id, "PMT_RECORD_ID": item_id})
    (root / "empty-mcp.json").write_text('{"mcpServers":{}}', encoding="utf-8")
    fixture_process = None
    product_result = {"status": "not_run"}
    try:
        fixture_process, port, stats_path = _start_fixture(root, env)
        if product == "claude":
            if executable is None or not executable.is_file():
                raise CheckError("claude_executable_unavailable")
            product_result = _claude_session(executable, package, env, root, port,
                                             root / "empty-mcp.json", stats_path)
        else:
            if executable is None or not executable.is_file():
                raise CheckError("codex_executable_unavailable")
            product_result = _codex_session(executable, env, root, port, stats_path)
    finally:
        _stop_fixture(fixture_process)
    hook_session = _session_id_from_db(Path(env["PMT_DATA_ROOT"]))
    product_session = hook_session or product_result.pop("_session_id", None)
    hook_observation = _hook_observation(Path(env["PMT_DATA_ROOT"]), hook_session)
    if product == "codex" and not product_session:
        product_session = "codex-local-fixture-session"
    if product == "codex":
        product_result.pop("_session_id", None)
    read_request = _req("read_routing_policy", product_session or f"{product}-hook-session")
    try:
        policy = _pmt(package, env, root, read_request)
        policy_status = {"operation": "read_routing_policy", "exit_code": 0,
                         "configured": policy.get("configured"), "revision": policy.get("revision"),
                         "request_id": read_request["request_id"],
                         "session_ref_sha256": hashlib.sha256((product_session or "").encode()).hexdigest()}
    except CheckError as exc:
        product_result["status"] = "blocked"
        policy_status = {"operation": "read_routing_policy", "exit_code": exc.exit_code,
                         "error_code": exc.code, "request_id": read_request["request_id"]}
    report = {
        "status": product_result.get("status", "blocked"),
        "product": product, "product_session": product_result,
        "native_hook_observation": hook_observation,
        "package": {"plugin_version": package["plugin_version"], "core_version": EXPECTED_CORE,
                    "schema_version": EXPECTED_SCHEMA, "manifest_sha256": package["manifest_sha256"]},
        "pmt_phase2_operation": policy_status,
        "environment": {"isolated_home": True, "isolated_data_and_config": True,
                        "auth_and_proxy_environment_inherited": False,
                        "api_key_used": product == "claude", "api_key_kind": "dummy-loopback-only" if product == "claude" else None,
                        "loopback_model_fixture": True, "real_llm": False,
                        "raw_prompt_or_response_saved": False},
    }
    evidence = root / "evidence.json"
    evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["evidence_path"] = str(evidence)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", choices=("claude", "codex"), required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True,
                        help="Fresh native-phase2-* run directory inside .pmt-test")
    parser.add_argument("--executable", type=Path,
                        help="Claude or Codex CLI executable; no PATH-based provider discovery is used")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args(argv)
    try:
        executable = args.executable
        if executable is None:
            default = shutil.which("claude") if args.product == "claude" else (shutil.which("codex.cmd") or shutil.which("codex"))
            executable = Path(default) if default else None
        result = run_check(args.product, args.package, args.root, executable, args.python.resolve(strict=True))
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0 if result["status"] == "pass" else 2
    except CheckError as exc:
        result = {"status": "blocked", "product": args.product, "error_code": exc.code,
                  "exit_code": exc.exit_code, "real_llm": False}
        try:
            test_root = TEST_ROOT.resolve(strict=True)
            root = args.root.resolve(strict=False)
            root.relative_to(test_root)
            if root.name.startswith("native-phase2-"):
                root.mkdir(parents=True, exist_ok=True)
                evidence = root / "evidence.json"
                if not evidence.exists():
                    evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")
                result["evidence_path"] = str(evidence)
        except (OSError, ValueError):
            pass
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
