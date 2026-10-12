from __future__ import annotations

import json
import os
import socket
import sqlite3
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

from pmt.db import Database
from pmt.errors import PmtError
from pmt.host.auth import AuthRegistry
from pmt.server_admin.config import load_config, load_config_snapshot, publish_config
from pmt.server_admin.doctor import run_doctor
from pmt.server_admin.logging import read_logs, server_log_config
from pmt.server_admin.serve import instance_lock
from pmt.server_admin.status import read_status
from pmt.server_admin.tls import _validate_pair, check_tls, register_tls


def _certificate_files(root, hostname="127.0.0.1", *, days=90, wrong_key=False):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import ipaddress

    root.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                   .not_valid_after(now + timedelta(days=days))
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(hostname))]), critical=False)
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .sign(key, hashes.SHA256()))
    cert_path, key_path, ca_path = root / "synthetic.crt", root / "synthetic.key", root / "synthetic-ca.crt"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes((other if wrong_key else key).private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)
    ca_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path, ca_path


def _base_config(root, data, port):
    return {"schema_version": 1, "revision": 1,
            "paths": {"data_root": str(data), "log_dir": str(root / "logs"), "backup_dir": str(root / "backup")},
            "listen": {"host": "127.0.0.1", "port": port}, "public_url": f"https://127.0.0.1:{port}",
            "tls": {}, "claim_key": {"key_id": "primary", "source": {"kind": "file", "path": str(root / "secrets" / "claim-primary.key")}, "retained": []},
            "proxy": {"enabled": False, "trusted": []}, "access": {"allowed_sources": []},
            "service": {"kind": "none", "name": "PMT Host", "account": "current", "app_root": str(root)},
            "logging": {"level": "info", "retain_days": 30}, "registry": {"projects": []}}


def _initialized(root, data, port):
    from pmt.server_admin.cli import main
    argv = ["init", "--config-root", str(root), "--public-url", f"https://127.0.0.1:{port}",
            "--listen", f"127.0.0.1:{port}", "--data-root", str(data), "--log-dir", str(root.parent / "logs"),
            "--backup-dir", str(root.parent / "backup"), "--apply", "--json"]
    assert main(argv) == 0
    config = load_config(root / "host-config.json")
    database = Database(root=data, config_root=root)
    auth = AuthRegistry(database)
    return config, auth


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(url, *, context=None, headers=None, timeout=2):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
    with opener.open(urllib.request.Request(url, headers=headers or {}), timeout=timeout) as response:
        return response.status, response.read()


def test_tls_register_dry_run_and_apply_preserve_sources_and_validate_san_key_chain(tmp_path):
    config_root, data_root = tmp_path / "config", tmp_path / "data"
    config_root.mkdir()
    config = _base_config(config_root, data_root, 19443)
    publish_config(config_root, config, create=True)
    cert, key, ca = _certificate_files(tmp_path / "sources")
    before = (load_config_snapshot(config_root / "host-config.json")[1], cert.read_bytes(), key.read_bytes(), ca.read_bytes())
    plan = register_tls(config_root, cert, key, ca)
    assert not plan["applied"] and not (config_root / "tls").exists()
    applied = register_tls(config_root, cert, key, ca, apply=True)
    assert plan["paths"] == applied["paths"]
    updated = load_config(config_root / "host-config.json")
    assert applied["applied"] and updated["revision"] == 2
    assert _validate_pair(updated["tls"]["cert_file"], updated["tls"]["key_file"], updated["tls"]["ca_file"], updated["public_url"])["ok"]
    assert check_tls(updated)["configured"]
    assert cert.read_bytes() == before[1] and key.read_bytes() == before[2] and ca.read_bytes() == before[3]
    from pmt.server_admin.secrets import _check_windows_acl
    if os.name == "nt": _check_windows_acl(updated["tls"]["key_file"], "current")
    else: assert Path(updated["tls"]["key_file"]).stat().st_mode & 0o077 == 0

    with pytest.raises(PmtError):
        _validate_pair(cert, key, ca, "https://wrong.example:19443")
    _, wrong_key, _ = _certificate_files(tmp_path / "wrong", wrong_key=True)
    with pytest.raises(PmtError): _validate_pair(cert, wrong_key, ca, config["public_url"])

    short_cert, short_key, short_ca = _certificate_files(tmp_path / "short-lived", days=10)
    assert _validate_pair(short_cert, short_key, short_ca, config["public_url"])["warning"]
    _, _, unrelated_ca = _certificate_files(tmp_path / "unrelated-ca")
    with pytest.raises(PmtError): _validate_pair(cert, key, unrelated_ca, config["public_url"])


