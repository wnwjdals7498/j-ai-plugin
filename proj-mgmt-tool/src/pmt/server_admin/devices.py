"""S12 local AuthRegistry device lifecycle and S13 handoff outputs."""
from __future__ import annotations

import uuid
from pathlib import Path

from ..errors import PmtError
from ..host.auth import PERMISSIONS
from ..util import canonical_json
from .config import _process_lock, config_path, load_config_snapshot
from .registry import (_host, build_device_handoff, remove_owned_file, write_new_file,
                       _validate_new_file_path)

_DEFAULT_PERMISSIONS = ["read", "write", "runtime", "review"]


def _permission_values(values, default=False):
    if not values:
        return sorted(_DEFAULT_PERMISSIONS) if default else None
    result = []
    for value in values:
        result.extend(part.strip() for part in value.split(",") if part.strip())
    if not result or any(item not in PERMISSIONS for item in result) or len(set(result)) != len(result):
        raise PmtError("host_input_invalid", "Device permissions must be distinct supported values")
    return sorted(result)


def _device_scopes(config, project_names, allow_admin):
    if not isinstance(project_names, list) or not project_names:
        raise PmtError("host_input_invalid", "Select at least one project")
    by_name = {item["name"]: item for item in config["registry"]["projects"]}
    if "*" in project_names:
        if not allow_admin:
            raise PmtError("scope_forbidden", "Wildcard scope requires --allow-admin")
        if len(project_names) != 1: raise PmtError("host_input_invalid", "Wildcard cannot be combined with named projects")
        return ["*"]
    scopes = []
    for name in project_names:
        if name not in by_name: raise PmtError("project_not_found", "Selected project is not registered")
        scopes.append(by_name[name]["project_id"])
    if len(set(scopes)) != len(scopes): raise PmtError("host_input_invalid", "Project selections must be distinct")
    return sorted(scopes)


def _check_privileged(scopes, permissions, allow_admin):
    if ("*" in scopes or "admin" in permissions) and not allow_admin:
        raise PmtError("scope_forbidden", "Wildcard scope and admin permission require --allow-admin")


def _device_row(auth, device_id):
    row = next((device for device in auth.list_devices() if device["device_id"] == device_id), None)
    if row is None: raise PmtError("device_not_found", "Device is not registered")
    return row


def device_list(config_root):
    with _process_lock(Path(config_root) / ".host-config.lock"):
        _config, _digest, _db, auth = _host(config_root)
        return {"ok": True, "devices": auth.list_devices()}


def device_issue(config_root, actor, project_names, permissions=None, *, allow_admin=False,
                 credential_out=None, handoff_out=None, include_ca=False, apply=False):
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 200 or any(ord(char) < 32 for char in actor):
        raise PmtError("host_input_invalid", "Device actor must be bounded text")
    root = Path(config_root)
    if not apply:
        config, _ = load_config_snapshot(config_path(root))
        scopes = _device_scopes(config, project_names, allow_admin)
        grants = _permission_values(permissions, default=True)
        _check_privileged(scopes, grants, allow_admin)
        if include_ca and not handoff_out: raise PmtError("host_input_invalid", "--include-ca requires --handoff-out")
        if handoff_out and "*" in scopes: raise PmtError("handoff_invalid", "Wildcard devices cannot be included in a handoff")
        return {"ok": True, "applied": False, "operation": "device_issue", "actor": actor,
                "scopes": scopes, "permissions": grants}

    with _process_lock(root / ".host-config.lock"):
        config, _digest, _db, auth = _host(root)
        scopes = _device_scopes(config, project_names, allow_admin)
        grants = _permission_values(permissions, default=True)
        _check_privileged(scopes, grants, allow_admin)
        if include_ca and not handoff_out: raise PmtError("host_input_invalid", "--include-ca requires --handoff-out")
        if handoff_out and "*" in scopes: raise PmtError("handoff_invalid", "Wildcard devices cannot be included in a handoff")
        if credential_out: _validate_new_file_path(credential_out)
        if handoff_out: _validate_new_file_path(handoff_out)
        device_id = str(uuid.uuid4())
        device = None
        credential_file = handoff_file = None
        try:
            device = auth.issue_device(actor, scopes, grants, device_id=device_id)
            handoff = None
            if handoff_out:
                row = {key: device[key] for key in ("device_id", "actor", "scopes", "permissions")}
                row.update(revision=1, state="active")
                handoff = build_device_handoff(config, auth, row, include_ca=include_ca)
            if credential_out:
                credential_file = write_new_file(credential_out, device["credential"] + "\n", sensitive=True)
            if handoff_out:
                handoff_file = write_new_file(handoff_out, canonical_json(handoff) + "\n")
        except Exception:
            if handoff_file: remove_owned_file(handoff_file)
            if credential_file: remove_owned_file(credential_file)
            if device is not None:
                try: auth.revoke_device(device_id, 1)
                except Exception as revoke_error:
                    raise PmtError("device_issue_revoke_failed",
                                   f"Device {device_id} could not be revoked after output failure; revoke it manually") from revoke_error
            raise
        result = {"ok": True, "applied": True, "device_id": device_id, "actor": actor,
                  "namespace_id": auth.namespace_id, "scopes": scopes, "permissions": grants,
                  "revision": 1, "state": "active"}
        if credential_file: result["credential_file"] = credential_file["path"]
        else: result["credential"] = device["credential"]  # one-time output only when caller omitted --credential-out
        if handoff_file: result["handoff_file"] = handoff_file["path"]
        return result


