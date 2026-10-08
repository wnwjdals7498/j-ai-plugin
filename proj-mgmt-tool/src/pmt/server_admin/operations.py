"""Read-only plan and explicit apply orchestration for Host operations."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess

from ..errors import PmtError
from .config import _process_lock, config_path, load_config_snapshot
from .secrets import _account_sid, _account_uid, check_source
from .tls import check_tls
from . import firewall, runtime_paths, service

COMPONENTS = ("dirs", "acl", "tls", "claim_key", "firewall", "service")


def _exec(argv, *, stdin=None):
    value = {"kind": "exec", "argv": [str(arg) for arg in argv]}
    if stdin is not None:
        value["stdin"] = stdin
    return value


def _powershell(script):
    return {"kind": "powershell", "script": script}


def _ps(value):
    return "'" + str(value).replace("'", "''") + "'"


def _uid_gid(account, *, resolved=None):
    if resolved is not None:
        uid, gid = resolved
        return int(uid), int(gid)
    uid = _account_uid(account)
    if uid is None:
        raise PmtError("host_account_invalid", "Could not resolve the configured service account")
    import pwd
    try:
        gid = pwd.getpwuid(uid).pw_gid
    except KeyError as exc:
        raise PmtError("host_account_invalid", "Could not resolve the configured service account group") from exc
    return uid, gid


def _acl_policy(path, config_root, config):
    path = Path(path)
    root = Path(config_root)
    account = config["service"]["account"]
    if path == root:
        return {"kind": "directory", "owner": "Administrators" if os.name == "nt" else 0,
                "account": account, "account_rights": "ReadAndExecute", "mode": 0o750}
    if path == config_path(root):
        return {"kind": "file", "owner": "Administrators" if os.name == "nt" else 0,
                "account": account, "account_rights": "Read", "mode": 0o640}
    return {"kind": "directory", "owner": account, "account": account,
            "account_rights": "Modify", "mode": 0o700}


def _acl_command(path, policy, platform, *, account_ids=None):
    if platform == "windows":
        sid = _account_sid(policy["account"])
        rights = "Modify" if policy["account_rights"] == "Modify" else policy["account_rights"]
        class_name = "DirectorySecurity" if policy["kind"] == "directory" else "FileSecurity"
        access_type = "FileSystemAccessRule"
        inherit = "$inherit = [System.Security.AccessControl.InheritanceFlags]::None; $prop = [System.Security.AccessControl.PropagationFlags]::None"
        if policy["kind"] == "directory":
            inherit = "$inherit = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit; $prop = [System.Security.AccessControl.PropagationFlags]::None"
        script = "\n".join([
            f"$acl = New-Object System.Security.AccessControl.{class_name}",
            "$acl.SetAccessRuleProtection($true, $false)",
            inherit,
            "$admin = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')",
            "$system = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')",
            f"$account = New-Object System.Security.Principal.SecurityIdentifier({_ps(sid)})",
            "$allow = [System.Security.AccessControl.AccessControlType]::Allow",
            "$full = [System.Security.AccessControl.FileSystemRights]::FullControl",
            f"$right = [System.Security.AccessControl.FileSystemRights]::{rights}",
            f"$acl.AddAccessRule((New-Object System.Security.AccessControl.{access_type}($admin, $full, $inherit, $prop, $allow)))",
            f"$acl.AddAccessRule((New-Object System.Security.AccessControl.{access_type}($system, $full, $inherit, $prop, $allow)))",
            f"$acl.AddAccessRule((New-Object System.Security.AccessControl.{access_type}($account, $right, $inherit, $prop, $allow)))",
            f"$acl.SetOwner($admin)",
            f"Set-Acl -LiteralPath {_ps(path)} -AclObject $acl",
        ])
        return _powershell(script)
    uid, gid = _uid_gid(policy["account"], resolved=account_ids)
    owner = "root" if policy["owner"] == 0 else str(uid)
    group = str(gid) if policy["owner"] == 0 else str(gid)
    mode = format(policy["mode"], "04o")
    return _exec(["chown", "--", f"{owner}:{group}", str(path)]), _exec(["chmod", "--", mode, str(path)])


def _expected_dirs(config_root, config):
    return [Path(config_root) / "secrets", Path(config_root) / "tls",
            *(Path(value) for value in config["paths"].values())]


def _directory_commands(path, config, platform, *, account_ids=None):
    account = config["service"]["account"]
    if platform == "windows":
        return [_powershell(f"New-Item -ItemType Directory -Force -LiteralPath {_ps(path)} | Out-Null")]
    uid, gid = _uid_gid(account, resolved=account_ids)
    return [_exec(["install", "-d", "-m", "0700", "-o", str(uid), "-g", str(gid), "--", str(path)])]


def _select_only(only):
    if only is None:
        return COMPONENTS
    names = tuple(dict.fromkeys(item.strip() for item in only if item.strip()))
    unsupported = set(names) - set(COMPONENTS)
    if unsupported:
        raise PmtError("host_input_invalid", "Unsupported plan component")
    return tuple(name for name in COMPONENTS if name in names)


def _component(name, status, commands=(), **details):
    return {"name": name, "status": status, "commands": list(commands), **details}


class NativeOperationsAdapter:
    """Read state without mutation; execute only a plan explicitly passed to apply."""

    platform = "windows" if os.name == "nt" else "linux"

    def is_admin(self):
        if os.name == "nt":
            import ctypes
            try:
                return bool(ctypes.windll.shell32.IsUserAnAdmin())
            except (AttributeError, OSError):
                return False
        return hasattr(os, "geteuid") and os.geteuid() == 0

    def account_ids(self, account):
        return _uid_gid(account)

    def execute(self, command):
        if command["kind"] == "powershell":
            argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command["script"]]
        else:
            argv = command["argv"]
        try:
            result = subprocess.run(argv, input=command.get("stdin"), capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", check=False)
        except (OSError, subprocess.TimeoutExpired):
            return {"ok": False, "exit_code": 127}
        return {"ok": result.returncode == 0, "exit_code": result.returncode}

    def directory_exists(self, path):
        state = self.directory_state(path)
        return state["exists"] and state["safe"]

    def directory_state(self, path):
        target = Path(path)
        try:
            info = target.lstat()
            reparse = target.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)
            return {"exists": True, "safe": stat.S_ISDIR(info.st_mode) and not reparse}
        except FileNotFoundError:
            return {"exists": False, "safe": True}
        except OSError:
            return {"exists": False, "safe": False}

    def acl_matches(self, path, policy):
        if not Path(path).exists():
            return False
        if os.name == "nt":
            try:
                from .secrets import _check_windows_acl
                _check_windows_acl(path, policy["account"])
                return True
            except PmtError:
                return False
        try:
            target = Path(path)
            info = target.stat()
            expected_uid = 0 if policy["owner"] == 0 else _account_uid(policy["account"])
            expected_mode = policy["mode"]
            if info.st_uid != expected_uid or stat.S_IMODE(info.st_mode) != expected_mode:
                return False
            if policy["owner"] == 0:
                return info.st_gid == _uid_gid(policy["account"])[1]
            return info.st_gid == _uid_gid(policy["account"])[1]
        except (OSError, PmtError):
            return False

    def firewall_state(self, config, allow_public=False):
        from .firewall import inspect_firewall_state
        return inspect_firewall_state(config, platform=self.platform, runner=self._read_command,
                                      allow_public=allow_public)

    def service_state(self, config, config_root, *, current_account=None, with_backup_timer=False):
        from .service import inspect_service_state
        if self.platform == "windows" and config["service"]["account"] == "current":
            current_account = current_account or self.current_account()
        return inspect_service_state(config, config_root, platform=self.platform,
                                     runner=self._read_command, current_account=current_account,
                                     with_backup_timer=with_backup_timer)

    def current_account(self):
        from .service import resolve_windows_current_account
        return resolve_windows_current_account() if os.name == "nt" else None

    @staticmethod
    def _read_command(command):
        if command["kind"] == "powershell":
            argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command["script"]]
        else:
            argv = command["argv"]
        try:
            return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", check=False, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None


def plan_operations(config_root, *, only=None, allow_public=False, adapter=None):
    """Compute current-to-desired operations from the strict persisted config."""
    adapter = adapter or NativeOperationsAdapter()
    config, digest = load_config_snapshot(config_path(config_root))
    names = _select_only(only)
    platform = adapter.platform
    validation_config, resolution_error = config, None
    if {"tls", "claim_key"} & set(names):
        try:
            validation_config = runtime_paths.resolve_runtime_config(config, config_root, environ={})
        except PmtError as error:
            resolution_error = error
    components = []
    account = config["service"]["account"]
    if "dirs" in names:
        commands = []
        missing = []
        unsafe = []
        account_ids = adapter.account_ids(account) if platform == "linux" else None
        for path in _expected_dirs(config_root, config):
            state = adapter.directory_state(path)
            if not state.get("safe", False):
                unsafe.append(str(path))
            elif not state.get("exists", False):
                missing.append(str(path))
                commands.extend(_directory_commands(path, config, platform, account_ids=account_ids))
        components.append(_component("dirs", "blocked" if unsafe else "planned" if commands else "current", commands,
                                     missing=missing, unsafe=unsafe, requires_admin=bool(commands),
                                     error_code="path_unsafe" if unsafe else None,
                                     message="A managed directory is a symlink, reparse point, or non-directory" if unsafe else None))
    if "acl" in names:
        config_dir = Path(config_root)
        account_ids = adapter.account_ids(account) if platform == "linux" else None
        targets = [(config_dir, _acl_policy(config_dir, config_root, config)),
                   (config_path(config_dir), _acl_policy(config_path(config_dir), config_root, config)),
                   *((path, _acl_policy(path, config_root, config)) for path in _expected_dirs(config_dir, config))]
        commands, drift = [], []
        for path, policy in targets:
            if not adapter.acl_matches(path, policy):
                drift.append(str(path))
                built = _acl_command(path, policy, platform, account_ids=account_ids)
                commands.extend(built if isinstance(built, tuple) else [built])
        components.append(_component("acl", "planned" if commands else "current", commands,
                                     drift=drift, requires_admin=bool(commands)))
    if "tls" in names:
        try:
            if resolution_error:
                raise resolution_error
            tls = check_tls(validation_config)
            components.append(_component("tls", "current", configured=tls.get("configured", False),
                                         requires_admin=False))
        except PmtError as error:
            components.append(_component("tls", "blocked", error_code=error.code,
                                         message=str(error), commands=[], requires_admin=False))
    if "claim_key" in names:
        try:
            if resolution_error:
                raise resolution_error
            claim = validation_config["claim_key"]
            checked = [check_source(claim["source"], account=account)]
            checked.extend(check_source(item["source"], account=account) for item in claim.get("retained", []))
            components.append(_component("claim_key", "current", key_ids=[claim["key_id"],
                                         *[item["key_id"] for item in claim.get("retained", [])]],
                                         source_kinds=[item["kind"] for item in checked], requires_admin=False))
        except PmtError as error:
            components.append(_component("claim_key", "blocked", key_id=config["claim_key"]["key_id"],
                                         error_code=error.code, message=str(error), commands=[], requires_admin=False))
    if "firewall" in names:
        state = adapter.firewall_state(config, allow_public=allow_public)
        value = firewall.plan_firewall(config, current=state, platform=platform,
                                       backend=state.get("backend"), allow_public=allow_public)
        components.append({"name": "firewall", **value, "requires_admin": bool(value.get("commands"))})
    if "service" in names:
        current_account = adapter.current_account() if platform == "windows" and account == "current" else None
        state = adapter.service_state(config, config_root, current_account=current_account)
        value = service.service_plan(config, config_root, current=state, platform=platform,
                                     current_account=current_account)
        components.append({"name": "service", **value, "requires_admin": bool(value.get("commands"))})
    blocked = next((item for item in components if item["status"] == "blocked"), None)
    manual = next((item for item in components if item["status"] == "manual"), None)
    return {"ok": blocked is None, "applied": False, "config_sha256": digest,
            "components": components, "blocked_component": blocked["name"] if blocked else None,
            "manual_component": manual["name"] if manual else None}


def apply_operations(config_root, *, only=None, allow_public=False, adapter=None, apply=False):
    """Apply the exact dry-run command records, stopping at first failure."""
    adapter = adapter or NativeOperationsAdapter()
    if not apply:
        return plan_operations(config_root, only=only, allow_public=allow_public, adapter=adapter)
    # The same lock is used by config.set, secret rotation, TLS registration,
    # and initialization. Hold it through snapshot, digest check, execution,
    # and read-only verification so apply never executes a stale config plan.
    with _process_lock(Path(config_root) / ".host-config.lock"):
        plan = plan_operations(config_root, only=only, allow_public=allow_public, adapter=adapter)
        blocked = next((item for item in plan["components"] if item["status"] == "blocked"), None)
        if blocked:
            raise PmtError(blocked.get("error_code", "operation_blocked"),
                           blocked.get("message", "A planned component is blocked"), 2)
        manual = next((item for item in plan["components"] if item["status"] == "manual"), None)
        if manual:
            raise PmtError("firewall_unavailable", manual.get("message", "A component requires manual action"), 2)
        commands = [(component["name"], command) for component in plan["components"]
                    for command in component.get("commands", [])]
        if not commands:
            return {**plan, "applied": True, "unchanged": True, "results": []}
        if not adapter.is_admin():
            raise PmtError("admin_required", "Administrator privileges are required for the planned changes", 2)
        _config, current_digest = load_config_snapshot(config_path(config_root))
        if current_digest != plan["config_sha256"]:
            raise PmtError("config_conflict", "Host configuration changed after plan; reload and retry", 2)
        results = []
        for index, (component, command) in enumerate(commands):
            outcome = adapter.execute(command)
            results.append({"component": component, **outcome})
            if not outcome.get("ok", False):
                return {"ok": False, "applied": True, "failed_component": component,
                        "failed_command_index": index, "results": results}
        refreshed = plan_operations(config_root, only=only, allow_public=allow_public, adapter=adapter)
        return {"ok": refreshed["ok"] and not any(item["status"] == "planned" for item in refreshed["components"]),
                "applied": True, "unchanged": False, "results": results,
                "verification": refreshed["components"]}


def add_operations_commands(command_factory):
    """Add S-08 plan/apply parsers through main-owned CLI command factory."""
    plan = command_factory("plan")
    plan.add_argument("--only", help="Comma-separated component names")
    plan.add_argument("--allow-public", action="store_true")
    apply = command_factory("apply")
    apply.add_argument("--only", help="Comma-separated component names")
    apply.add_argument("--allow-public", action="store_true")
    apply.add_argument("--apply", action="store_true")
    return plan, apply


def run_operations_command(args, config_root, *, adapter=None):
    only = getattr(args, "only", None)
    if isinstance(only, str):
        only = only.split(",")
    if args.command == "plan":
        return plan_operations(config_root, only=only, allow_public=bool(getattr(args, "allow_public", False)),
                               adapter=adapter)
    if args.command == "apply":
        return apply_operations(config_root, only=only, allow_public=bool(getattr(args, "allow_public", False)),
                                adapter=adapter, apply=bool(getattr(args, "apply", False)))
    raise PmtError("host_input_invalid", "Unsupported operations command")