def test_tls_register_publish_conflict_removes_only_new_files(tmp_path, monkeypatch):
    import pmt.server_admin.tls as tls_module
    config_root, data_root = tmp_path / "config", tmp_path / "data"
    config_root.mkdir()
    publish_config(config_root, _base_config(config_root, data_root, 19444), create=True)
    cert, key, ca = _certificate_files(tmp_path / "sources")
    before = (config_root / "host-config.json").read_bytes()
    monkeypatch.setattr(tls_module, "_publish_config_locked", lambda *_a, **_k: (_ for _ in ()).throw(PmtError("config_conflict", "injected")))
    with pytest.raises(PmtError): register_tls(config_root, cert, key, ca, apply=True)
    assert (config_root / "host-config.json").read_bytes() == before
    assert not (config_root / "tls").exists()
    assert cert.exists() and key.exists() and ca.exists()


def test_doctor_returns_thirteen_checks_and_failure_exit_signal(tmp_path, monkeypatch):
    from pmt.server_admin.cli import main
    root = tmp_path / "config"
    data = tmp_path / "data"
    config, _auth = _initialized(root, data, 19445)
    first = run_doctor(root)
    assert len(first["checks"]) == 13
    assert next(item for item in first["checks"] if item["name"] == "python")["status"] == "warn"
    assert {item["name"] for item in first["checks"]} == {"python", "deps", "version", "config", "paths", "claim_key", "tls", "port", "firewall", "service", "runtime", "duplicates", "selinux"}
    assert main(["--config-root", str(root), "doctor", "--json"]) == 0
    Path(config["paths"]["log_dir"]).rmdir()
    failed = run_doctor(root)
    assert next(item for item in failed["checks"] if item["name"] == "paths")["status"] == "fail"
    assert main(["--config-root", str(root), "doctor", "--json"]) == 1


def test_doctor_detects_occupied_port_and_duplicate_lock(tmp_path):
    root, data = tmp_path / "config", tmp_path / "data"
    config, _ = _initialized(root, data, 19446)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", config["listen"]["port"])); listener.listen()
        result = run_doctor(root)
        assert next(item for item in result["checks"] if item["name"] == "port")["status"] == "fail"
    with instance_lock(data / ".pmt-server.lock"):
        result = run_doctor(root)
        duplicate = next(item for item in result["checks"] if item["name"] == "duplicates")
        assert duplicate["status"] == "fail" and duplicate["code"] == "host_duplicate"


