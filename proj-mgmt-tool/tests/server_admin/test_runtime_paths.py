from __future__ import annotations
import base64
import json
import pytest
from pmt.errors import PmtError
from pmt.server_admin.runtime_paths import resolve_runtime_config


def _config(root):
    return {"service": {"kind": "systemd"},
            "claim_key": {"key_id": "primary", "source": {"kind": "file", "path": str(root / "secrets/claim-primary.key")},
                          "retained": [{"key_id": "old", "source": {"kind": "file", "path": str(root / "secrets/old.key")}}]},
            "tls": {"cert_file": str(root / "tls/cert.crt"), "key_file": str(root / "tls/hashed.key"),
                    "ca_file": str(root / "tls/ca.crt")}}


def test_unit_consumes_delivered_files_and_does_not_mutate_source(tmp_path):
    config = _config(tmp_path / "host")
    before = json.dumps(config, sort_keys=True)
    directory = tmp_path / "credentials"
    resolved = resolve_runtime_config(config, tmp_path / "host", environ={"CREDENTIALS_DIRECTORY": str(directory)})
    assert resolved["claim_key"]["source"]["path"] == str(directory / "claim-primary")
    assert resolved["claim_key"]["retained"][0]["source"]["path"] == str(directory / "claim-old")
    assert resolved["tls"]["key_file"] == str(directory / "tls-key")
    assert resolved["tls"]["cert_file"] == config["tls"]["cert_file"]
    assert json.dumps(config, sort_keys=True) == before
    assert not directory.exists()


def test_manual_absolute_sources_and_placeholder_fallback(tmp_path):
    config = _config(tmp_path)
    assert resolve_runtime_config(config, tmp_path, environ={}) == config
    config["claim_key"]["source"]["path"] = "${CREDENTIALS_DIRECTORY}/claim-primary"
    config["tls"]["key_file"] = "${CREDENTIALS_DIRECTORY}/tls-key"
    resolved = resolve_runtime_config(config, tmp_path, environ={})
    assert resolved["claim_key"]["source"]["path"] == str(tmp_path / "secrets/claim-primary.key")
    assert resolved["tls"]["key_file"] == str(tmp_path / "tls/tls-key")


@pytest.mark.parametrize("directory", ["", "relative", "../credentials", "/run/../credentials", "/run/$OTHER", "/run/~user", "/run/cred\n"])
def test_invalid_directory_rejected_before_file_access(tmp_path, directory):
    with pytest.raises(PmtError, match="absolute path"):
        resolve_runtime_config(_config(tmp_path), tmp_path, environ={"CREDENTIALS_DIRECTORY": directory})


def test_wrong_placeholder_alias_rejected(tmp_path):
    config = _config(tmp_path)
    config["claim_key"]["source"]["path"] = "${CREDENTIALS_DIRECTORY}/other"
    with pytest.raises(PmtError) as error:
        resolve_runtime_config(config, tmp_path, environ={})
    assert error.value.code == "service_unsupported"


def test_other_service_ignores_unrelated_credential_directory(tmp_path):
    config = _config(tmp_path)
    config["service"]["kind"] = "none"
    assert resolve_runtime_config(config, tmp_path, environ={"CREDENTIALS_DIRECTORY": "relative"}) == config
    config["tls"]["key_file"] = "${CREDENTIALS_DIRECTORY}/tls-key"
    with pytest.raises(PmtError):
        resolve_runtime_config(config, tmp_path, environ={})


def test_real_serve_loads_delivered_claim_and_preserves_config(tmp_path, monkeypatch, capsys):
    from pmt.server_admin.cli import main
    from pmt.server_admin.config import load_config_snapshot, publish_config
    from pmt.server_admin.secrets import store_key
    from pmt.server_admin.serve import serve_host
    import pmt.host.cli

    root = tmp_path / "host"
    assert main(["init", "--config-root", str(root), "--public-url", "https://127.0.0.1:18765",
                 "--listen", "127.0.0.1:18765", "--data-root", str(tmp_path / "data"),
                 "--log-dir", str(tmp_path / "logs"), "--backup-dir", str(tmp_path / "backups"),
                 "--service", "none", "--apply", "--json"]) == 0
    capsys.readouterr()
    config, digest = load_config_snapshot(root / "host-config.json")
    config["service"]["kind"] = "systemd"
    config["claim_key"]["source"] = {"kind": "file", "path": str(root / "missing-original.key")}
    config["revision"] += 1
    publish_config(root, config, digest)
    before = (root / "host-config.json").read_bytes()
    directory = tmp_path / "runtime-credentials"
    material = b"synthetic-runtime-claim-key" * 2
    store_key(base64.b64encode(material), directory / "claim-primary", "file", account="current")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(directory))
    captured = {}
    def runner(db, args, *, claim_keys, log_config):
        captured.update(claim_keys)
        return 0
    monkeypatch.setattr(pmt.host.cli, "serve_host", runner)
    assert serve_host(root, allow_loopback_http=True) == 0
    assert captured == {"primary": material}
    assert (root / "host-config.json").read_bytes() == before
    (directory / "claim-primary").unlink()
    with pytest.raises(PmtError) as error:
        serve_host(root, allow_loopback_http=True)
    assert error.value.code == "host_key_unavailable"

def test_initialized_config_root_requires_read_access_not_write_access(tmp_path, monkeypatch, capsys):
    from pmt.server_admin.cli import main
    from pmt.server_admin.serve import serve_host
    import pmt.server_admin.serve as serve_module
    import pmt.host.cli
    root = tmp_path / "host"
    assert main(["init", "--config-root", str(root), "--public-url", "https://127.0.0.1:18765",
                 "--listen", "127.0.0.1:18765", "--data-root", str(tmp_path / "data"),
                 "--log-dir", str(tmp_path / "logs"), "--backup-dir", str(tmp_path / "backups"),
                 "--service", "none", "--apply", "--json"]) == 0
    capsys.readouterr()
    profile_before = (root / "profile.json").read_bytes()
    original_access = serve_module.os.access
    def access(path, flags):
        if str(path) == str(root) and flags & serve_module.os.W_OK:
            return False
        return original_access(path, flags)
    monkeypatch.setattr(serve_module.os, "access", access)
    monkeypatch.setattr(pmt.host.cli, "serve_host", lambda *_args, **_kwargs: 0)
    assert serve_host(root, allow_loopback_http=True) == 0
    assert (root / "profile.json").read_bytes() == profile_before
