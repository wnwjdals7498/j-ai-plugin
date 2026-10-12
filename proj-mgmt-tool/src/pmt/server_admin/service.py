"""Deterministic automatic-start plans for the PMT Host service."""
from __future__ import annotations

import os
import getpass
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import subprocess

from ..errors import PmtError

WINDOWS_TASK_NAME = "PMT Host"
WINDOWS_BACKUP_TASK_NAME = "PMT Host Backup"
WINDOWS_TASK_PATH = "\\"
SYSTEMD_UNIT_NAME = "pmt-host.service"
SYSTEMD_UNIT_PATH = "/etc/systemd/system/pmt-host.service"
SYSTEMD_BACKUP_SERVICE_NAME = "pmt-host-backup.service"
SYSTEMD_BACKUP_SERVICE_PATH = "/etc/systemd/system/pmt-host-backup.service"
SYSTEMD_BACKUP_TIMER_NAME = "pmt-host-backup.timer"
SYSTEMD_BACKUP_TIMER_PATH = "/etc/systemd/system/pmt-host-backup.timer"
_CREDENTIALS_DIRECTORY = "${CREDENTIALS_DIRECTORY}/"


def credential_source_map(config, config_root):
    """Map stable systemd credential ids to their configured file sources.

    Absolute file references map to themselves. Credential-directory references
    use the explicit HostConfigRoot fallback convention shared with the runtime
    resolver. This function is intentionally pure; callers validate files before
    rendering or applying a service plan.
    """
    root = Path(config_root)
    result = {}

    def add(alias, source):
        if alias in result:
            raise PmtError("service_unsupported", "Systemd credential aliases are ambiguous")
        if not isinstance(source, dict) or source.get("kind") != "file":
            raise PmtError("service_unsupported", "systemd LoadCredential requires file-backed secrets")
        value = source.get("path")
        if not isinstance(value, str) or not value:
            raise PmtError("service_unsupported", "A file-backed credential source is unavailable")
        prefix = _CREDENTIALS_DIRECTORY
        if value.startswith(prefix):
            if value != prefix + alias:
                raise PmtError("service_unsupported", "Credential path does not match its stable systemd alias")
            if alias.startswith("claim-"):
                path = root / "secrets" / f"{alias}.key"
            else:
                path = root / "tls" / alias
            result[alias] = str(path)
            return
        if not (Path(value).is_absolute() or PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()):
            raise PmtError("service_unsupported", "Credential source must be an absolute file path")
        result[alias] = value

    claim = config["claim_key"]
    key_id = claim["key_id"]
    add(f"claim-{key_id}", claim["source"])
    for item in claim.get("retained", []):
        add(f"claim-{item['key_id']}", item["source"])
    key_file = config["tls"].get("key_file")
    if key_file:
        add("tls-key", {"kind": "file", "path": key_file})
    return result


def _ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _systemd_quote(value):
    text = str(value)
    if any(char in text for char in "\r\n\x00"):
        raise PmtError("service_unsupported", "Systemd values cannot contain line breaks")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("%", "%%") + '"'