def test_doctor_failure_injection_for_config_dependencies_version_key_and_tls(tmp_path, monkeypatch):
    import pmt.server_admin.doctor as doctor_module
    root, data = tmp_path / "config", tmp_path / "data"
    config, _auth = _initialized(root, data, 19449)

    original_loader = doctor_module.load_config_snapshot
    monkeypatch.setattr(doctor_module, "load_config_snapshot", lambda *_a, **_k: (_ for _ in ()).throw(PmtError("config_invalid", "injected")))
    config_checks = {item["name"]: item for item in run_doctor(root)["checks"]}
    assert config_checks["config"]["status"] == "fail" and config_checks["version"]["status"] == "fail"
    monkeypatch.setattr(doctor_module, "load_config_snapshot", original_loader)

    original_import = doctor_module.importlib.import_module
    monkeypatch.setattr(doctor_module.importlib, "import_module", lambda _name: (_ for _ in ()).throw(ImportError("injected")))
    assert next(item for item in run_doctor(root)["checks"] if item["name"] == "deps")["status"] == "fail"
    monkeypatch.setattr(doctor_module.importlib, "import_module", original_import)

    with sqlite3.connect(data / "pmt.sqlite3") as conn:
        conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    assert next(item for item in run_doctor(root)["checks"] if item["name"] == "version")["status"] == "fail"
    with sqlite3.connect(data / "pmt.sqlite3") as conn:
        conn.execute("UPDATE meta SET value='5' WHERE key='schema_version'")

    key_path = Path(config["claim_key"]["source"]["path"])
    held_key = key_path.with_suffix(key_path.suffix + ".held")
    key_path.replace(held_key)
    try:
        assert next(item for item in run_doctor(root)["checks"] if item["name"] == "claim_key")["status"] == "fail"
    finally: held_key.replace(key_path)

    monkeypatch.setattr(doctor_module, "check_tls", lambda *_a, **_k: (_ for _ in ()).throw(PmtError("host_tls_invalid", "injected")))
    assert next(item for item in run_doctor(root)["checks"] if item["name"] == "tls")["status"] == "fail"


def test_status_is_read_only_and_logs_redact_secrets(tmp_path):
    root, data = tmp_path / "config", tmp_path / "data"
    config, _auth = _initialized(root, data, 19447)
    db_before = (data / "pmt.sqlite3").read_bytes()
    profile_before = (root / "profile.json").read_bytes()
    result = read_status(root)
    assert result["namespace_id"] and result["devices_active"] == 0 and not result["service"]["running"]
    assert (data / "pmt.sqlite3").read_bytes() == db_before and (root / "profile.json").read_bytes() == profile_before
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "host.log").write_text(
        '{"message":"Authorization: Bearer abc123 token=secretValue"}\n'
        '{"credential":"credential-secret","private_key":"pem-secret"}\n', encoding="utf-8")
    lines = read_logs(tmp_path / "logs", 2)
    assert all(secret not in "\n".join(lines) for secret in ("abc123", "secretValue", "credential-secret", "pem-secret"))
    assert all("[REDACTED]" in line for line in lines)
    handler = server_log_config(tmp_path / "logs")["handlers"]["pmt_file"]
    assert handler["class"].endswith("TimedRotatingFileHandler") and handler["backupCount"] == 30


def test_serve_preflights_before_database_effects_and_instance_lock_is_exclusive(tmp_path, monkeypatch):
    import pmt.server_admin.serve as serve_module
    root, data = tmp_path / "config", tmp_path / "data"
    config, _ = _initialized(root, data, 19448)
    monkeypatch.setattr(serve_module, "Database", lambda **_kw: pytest.fail("DB must not open after failed key preflight"))
    monkeypatch.setattr(serve_module, "validate_service_account", lambda _account: "foreign-account")
    with pytest.raises(PmtError, match="configured service account"):
        serve_module.serve_host(root, allow_loopback_http=True)
    monkeypatch.undo()
    Path(config["claim_key"]["source"]["path"]).unlink()
    monkeypatch.setattr(serve_module, "Database", lambda **_kw: pytest.fail("DB must not open after failed key preflight"))
    with pytest.raises(PmtError, match="Claim-key"):
        serve_module.serve_host(root, allow_loopback_http=True)
    lock = data / ".pmt-server.lock"
    with instance_lock(lock):
        with pytest.raises(PmtError, match="Another Host"):
            with instance_lock(lock): pass


