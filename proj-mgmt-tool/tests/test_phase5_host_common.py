import argparse
import base64
from contextlib import closing
import json
from pathlib import Path

import pytest

from pmt.db import Database
from pmt.errors import PmtError
from pmt.host.auth import AuthRegistry
from pmt.host import cli
from pmt.util import new_id
from test_phase3_host_network import live_host

def options(**changes):
    values = dict(host="127.0.0.1", port=18765, ssl_certfile=None, ssl_keyfile=None,
        allow_loopback_http=True, behind_proxy=False, trusted_proxy=[],
        claim_key_id="primary", claim_key_env="PMT_SYNTHETIC_A1_KEY", retained_key=[])
    values.update(changes)
    return argparse.Namespace(**values)

@pytest.mark.parametrize("changes,code", [
    ({"host": "example.test"}, "host_bind_invalid"), ({"port": -1}, "host_bind_invalid"),
    ({"host": "0.0.0.0"}, "host_tls_required"),
    ({"ssl_certfile": "certificate-only"}, "host_tls_invalid"),
    ({"behind_proxy": True}, "host_proxy_invalid"),
    ({"trusted_proxy": ["127.0.0.1"]}, "host_proxy_invalid")])
def test_legacy_serve_validation_order_unchanged(tmp_path, monkeypatch, changes, code):
    monkeypatch.delenv("PMT_SYNTHETIC_A1_KEY", raising=False)
    db = Database(tmp_path / "data", tmp_path / "config")
    with pytest.raises(PmtError) as error:
        cli._serve(db, options(**changes))
    assert error.value.code == code

def test_internal_launcher_preserves_legacy_uvicorn_settings(tmp_path, monkeypatch):
    import uvicorn
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    monkeypatch.setenv("PMT_SYNTHETIC_A1_KEY", base64.b64encode(b"k" * 48).decode())
    db = Database(tmp_path / "data", tmp_path / "config")
    assert cli._serve(db, options()) == 0
    assert calls[0][0].state.pmt.extension is not None
    assert calls[0][0].state.pmt.resources is not None
    legacy = calls[0][1]
    assert legacy == dict(host="127.0.0.1", port=18765, workers=1, ssl_certfile=None,
        ssl_keyfile=None, proxy_headers=False, forwarded_allow_ips=[],
        access_log=False, log_level="info", timeout_graceful_shutdown=15)
    log_config = {"version": 1, "disable_existing_loggers": False}
    monkeypatch.delenv("PMT_SYNTHETIC_A1_KEY")
    assert cli.serve_host(db, options(), claim_keys={"primary": b"k" * 48}, log_config=log_config) == 0
    assert calls[1][1] == dict(legacy, log_config=log_config)

def test_device_list_has_no_credential_hash_and_does_not_mutate_registry(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    registry = AuthRegistry(db)
    first = registry.issue_device("first", ["*"], ["read"])
    second = registry.issue_device("second", ["*"], ["read", "write"])
    registry.revoke_device(second["device_id"], 1)
    with closing(db.connect()) as conn:
        before = list(conn.execute("SELECT id,revision,state,credential_hash FROM host_devices ORDER BY id"))
    listing = registry.list_devices()
    assert len(listing) == 2
    assert {d["state"] for d in listing} == {"active", "revoked"}
    assert all("credential" not in d and "credential_hash" not in d for d in listing)
    assert first["credential"] not in json.dumps(listing)
    with closing(db.connect()) as conn:
        after = list(conn.execute("SELECT id,revision,state,credential_hash FROM host_devices ORDER BY id"))
    assert [tuple(row) for row in before] == [tuple(row) for row in after]

def test_actual_legacy_host_tls_health_and_compatibility(live_host):
    env = live_host
    import ssl
    from urllib.request import HTTPSHandler, ProxyHandler, build_opener
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context(cafile=str(env["cert"]))))
    with opener.open(env["base"] + "/health", timeout=2) as response:
        assert json.loads(response.read()) == {"status": "ok", "api_version": 1}
    result = env["store_a"].check_compatibility()
    assert result["compatible"] is True
    assert result["actor"] == env["actor"]
    assert result["device_id"] != env["store_b"].check_compatibility()["device_id"]

def test_root_manifest_is_single_optional_host_configuration():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / ".claude-plugin/plugin.json").read_text())
    assert not (root / "integrations/claude/.claude-plugin/plugin.json").exists()
    assert manifest["name"] == "pmt-lifecycle"
    assert manifest["displayName"] == "PMT"
    assert manifest["version"] == "0.5.0"
    assert manifest["userConfig"]["python_path"]["required"] is True
    assert all(not option.get("required", False) for name, option in manifest["userConfig"].items()
               if name != "python_path")
    assert manifest["userConfig"]["device_credential"]["sensitive"] is True
    assert manifest["userConfig"]["handoff_file"]["type"] == "file"


def test_built_client_manifests_use_root_options_and_valid_hook_paths(tmp_path):
    from test_packaging import BUILDER, ROOT
    built = BUILDER.build_plugins(tmp_path / "distribution", "0.5.0", ROOT)
    for product in ("claude", "codex"):
        package = Path(built["products"][product]["directory"])
        manifest_dir = ".claude-plugin" if product == "claude" else ".codex-plugin"
        manifest = json.loads((package / manifest_dir / "plugin.json").read_text())
        assert (package / manifest["hooks"]).is_file()
        assert manifest["version"] == "0.5.0"
        if product == "claude":
            assert "handoff_file" in manifest["userConfig"]
            assert not manifest["userConfig"]["host_url"].get("required", False)


def test_server_entry_point_version_is_importable_and_preserves_core_contract(capsys):
    from pmt.server_admin.cli import main
    assert main(["version", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["version"] == "0.5.0"
    assert result["core_version"] == "0.4.1"
    assert (result["db_schema"], result["graph_schema"], result["protocol"], result["host_schema"]) == (5, 1, 1, 1)
