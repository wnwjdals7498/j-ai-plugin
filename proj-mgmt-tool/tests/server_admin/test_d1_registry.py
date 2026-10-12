from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import socket
import ssl
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pmt.errors import PmtError
from pmt.db import Database
from pmt.handoff import load_handoff
from pmt.host.auth import AuthRegistry
from pmt.http_store import HttpStore
from pmt.server_admin.cli import main
from pmt.server_admin.config import load_config
from pmt.server_admin.secrets import _check_windows_acl


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _tls_sources(folder):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import ipaddress

    folder.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=90))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path, key_path, ca_path = folder / "synthetic.crt", folder / "synthetic.key", folder / "synthetic-ca.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)
    ca_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path, ca_path


def _json_call(argv, capsys):
    code = main(argv + ["--json"])
    output = capsys.readouterr().out.splitlines()
    return code, json.loads(output[-1])


@pytest.fixture
def live_admin_host(tmp_path):
    root, data = tmp_path / "config", tmp_path / "data"
    logs, backup = tmp_path / "logs", tmp_path / "backup"
    port = _port()
    assert main(["init", "--config-root", str(root), "--public-url", f"https://127.0.0.1:{port}",
                 "--listen", f"127.0.0.1:{port}", "--data-root", str(data), "--log-dir", str(logs),
                 "--backup-dir", str(backup), "--apply", "--json"]) == 0
    cert, key, ca = _tls_sources(tmp_path / "tls-source")
    assert main(["tls", "register", "--config-root", str(root), "--cert", str(cert), "--key", str(key),
                 "--ca", str(ca), "--apply", "--json"]) == 0
    config = load_config(root / "host-config.json")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    process = subprocess.Popen([sys.executable, "-m", "pmt.server_admin", "--config-root", str(root), "serve"],
                               cwd=Path(__file__).resolve().parents[2], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    context = ssl.create_default_context(cafile=config["tls"]["ca_file"])
    deadline = time.monotonic() + 20
    try:
        while True:
            if process.poll() is not None: pytest.fail("synthetic D1 Host exited before readiness")
            try:
                with urllib.request.urlopen(config["public_url"] + "/health", context=context, timeout=1) as response:
                    if response.status == 200: break
            except (OSError, urllib.error.URLError):
                if time.monotonic() >= deadline: raise
                time.sleep(0.1)
        yield {"root": root, "data": data, "config": config, "process": process}
    finally:
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)


def _credential_path():
    folder = Path(r"C:\PMT\work\phase5-test\D1\credentials")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"device-{uuid.uuid4()}.credential"


def _read_protected_credential(path):
    # Test-only use: keep plaintext in process memory and never assert or print its value.
    value = path.read_text(encoding="ascii").strip()
    assert len(value) >= 32
    if os.name == "nt": _check_windows_acl(path, "current")
    else: assert path.stat().st_mode & 0o077 == 0
    return value


def _issue(live_admin_host, capsys, project, *, handoff=None, include_ca=False):
    credential = _credential_path()
    argv = ["device", "issue", "--config-root", str(live_admin_host["root"]), "--actor", "d1-fixture",
            "--project", project, "--permission", "read,write,runtime,review", "--credential-out", str(credential), "--apply"]
    if handoff: argv += ["--handoff-out", str(handoff)]
    if include_ca: argv += ["--include-ca"]
    try:
        code, result = _json_call(argv, capsys)
        assert code == 0 and "credential" not in result
        return credential, result
    except Exception:
        credential.unlink(missing_ok=True)
        raise