def test_serve_rejects_plain_http_without_explicit_loopback_mode_before_database(tmp_path, monkeypatch):
    import pmt.server_admin.serve as serve_module
    root, data = tmp_path / "config", tmp_path / "data"
    _initialized(root, data, 19450)
    monkeypatch.setattr(serve_module, "Database", lambda **_kw: pytest.fail("DB must not open when TLS policy fails"))
    with pytest.raises(PmtError) as error:
        serve_module.serve_host(root)
    assert error.value.code == "host_tls_required"


def test_serve_explicit_loopback_http_and_trusted_proxy_modes_remain_supported(tmp_path, monkeypatch):
    import pmt.host.cli as host_cli
    import pmt.server_admin.serve as serve_module
    monkeypatch.setattr(serve_module, "Database", lambda **_kw: object())
    captured = []

    def fake_legacy(_db, args, *, claim_keys, log_config):
        captured.append((args.allow_loopback_http, args.behind_proxy, args.trusted_proxy, bool(claim_keys), bool(log_config)))
        return 0

    monkeypatch.setattr(host_cli, "serve_host", fake_legacy)
    for name, port, allow_http, proxy in (("direct", 19451, True, False), ("proxy", 19452, False, True)):
        root, data = tmp_path / name / "config", tmp_path / name / "data"
        config, _ = _initialized(root, data, port)
        if proxy:
            config["proxy"] = {"enabled": True, "trusted": ["127.0.0.1"]}
            config["revision"] += 1
            _, digest = load_config_snapshot(root / "host-config.json")
            publish_config(root, config, digest)
        assert serve_module.serve_host(root, allow_loopback_http=allow_http) == 0

    assert captured == [(True, False, [], True, True), (False, True, ["127.0.0.1"], True, True)]


def test_actual_isolated_https_health_and_authenticated_compatibility(tmp_path, monkeypatch):
    root, data = tmp_path / "config", tmp_path / "data"
    port = _free_port()
    config, auth = _initialized(root, data, port)
    cert, key, ca = _certificate_files(tmp_path / "sources")
    register_tls(root, cert, key, ca, apply=True)
    config = load_config(root / "host-config.json")
    issued = auth.issue_device("c2-fixture", ["*"], ["read"])
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    process = subprocess.Popen([sys.executable, "-m", "pmt.server_admin", "--config-root", str(root), "serve"],
                               cwd=Path(__file__).resolve().parents[2], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    context = ssl.create_default_context(cafile=config["tls"]["ca_file"])
    try:
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None: pytest.fail("isolated Host exited before health check")
            try:
                status, body = _request(config["public_url"] + "/health", context=context)
                if status == 200: break
            except (OSError, urllib.error.URLError):
                if time.monotonic() >= deadline: raise
                time.sleep(0.1)
        assert json.loads(body)["status"] == "ok"
        from pmt.http_store import HttpStore
        credential_env = "PMT_C2_COMPAT_CREDENTIAL"
        monkeypatch.setenv(credential_env, issued["credential"])
        client = HttpStore(config["public_url"], credential_env, issued["device_id"], str(uuid.uuid4()),
                           issued["namespace_id"], ca_file=config["tls"]["ca_file"], timeout=3)
        compat = client.check_compatibility()
        assert compat["namespace_id"] == issued["namespace_id"] and compat["device_id"] == issued["device_id"]
        doctor_checks = {item["name"]: item for item in run_doctor(root)["checks"]}
        assert doctor_checks["duplicates"]["status"] == "fail"
        assert doctor_checks["port"]["status"] == "warn"
        live_status = read_status(root)
        assert live_status["service"]["running"] and isinstance(live_status["service"]["pid"], int)
        assert live_status["service"]["started_at"]
        log_path = Path(config["paths"]["log_dir"]) / "host.log"
        if log_path.exists(): assert issued["credential"] not in log_path.read_text(encoding="utf-8", errors="replace")

        duplicate = subprocess.run([sys.executable, "-m", "pmt.server_admin", "--config-root", str(root), "serve"],
                                   cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True, text=True, timeout=10)
        assert duplicate.returncode == 1 and "host_duplicate" in duplicate.stdout
    finally:
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
