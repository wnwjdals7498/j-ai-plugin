"""PMT server administrative CLI. Secret bytes never appear in output."""
from __future__ import annotations

import argparse
import base64
import importlib.metadata
import importlib.util
import hashlib
import sys
import copy
import json
import os
import sqlite3
from pathlib import Path

from .. import __version__
from ..db import SCHEMA_VERSION
from ..errors import PmtError
from ..handoff import PLUGIN_VERSION
from ..host.auth import HOST_SCHEMA_VERSION
from ..util import canonical_json
from .config import _process_lock, _publish_config_locked, config_path, config_sha256, load_config, load_config_snapshot, publish_config, validate_config
from .init import init_host
from .secrets import check_source, create_key_reference, read_key, store_key
from .doctor import run_doctor
from .logging import read_logs
from .serve import serve_host
from .status import read_status
from .tls import check_tls, register_tls
from .operations import add_operations_commands, run_operations_command, NativeOperationsAdapter
from .service import add_service_commands, run_service_command
from .registry import list_projects, project_add, project_repo_add
from .devices import (device_grants, device_issue, device_list, device_revoke,
                      device_rotate, handoff_create)


def version_info():
    dependencies = {}
    for module in ("fastapi", "pydantic", "uvicorn"):
        try:
            available = importlib.util.find_spec(module) is not None
            dependencies[module] = importlib.metadata.version(module) if available else None
        except (ImportError, importlib.metadata.PackageNotFoundError):
            dependencies[module] = None
    return {"version": PLUGIN_VERSION, "core_version": __version__,
            "db_schema": SCHEMA_VERSION, "graph_schema": 1, "protocol": 1,
            "host_schema": HOST_SCHEMA_VERSION, "python": sys.executable,
            "python_version": sys.version.split()[0], "dependencies": dependencies}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pmt-server", description="PMT Host administration")
    parser.add_argument("--config-root")
    parser.add_argument("--json", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, **kwargs):
        sub = commands.add_parser(name, **kwargs)
        sub.add_argument("--config-root", default=argparse.SUPPRESS)
        sub.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        return sub

    command("version")
    config = command("config").add_subparsers(dest="config_command", required=True)
    for name in ("show", "validate"):
        sub = config.add_parser(name); sub.add_argument("--json", action="store_true", default=argparse.SUPPRESS); sub.add_argument("--config-root", default=argparse.SUPPRESS)
    set_cmd = config.add_parser("set")
    set_cmd.add_argument("key"); set_cmd.add_argument("value"); set_cmd.add_argument("--expected-sha256", required=True)
    set_cmd.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    set_cmd.add_argument("--config-root", default=argparse.SUPPRESS)
    init = command("init")
    init.add_argument("--public-url", required=True); init.add_argument("--listen", default="0.0.0.0:8765")
    init.add_argument("--data-root"); init.add_argument("--log-dir"); init.add_argument("--backup-dir"); init.add_argument("--app-root")
    init.add_argument("--service", choices=("windows-task", "systemd", "none")); init.add_argument("--account")
    init.add_argument("--allow", action="append"); init.add_argument("--tls-cert"); init.add_argument("--tls-key"); init.add_argument("--tls-ca")
    init.add_argument("--claim-key-id", default="primary"); init.add_argument("--claim-key-env"); init.add_argument("--retained", action="append")
    init.add_argument("--adopt", action="store_true"); init.add_argument("--host-config-root"); init.add_argument("--apply", action="store_true")
    secret = command("secret").add_subparsers(dest="secret_command", required=True)
    keyinit = secret.add_parser("init-claim-key"); keyinit.add_argument("--key-id", default="primary"); keyinit.add_argument("--apply", action="store_true")
    rotate = secret.add_parser("rotate-claim-key"); rotate.add_argument("--new-key-id", required=True); rotate.add_argument("--apply", action="store_true")
    retire = secret.add_parser("retire-claim-key"); retire.add_argument("--key-id", required=True); retire.add_argument("--apply", action="store_true")
    migrate = secret.add_parser("migrate-claim-key"); migrate.add_argument("--key-id", required=True); migrate.add_argument("--to", choices=("dpapi", "file"), required=True); migrate.add_argument("--apply", action="store_true")
    secret.add_parser("check")
    for child in secret.choices.values():
        child.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        child.add_argument("--config-root", default=argparse.SUPPRESS)

    tls = command("tls").add_subparsers(dest="tls_command", required=True)
    tls_check = tls.add_parser("check")
    tls_register = tls.add_parser("register")
    tls_register.add_argument("--cert", required=True); tls_register.add_argument("--key", required=True); tls_register.add_argument("--ca")
    tls_register.add_argument("--apply", action="store_true")
    for child in tls.choices.values():
        child.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        child.add_argument("--config-root", default=argparse.SUPPRESS)
    doctor = command("doctor")
    serve = command("serve"); serve.add_argument("--listen"); serve.add_argument("--allow-loopback-http", action="store_true")
    command("status")
    logs = command("logs"); logs.add_argument("--tail", type=int, default=200); logs.add_argument("--since")

    project = command("project").add_subparsers(dest="project_command", required=True)
    project_add_cmd = project.add_parser("add")
    project_add_cmd.add_argument("--name", required=True); project_add_cmd.add_argument("--title"); project_add_cmd.add_argument("--apply", action="store_true")
    project_repo = project.add_parser("repo").add_subparsers(dest="repo_command", required=True)
    repo_add = project_repo.add_parser("add")
    repo_add.add_argument("--project", required=True); repo_add.add_argument("--name", required=True)
    repo_add.add_argument("--remote", required=True); repo_add.add_argument("--graph-path", default="docs/pmt-docs/graph.json")
    repo_add.add_argument("--apply", action="store_true")
    project.add_parser("list")
    for child in (project_add_cmd, repo_add, project.choices["list"]):
        child.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        child.add_argument("--config-root", default=argparse.SUPPRESS)

    device = command("device").add_subparsers(dest="device_command", required=True)
    device.add_parser("list")
    issue = device.add_parser("issue")
    issue.add_argument("--actor", required=True); issue.add_argument("--project", action="append", required=True)
    issue.add_argument("--permission", action="append"); issue.add_argument("--allow-admin", action="store_true")
    issue.add_argument("--credential-out"); issue.add_argument("--handoff-out"); issue.add_argument("--include-ca", action="store_true"); issue.add_argument("--apply", action="store_true")
    rotate_device = device.add_parser("rotate")
    rotate_device.add_argument("--device", required=True); rotate_device.add_argument("--credential-out"); rotate_device.add_argument("--apply", action="store_true")
    grants = device.add_parser("grants")
    grants.add_argument("--device", required=True); grants.add_argument("--project", action="append", required=True)
    grants.add_argument("--permission", action="append", required=True); grants.add_argument("--allow-admin", action="store_true"); grants.add_argument("--apply", action="store_true")
    revoke_device = device.add_parser("revoke")
    revoke_device.add_argument("--device", required=True); revoke_device.add_argument("--apply", action="store_true")
    handoff = command("handoff").add_subparsers(dest="handoff_command", required=True)
    handoff_create_cmd = handoff.add_parser("create")
    handoff_create_cmd.add_argument("--device", required=True); handoff_create_cmd.add_argument("--out", required=True)
    handoff_create_cmd.add_argument("--include-ca", action="store_true"); handoff_create_cmd.add_argument("--apply", action="store_true")
    for child in (*device.choices.values(), handoff_create_cmd):
        child.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        child.add_argument("--config-root", default=argparse.SUPPRESS)

    add_operations_commands(command)
    add_service_commands(command)

    args = parser.parse_args(argv)
    root = Path(getattr(args, "config_root", None) or os.environ.get("PMT_HOST_CONFIG_ROOT") or (r"C:\ProgramData\PMT\host-config" if os.name == "nt" else "/etc/pmt-host"))
    try:
        if args.command == "version":
            result = version_info()
            missing = [name for name, installed in result["dependencies"].items() if installed is None]
            if missing:
                value = {"ok": False, "result": result, "error": {"code": "host_dependency_missing", "message": "Install proj-mgmt-tool[host]", "retryable": False}}
                print(canonical_json(value) if args.json else "Install proj-mgmt-tool[host]")
                return 5
        elif args.command in {"plan", "apply"}:
            result = run_operations_command(args, root)
        elif args.command == "service":
            result = run_service_command(args, root, adapter=NativeOperationsAdapter())
        elif args.command == "init":
            result = init_host(args, root)
        elif args.command == "doctor":
            result = run_doctor(root)
        elif args.command == "status":
            result = read_status(root)
        elif args.command == "logs":
            result = {"ok": True, "lines": read_logs(load_config(root / "host-config.json")["paths"]["log_dir"], args.tail, since=args.since)}
        elif args.command == "serve":
            return serve_host(root, listen=args.listen, allow_loopback_http=args.allow_loopback_http)
        elif args.command == "tls":
            config_value = load_config(root / "host-config.json")
            if args.tls_command == "check":
                from .runtime_paths import resolve_runtime_config
                result = check_tls(resolve_runtime_config(config_value, root, environ={}))
            else: result = register_tls(root, args.cert, args.key, args.ca, apply=args.apply)
        elif args.command == "project":
            if args.project_command == "add": result = project_add(root, args.name, title=args.title, apply=args.apply)
            elif args.project_command == "repo":
                result = project_repo_add(root, args.project, args.name, args.remote, args.graph_path, apply=args.apply)
            else: result = list_projects(root)
        elif args.command == "device":
            if args.device_command == "list": result = device_list(root)
            elif args.device_command == "issue":
                result = device_issue(root, args.actor, args.project, args.permission, allow_admin=args.allow_admin,
                                      credential_out=args.credential_out, handoff_out=args.handoff_out,
                                      include_ca=args.include_ca, apply=args.apply)
            elif args.device_command == "rotate": result = device_rotate(root, args.device, credential_out=args.credential_out, apply=args.apply)
            elif args.device_command == "grants":
                result = device_grants(root, args.device, args.project, args.permission, allow_admin=args.allow_admin, apply=args.apply)
            else: result = device_revoke(root, args.device, apply=args.apply)
        elif args.command == "handoff":
            result = handoff_create(root, args.device, args.out, include_ca=args.include_ca, apply=args.apply)
        elif args.command == "config":
            path = config_path(root)
            if args.config_command == "show":
                config_value, digest = load_config_snapshot(path)
                result = {"ok": True, "config": config_value, "sha256": digest}
            elif args.config_command == "validate":
                _, digest = load_config_snapshot(path); result = {"ok": True, "valid": True, "sha256": digest}
            else:
                current, digest = load_config_snapshot(path)
                if digest != args.expected_sha256:
                    raise PmtError("config_conflict", "Host configuration changed; reload and retry")
                updated = copy.deepcopy(current)
                try: value = json.loads(args.value)
                except json.JSONDecodeError: value = args.value
                keys = args.key.split(".")
                target = updated
                for key in keys[:-1]:
                    if not isinstance(target, dict) or key not in target: raise PmtError("config_invalid", "Unknown configuration field")
                    target = target[key]
                if not isinstance(target, dict) or keys[-1] not in target: raise PmtError("config_invalid", "Unknown configuration field")
                target[keys[-1]] = value; updated["revision"] += 1; validate_config(updated)
                publish_config(root, updated, args.expected_sha256)
                result = {"ok": True, "revision": updated["revision"], "sha256": config_sha256(path)}
        else:
            result = _secret_command(args, root)
        if args.command == "version" and not args.json:
            print(f"PMT Server {result['version']} | Core {result['core_version']}")
            print(f"SQLite {result['db_schema']} | graph {result['graph_schema']} | protocol {result['protocol']}")
        elif args.json:
            print(canonical_json(result))
        else:
            if args.command == "doctor":
                for check in result["checks"]:
                    print(f"{check['status'].upper():4} {check['name']}: {check['message']}")
                    if check.get("guidance"): print(f"     {check['guidance']}")
            elif args.command == "logs": print("\n".join(result["lines"]))
            else: print(_human_result(args, result))
        return 1 if result.get("ok") is False else 0
    except PmtError as exc:
        value = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        print(canonical_json(value) if args.json else f"Error {exc.code}: {exc}")
        return getattr(exc, "exit_code", 2)
    except (OSError, sqlite3.Error) as exc:
        value = {"ok": False, "error": {"code": "host_io_error", "message": "The operation failed without changing existing state"}}
        print(canonical_json(value) if args.json else "Error host_io_error: The operation failed without changing existing state")
        return 2
    except ValueError:
        value = {"ok": False, "error": {"code": "host_input_invalid", "message": "The requested value is invalid"}}
        print(canonical_json(value) if args.json else "Error host_input_invalid: The requested value is invalid")
        return 2