def test_project_add_uses_http_scope_chain_and_repo_metadata_tracks_actual_parent(live_admin_host, capsys):
    root, data = live_admin_host["root"], live_admin_host["data"]
    code, project_result = _json_call(["project", "add", "--config-root", str(root), "--name", "alpha",
                                       "--title", "Alpha fixture", "--apply"], capsys)
    assert code == 0 and project_result["bootstrap_state"] == "revoked"
    with sqlite3.connect(f"file:{(data / 'pmt.sqlite3').as_posix()}?mode=ro", uri=True) as connection:
        project_id = project_result["project_id"]
        row = connection.execute("SELECT p.kind,p.parent_id,r.kind,r.parent_id,e.kind FROM scopes p "
                                 "JOIN scopes r ON r.id=p.parent_id JOIN scopes e ON e.id=r.parent_id WHERE p.id=?", (project_id,)).fetchone()
    assert row[0] == "project" and row[2] == "repository" and row[4] == "environment"
    actual_repository_id = row[1]
    code, repo_result = _json_call(["project", "repo", "add", "--config-root", str(root), "--project", "alpha",
                                    "--name", "alpha", "--remote", "https://example.invalid/acme/alpha.git",
                                    "--graph-path", "docs/graph.json", "--apply"], capsys)
    assert code == 0 and repo_result["repository_id"] == actual_repository_id
    code, conflict = _json_call(["project", "repo", "add", "--config-root", str(root), "--project", "alpha",
                                 "--name", "another", "--remote", "https://example.invalid/acme/another.git",
                                 "--apply"], capsys)
    assert code != 0 and conflict["error"]["code"] == "project_repository_conflict"
    current = load_config(root / "host-config.json")["registry"]["projects"][0]
    assert len(current["repositories"]) == 1 and current["repositories"][0]["repository_id"] == actual_repository_id
    devices = AuthRegistry(Database(root=data, config_root=root)).list_devices()
    bootstrap = [item for item in devices if item["actor"] == "pmt-server-bootstrap"]
    assert len(bootstrap) == 1 and bootstrap[0]["state"] == "revoked"


def test_project_add_reports_bootstrap_revoke_failure_with_device_id_and_no_secret(live_admin_host, capsys, monkeypatch):
    original = AuthRegistry.revoke_device
    def injected_failure(self, device_id, expected_revision):
        row = next(item for item in self.list_devices() if item["device_id"] == device_id)
        if row["actor"] == "pmt-server-bootstrap": raise PmtError("injected_revoke_failure", "test fixture")
        return original(self, device_id, expected_revision)
    monkeypatch.setattr(AuthRegistry, "revoke_device", injected_failure)
    code, payload = _json_call(["project", "add", "--config-root", str(live_admin_host["root"]),
                                "--name", "revoke-fixture", "--apply"], capsys)
    assert code != 0 and payload["error"]["code"] == "bootstrap_revoke_failed"
    message = payload["error"]["message"]
    device_id = message.split("Bootstrap device ", 1)[1].split(" ", 1)[0]
    assert str(uuid.UUID(device_id)) == device_id
    monkeypatch.setattr(AuthRegistry, "revoke_device", original)
    from pmt.db import Database
    auth = AuthRegistry(Database(root=live_admin_host["data"], config_root=live_admin_host["root"]))
    row = next(item for item in auth.list_devices() if item["device_id"] == device_id)
    assert row["state"] == "active"
    assert original(auth, device_id, row["revision"])["state"] == "revoked"


def test_dry_runs_do_not_mutate_and_wildcard_requires_explicit_admin(live_admin_host, capsys):
    root = live_admin_host["root"]
    _json_call(["project", "add", "--config-root", str(root), "--name", "dry-plan"], capsys)
    config = load_config(root / "host-config.json")
    assert not any(item["name"] == "dry-plan" for item in config["registry"]["projects"])
    devices = AuthRegistry(Database(root=live_admin_host["data"], config_root=root)).list_devices()
    assert not any(item["actor"] == "pmt-server-bootstrap" for item in devices)
    assert _json_call(["project", "add", "--config-root", str(root), "--name", "grant-plan", "--apply"], capsys)[0] == 0
    devices = AuthRegistry(Database(root=live_admin_host["data"], config_root=root)).list_devices()
    code, planned = _json_call(["device", "issue", "--config-root", str(root), "--actor", "dry-plan", "--project", "grant-plan"], capsys)
    assert code == 0 and planned["applied"] is False and "credential" not in planned
    assert AuthRegistry(Database(root=live_admin_host["data"], config_root=root)).list_devices() == devices
    code, rejected = _json_call(["device", "issue", "--config-root", str(root), "--actor", "admin-plan", "--project", "*", "--apply"], capsys)
    assert code != 0 and rejected["error"]["code"] == "scope_forbidden"


