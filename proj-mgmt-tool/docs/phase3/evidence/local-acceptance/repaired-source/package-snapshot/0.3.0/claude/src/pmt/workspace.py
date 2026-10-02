"""Resolve authorized canonical workspaces to local checkouts.

Authorization is a required, current RPC callback. Canonical workspace refs are
logical identifiers; physical paths are resolved only after that RPC succeeds.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
from collections.abc import Mapping

from .errors import PmtError
from .efficiency.source import inspect_graph_source, pin_source, verify_source_pin
from .util import canonical_json, fingerprint

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def canonical_workspace(repository_id, branch_key):
    """Return stable logical workspace URI; no environment path is included."""
    repository = _uuid(repository_id, "repository_id")
    if not isinstance(branch_key, str) or not branch_key or len(branch_key) > 1024:
        raise PmtError("workspace_mapping_invalid", "branch_key must be nonempty bounded text")
    branch_hash = hashlib.sha256(branch_key.encode("utf-8")).hexdigest()
    return f"pmt://{repository}/{branch_hash}"


def _uuid(value, field):
    import uuid
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise PmtError("workspace_mapping_invalid", f"{field} must be a canonical UUID") from exc
    return value


def _safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("-"):
        raise PmtError("workspace_mapping_invalid", "relative_graph_path must be a POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PmtError("workspace_mapping_invalid", "relative_graph_path must remain inside the workspace")
    return path.as_posix()


def _scope_has_path(scopes, relative_path, canonical_ref):
    target = PurePosixPath(relative_path).as_posix().strip("/")
    for scope in scopes:
        if not isinstance(scope, Mapping) or scope.get("kind") not in {"path", "workspace"}:
            continue
        resource = scope.get("resource")
        if not isinstance(resource, str):
            continue
        if scope["kind"] == "workspace" and resource == canonical_ref:
            return True
        if scope["kind"] == "path":
            resource_path = PurePosixPath(resource)
            root = resource_path.as_posix().strip("/") or "."
            if ("\\" in resource or resource_path.is_absolute()
                    or any(part in {".", ".."} for part in resource_path.parts)):
                continue
            if root == "." or target == root or target.startswith(root.rstrip("/") + "/"):
                return True
    return False


class ClientWorkspaceResolver:
    """Map canonical repository/branch refs to locally owned physical checkouts.

    `authorize_callback` is an injected StatePort RPC adapter. It must return
    the actual, current server-authorized run, owner, scope and SourcePin data;
    a caller-provided boolean or cached assertion is not accepted.
    """

    def __init__(self, authorize_callback, source_inspector=inspect_graph_source):
        if not callable(authorize_callback):
            raise PmtError("workspace_authority_unavailable", "A current authorization RPC is required", 3)
        self.authorize_callback = authorize_callback
        self.source_inspector = source_inspector

    def resolve(self, mapping, runtime_context):
        if not isinstance(mapping, Mapping) or not isinstance(runtime_context, Mapping):
            raise PmtError("workspace_mapping_invalid", "Workspace mapping and runtime context are required")
        expected_fields = {"repository_id", "project_id", "branch", "local_workspace", "relative_graph_path"}
        if not expected_fields <= set(mapping) or set(mapping) - expected_fields - {"remote", "canonical_workspace"}:
            raise PmtError("workspace_mapping_invalid", "Workspace mapping fields are incomplete or unsupported")
        context_fields = {"expected_source_pin", "repository_id", "project_id", "branch", "canonical_workspace",
                          "scopes", "run_id", "current_owner", "revision"}
        if not context_fields <= set(runtime_context):
            raise PmtError("workspace_context_invalid", "Runtime context lacks current authorization bindings")

        repository_id = _uuid(mapping["repository_id"], "repository_id")
        project_id = _uuid(mapping["project_id"], "project_id")
        pin = pin_source(runtime_context["expected_source_pin"])
        if pin.repository_id != repository_id or pin.project_id != project_id:
            raise PmtError("source_conflict", "Expected SourcePin does not match the workspace mapping", 3)
        branch = mapping["branch"]
        if branch is not None and (not isinstance(branch, str) or not branch or len(branch) > 1024
                                   or any(ord(c) < 0x20 for c in branch)):
            raise PmtError("workspace_mapping_invalid", "branch must be null or bounded text")
        if branch != pin.selected_ref:
            raise PmtError("source_conflict", "Mapped branch does not match the expected SourcePin", 3)
        if pin.source_kind == "git":
            branch_key = branch if branch is not None else "detached:" + pin.reviewed_commit
        elif pin.source_kind == "non_git":
            if branch is not None:
                raise PmtError("source_conflict", "A non-Git source cannot select a branch", 3)
            branch_key = "non-git"
        else:
            raise PmtError("source_kind_unknown", "Unknown SourcePin cannot authorize a local checkout", 3)
        canonical_ref = canonical_workspace(repository_id, branch_key)
        if mapping.get("canonical_workspace", canonical_ref) != canonical_ref:
            raise PmtError("workspace_mapping_invalid", "Mapped canonical workspace ref is inconsistent", 3)
        if runtime_context["repository_id"] != repository_id or runtime_context["project_id"] != project_id:
            raise PmtError("workspace_context_invalid", "Runtime project/repository binding changed", 3)
        if runtime_context["branch"] != branch or runtime_context["canonical_workspace"] != canonical_ref:
            raise PmtError("workspace_context_invalid", "Runtime branch/workspace binding changed", 3)
        run_id = _uuid(runtime_context["run_id"], "run_id")
        revision = runtime_context["revision"]
        if type(revision) is not int or revision < 1:
            raise PmtError("workspace_context_invalid", "Run revision must be positive")
        owner = runtime_context["current_owner"]
        if not isinstance(owner, Mapping) or set(owner) != {"actor", "session_id"}:
            raise PmtError("workspace_context_invalid", "Current actor and session are required")
        actor, session_id = owner["actor"], owner["session_id"]
        if any(not isinstance(value, str) or not value.strip() or len(value) > 200
               or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
               for value in (actor, session_id)):
            raise PmtError("workspace_context_invalid", "Current actor and session are invalid")
        relative_path = _safe_relative(mapping["relative_graph_path"])
        scopes = runtime_context["scopes"]
        if not isinstance(scopes, list) or not _scope_has_path(scopes, relative_path, canonical_ref):
            raise PmtError("workspace_scope_denied", "Current scopes do not cover the graph source", 3)
        workspace_value = mapping["local_workspace"]
        if not isinstance(workspace_value, str) or not workspace_value.strip() or not Path(workspace_value).is_absolute():
            raise PmtError("workspace_mapping_invalid", "local_workspace must be absolute")

        auth_request = {
            "repository_id": repository_id, "project_id": project_id, "branch": branch,
            "canonical_workspace": canonical_ref, "scopes": scopes, "run_id": run_id,
            "owner": {"actor": actor, "session_id": session_id}, "run_revision": revision,
            "expected_source_hash": pin.source_hash,
        }
        auth_request["context_hash"] = fingerprint(auth_request)
        try:
            authority = self.authorize_callback(auth_request)
        except Exception as exc:
            raise PmtError("workspace_authority_unavailable", "Current workspace authorization could not be read", 3,
                           True) from exc
        required_authority = {"status", "repository_id", "project_id", "branch", "canonical_workspace",
                              "scopes", "run_id", "run_revision", "owner", "source_pin", "context_hash"}
        if not isinstance(authority, Mapping) or not required_authority <= set(authority):
            raise PmtError("workspace_authority_invalid", "Authorization RPC returned incomplete current state", 3)
        if (authority["status"] != "authorized" or authority["repository_id"] != repository_id
                or authority["project_id"] != project_id or authority["branch"] != branch
                or authority["canonical_workspace"] != canonical_ref or authority["run_id"] != run_id
                or authority["run_revision"] != revision or authority["owner"] != dict(owner)
                or authority["context_hash"] != auth_request["context_hash"]
                or canonical_json(authority["scopes"]) != canonical_json(scopes)):
            raise PmtError("workspace_authority_stale", "Current authorization or run scope does not match", 3)
        verify_source_pin(pin, authority["source_pin"])

        # No filesystem operation above this point examines the mapped path.
        from .resources import _reject_links
        workspace = Path(os.path.abspath(workspace_value))
        try:
            _reject_links(workspace)
            workspace = workspace.resolve(strict=True)
        except OSError as exc:
            raise PmtError("workspace_mapping_unavailable", "Mapped local workspace is unavailable", 3) from exc
        if not workspace.is_dir():
            raise PmtError("workspace_mapping_invalid", "Mapped local workspace is not a directory", 3)
        graph_path = workspace.joinpath(*PurePosixPath(relative_path).parts)
        try:
            _reject_links(graph_path)
            graph_path.resolve(strict=True).relative_to(workspace)
        except (OSError, ValueError) as exc:
            raise PmtError("workspace_path_invalid", "Graph path is missing or escapes the workspace", 3) from exc
        if not graph_path.is_file():
            raise PmtError("workspace_path_invalid", "Graph source must be a regular file", 3)
        expected_remote = mapping.get("remote")
        if expected_remote is not None and (not isinstance(expected_remote, str) or not expected_remote.strip()
                                            or len(expected_remote) > 2048):
            raise PmtError("workspace_mapping_invalid", "remote must be nonempty bounded text or null")

        def validate_local_git_identity(source):
            if source["selected_ref"] != branch or source["reviewed_commit"] != pin.reviewed_commit:
                raise PmtError("source_conflict", "Local checkout branch or commit changed", 3)
            if expected_remote:
                if not source["is_git"]:
                    raise PmtError("git_source_unavailable", "Mapped remote requires an accessible Git checkout", 3)
                if not source["git_origin"]:
                    raise PmtError("repository_remote_mismatch", "Local Git origin is unavailable", 3)
                from .lifecycle import _canonical_remote
                try:
                    same_remote = _canonical_remote(source["git_origin"]) == _canonical_remote(expected_remote)
                except PmtError as exc:
                    raise PmtError("repository_remote_mismatch", "Local Git origin is invalid", 3) from exc
                if not same_remote:
                    raise PmtError("repository_remote_mismatch", "Local Git origin does not match the mapping", 3)

        inspected = self.source_inspector(workspace, graph_path, repository_id, project_id,
                                          graph_scope_id=project_id,
                                          pre_read_validator=validate_local_git_identity)
        current_pin = inspected["source_pin"]
        verify_source_pin(pin, current_pin)
        return {"canonical_workspace": canonical_ref, "repository_id": repository_id,
                "project_id": project_id, "branch": branch, "workspace": workspace,
                "relative_graph_path": relative_path, "graph_path": graph_path,
                "source_pin": current_pin, "graph": inspected["graph"],
                "report": inspected["report"], "authority": dict(authority)}