def _secret_command(args, root):
    with _process_lock(Path(root) / ".host-config.lock"):
        return _secret_command_locked(args, root)


def _secret_command_locked(args, root):
    path = config_path(root)
    config, digest = load_config_snapshot(path)
    current = config["claim_key"]
    account = config["service"]["account"]
    if args.secret_command == "check":
        from .runtime_paths import resolve_runtime_config
        resolved = resolve_runtime_config(config, root)["claim_key"]
        return {"ok": True, "primary": check_source(resolved["source"], account=account),
                "retained": [{"key_id": item["key_id"], **check_source(item["source"], account=account)} for item in resolved.get("retained", [])]}
    if not args.apply:
        target_id = getattr(args, "new_key_id", None) or getattr(args, "key_id", None)
        return {"ok": True, "applied": False, "key_id": target_id, "operation": args.secret_command}

    created_target = None
    created_digest = None
    if args.secret_command == "init-claim-key":
        if args.key_id != current["key_id"]:
            raise PmtError("config_invalid", "init-claim-key must match the configured primary key id")
        source = current["source"]
        if source["kind"] == "env":
            raise PmtError("host_key_unavailable", "An environment key reference cannot be regenerated")
        created_target = Path(source["path"])
        if created_target.exists():
            raise PmtError("config_exists", "The configured primary claim key already exists")
        store_key(base64.b64encode(os.urandom(48)), created_target, source["kind"], account=account)
        created_digest = _file_digest(created_target)
    elif args.secret_command == "rotate-claim-key":
        if args.new_key_id == current["key_id"] or any(item["key_id"] == args.new_key_id for item in current.get("retained", [])):
            raise PmtError("config_invalid", "New claim key id is already configured")
        source = create_key_reference(root, args.new_key_id, account=account)
        created_target = Path(source["path"])
        created_digest = _file_digest(created_target)
        config["claim_key"]["retained"] = [{"key_id": current["key_id"], "source": current["source"]}, *current.get("retained", [])]
        config["claim_key"]["key_id"], config["claim_key"]["source"] = args.new_key_id, source
    elif args.secret_command == "migrate-claim-key":
        item = current if current["key_id"] == args.key_id else next((entry for entry in current.get("retained", []) if entry["key_id"] == args.key_id), None)
        if item is None:
            raise PmtError("host_key_unavailable", "Claim key id is not configured")
        if item["source"]["kind"] == args.to:
            check_source(item["source"], account=account)
            return {"ok": True, "applied": True, "key_id": args.key_id, "unchanged": True}
        from .runtime_paths import resolve_runtime_config
        resolved = resolve_runtime_config(config, root)["claim_key"]
        resolved_item = resolved if resolved["key_id"] == args.key_id else next(entry for entry in resolved.get("retained", []) if entry["key_id"] == args.key_id)
        value = base64.b64encode(read_key(resolved_item["source"], account=account))
        suffix = ".dpapi" if args.to == "dpapi" else ".key"
        created_target = root / "secrets" / f"claim-{args.key_id}{suffix}"
        item["source"] = store_key(value, created_target, args.to, account=account)
        created_digest = _file_digest(created_target)
    elif args.secret_command == "retire-claim-key":
        item = current if current["key_id"] == args.key_id else next((entry for entry in current.get("retained", []) if entry["key_id"] == args.key_id), None)
        if item is None:
            raise PmtError("host_key_unavailable", "Claim key id is not configured")
        database = Path(config["paths"]["data_root"]) / "pmt.sqlite3"
        try:
            with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as conn:
                active = conn.execute("SELECT 1 FROM host_claim_leases WHERE key_id=? AND state='active' LIMIT 1", (args.key_id,)).fetchone()
        except sqlite3.Error as exc:
            raise PmtError("host_schema_unsupported", "Could not verify active claim leases") from exc
        if active:
            raise PmtError("claim_key_in_use", "Claim key has an active lease")
        if item is current:
            if not current.get("retained"):
                raise PmtError("config_invalid", "Cannot retire the only configured claim key")
            replacement = current["retained"].pop(0)
            current["key_id"], current["source"] = replacement["key_id"], replacement["source"]
        else:
            current["retained"].remove(item)
    else:
        raise PmtError("config_invalid", "Unsupported secret command")

    config["revision"] += 1
    try:
        _publish_config_locked(root, config, digest)
    except Exception:
        if created_target is not None and created_digest is not None:
            try:
                if _file_digest(created_target) == created_digest: created_target.unlink()
            except OSError:
                pass
        raise
    return {"ok": True, "applied": True, "revision": config["revision"]}