def test_device_issue_grants_rotate_revoke_and_handoff_are_safe(live_admin_host, capsys, monkeypatch):
    root = live_admin_host["root"]
    for name in ("alpha", "beta"):
        code, _ = _json_call(["project", "add", "--config-root", str(root), "--name", name, "--apply"], capsys)
        assert code == 0
        _json_call(["project", "repo", "add", "--config-root", str(root), "--project", name,
                    "--name", name, "--remote", f"https://example.invalid/acme/{name}.git", "--apply"], capsys)
    config = load_config(root / "host-config.json")
    beta_id = next(item["project_id"] for item in config["registry"]["projects"] if item["name"] == "beta")
    handoff_path = Path(r"C:\PMT\work\phase5-test\D1\handoffs") / f"handoff-{uuid.uuid4()}.json"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    second_handoff_path = handoff_path.with_name(f"handoff-copy-{uuid.uuid4()}.json")
    credential_path, issued = _issue(live_admin_host, capsys, "alpha", handoff=handoff_path, include_ca=True)
    envname = "PMT_D1_TEST_CREDENTIAL"
    old_credential = _read_protected_credential(credential_path)
    monkeypatch.setenv(envname, old_credential)
    try:
        handoff = load_handoff(handoff_path)
        raw_handoff = handoff_path.read_text(encoding="utf-8")
        if old_credential in raw_handoff: raise AssertionError("handoff exposed credential")
        assert "PRIVATE KEY" not in raw_handoff and "ca_pem" in handoff["host"]
        pem_bytes = handoff["host"]["ca_pem"].encode("utf-8")
        assert hashlib.sha256(pem_bytes).hexdigest() == handoff["host"]["ca_sha256"]
        assert handoff["device"]["credential"] == {"delivery": "separate", "env": "PMT_HOST_CREDENTIAL"}
        if str(root) in raw_handoff: raise AssertionError("handoff exposed an internal config path")
        code, created = _json_call(["handoff", "create", "--config-root", str(root), "--device", issued["device_id"],
                                    "--out", str(second_handoff_path), "--include-ca", "--apply"], capsys)
        assert code == 0 and created["includes_ca"] and "credential" not in created
        assert load_handoff(second_handoff_path)["device"]["device_id"] == issued["device_id"]
        client = HttpStore(config["public_url"], envname, issued["device_id"], str(uuid.uuid4()),
                           issued["namespace_id"], ca_file=config["tls"]["ca_file"], timeout=4)
        assert client.check_compatibility()["device_id"] == issued["device_id"]
        session_id = str(uuid.uuid4())
        client.register_session(session_id)
        denied, exit_code = client.execute({"protocol_version": 1, "operation": "read_context", "request_id": str(uuid.uuid4()),
            "actor": "d1-fixture", "session_id": session_id, "scope_id": beta_id,
            "payload": {"query": "", "limit": 10, "budget": 1024}})
        assert exit_code != 0 and denied["error"]["code"] == "scope_forbidden"

        code, grants = _json_call(["device", "grants", "--config-root", str(root), "--device", issued["device_id"],
                                   "--project", "beta", "--permission", "read", "--apply"], capsys)
        assert code == 0 and grants["scopes"] == [beta_id] and grants["revision"] == 2
        replacement_path = _credential_path()
        code, rotation = _json_call(["device", "rotate", "--config-root", str(root), "--device", issued["device_id"],
                                     "--credential-out", str(replacement_path), "--apply"], capsys)
        assert code == 0 and rotation["revision"] == 3 and "credential" not in rotation
        replacement = _read_protected_credential(replacement_path)
        newenv = "PMT_D1_ROTATED_CREDENTIAL"
        monkeypatch.setenv(newenv, replacement)
        old_client = HttpStore(config["public_url"], envname, issued["device_id"], str(uuid.uuid4()),
                               issued["namespace_id"], ca_file=config["tls"]["ca_file"], timeout=3)
        with pytest.raises(PmtError): old_client.check_compatibility()
        new_client = HttpStore(config["public_url"], newenv, issued["device_id"], str(uuid.uuid4()),
                               issued["namespace_id"], ca_file=config["tls"]["ca_file"], timeout=3)
        assert new_client.check_compatibility()["device_id"] == issued["device_id"]
        code, revoked = _json_call(["device", "revoke", "--config-root", str(root), "--device", issued["device_id"], "--apply"], capsys)
        assert code == 0 and revoked["state"] == "revoked" and revoked["revision"] == 4
        with pytest.raises(PmtError): new_client.check_compatibility()

        devices_code, device_listing = _json_call(["device", "list", "--config-root", str(root)], capsys)
        assert devices_code == 0
        assert all("credential" not in row and "credential_hash" not in row for row in device_listing["devices"])
    finally:
        credential_path.unlink(missing_ok=True)
        replacement_path = locals().get("replacement_path")
        if replacement_path: replacement_path.unlink(missing_ok=True)
        handoff_path.unlink(missing_ok=True)
        second_handoff_path.unlink(missing_ok=True)


