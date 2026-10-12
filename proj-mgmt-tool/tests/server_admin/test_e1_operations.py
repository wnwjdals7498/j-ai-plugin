from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from pmt.errors import PmtError
from pmt.server_admin.config import load_config_snapshot, publish_config
from pmt.server_admin import firewall, operations, service
from pmt.server_admin import secrets


def _config(root, *, platform="windows", service_kind=None, account=None, sources=None, port=18765):
    root = Path(root)
    systemd = platform == "linux"
    kind = service_kind or ("systemd" if systemd else "windows-task")
    account = account or ("pmt" if systemd else "NT AUTHORITY\\LOCAL SERVICE")
    config_root = root / "Host Config"
    config_root.mkdir(parents=True, exist_ok=True)
    app = root / "PMT App With Spaces"
    app.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "revision": 1,
        "paths": {"data_root": str(root / "host data"), "log_dir": str(root / "host logs"),
                  "backup_dir": str(root / "host backup")},
        "listen": {"host": "127.0.0.1", "port": port},
        "public_url": f"https://127.0.0.1:{port}",
        "tls": {},
        "claim_key": {"key_id": "primary", "source": {"kind": "env", "name": "PMT_TEST_CLAIM_KEY"},
                       "retained": []},
        "proxy": {"enabled": False, "trusted": []},
        "access": {"allowed_sources": list(["10.8.0.0/24"] if sources is None else sources)},
        "service": {"kind": kind, "name": "PMT Host", "account": account, "app_root": str(app)},
        "logging": {"level": "info", "retain_days": 30},
        "registry": {"projects": []},
    }
    publish_config(config_root, config, create=True)
    return config_root, config


class _FakeAdapter:
    platform = "windows"

    def __init__(self, *, admin=True, missing_dirs=False, bad_acl=False, fail_at=None):
        self.admin = admin
        self.missing_dirs = missing_dirs
        self.bad_acl = bad_acl
        self.fail_at = fail_at
        self.executed = []
        self.firewall = {"backend": "windows", "exists": False, "matches": False}
        self.service = {"exists": False, "matches": False}
        self.before_admin = None
        self.before_execute = None

    def is_admin(self):
        if self.before_admin:
            self.before_admin()
        return self.admin

    def current_account(self):
        return "EXAMPLE\\current-user"

    def directory_state(self, _path):
        return {"exists": not self.missing_dirs, "safe": True}

    def acl_matches(self, _path, _policy):
        return not self.bad_acl

    def firewall_state(self, _config, allow_public=False):
        return {**self.firewall, "allow_public": allow_public}

    def service_state(self, _config, _root, *, current_account=None, with_backup_timer=False):
        return {**self.service, "current_account": current_account}

    def execute(self, command):
        if self.before_execute:
            self.before_execute()
        self.executed.append(copy.deepcopy(command))
        if self.fail_at is not None and len(self.executed) == self.fail_at:
            return {"ok": False, "exit_code": 23}
        self.firewall = {"backend": "windows", "exists": True, "matches": True}
        self.service = {"exists": True, "matches": True}
        return {"ok": True, "exit_code": 0}


def test_windows_task_plan_quotes_paths_and_uses_required_restart_and_limited_settings(tmp_path):
    root = tmp_path / "package root with spaces"
    root.mkdir()
    config_root, config = _config(root)
    spec = service.windows_task_spec(config, config_root)
    assert spec["name"] == "PMT Host"
    assert spec["task_path"] == "\\"
    assert spec["executable"].endswith(r"PMT App With Spaces\venv\Scripts\python.exe")
    assert "--config-root \"" in spec["arguments"] and spec["arguments"].endswith('" serve')
    assert spec["logon_type"] == "ServiceAccount"
    assert spec["run_level"] == "Limited"
    assert spec["multiple_instances"] == "IgnoreNew"
    assert spec["restart_count"] == 3 and spec["restart_interval_minutes"] == 1
    assert spec["execution_time_limit_seconds"] == 0
    assert spec["start_when_available"] and spec["allow_start_on_battery"] and not spec["stop_on_battery"]
    script = service.windows_register_command(spec)
    assert "-RunLevel Limited" in script and "-RestartCount 3" in script
    assert "-RestartInterval (New-TimeSpan -Minutes 1)" in script
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in script
    assert "Register-ScheduledTask -TaskName 'PMT Host' -TaskPath '\\'" in script
    assert "Start-ScheduledTask -TaskName 'PMT Host' -TaskPath '\\'" in script
    assert "password" not in script.casefold()
    inspection = service.windows_task_inspection_script(spec)
    assert "Get-ScheduledTask -TaskName 'PMT Host' -TaskPath '\\'" in inspection
    assert "NTAccount]::new($identity)).Translate([System.Security.Principal.SecurityIdentifier])" in inspection
    assert "$actualSid -eq $expectedSid" in inspection