def device_rotate(config_root, device_id, *, credential_out=None, apply=False):
    root = Path(config_root)
    if not apply:
        return {"ok": True, "applied": False, "operation": "device_rotate", "device_id": device_id}
    with _process_lock(root / ".host-config.lock"):
        _config, _digest, _db, auth = _host(root)
        row = _device_row(auth, device_id)
        if row["state"] != "active": raise PmtError("device_revision_conflict", "Device is absent, revoked or changed", 3)
        if credential_out: _validate_new_file_path(credential_out)
        rotation = auth.rotate_device(device_id, row["revision"])
        try:
            credential_file = write_new_file(credential_out, rotation["credential"] + "\n", sensitive=True) if credential_out else None
        except Exception:
            try: auth.revoke_device(device_id, rotation["revision"])
            except Exception as revoke_error:
                raise PmtError("device_rotate_revoke_failed",
                               f"Device {device_id} could not be revoked after credential-output failure") from revoke_error
            raise
        result = {"ok": True, "applied": True, "device_id": device_id, "revision": rotation["revision"]}
        if credential_file: result["credential_file"] = credential_file["path"]
        else: result["credential"] = rotation["credential"]
        return result


def device_grants(config_root, device_id, project_names, permissions, *, allow_admin=False, apply=False):
    root = Path(config_root)
    if not apply:
        config, _ = load_config_snapshot(config_path(root))
        scopes = _device_scopes(config, project_names, allow_admin)
        grants = _permission_values(permissions)
        _check_privileged(scopes, grants, allow_admin)
        return {"ok": True, "applied": False, "operation": "device_grants", "device_id": device_id,
                "scopes": scopes, "permissions": grants}
    with _process_lock(root / ".host-config.lock"):
        config, _digest, _db, auth = _host(root)
        scopes = _device_scopes(config, project_names, allow_admin)
        grants = _permission_values(permissions)
        _check_privileged(scopes, grants, allow_admin)
        row = _device_row(auth, device_id)
        result = auth.update_grants(device_id, row["revision"], scopes, grants)
        return {"ok": True, "applied": True, **result, "state": "active"}


def device_revoke(config_root, device_id, *, apply=False):
    root = Path(config_root)
    if not apply: return {"ok": True, "applied": False, "operation": "device_revoke", "device_id": device_id}
    with _process_lock(root / ".host-config.lock"):
        _config, _digest, _db, auth = _host(root)
        row = _device_row(auth, device_id)
        result = auth.revoke_device(device_id, row["revision"])
        return {"ok": True, "applied": True, **result}


def handoff_create(config_root, device_id, output, *, include_ca=False, apply=False):
    root = Path(config_root)
    if not apply:
        config, _ = load_config_snapshot(config_path(root))
        return {"ok": True, "applied": False, "operation": "handoff_create", "device_id": device_id,
                "output": str(output), "include_ca": bool(include_ca)}
    _validate_new_file_path(output)
    with _process_lock(root / ".host-config.lock"):
        config, _digest, _db, auth = _host(root)
        row = _device_row(auth, device_id)
        document = build_device_handoff(config, auth, row, include_ca=include_ca)
        written = write_new_file(output, canonical_json(document) + "\n")
        return {"ok": True, "applied": True, "device_id": device_id, "output": written["path"],
                "namespace_id": auth.namespace_id, "project_count": len(document["projects"]),
                "includes_ca": "ca_pem" in document["host"]}