@pytest.mark.skipif(not os.environ.get("PMT_D1_SAMPLE_ROOT"), reason="persistent D2 sample is created only by explicit D1 evidence run")
def test_create_persistent_d1_handoff_sample(capsys):
    """Create a stopped, isolated Host fixture for D2; never print credential material."""
    root = Path(os.environ["PMT_D1_SAMPLE_ROOT"])
    if root.exists() and any(root.iterdir()): pytest.fail("D1 sample root must be empty before creation")
    data = root / "data"
    port = 18765
    root.mkdir(parents=True, exist_ok=True)
    assert main(["init", "--config-root", str(root / "config"), "--public-url", f"https://127.0.0.1:{port}",
                 "--listen", f"127.0.0.1:{port}", "--data-root", str(data), "--log-dir", str(root / "logs"),
                 "--backup-dir", str(root / "backup"), "--apply", "--json"]) == 0
    cert, key, ca = _tls_sources(root / "tls-source")
    assert main(["tls", "register", "--config-root", str(root / "config"), "--cert", str(cert), "--key", str(key),
                 "--ca", str(ca), "--apply", "--json"]) == 0
    config = load_config(root / "config" / "host-config.json")
    env = os.environ.copy(); env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    process = subprocess.Popen([sys.executable, "-m", "pmt.server_admin", "--config-root", str(root / "config"), "serve"],
                               cwd=Path(__file__).resolve().parents[2], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    credential_path = Path(r"C:\PMT\work\phase5-test\D1\credentials\d2-sample.credential")
    credential_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path = root / "handoff.json"
    try:
        context = ssl.create_default_context(cafile=config["tls"]["ca_file"])
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None: pytest.fail("persistent D1 Host exited before readiness")
            try:
                with urllib.request.urlopen(config["public_url"] + "/health", context=context, timeout=1) as response:
                    if response.status == 200: break
            except (OSError, urllib.error.URLError):
                if time.monotonic() >= deadline: raise
                time.sleep(0.1)
        assert main(["project", "add", "--config-root", str(root / "config"), "--name", "d2-sample", "--title", "D2 sample project", "--apply", "--json"]) == 0
        assert main(["project", "repo", "add", "--config-root", str(root / "config"), "--project", "d2-sample", "--name", "d2-sample",
                     "--remote", "https://example.invalid/acme/d2-sample.git", "--graph-path", "docs/graph.json", "--apply", "--json"]) == 0
        code, issued = _json_call(["device", "issue", "--config-root", str(root / "config"), "--actor", "d2-sample", "--project", "d2-sample",
                                   "--permission", "read,write,runtime,review", "--credential-out", str(credential_path),
                                   "--handoff-out", str(handoff_path), "--include-ca", "--apply"], capsys)
        assert code == 0 and "credential" not in issued and issued["credential_file"] == str(credential_path)
        assert credential_path.is_file()
        doc = load_handoff(handoff_path)
        assert doc["projects"][0]["name"] == "d2-sample" and "ca_pem" in doc["host"]
        return
    finally:
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