def _file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _human_result(args, result):
    if args.command == "init":
        return ("Plan" if not result.get("applied") else "Initialized") + f" {result.get('config_path', '')}"
    if args.command == "config":
        return "Configuration valid" if result.get("valid") else f"Configuration revision {result.get('revision', result.get('config', {}).get('revision', '?'))}"
    if args.command == "secret": return "Claim key check complete" if args.secret_command == "check" else "Claim key plan complete" if not result.get("applied") else "Claim key configuration updated"
    if args.command == "doctor": return "Host diagnostics passed" if result.get("ok") else "Host diagnostics found failures"
    if args.command == "status": return f"Host {'running' if result['service']['running'] else 'not running'}; namespace {result.get('namespace_id') or 'unavailable'}"
    if args.command == "tls": return "TLS check complete" if args.tls_command == "check" else "TLS registration plan complete" if not result.get("applied") else "TLS files registered"
    if args.command == "project":
        if args.project_command == "list": return "\n".join(f"{item['name']}\t{item['project_id']}" for item in result["projects"]) or "No projects registered"
        return "Project registration plan complete" if not result.get("applied") else "Project registered"
    if args.command == "device":
        if args.device_command == "list": return "\n".join(f"{item['device_id']}\t{item['actor']}\t{item['state']}\t{','.join(item['permissions'])}" for item in result["devices"])
        if "credential" in result: return canonical_json(result)
        return "Device plan complete" if not result.get("applied") else "Device operation complete"
    if args.command == "handoff": return "Handoff plan complete" if not result.get("applied") else "Handoff file created"
    return canonical_json(result)


if __name__ == "__main__":
    raise SystemExit(main())