def test_windows_named_user_uses_s4u_and_current_resolves_actual_account(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    config_root, config = _config(root, account="pmt-host")
    assert service.windows_task_spec(config, config_root)["logon_type"] == "S4U"
    config["service"]["account"] = "current"
    monkeypatch.setattr(service, "resolve_windows_current_account", lambda: "EXAMPLE\\operator")
    spec = service.windows_task_spec(config, config_root)
    assert spec["account"] == "EXAMPLE\\operator" and spec["account"] != "current"
    assert spec["logon_type"] == "S4U"


@pytest.mark.skipif(os.name != "nt", reason="Windows account SID alias resolution")
def test_windows_scheduled_task_account_aliases_resolve_to_same_sid_without_task_changes():
    script = r"""
$names = @('LOCALSERVICE', 'NT AUTHORITY\LOCAL SERVICE')
$sids = @($names | ForEach-Object { ([System.Security.Principal.NTAccount]::new($_)).Translate([System.Security.Principal.SecurityIdentifier]).Value })
$currentName = (& whoami.exe).Trim()
$currentByName = ([System.Security.Principal.NTAccount]::new($currentName)).Translate([System.Security.Principal.SecurityIdentifier]).Value
$currentToken = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
ConvertTo-Json -Compress -InputObject @{ aliases = $sids; currentName = $currentName; currentByName = $currentByName; currentToken = $currentToken }
"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout.strip())
    assert len(set(value["aliases"])) == 1
    assert value["currentName"] != "current" and value["currentByName"] == value["currentToken"]


def test_windows_restart_is_stop_then_start_and_only_named_task_is_touched(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    config_root, config = _config(root)
    commands = service.service_commands("restart", config, config_root, platform="windows")
    assert len(commands) == 2
    assert "Stop-ScheduledTask -TaskName 'PMT Host' -TaskPath '\\'" in commands[0]["script"]
    assert "Start-ScheduledTask -TaskName 'PMT Host' -TaskPath '\\'" in commands[1]["script"]
    assert all("Restart-ScheduledTask" not in command["script"] for command in commands)
    status = service.service_commands("status", config, config_root, platform="windows")[0]["script"]
    remove = service.service_commands("remove", config, config_root, platform="windows")[0]["script"]
    assert "Get-ScheduledTask -TaskName 'PMT Host' -TaskPath '\\'" in status
    assert "Unregister-ScheduledTask -TaskName 'PMT Host' -TaskPath '\\'" in remove


def test_optional_windows_backup_task_is_named_daily_and_not_started_immediately(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    config_root, config = _config(root)
    spec = service.windows_backup_task_spec(config, config_root)
    assert spec["name"] == "PMT Host Backup" and spec["task_path"] == "\\"
    assert spec["schedule"] == "daily" and spec["daily_at"] == "03:00"
    assert spec["arguments"].endswith(" backup --apply")
    script = service.windows_register_command(spec)
    assert "New-ScheduledTaskTrigger -Daily -At $backupAt" in script
    assert "Register-ScheduledTask -TaskName 'PMT Host Backup' -TaskPath '\\'" in script
    assert "Start-ScheduledTask" not in script
    inspection = service.windows_task_inspection_script(spec)
    assert "Get-ScheduledTask -TaskName 'PMT Host Backup' -TaskPath '\\'" in inspection
    assert "CalendarTrigger" in inspection and "FromHours(3)" in inspection


def test_backup_flag_adds_only_missing_named_artifact_and_hash_is_stable(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    config_root, config = _config(root)
    current = {"exists": True, "matches": True, "backup_exists": False, "backup_matches": False}
    with_backup = service.service_plan(config, config_root, current=current, platform="windows",
                                      action="install", with_backup_timer=True)
    assert with_backup["status"] == "planned"
    assert with_backup["commands"] and all("PMT Host Backup" in item["script"]
                                           for item in with_backup["commands"])
    assert "backup_timer" in with_backup["artifact_sha256"]
    repeated = service.service_artifact_hashes(config, config_root, platform="windows", with_backup_timer=True)
    assert repeated == with_backup["artifact_sha256"]

    # A normal install never observes or alters a companion backup definition.
    with_backup_state = {**current, "backup_exists": True, "backup_matches": False}
    normal = service.service_plan(config, config_root, current=with_backup_state, platform="windows",
                                  action="install", with_backup_timer=False)
    assert normal["status"] == "current" and normal["commands"] == []
    removal = service.service_plan(config, config_root, current=with_backup_state, platform="windows",
                                   action="remove")
    assert any("PMT Host Backup" in item["script"] for item in removal["commands"])
    assert all("Other" not in item["script"] for item in removal["commands"])


def test_optional_linux_backup_units_are_daily_persistent_with_artifact_hash(tmp_path, monkeypatch):
    root = tmp_path / "linux-config"
    root.mkdir()
    config_root, config = _config(root, platform="linux", account="pmt")
    config["claim_key"]["source"] = {"kind": "file", "path": str(root / "secrets" / "claim-primary.key")}
    monkeypatch.setattr(service, "validate_systemd_credential_sources", lambda *_args: {})
    service_plan = service.service_plan(config, config_root,
        current={"exists": True, "matches": True, "backup_exists": False, "backup_matches": False},
        platform="linux", action="install", with_backup_timer=True)
    backup_unit, timer = service.systemd_backup_units(config, config_root)
    assert "ExecStart=" in backup_unit and "backup" in backup_unit and "--apply" in backup_unit
    assert "OnCalendar=*-*-* 03:00:00" in timer and "Persistent=true" in timer
    assert "Unit=pmt-host-backup.service" in timer
    assert service_plan["commands"]
    assert any("pmt-host-backup.service" in " ".join(command.get("argv", []))
               or "ExecStart=" in command.get("stdin", "") and "backup --apply" in command.get("stdin", "")
               for command in service_plan["commands"])
    assert any("pmt-host-backup.timer" in " ".join(command.get("argv", []))
               or "Unit=pmt-host-backup.service" in command.get("stdin", "")
               for command in service_plan["commands"])
    assert service_plan["artifact_sha256"]["backup_timer"]
    regular = service.service_plan(config, config_root,
        current={"exists": True, "matches": True, "backup_exists": True, "backup_matches": False},
        platform="linux", action="install", with_backup_timer=False)
    assert regular["status"] == "current" and regular["commands"] == []


def test_service_parser_exposes_backup_timer_only_on_install():
    import argparse

    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    service.add_service_commands(commands.add_parser)
    args = parser.parse_args(["service", "install", "--with-backup-timer"])
    assert args.service_command == "install" and args.with_backup_timer
    with pytest.raises(SystemExit):
        parser.parse_args(["service", "start", "--with-backup-timer"])


def test_systemd_unit_has_restrictive_paths_and_stable_credential_ids_without_config_writes(tmp_path):
    root = tmp_path / "linux-config"
    root.mkdir()
    config_root, config = _config(root, platform="linux", account="pmt")
    config["claim_key"]["source"] = {"kind": "file", "path": "/etc/pmt-host/secrets/claim-primary.key"}
    config["claim_key"]["retained"] = [{"key_id": "previous", "source": {
        "kind": "file", "path": "/etc/pmt-host/secrets/claim-previous.key"}}]
    config["tls"] = {"cert_file": "/etc/pmt-host/tls/host.crt", "key_file": "/etc/pmt-host/tls/host.key"}
    before = copy.deepcopy(config)
    unit, mapping = service.systemd_unit(config, config_root)
    assert mapping == {
        "claim-primary": "/etc/pmt-host/secrets/claim-primary.key",
        "claim-previous": "/etc/pmt-host/secrets/claim-previous.key",
        "tls-key": "/etc/pmt-host/tls/host.key",
    }
    assert "LoadCredential=claim-primary:" in unit and "LoadCredential=claim-previous:" in unit
    assert "LoadCredential=tls-key:" in unit
    assert "ProtectSystem=strict" in unit and "ProtectHome=true" in unit
    assert "PrivateTmp=true" in unit and "UMask=0077" in unit
    assert '"' + str(config_root).replace("\\", "\\\\") + '"' in unit
    assert '"' + config["paths"]["data_root"].replace("\\", "\\\\") + '"' in unit
    assert config == before


def test_systemd_placeholder_fallbacks_are_deterministic_and_unsupported_sources_fail(tmp_path):
    root = tmp_path / "pmt-host"
    config = {
        "claim_key": {"key_id": "primary", "source": {"kind": "file", "path": "${CREDENTIALS_DIRECTORY}/claim-primary"},
                       "retained": []},
        "tls": {"key_file": "${CREDENTIALS_DIRECTORY}/tls-key"},
    }
    assert service.credential_source_map(config, root) == {
        "claim-primary": str(root / "secrets" / "claim-primary.key"),
        "tls-key": str(root / "tls" / "tls-key"),
    }
    config["claim_key"]["source"] = {"kind": "env", "name": "PMT_SECRET"}
    with pytest.raises(PmtError) as error:
        service.credential_source_map(config, root)
    assert error.value.code == "service_unsupported"


@pytest.mark.parametrize("error_code", ["host_key_unavailable", "host_key_insecure"])
def test_systemd_placeholder_validation_blocks_missing_or_insecure_fallback(monkeypatch, tmp_path, error_code):
    root = tmp_path / "host config"
    root.mkdir()
    config_root, config = _config(root, platform="linux", account="pmt")
    config["claim_key"]["source"] = {"kind": "file", "path": "${CREDENTIALS_DIRECTORY}/claim-primary"}
    monkeypatch.setattr(secrets, "check_source", lambda source, account=None: (_ for _ in ()).throw(
        PmtError(error_code, "fallback unavailable or insecure")))
    plan = service.service_plan(config, config_root, current={"exists": False, "matches": False},
                                platform="linux", action="install")
    assert plan["status"] == "blocked" and plan["error_code"] == error_code
    assert plan["commands"] == []


def test_windows_firewall_uses_named_restricted_rule_and_public_is_explicit(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    _config_root, config = _config(root, sources=["10.8.0.0/24", "192.168.0.0/24"])
    current = {"backend": "windows", "exists": True, "matches": False}
    plan = firewall.plan_firewall(config, current=current, platform="windows")
    script = plan["commands"][0]["script"]
    assert "Remove-NetFirewallRule -DisplayName 'PMT Host 18765'" in script
    assert "-Direction Inbound -Protocol TCP -LocalPort 18765" in script
    assert "-Profile Domain,Private" in script and "Public" not in script
    assert "10.8.0.0/24" in script and "192.168.0.0/24" in script
    public = firewall.plan_firewall(config, current={"matches": False}, platform="windows", allow_public=True)
    assert "-Profile Domain,Private,Public" in public["commands"][0]["script"]


def test_linux_firewall_plans_idempotent_rich_rules_and_named_ufw_rules(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    _config_root, config = _config(root, platform="linux", sources=["10.8.0.0/24", "2001:db8::/64"])
    expected = firewall.plan_firewall(config, current={"rich_rules": []}, platform="linux", backend="firewalld")
    assert len(expected["commands"]) == 3
    assert expected["commands"][0]["argv"][:2] == ["firewall-cmd", "--permanent"]
    already = firewall.plan_firewall(config, current={"rich_rules": expected["desired"]["rich_rules"]},
                                     platform="linux", backend="firewalld")
    assert already["status"] == "current" and already["commands"] == []
    conflict = firewall.plan_firewall(config, current={"rich_rules": [
        'rule family="ipv4" source address="192.0.2.0/24" port port="18765" protocol="tcp" accept'
    ]}, platform="linux", backend="firewalld")
    assert conflict["status"] == "blocked" and conflict["error_code"] == "firewall_unowned_rules"
    assert conflict["commands"] == []

    ufw = firewall.plan_firewall(config,
        current={"rules": [("10.8.0.0/24", 18765, "PMT Host 18765"),
                           ("192.0.2.0/24", 18765, "Other app"),
                           ("192.0.2.0/24", 18765, "PMT Host 18765")]},
        platform="linux", backend="ufw")
    assert any(command["argv"][1] == "allow" and "2001:db8::/64" in command["argv"] for command in ufw["commands"])
    assert any(command["argv"][1:3] == ["delete", "allow"] for command in ufw["commands"])
    assert all("Other app" not in command["argv"] for command in ufw["commands"])


def test_firewall_requires_allowed_sources_and_unknown_linux_backend_is_manual(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    _config_root, config = _config(root, sources=[])
    blocked = firewall.plan_firewall(config, current={"backend": "windows"}, platform="windows")
    assert blocked["status"] == "blocked" and blocked["error_code"] == "firewall_sources_required"
    config["access"]["allowed_sources"] = ["10.8.0.0/24"]
    manual = firewall.plan_firewall(config, current={"backend": "none"}, platform="linux", backend="none")
    assert manual["status"] == "manual" and manual["commands"] == []


def test_operations_plan_commands_equal_applied_commands_and_second_apply_is_noop(tmp_path):
    config_root, config = _config(tmp_path)
    before = (config_root / "host-config.json").read_bytes()
    adapter = _FakeAdapter()
    plan = operations.plan_operations(config_root, only=["dirs", "acl", "tls", "firewall", "service"], adapter=adapter)
    expected = [command for item in plan["components"] for command in item["commands"]]
    result = operations.apply_operations(config_root, only=["dirs", "acl", "tls", "firewall", "service"],
                                         adapter=adapter, apply=True)
    assert result["ok"] and adapter.executed == expected
    count = len(adapter.executed)
    second = operations.apply_operations(config_root, only=["dirs", "acl", "tls", "firewall", "service"],
                                         adapter=adapter, apply=True)
    assert second["unchanged"] and len(adapter.executed) == count
    assert (config_root / "host-config.json").read_bytes() == before


def test_operations_plans_explicit_directory_acl_repair_without_inherited_only_grants(monkeypatch, tmp_path):
    config_root, _config_value = _config(tmp_path)
    monkeypatch.setattr(operations, "_account_sid", lambda _account: "S-1-5-19")
    adapter = _FakeAdapter(missing_dirs=True, bad_acl=True)
    plan = operations.plan_operations(config_root, only=["dirs", "acl"], adapter=adapter)
    dirs, acl = plan["components"]
    assert dirs["status"] == "planned" and dirs["missing"]
    assert all(command["kind"] == "powershell" and "New-Item -ItemType Directory" in command["script"]
               for command in dirs["commands"])
    assert acl["status"] == "planned" and acl["drift"]
    acl_script = acl["commands"][0]["script"]
    assert "$acl.SetAccessRuleProtection($true, $false)" in acl_script
    assert "S-1-5-32-544" in acl_script and "S-1-5-18" in acl_script and "S-1-5-19" in acl_script
    assert all(item["requires_admin"] for item in (dirs, acl))


def test_operations_requires_explicit_apply_and_admin_and_stops_at_first_failure(tmp_path):
    config_root, config = _config(tmp_path)
    dry = _FakeAdapter()
    plan = operations.apply_operations(config_root, only=["firewall", "service"], adapter=dry)
    assert not plan["applied"] and dry.executed == []
    with pytest.raises(PmtError) as error:
        operations.apply_operations(config_root, only=["firewall", "service"],
                                     adapter=_FakeAdapter(admin=False), apply=True)
    assert error.value.code == "admin_required"
    failing = _FakeAdapter(fail_at=2)
    result = operations.apply_operations(config_root, only=["firewall", "service"], adapter=failing, apply=True)
    assert not result["ok"] and result["failed_component"] == "service"
    assert len(failing.executed) == 2


def test_operations_unknown_component_fails_before_planning(tmp_path):
    config_root, _config_value = _config(tmp_path)
    with pytest.raises(PmtError) as error:
        operations.plan_operations(config_root, only=["firewall", "host"], adapter=_FakeAdapter())
    assert error.value.code == "host_input_invalid"


@pytest.mark.parametrize("entrypoint", ["operations", "service"])
def test_apply_lock_blocks_concurrent_config_cas_until_first_command_finishes(tmp_path, entrypoint):
    config_root, _config_value = _config(tmp_path)
    adapter = _FakeAdapter()
    publisher = {"process": None}
    project_src = Path(__file__).resolve().parents[2] / "src"
    child_code = (
        "import sys; "
        f"sys.path.insert(0, {str(project_src)!r}); "
        "from pmt.server_admin.config import load_config_snapshot, publish_config; "
        "from pathlib import Path; "
        f"root=Path({str(config_root)!r}); "
        "print('publisher-ready', flush=True); "
        "cfg,digest=load_config_snapshot(root/'host-config.json'); "
        "cfg['revision']+=1; publish_config(root,cfg,digest)"
    )

    def start_publisher():
        publisher["process"] = subprocess.Popen(
            [sys.executable, "-c", child_code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert publisher["process"].stdout.readline().strip() == "publisher-ready"
        time.sleep(0.15)
        assert publisher["process"].poll() is None, "config CAS escaped the apply lock before command execution"

    def assert_still_blocked():
        assert publisher["process"].poll() is None, "config CAS completed while the stale plan was executing"

    adapter.before_admin = start_publisher
    adapter.before_execute = assert_still_blocked
    try:
        if entrypoint == "operations":
            result = operations.apply_operations(config_root, only=["firewall"], adapter=adapter, apply=True)
        else:
            result = service.run_service_command(
                SimpleNamespace(service_command="install", apply=True), config_root, adapter=adapter,
            )
        assert result["ok"] is True
        assert publisher["process"].wait(timeout=5) == 0
        assert load_config_snapshot(config_root / "host-config.json")[0]["revision"] == 2
    finally:
        if publisher["process"] is not None and publisher["process"].poll() is None:
            publisher["process"].terminate()
            publisher["process"].wait(timeout=5)