def resolve_windows_current_account():
    """Resolve the actual token account without trusting a `current` literal."""
    try:
        result = subprocess.run(["whoami.exe"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PmtError("host_account_invalid", "Could not resolve the current Windows task account") from exc
    account = result.stdout.strip()
    if result.returncode or not account or any(char in account for char in "\r\n\x00"):
        raise PmtError("host_account_invalid", "Could not resolve the current Windows task account")
    return account


def windows_task_spec(config, config_root, *, current_account=None, task_name=WINDOWS_TASK_NAME,
                     schedule="startup"):
    """Return the exact Task Scheduler fields and argv for the Host task."""
    service = config["service"]
    app_root = Path(service["app_root"])
    executable = app_root / "venv" / "Scripts" / "python.exe"
    config_root = str(config_root)
    operation = ["backup", "--apply"] if schedule == "daily" else ["serve"]
    arguments = subprocess.list2cmdline(["-m", "pmt.server_admin", "--config-root", config_root, *operation])
    account = service["account"]
    if account == "current":
        account = current_account or resolve_windows_current_account()
        if not account or account == "current":
            raise PmtError("host_account_invalid", "Could not resolve the current Windows task account")
    builtin = {"system", "nt authority\\system", "localsystem", "nt authority\\local service",
               "localservice", "nt authority\\network service", "networkservice"}
    logon_type = "ServiceAccount" if account.casefold() in builtin else "S4U"
    if logon_type == "S4U":
        paths = [str(config_root), service["app_root"], *config["paths"].values()]
        paths.extend(value for value in config["tls"].values())
        claim_sources = [config["claim_key"]["source"],
                         *(item["source"] for item in config["claim_key"].get("retained", []))]
        paths.extend(source["path"] for source in claim_sources if source.get("kind") == "file")
        if any(PureWindowsPath(path).drive.startswith("\\\\") for path in paths):
            raise PmtError("service_unsupported", "S4U tasks cannot use network-backed PMT paths")
    return {
        "name": task_name,
        "task_path": WINDOWS_TASK_PATH,
        "executable": str(executable),
        "arguments": arguments,
        "working_directory": str(app_root),
        "account": account,
        "logon_type": logon_type,
        "trigger": "Daily" if schedule == "daily" else "AtStartup",
        "schedule": schedule,
        "daily_at": "03:00" if schedule == "daily" else None,
        "run_level": "Limited",
        "multiple_instances": "IgnoreNew",
        "restart_count": 3,
        "restart_interval_minutes": 1,
        "execution_time_limit_seconds": 0,
        "start_when_available": True,
        "allow_start_on_battery": True,
        "stop_on_battery": False,
    }


def windows_register_command(spec):
    """Build one quoted PowerShell command for the exact named PMT task."""
    p = _ps_literal
    return "\n".join([
        "$action = New-ScheduledTaskAction -Execute " + p(spec["executable"])
        + " -Argument " + p(spec["arguments"])
        + " -WorkingDirectory " + p(spec["working_directory"]),
        ("$backupAt = [datetime]::Today.AddHours(3); $trigger = New-ScheduledTaskTrigger -Daily -At $backupAt"
         if spec["schedule"] == "daily" else "$trigger = New-ScheduledTaskTrigger -AtStartup"),
        "$principal = New-ScheduledTaskPrincipal -UserId " + p(spec["account"])
        + " -LogonType " + spec["logon_type"] + " -RunLevel Limited",
        "$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3"
        + " -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)"
        + " -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries",
        "Register-ScheduledTask -TaskName " + p(spec["name"])
        + " -TaskPath " + p(WINDOWS_TASK_PATH)
        + " -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null",
        *(["Start-ScheduledTask -TaskName " + p(spec["name"]) + " -TaskPath " + p(WINDOWS_TASK_PATH)]
          if spec["schedule"] == "startup" else []),
    ])


def windows_backup_task_spec(config, config_root, *, current_account=None):
    """Stable daily backup task definition, isolated from the Host task."""
    return windows_task_spec(config, config_root, current_account=current_account,
                             task_name=WINDOWS_BACKUP_TASK_NAME, schedule="daily")


def windows_task_inspection_script(spec):
    """Inspect only the rooted PMT task and compare principals by resolved SID."""
    p = _ps_literal
    return "\n".join([
        f"$tasks = @(Get-ScheduledTask -TaskName {p(spec['name'])} -TaskPath {p(WINDOWS_TASK_PATH)} -ErrorAction SilentlyContinue)",
        "if ($tasks.Count -eq 0) { '{\"exists\":false,\"matches\":false}' ; exit 0 }",
        "$t = $tasks[0]; $a = $t.Actions[0]; $p = $t.Principal; $s = $t.Settings; $tr = $t.Triggers[0]",
        "$resolveSid = { param([string]$identity); if ([string]::IsNullOrWhiteSpace($identity)) { return '' }; if ($identity -match '^S-1-') { return $identity.ToUpperInvariant() }; try { return ([System.Security.Principal.NTAccount]::new($identity)).Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { return '' } }",
        "$actualSid = & $resolveSid ([string]$p.UserId)",
        f"$expectedSid = & $resolveSid {p(spec['account'])}",
        f"$match = ($tasks.Count -eq 1) -and ($a.Execute -ieq {p(spec['executable'])})",
        f"$match = $match -and ($a.Arguments -ceq {p(spec['arguments'])}) -and ($a.WorkingDirectory -ieq {p(spec['working_directory'])})",
        f"$match = $match -and ($actualSid -ne '') -and ($actualSid -eq $expectedSid) -and ($p.LogonType -eq {p(spec['logon_type'])}) -and ($p.RunLevel -eq 'Limited')",
        ("$match = $match -and ($tr.CimClass.CimClassName -match 'CalendarTrigger') -and ($tr.DaysInterval -eq 1) -and (([datetime]$tr.StartBoundary).TimeOfDay -eq ([TimeSpan]::FromHours(3)))"
         if spec["schedule"] == "daily" else "$match = $match -and ($tr.CimClass.CimClassName -match 'BootTrigger')"),
        "$match = $match -and ($s.MultipleInstances -eq 'IgnoreNew') -and ($s.RestartCount -eq 3)",
        "$match = $match -and ($s.RestartInterval -eq ([TimeSpan]::FromMinutes(1))) -and ($s.ExecutionTimeLimit -eq [TimeSpan]::Zero)",
        "$match = $match -and $s.StartWhenAvailable -and $s.AllowStartIfOnBatteries -and $s.DontStopIfGoingOnBatteries",
        ("$match = $match -and ($t.State -in @('Ready','Running'))"
         if spec["schedule"] == "daily" else "$match = $match -and ($t.State -eq 'Running')"),
        "ConvertTo-Json -Compress -InputObject @{ exists = $true; matches = [bool]$match; state = [string]$t.State }",
    ])


def systemd_unit(config, config_root):
    """Render the restrictive pmt-host.service unit and its credential source map."""
    service = config["service"]
    if service["kind"] != "systemd":
        raise PmtError("service_unsupported", "systemd service generation requires service.kind=systemd")
    app_root = Path(service["app_root"])
    python = app_root / "venv" / "bin" / "python"
    args = ["-m", "pmt.server_admin", "--config-root", str(config_root), "serve"]
    command = " ".join(_systemd_quote(value) for value in [str(python), *args])
    data, logs, backup = (str(config["paths"][key]) for key in ("data_root", "log_dir", "backup_dir"))
    credentials = credential_source_map(config, config_root)
    account = service["account"]
    if account == "current":
        account = getpass.getuser()
    lines = [
        "[Unit]",
        "Description=PMT Host",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "User=" + account,
        "Group=" + account,
        "WorkingDirectory=" + _systemd_quote(app_root),
        "Environment=PMT_HOST_CONFIG_ROOT=" + _systemd_quote(config_root),
        "ExecStart=" + command,
    ]
    lines.extend("LoadCredential=" + alias + ":" + _systemd_quote(source)
                 for alias, source in sorted(credentials.items()))
    lines.extend([
        "Restart=on-failure",
        "RestartSec=5",
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "PrivateTmp=true",
        "ReadWritePaths=" + " ".join(_systemd_quote(path) for path in (str(config_root), data, logs, backup)),
        "UMask=0077",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])
    return "\n".join(lines), credentials


def systemd_backup_units(config, config_root):
    """Render the PMT-owned oneshot service and daily backup timer."""
    service_cfg = config["service"]
    account = getpass.getuser() if service_cfg["account"] == "current" else service_cfg["account"]
    app_root = Path(service_cfg["app_root"])
    python = app_root / "venv" / "bin" / "python"
    command = " ".join(_systemd_quote(value) for value in
                       [str(python), "-m", "pmt.server_admin", "--config-root", str(config_root), "backup", "--apply"])
    data, logs, backup = (str(config["paths"][key]) for key in ("data_root", "log_dir", "backup_dir"))
    unit = "\n".join([
        "[Unit]", "Description=PMT Host backup", "",
        "[Service]", "Type=oneshot", "User=" + account, "Group=" + account,
        "WorkingDirectory=" + _systemd_quote(app_root),
        "Environment=PMT_HOST_CONFIG_ROOT=" + _systemd_quote(config_root),
        "ExecStart=" + command,
        "NoNewPrivileges=true", "ProtectSystem=strict", "ProtectHome=true", "PrivateTmp=true",
        "ReadWritePaths=" + " ".join(_systemd_quote(path) for path in (str(config_root), data, logs, backup)),
        "UMask=0077", "",
    ])
    timer = "\n".join([
        "[Unit]", "Description=Daily PMT Host backup", "",
        "[Timer]", "OnCalendar=*-*-* 03:00:00", "Persistent=true",
        "Unit=" + SYSTEMD_BACKUP_SERVICE_NAME, "",
        "[Install]", "WantedBy=timers.target", "",
    ])
    return unit, timer


def _artifact_hash(*values):
    digest = hashlib.sha256()
    for value in values:
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def service_artifact_hashes(config, config_root, *, platform=None, current_account=None, with_backup_timer=False):
    platform = platform or ("windows" if os.name == "nt" else "linux")
    if platform == "windows":
        spec = windows_task_spec(config, config_root, current_account=current_account)
        service_hash = _artifact_hash(json.dumps(spec, sort_keys=True, separators=(",", ":")))
        if not with_backup_timer:
            return {"service": service_hash}
        backup = windows_backup_task_spec(config, config_root, current_account=current_account)
        return {"service": service_hash,
                "backup_timer": _artifact_hash(json.dumps(backup, sort_keys=True, separators=(",", ":")))}
    unit, _ = systemd_unit(config, config_root)
    result = {"service": _artifact_hash(unit)}
    if with_backup_timer:
        backup_service, backup_timer = systemd_backup_units(config, config_root)
        result["backup_timer"] = _artifact_hash(backup_service, backup_timer)
    return result


def validate_systemd_credential_sources(config, config_root):
    """Verify every systemd credential alias resolves to a safe local file."""
    from .secrets import check_source
    from .tls import check_tls
    from .runtime_paths import resolve_runtime_config

    sources = credential_source_map(config, config_root)
    account = config["service"]["account"]
    if account == "current":
        account = getpass.getuser()
    resolved = resolve_runtime_config(config, config_root, environ={})
    claim = resolved["claim_key"]
    items = [(claim["key_id"], claim["source"]),
             *((item["key_id"], item["source"]) for item in claim.get("retained", []))]
    for _key_id, source in items:
        check_source(source, account=account)
    if "tls-key" in sources:
        check_tls(resolved)
    return sources


def backup_service_commands(action, config, config_root, *, platform=None, current_account=None):
    """Build commands for only the PMT-named daily backup artifacts."""
    platform = platform or ("windows" if os.name == "nt" else "linux")
    if action == "install":
        if platform == "windows":
            spec = windows_backup_task_spec(config, config_root, current_account=current_account)
            return [{"kind": "powershell", "script": windows_register_command(spec)}]
        unit, timer = systemd_backup_units(config, config_root)
        return [
            {"kind": "exec", "argv": ["install", "-D", "-m", "0644", "/dev/stdin", SYSTEMD_BACKUP_SERVICE_PATH], "stdin": unit},
            {"kind": "exec", "argv": ["install", "-D", "-m", "0644", "/dev/stdin", SYSTEMD_BACKUP_TIMER_PATH], "stdin": timer},
            {"kind": "exec", "argv": ["systemctl", "daemon-reload"]},
            {"kind": "exec", "argv": ["systemctl", "enable", "--now", SYSTEMD_BACKUP_TIMER_NAME]},
        ]
    if action == "remove":
        if platform == "windows":
            name = _ps_literal(WINDOWS_BACKUP_TASK_NAME)
            path = _ps_literal(WINDOWS_TASK_PATH)
            script = (f"$task = Get-ScheduledTask -TaskName {name} -TaskPath {path} -ErrorAction SilentlyContinue; "
                      f"if ($null -ne $task) {{ Stop-ScheduledTask -TaskName {name} -TaskPath {path} -ErrorAction SilentlyContinue; "
                      f"Unregister-ScheduledTask -TaskName {name} -TaskPath {path} -Confirm:$false }}")
            return [{"kind": "powershell", "script": script}]
        return [
            {"kind": "exec", "argv": ["systemctl", "disable", "--now", SYSTEMD_BACKUP_TIMER_NAME]},
            {"kind": "exec", "argv": ["rm", "--", SYSTEMD_BACKUP_SERVICE_PATH]},
            {"kind": "exec", "argv": ["rm", "--", SYSTEMD_BACKUP_TIMER_PATH]},
            {"kind": "exec", "argv": ["systemctl", "daemon-reload"]},
            {"kind": "exec", "argv": ["systemctl", "reset-failed", SYSTEMD_BACKUP_SERVICE_NAME]},
        ]
    raise PmtError("host_input_invalid", "Unsupported PMT backup timer action")


def service_commands(action, config, config_root, *, platform=None, current_account=None, with_backup_timer=False):
    """Build ordered, exact commands for a service lifecycle operation."""
    platform = platform or ("windows" if os.name == "nt" else "linux")
    kind = config["service"]["kind"]
    if kind == "none":
        raise PmtError("service_unsupported", "Automatic service is disabled in host configuration")
    expected = "windows-task" if platform == "windows" else "systemd"
    if kind != expected:
        raise PmtError("service_unsupported", "Configured service kind is not supported on this operating system")
    if action not in {"install", "remove", "status", "start", "stop", "restart"}:
        raise PmtError("host_input_invalid", "Unsupported service action")

    if platform == "windows":
        p = _ps_literal
        if action == "install":
            spec = windows_task_spec(config, config_root, current_account=current_account)
            commands = [{"kind": "powershell", "script": windows_register_command(spec)}]
            if with_backup_timer:
                backup_spec = windows_backup_task_spec(config, config_root, current_account=current_account)
                commands.append({"kind": "powershell", "script": windows_register_command(backup_spec)})
            return commands
        if action == "remove":
            names = [WINDOWS_TASK_NAME, *([WINDOWS_BACKUP_TASK_NAME] if with_backup_timer else [])]
            commands = []
            for name in names:
                script = ("$task = Get-ScheduledTask -TaskName " + p(name) + " -TaskPath " + p(WINDOWS_TASK_PATH)
                          + " -ErrorAction SilentlyContinue; if ($null -ne $task) { Stop-ScheduledTask -TaskName "
                          + p(name) + " -TaskPath " + p(WINDOWS_TASK_PATH)
                          + " -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName "
                          + p(name) + " -TaskPath " + p(WINDOWS_TASK_PATH) + " -Confirm:$false }")
                commands.append({"kind": "powershell", "script": script})
            return commands
        elif action == "status":
            script = ("$task = Get-ScheduledTask -TaskName " + p(WINDOWS_TASK_NAME) + " -TaskPath " + p(WINDOWS_TASK_PATH)
                      + " -ErrorAction SilentlyContinue; if ($null -eq $task) { '{\"exists\":false}' } "
                      + "else { $task | Select-Object TaskName,State,Actions,Triggers,Principal,Settings | ConvertTo-Json -Depth 8 -Compress }")
        elif action == "restart":
            return [{"kind": "powershell", "script": f"Stop-ScheduledTask -TaskName {p(WINDOWS_TASK_NAME)} -TaskPath {p(WINDOWS_TASK_PATH)} -ErrorAction SilentlyContinue"},
                    {"kind": "powershell", "script": f"Start-ScheduledTask -TaskName {p(WINDOWS_TASK_NAME)} -TaskPath {p(WINDOWS_TASK_PATH)}"}]
        else:
            verb = {"start": "Start", "stop": "Stop"}[action]
            script = f"{verb}-ScheduledTask -TaskName {p(WINDOWS_TASK_NAME)} -TaskPath {p(WINDOWS_TASK_PATH)}"
        return [{"kind": "powershell", "script": script}]

    if action == "install":
        unit, _credentials = systemd_unit(config, config_root)
        commands = [{"kind": "exec", "argv": ["install", "-D", "-m", "0644", "/dev/stdin", SYSTEMD_UNIT_PATH], "stdin": unit}]
        if with_backup_timer:
            backup_service, backup_timer = systemd_backup_units(config, config_root)
            commands.extend([
                {"kind": "exec", "argv": ["install", "-D", "-m", "0644", "/dev/stdin", SYSTEMD_BACKUP_SERVICE_PATH], "stdin": backup_service},
                {"kind": "exec", "argv": ["install", "-D", "-m", "0644", "/dev/stdin", SYSTEMD_BACKUP_TIMER_PATH], "stdin": backup_timer},
            ])
        commands.extend([
            {"kind": "exec", "argv": ["systemctl", "daemon-reload"]},
            {"kind": "exec", "argv": ["systemctl", "enable", "--now", SYSTEMD_UNIT_NAME]},
        ])
        if with_backup_timer:
            commands.append({"kind": "exec", "argv": ["systemctl", "enable", "--now", SYSTEMD_BACKUP_TIMER_NAME]})
        return commands
    if action == "remove":
        commands = [
            {"kind": "exec", "argv": ["systemctl", "disable", "--now", SYSTEMD_UNIT_NAME]},
            {"kind": "exec", "argv": ["rm", "--", SYSTEMD_UNIT_PATH]},
        ]
        if with_backup_timer:
            commands.extend([
                {"kind": "exec", "argv": ["systemctl", "disable", "--now", SYSTEMD_BACKUP_TIMER_NAME]},
                {"kind": "exec", "argv": ["rm", "--", SYSTEMD_BACKUP_SERVICE_PATH]},
                {"kind": "exec", "argv": ["rm", "--", SYSTEMD_BACKUP_TIMER_PATH]},
            ])
        commands.extend([
            {"kind": "exec", "argv": ["systemctl", "daemon-reload"]},
            {"kind": "exec", "argv": ["systemctl", "reset-failed", SYSTEMD_UNIT_NAME]},
        ])
        if with_backup_timer:
            commands.append({"kind": "exec", "argv": ["systemctl", "reset-failed", SYSTEMD_BACKUP_SERVICE_NAME]})
        return commands
    if action == "status":
        return [{"kind": "exec", "argv": ["systemctl", "show", SYSTEMD_UNIT_NAME,
                                             "--property=LoadState,ActiveState,SubState,UnitFileState"]}]
    if action == "restart":
        return [{"kind": "exec", "argv": ["systemctl", "stop", SYSTEMD_UNIT_NAME]},
                {"kind": "exec", "argv": ["systemctl", "start", SYSTEMD_UNIT_NAME]}]
    verb = {"start": "start", "stop": "stop"}[action]
    return [{"kind": "exec", "argv": ["systemctl", verb, SYSTEMD_UNIT_NAME]}]


def service_plan(config, config_root, *, current=None, action="install", platform=None,
                 current_account=None, with_backup_timer=False):
    """Compare registered service state to the desired PMT-owned definition."""
    if current is None:
        current = {"exists": False, "matches": False}
    if config["service"]["kind"] == "none" and action in {"install", "status"}:
        return {"component": "service", "action": action, "status": "disabled",
                "commands": [], "current": current}
    if action == "install" and platform == "linux":
        try:
            validate_systemd_credential_sources(config, config_root)
        except PmtError as error:
            return {"component": "service", "action": action, "status": "blocked",
                    "error_code": error.code, "message": str(error), "commands": []}
    artifacts = (service_artifact_hashes(config, config_root, platform=platform,
                                        current_account=current_account, with_backup_timer=with_backup_timer)
                 if action == "install" else {})
    if action == "install":
        commands = []
        if not current.get("matches"):
            if current.get("exists"):
                if platform == "windows":
                    commands.extend(service_commands("remove", config, config_root, platform=platform,
                                                     current_account=current_account))
                else:
                    commands.extend([
                        {"kind": "exec", "argv": ["systemctl", "disable", "--now", SYSTEMD_UNIT_NAME]},
                        {"kind": "exec", "argv": ["rm", "--", SYSTEMD_UNIT_PATH]},
                        {"kind": "exec", "argv": ["systemctl", "daemon-reload"]},
                    ])
            commands.extend(service_commands("install", config, config_root, platform=platform,
                                             current_account=current_account))
        if with_backup_timer and not current.get("backup_matches"):
            if current.get("backup_exists"):
                commands.extend(backup_service_commands("remove", config, config_root, platform=platform,
                                                        current_account=current_account))
            commands.extend(backup_service_commands("install", config, config_root, platform=platform,
                                                    current_account=current_account))
    elif action == "remove":
        commands = []
        if current.get("exists"):
            commands.extend(service_commands("remove", config, config_root, platform=platform,
                                             current_account=current_account))
        if current.get("backup_exists"):
            commands.extend(backup_service_commands("remove", config, config_root, platform=platform,
                                                    current_account=current_account))
    else:
        commands = service_commands(action, config, config_root, platform=platform,
                                    current_account=current_account)
    return {"component": "service", "action": action,
            "status": "current" if not commands else "planned",
            "commands": commands, "current": current, "with_backup_timer": with_backup_timer,
            "artifact_sha256": artifacts}


def add_service_commands(command_factory):
    """Add S-09 subcommands to the shared main-owned server CLI parser."""
    service = command_factory("service")
    actions = service.add_subparsers(dest="service_command", required=True)
    for action in ("install", "remove", "status", "start", "stop", "restart"):
        child = actions.add_parser(action)
        import argparse
        child.add_argument("--config-root", default=argparse.SUPPRESS)
        child.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        if action != "status":
            child.add_argument("--apply", action="store_true")
        if action == "install":
            child.add_argument("--with-backup-timer", action="store_true")
    return service


def execute_service_plan(plan, adapter, *, apply=False):
    """Return a dry-run plan or execute the exact listed commands in order."""
    if plan["status"] == "disabled":
        return {"ok": True, "applied": bool(apply), "disabled": True, "component": "service"}
    if not apply:
        return {"ok": True, "applied": False, **plan}
    if plan["status"] == "current":
        return {"ok": True, "applied": True, "unchanged": True, **plan}
    if not adapter.is_admin():
        raise PmtError("admin_required", "Administrator privileges are required for service changes", 2)
    results = []
    for index, command in enumerate(plan["commands"]):
        result = adapter.execute(command)
        results.append(result)
        if not result.get("ok", False):
            raise PmtError("service_apply_failed", f"Service plan stopped at command {index + 1}", 2)
    return {"ok": True, "applied": True, "unchanged": False,
            "component": "service", "action": plan["action"], "results": results}


def inspect_service_state(config, config_root, *, platform, runner, current_account=None, with_backup_timer=False):
    """Read-only inspection of the rooted Host service and optional backup timer."""
    if config["service"]["kind"] == "none":
        return {"exists": False, "matches": False, "kind": "none"}
    if platform == "windows":
        spec = windows_task_spec(config, config_root, current_account=current_account)
        result = runner({"kind": "powershell", "script": windows_task_inspection_script(spec)})
        if result is None or result.returncode != 0:
            state = {"exists": False, "matches": False, "unverified": True}
        else:
            try:
                value = json.loads(result.stdout.strip())
                state = {"exists": bool(value.get("exists")), "matches": bool(value.get("matches")),
                         "state": value.get("state")}
            except (ValueError, AttributeError):
                state = {"exists": False, "matches": False, "unverified": True}
        if with_backup_timer:
            spec = windows_backup_task_spec(config, config_root, current_account=current_account)
            backup_result = runner({"kind": "powershell", "script": windows_task_inspection_script(spec)})
            if backup_result is None or backup_result.returncode != 0:
                state.update({"backup_exists": False, "backup_matches": False, "backup_unverified": True})
            else:
                try:
                    backup = json.loads(backup_result.stdout.strip())
                    state.update({"backup_exists": bool(backup.get("exists")),
                                  "backup_matches": bool(backup.get("matches")),
                                  "backup_state": backup.get("state")})
                except (ValueError, AttributeError):
                    state.update({"backup_exists": False, "backup_matches": False, "backup_unverified": True})
        return state
    try:
        unit, _ = systemd_unit(config, config_root)
    except PmtError:
        unit = None
    try:
        existing = Path(SYSTEMD_UNIT_PATH).read_text(encoding="utf-8")
    except OSError:
        existing = None
    enabled = runner({"kind": "exec", "argv": ["systemctl", "is-enabled", SYSTEMD_UNIT_NAME]})
    active = runner({"kind": "exec", "argv": ["systemctl", "is-active", SYSTEMD_UNIT_NAME]})
    state = {"exists": existing is not None,
             "matches": unit is not None and existing == unit and enabled is not None and enabled.returncode == 0
             and active is not None and active.returncode == 0,
             "enabled": enabled.stdout.strip() if enabled else "unknown",
             "active": active.stdout.strip() if active else "unknown"}
    if with_backup_timer:
        backup_service, backup_timer = systemd_backup_units(config, config_root)
        try:
            existing_backup_service = Path(SYSTEMD_BACKUP_SERVICE_PATH).read_text(encoding="utf-8")
        except OSError:
            existing_backup_service = None
        try:
            existing_backup_timer = Path(SYSTEMD_BACKUP_TIMER_PATH).read_text(encoding="utf-8")
        except OSError:
            existing_backup_timer = None
        timer_enabled = runner({"kind": "exec", "argv": ["systemctl", "is-enabled", SYSTEMD_BACKUP_TIMER_NAME]})
        timer_active = runner({"kind": "exec", "argv": ["systemctl", "is-active", SYSTEMD_BACKUP_TIMER_NAME]})
        state.update({
            "backup_exists": existing_backup_service is not None or existing_backup_timer is not None,
            "backup_matches": existing_backup_service == backup_service and existing_backup_timer == backup_timer
            and timer_enabled is not None and timer_enabled.returncode == 0
            and timer_active is not None and timer_active.returncode == 0,
            "backup_enabled": timer_enabled.stdout.strip() if timer_enabled else "unknown",
            "backup_active": timer_active.stdout.strip() if timer_active else "unknown",
        })
    return state


def run_service_command(args, config_root, *, adapter=None):
    """Service CLI entry point for main-owned `cli.py` integration."""
    from .config import _process_lock, config_path, load_config_snapshot

    if adapter is None:
        from .operations import NativeOperationsAdapter
        adapter = NativeOperationsAdapter()
    action = args.service_command
    should_apply = bool(getattr(args, "apply", False))
    with_backup_timer = (bool(getattr(args, "with_backup_timer", False)) if action == "install"
                         else action in {"remove", "status"})

    def run_locked():
        config, digest = load_config_snapshot(config_path(config_root))
        current_account = adapter.current_account() if adapter.platform == "windows" and config["service"]["account"] == "current" else None
        current = adapter.service_state(config, config_root, current_account=current_account,
                                        with_backup_timer=with_backup_timer)
        if action == "status":
            return {"ok": True, "service": current, "config_sha256": digest}
        plan = service_plan(config, config_root, current=current, action=action, platform=adapter.platform,
                            current_account=current_account, with_backup_timer=with_backup_timer)
        if not should_apply:
            return execute_service_plan(plan, adapter, apply=False)
        _config, current_digest = load_config_snapshot(config_path(config_root))
        if current_digest != digest:
            raise PmtError("config_conflict", "Host configuration changed after plan; reload and retry", 2)
        return execute_service_plan(plan, adapter, apply=True)

    if action == "status" or not should_apply:
        return run_locked()
    with _process_lock(Path(config_root) / ".host-config.lock"):
        return run_locked()
