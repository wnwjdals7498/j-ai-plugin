from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pmt.db import Database
from pmt.host.application import HostApplication
from pmt.host.resources import HostResourceStore
from pmt.migration import MigrationCoordinator
from pmt.util import canonical_json, new_id
from pmt.host.transfer import _pack_bundle
from test_phase3_host_network import _certificate
from test_phase3_migration import _source_case


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_host(tmp_path: Path, label: str):
    tmp_path.mkdir(parents=True, exist_ok=True)
    data_root, config_root = tmp_path / f"{label}-data", tmp_path / f"{label}-config"
    db = Database(data_root, config_root)
    key = b"isolated-https-transfer-fixture-key-32bytes"
    app = HostApplication(db, {"fixture": key}, "fixture")
    device = app.auth.issue_device("transfer-admin", ["*"], ["read", "write", "runtime", "review", "admin"])
    session_id, environment_id = f"{label}-session", new_id()
    headers = {"authorization": "Bearer " + device["credential"], "x-pmt-device": device["device_id"],
        "x-pmt-environment": environment_id, "x-pmt-namespace": app.auth.namespace_id,
        "x-pmt-session": session_id}
    app.register_session(headers, {"session_id": session_id, "environment_id": environment_id})
    HostResourceStore(db, app.auth)
    cert, private_key = _certificate(tmp_path, label)
    port = _free_port()
    base = f"https://127.0.0.1:{port}"
    claim_key_env = "PMT_TRANSFER_TEST_CLAIM_KEY"
    child_env = os.environ.copy()
    child_env[claim_key_env] = base64.b64encode(key).decode("ascii")
    child_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src") + os.pathsep + child_env.get("PYTHONPATH", "")
    command = [sys.executable, "-m", "pmt.host.cli", "--data-root", str(data_root), "--config-root", str(config_root),
        "serve", "--host", "127.0.0.1", "--port", str(port), "--claim-key-env", claim_key_env,
        "--ssl-certfile", str(cert), "--ssl-keyfile", str(private_key)]
    stdout = (tmp_path / f"{label}.stdout").open("wb")
    stderr = (tmp_path / f"{label}.stderr").open("wb")
    process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], env=child_env,
        stdout=stdout, stderr=stderr)
    context = ssl.create_default_context(cafile=str(cert))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and process.poll() is None:
        try:
            with urlopen(base + "/health", context=context, timeout=0.5) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(0.1)
    else:
        process.terminate()
        process.wait(timeout=5)
        stdout.close()
        stderr.close()
        raise AssertionError(f"isolated HTTPS fixture {label} did not become ready")
    return {"db": db, "app": app, "headers": headers, "context": context,
        "base": base, "process": process, "stdout": stdout, "stderr": stderr}


def _stop_host(host):
    process = host["process"]
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    host["stdout"].close()
    host["stderr"].close()


def _request(host, method, path, body, headers):
    request = Request(host["base"] + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, context=host["context"], timeout=20) as response:
            return response.status, response.headers, response.read(64 * 1024 * 1024 + 1)
    except HTTPError as error:
        return error.code, error.headers, error.read(1024 * 1024)


def test_loopback_https_import_backup_download_and_isolated_restore(tmp_path):
    (tmp_path / "source").mkdir()
    source = _source_case(tmp_path / "source")
    source_bundle_dir = tmp_path / "prepared-bundle"
    manifest = MigrationCoordinator().create_backup(source["db"], source_bundle_dir, [source["mapping"]])
    bundle = _pack_bundle(source_bundle_dir, manifest)
    metadata = {"bundle_id": manifest["bundle_id"], "manifest_sha256": manifest["manifest_sha256"]}

    first = _start_host(tmp_path / "host-one", "transfer-one")
    second = None
    try:
        post_headers = {**first["headers"], "X-PMT-Transfer-Metadata": canonical_json(metadata),
            "Content-Type": "application/vnd.pmt.migration+zip"}
        status, _, raw = _request(first, "POST", "/api/v1/transfers/import", bundle, post_headers)
        assert status == 200, raw[:1000]
        imported = json.loads(raw)
        assert imported["receipt"]["state"] == "imported"
        status, _, raw = _request(first, "POST", "/api/v1/transfers/import", bundle, post_headers)
        assert status == 200 and json.loads(raw)["receipt"]["state"] == "replayed"

        backup_req = canonical_json({"request_id": new_id()}).encode("utf-8")
        status, _, raw = _request(first, "POST", "/api/v1/transfers/backup", backup_req,
            {**first["headers"], "Content-Type": "application/json"})
        assert status == 200, raw[:1000]
        backup = json.loads(raw)
        assert backup["source_kind"] == "host"
        download_path = "/api/v1/transfers/download/" + backup["download_ref"]
        status, download_headers, downloaded = _request(first, "GET", download_path, None, first["headers"])
        assert status == 200
        assert hashlib.sha256(downloaded).hexdigest() == download_headers["X-PMT-Bundle-SHA256"]
        assert download_headers["X-PMT-Manifest-SHA256"] == backup["manifest_sha256"]
        assert download_headers["X-PMT-Download-Ref"] == backup["download_ref"]

        second = _start_host(tmp_path / "host-two", "transfer-two")
        second_metadata = {"bundle_id": backup["bundle_id"],
            "manifest_sha256": download_headers["X-PMT-Manifest-SHA256"]}
        status, _, raw = _request(second, "POST", "/api/v1/transfers/import", downloaded,
            {**second["headers"], "X-PMT-Transfer-Metadata": canonical_json(second_metadata),
             "Content-Type": "application/vnd.pmt.migration+zip"})
        assert status == 200 and json.loads(raw)["receipt"]["state"] == "imported"
    finally:
        if second is not None:
            _stop_host(second)
        _stop_host(first)

    with source["db"].connect() as conn:
        assert conn.execute("SELECT 1 FROM records WHERE id=?", (source["step_id"],)).fetchone()
