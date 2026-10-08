"""Local adapter for the shared short PMT command surface."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from ..errors import PmtError
from ..storage_config import _read_profile, configure_storage

_PROJECTS_FILE = "projects.json"
_CLAIMS_FILE = "easy-claims.json"


@contextmanager
def _state_lock(root, name):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / f".{name}.lock"
    try:
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and (not stat.S_ISREG(info.st_mode) or path.is_symlink()
                                 or getattr(info, "st_file_attributes", 0) & 0x400):
            raise PmtError("client_state_insecure", "PMT state lock must be a regular file")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        stream = os.fdopen(fd, "r+b")
    except PmtError:
        raise
    except OSError as error:
        raise PmtError("client_state_lock_unavailable", "PMT local state could not be locked") from error
    try:
        if os.name != "nt":
            info = os.fstat(stream.fileno())
            if info.st_uid != os.getuid():
                raise PmtError("client_state_insecure", "PMT state lock has another owner")
            os.chmod(path, 0o600)
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
            os.fsync(stream.fileno())
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def _json_file(path, fallback):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return fallback
    except OSError as error:
        raise PmtError("client_state_unreadable", "PMT local state could not be read") from error
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise PmtError("client_state_insecure", "PMT local state must be a regular file")
    if info.st_size > 1024 * 1024:
        raise PmtError("client_state_invalid", "PMT local state exceeds its size limit")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        raise PmtError("client_state_unreadable", "PMT local state could not be read") from error
    except ValueError as error:
        raise PmtError("client_state_invalid", "PMT local state is not valid JSON") from error


def _write_json(path, value, *, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise PmtError("client_state_insecure", "PMT local state file cannot be a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".pmt-state-", suffix=".tmp", dir=str(path.parent))
    try:
        if os.name != "nt":
            os.chmod(temporary, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def projects(config_root):
    with _state_lock(config_root, "projects"):
        value = _read_projects(config_root)
    return value


def _read_projects(config_root):
    raw = _json_file(Path(config_root) / _PROJECTS_FILE, {"projects": []})
    if isinstance(raw, dict) and "schema_version" in raw and raw["schema_version"] != 1:
        raise PmtError("client_state_invalid", "Project registry schema version is unsupported")
    records = raw.get("projects") if isinstance(raw, dict) else raw
    if not isinstance(records, list):
        raise PmtError("client_state_invalid", "Project registry must contain a project list")
    flattened = []
    for project in records:
        if not isinstance(project, dict):
            raise PmtError("client_state_invalid", "Project registry entries must be objects")
        name, project_id = project.get("name"), project.get("project_id")
        if not isinstance(name, str) or not name.strip() or not _is_uuid(project_id):
            raise PmtError("client_state_invalid", "Project registry entry has invalid identity fields")
        repositories = project.get("repositories")
        if isinstance(repositories, list):
            for repository in repositories:
                if (not isinstance(repository, dict) or not _is_uuid(repository.get("repository_id"))
                        or not isinstance(repository.get("name"), str) or not repository["name"].strip()):
                    raise PmtError("client_state_invalid", "Project repository entry is invalid")
                flattened.append({"name": name, "project_id": project_id,
                                  "repository_id": repository["repository_id"],
                                  "repository_name": repository["name"], "remote": repository.get("remote"),
                                  "local_root": project.get("local_root"),
                                  **({"graph_path": repository["graph_path"]} if "graph_path" in repository else {})})
        elif repositories is not None:
            raise PmtError("client_state_invalid", "Project repositories must be a list")
        elif _is_uuid(project.get("repository_id")):
            flattened.append(project)
        else:
            raise PmtError("client_state_invalid", "Project registry entry has no valid repository")
    return flattened


def _save_projects(config_root, entries):
    with _state_lock(config_root, "projects"):
        latest = _read_projects(config_root)
        keys = {(item["project_id"], item["repository_id"]) for item in latest}
        merged = list(latest)
        for item in entries:
            key = (item["project_id"], item["repository_id"])
            if key not in keys:
                merged.append(item)
                keys.add(key)
        _write_json(Path(config_root) / _PROJECTS_FILE, {"schema_version": 1, "projects": merged})


def merge_handoff_projects(config_root, entries):
    """Merge validated server project names while retaining local annotations."""
    with _state_lock(config_root, "projects"):
        latest = _read_projects(config_root)
        path = Path(config_root) / _PROJECTS_FILE
        try:
            before = path.read_bytes()
        except FileNotFoundError:
            before = None
        merged = {(item["project_id"], item["repository_id"]): item for item in latest}
        for incoming in entries:
            key = (incoming["project_id"], incoming["repository_id"])
            if key in merged:
                current = merged[key]
                for field in ("name", "repository_name", "remote", "graph_path"):
                    if incoming.get(field) is not None:
                        current[field] = incoming[field]
            else:
                merged[key] = dict(incoming)
        _write_json(path, {"schema_version": 1, "projects": list(merged.values())})
        return before, path.read_bytes()


def _scope(cli, kind, slug, *, parent_id=None, body=None):
    payload = {"kind": kind, "slug": slug, "body": body or {}}
    if parent_id:
        payload["parent_id"] = parent_id
    result = cli.execute("create_scope", payload, allow_unscoped=True)
    return result["scope_id"]


def _new_project(cli, args, root, branch):
    config_root, _data_root, profile, config_hash = cli._roots()
    if profile["mode"] != "local":
        raise cli.EasyError("local_link_only", "pmt link --new is available in local mode only.")
    if any(os.path.normcase(os.path.abspath(item["local_root"])) == os.path.normcase(str(root))
           and item.get("branch") == branch for item in profile.get("workspace_mappings", [])):
        raise cli.EasyError("branch_already_linked", "Unlink this branch before creating a different project link.")
    title = args.new.strip()
    if not title:
        raise cli.EasyError("project_title_required", "Give a non-empty project name after --new.")
    registry = projects(config_root)
    related = next((item for item in registry if os.path.normcase(item.get("local_root", "")) ==
                    os.path.normcase(str(root))), None)
    if related:
        environment_id, repository_id = related["environment_id"], related["repository_id"]
    else:
        environment_id = _scope(cli, "environment", Path(root).name or "workspace", body={"path": str(root)})
        repository_id = _scope(cli, "repository", Path(root).name or "repository",
                               parent_id=environment_id, body={})
    project_id = _scope(cli, "project", title, parent_id=repository_id, body={"name": title})
    mapping = {"repository_id": repository_id, "project_id": project_id, "branch": branch,
               "branch_key_sha256": hashlib.sha256(branch.encode("utf-8")).hexdigest(),
               "local_root": str(root), "relative_graph_path": args.graph_path}
    configure_storage(str(config_root), {"mode": "local", "expected_config_sha256": config_hash,
                                         "workspace_mappings": [*profile.get("workspace_mappings", []), mapping]})
    registry.append({"name": title, "project_id": project_id, "repository_id": repository_id,
                     "repository_name": Path(root).name,
                     "environment_id": environment_id, "local_root": str(root)})
    _save_projects(config_root, registry)
    print(f"Created local project {title} ({project_id[:8]}) and linked {root} ({branch}).")
    return 0


def _resolve_project(cli, config_root, profile, project_ref, repository_ref=None):
    entries = projects(config_root)
    matches = [item for item in entries if project_ref.lower() in {
        item.get("name", "").lower(), item.get("project_id", "").lower()} or
        (item.get("project_id", "").startswith(project_ref.lower()) if project_ref else False)]
    if repository_ref:
        matches = [item for item in matches
                   if str(item.get("repository_id") or "").lower().startswith(repository_ref.lower())
                   or str(item.get("repository_name") or "").lower() == repository_ref.lower()]
    if len(matches) == 1:
        return matches[0]["project_id"], matches[0]["repository_id"]
    if not entries and repository_ref and _is_uuid(project_ref) and _is_uuid(repository_ref):
        return project_ref, repository_ref
    raise cli.EasyError("project_not_found" if not matches else "project_ambiguous",
                        "No unique project matches that name or ID. Run `pmt projects` to list projects.")


def resolve_project(cli, config_root, profile, project_ref, repository_ref=None):
    return _resolve_project(cli, config_root, profile, project_ref, repository_ref)


def _is_uuid(value):
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def link(cli, args):
    root, branch = cli.easy_setup.git_checkout(os.getcwd())
    if root is None or branch is None:
        raise cli.EasyError("not_a_branch_checkout", "Run pmt link inside a Git checkout on a branch.")
    if args.new is not None:
        return _new_project(cli, args, root, branch)
    config_root, _data_root, profile, config_hash = cli._roots()
    mappings = list(profile.get("workspace_mappings", []))
    same_root = [item for item in mappings if os.path.normcase(os.path.abspath(item["local_root"])) ==
                 os.path.normcase(root)]
    if any(item.get("branch") == branch for item in same_root):
        print(f"Already linked: {root} ({branch}).")
        return 0
    project_ref = args.project
    repository_ref = args.repository
    if same_root and project_ref is None:
        project_ref = same_root[0]["project_id"]
        repository_ref = repository_ref or same_root[0]["repository_id"]
    if project_ref is None:
        entries = projects(config_root)
        pairs = {(item.get("project_id"), item.get("repository_id")) for item in entries}
        if len(pairs) == 1 and args.yes:
            project_ref, repository_ref = next(iter(pairs))
        elif len(pairs) == 1:
            project_ref = next(iter(pairs))[0]
            print(f"Would link to {project_ref[:8]}; rerun `pmt link {project_ref[:8]} --yes` to confirm.")
            return 0
        elif not pairs:
            raise cli.EasyError("link_ids_required", "Create a project with `pmt link --new <name>` first.")
        else:
            raise cli.EasyError("project_ambiguous", "Choose a project name or ID with `pmt link <project>`.")
    project_id, repository_id = _resolve_project(cli, config_root, profile, project_ref, repository_ref)
    mapping = {"repository_id": repository_id, "project_id": project_id, "branch": branch,
               "branch_key_sha256": hashlib.sha256(branch.encode("utf-8")).hexdigest(),
               "local_root": root, "relative_graph_path": args.graph_path}
    request = {"mode": profile["mode"], "expected_config_sha256": config_hash,
               "workspace_mappings": [*mappings, mapping]}
    if profile["mode"] == "hosted":
        request.update(endpoint=profile["endpoint"], credential_env=profile["credential_env"],
                       device_id=profile["device_id"], namespace_id=profile["namespace_id"],
                       expected_actor=profile["actor"])
        if profile.get("ca_file"):
            request["ca_file"] = profile["ca_file"]
    configure_storage(str(config_root), request)
    print(f"Linked {root} ({branch}) to project {project_id[:8]}; available in the next session.")
    return 0


def unlink(cli):
    config_root, _data_root, profile, config_hash = cli._roots()
    root, branch = cli.easy_setup.git_checkout(os.getcwd())
    if root is None or branch is None:
        raise cli.EasyError("not_a_branch_checkout", "Run pmt unlink inside a Git checkout on a branch.")
    kept = [item for item in profile.get("workspace_mappings", [])
            if not (os.path.normcase(os.path.abspath(item["local_root"])) == os.path.normcase(root)
                    and item.get("branch") == branch)]
    if len(kept) == len(profile.get("workspace_mappings", [])):
        print("This branch is not linked.")
        return 0
    request = {"mode": profile["mode"], "expected_config_sha256": config_hash, "workspace_mappings": kept}
    if profile["mode"] == "hosted":
        request.update(endpoint=profile["endpoint"], credential_env=profile["credential_env"],
                       device_id=profile["device_id"], namespace_id=profile["namespace_id"],
                       expected_actor=profile["actor"])
        if profile.get("ca_file"):
            request["ca_file"] = profile["ca_file"]
    configure_storage(str(config_root), request)
    print(f"Unlinked {root} ({branch}).")
    return 0


def list_projects(cli):
    config_root, _data_root, profile, _hash = cli._roots()
    entries = projects(config_root)
    if entries:
        for item in entries:
            repository = item.get("repository_name") or item.get("repository_id")
            print(f"{item.get('name')} | {item.get('project_id')} | repository {repository}")
    elif profile.get("workspace_mappings"):
        for item in profile["workspace_mappings"]:
            print(f"{item['project_id']} | repository {item['repository_id']}")
    else:
        print("No linked projects. Use `pmt link --new <name>` to create a local project.")
    return 0


def mode(cli):
    config_root, data_root = cli.easy_setup.default_roots(os.environ)
    profile, _hash = _read_profile(config_root)
    current_mode = profile["mode"] if profile else "unconfigured"
    print(f"mode: {current_mode}")
    print(f"ConfigRoot: {config_root}")
    print(f"DataRoot: {data_root}")
    scope = cli._current_scope(profile) if profile else None
    print(f"project: {scope or 'not linked'}")
    if profile and profile["mode"] == "hosted":
        print(f"endpoint: {profile['endpoint']}")
        print(f"actor: {profile['actor']}")
        print(f"namespace: {profile['namespace_id']}")
    return 0


def _claim_path(data_root):
    return Path(data_root) / _CLAIMS_FILE


def load_claims(data_root):
    with _state_lock(data_root, "easy-claims"):
        return _read_claims(data_root)


def _read_claims(data_root):
    path = _claim_path(data_root)
    value = _json_file(path, {})
    if not isinstance(value, dict):
        raise PmtError("claim_store_invalid", "Local claim store must be an object")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
        raise PmtError("claim_store_insecure", "Local claim store must be a regular file")
    if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        raise PmtError("claim_store_insecure", "Local claim store ownership or permissions are too broad")
    for item_id, claim in value.items():
        if (not _is_uuid(item_id) or not isinstance(claim, dict)
                or not isinstance(claim.get("claim_token"), str) or not claim["claim_token"]
                or len(claim["claim_token"]) > 512 or not isinstance(claim.get("session_id"), str)
                or not claim["session_id"] or not isinstance(claim.get("revision"), int)
                or isinstance(claim["revision"], bool) or claim["revision"] < 1):
            raise PmtError("claim_store_invalid", "Local claim entry is invalid")
    return value


def save_claims(data_root, claims):
    with _state_lock(data_root, "easy-claims"):
        _write_claims(data_root, claims)


def _write_claims(data_root, claims):
    _write_json(_claim_path(data_root), claims)


def start(cli, record):
    item_id = cli._rid(record)
    config_root, data_root, _profile, _hash = cli._roots()
    with _state_lock(data_root, "easy-claims"):
        claims = _read_claims(data_root)
        result = cli.execute("claim_task", {}, record_id=item_id, expected_revision=record["revision"])
        claims[item_id] = {"claim_token": result["claim_token"], "session_id": result["owner_session"],
                           "revision": result["revision"]}
        try:
            _write_claims(data_root, claims)
        except (OSError, PmtError):
            cli.execute("release_claim", {"claim_token": result["claim_token"], "status": "Paused",
                                          "reason": "Local claim token could not be saved",
                                          "next": "retry pmt start"}, record_id=item_id,
                        expected_revision=result["revision"], session_id=result["owner_session"])
            raise cli.EasyError("claim_store_unavailable", "The item was paused because its local claim could not be saved.")
    print(f"Claimed {record.get('title')} (rev {result['revision']}).")
    return 0


def owned(cli, record):
    _config_root, data_root, _profile, _hash = cli._roots()
    claims = load_claims(data_root)
    claim = claims.get(cli._rid(record))
    if claim is None or not claim.get("claim_token"):
        raise cli.EasyError("not_claimed_here", "This machine does not hold the local claim. Run pmt start first.")
    return data_root, claims, claim


def pause(cli, record, args):
    _config_root, data_root, _profile, _hash = cli._roots()
    item_id = cli._rid(record)
    with _state_lock(data_root, "easy-claims"):
        claims = _read_claims(data_root)
        claim = claims.get(item_id)
        if claim is None:
            raise cli.EasyError("not_claimed_here", "This machine does not hold the local claim. Run pmt start first.")
        result = cli.execute("release_claim", {"claim_token": claim["claim_token"], "status": "Paused",
                                                "reason": args.reason or "pmt pause", "next": args.next},
                             record_id=item_id, expected_revision=record["revision"],
                             session_id=claim["session_id"])
        claims.pop(item_id, None)
        _write_claims(data_root, claims)
    print(f"Paused {record.get('title')} (rev {result['revision']}). Next: {args.next}")
    return 0


def _parse_command(command):
    if os.name != "nt":
        import shlex
        return shlex.split(command, posix=True)
    import ctypes
    from ctypes import wintypes
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    argv = shell32.CommandLineToArgvW(command, ctypes.byref(count))
    if not argv:
        raise ValueError("CommandLineToArgvW rejected the test command")
    try:
        return [argv[index] for index in range(count.value)]
    finally:
        kernel32.LocalFree(argv)


def done(cli, args, record):
    item_id = cli._rid(record)
    data_root, _claims, claim = owned(cli, record)
    profile = cli._roots()[2]
    root, branch = cli.easy_setup.git_checkout(os.getcwd())
    mapping, _reason = cli.easy_setup.select_mapping(profile, root, branch)
    if mapping is None or mapping["project_id"] != cli._current_scope(profile):
        raise cli.EasyError("project_not_linked", "Run pmt done inside the linked checkout and branch.")
    root = mapping["local_root"]
    dirty = subprocess.run(["git", "-C", root, "status", "--porcelain"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
    if dirty.strip():
        raise cli.EasyError("uncommitted_changes", "Commit all tracked and untracked changes before pmt done.")
    criteria = (record.get("body") or {}).get("criteria") or []
    if not criteria:
        raise cli.EasyError("criteria_missing", "The item has no completion criteria.")
    try:
        command = _parse_command(args.test)
    except (OSError, ValueError) as error:
        raise cli.EasyError("test_command_invalid", "The test command could not be parsed.") from error
    if not command:
        raise cli.EasyError("test_required", "Give the test command with --test.")
    command_id, definition = str(uuid.uuid4()), "pmt.item.test"
    inputs = {"workspace": root}
    before = cli.execute("lookup_verification", {"definition_id": definition, "definition_version": "1",
                           "target_id": item_id, "command": command, "inputs": inputs,
                           "criterion_ids": [item["id"] if isinstance(item, dict) else item for item in criteria]},
                          record_id=item_id)
    fingerprint = before.get("input_fingerprint")
    if not fingerprint or before.get("status") == "unknown":
        reasons = ", ".join(before.get("reasons") or [])
        raise cli.EasyError("verification_inputs_unknown",
                            f"The current workspace cannot be fingerprinted; the test was not run ({reasons}).")
    print(f"Running test: {args.test}")
    try:
        completed = subprocess.run(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=args.timeout, check=False)
        exit_code, output = completed.returncode, completed.stdout
    except subprocess.TimeoutExpired as error:
        exit_code, output = None, error.stdout or b""
    evidence_dir = Path(data_root) / "easy-cli" / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    evidence_path = evidence_dir / f"{uuid.uuid4()}.log"
    evidence_path.write_bytes(output)
    try:
        artifact = cli.execute("register_resource", {"source_path": str(evidence_path),
                                   "allowed_root": str(evidence_dir), "retention": "evidence",
                                   "owner_record_id": item_id}, scope_id=record["scope_id"])
        outcome = "pass" if exit_code == 0 else "fail"
        verification = cli.execute("record_verification", {
            "definition_id": definition, "definition_version": "1", "target_id": item_id,
            "command": command, "inputs": inputs, "outcome": outcome, "exit_code": exit_code,
            "evidence_ids": [artifact["artifact_id"]],
            "criterion_ids": [item["id"] if isinstance(item, dict) else item for item in criteria],
            "before_fingerprint": fingerprint}, record_id=item_id)
    finally:
        evidence_path.unlink(missing_ok=True)
    sysout = output.decode("utf-8", "replace")
    if sysout:
        print(sysout[-2000:], end="" if sysout.endswith("\n") else "\n")
    if exit_code != 0:
        raise cli.EasyError("test_failed", f"Test failed (exit {exit_code}); the item stays in progress.", 1)
    latest = cli._find(item_id)
    finished = cli.execute("finish_task", {"claim_token": claim["claim_token"], "result": args.result,
                                            "verification_ids": [verification["verification_id"]]},
                           record_id=item_id, expected_revision=latest["revision"],
                           session_id=claim["session_id"])
    with _state_lock(data_root, "easy-claims"):
        claims = _read_claims(data_root)
        claims.pop(item_id, None)
        _write_claims(data_root, claims)
    print(f"Completed {record.get('title')} → {finished.get('state')} (rev {finished.get('revision')}); "
          f"verification {verification['verification_id'][:8]}.")
    return 0


def check(cli):
    config_root, data_root, profile, _hash = cli._roots()
    rows = [("mode", profile["mode"], "")]
    scope = cli._current_scope(profile)
    if not scope:
        rows.append(("project", "not linked", "pmt link --new <name>"))
        cli._print_rows(rows)
        return 1
    setup = cli.execute("setup", {"product": "cli"}, scope_id=scope)
    rows.append(("SQLite setup", "ok", f"schema {setup['schema_version']}"))
    payload = {"kind": "fact", "title": "pmt check local", "reason": "pmt check local", "body": {}}
    request_id = str(uuid.uuid5(uuid.UUID(profile["environment_id"]), f"local-check:{scope}") )
    first = cli.execute("save_change", payload, scope_id=scope, request_id=request_id, session_id="pmt-check")
    found = cli.execute("read_context", {"limit": 200, "query": "pmt check local"}, scope_id=scope)
    replay = cli.execute("save_change", payload, scope_id=scope, request_id=request_id, session_id="pmt-check")
    same = cli._rid(first) == cli._rid(replay)
    read_ok = any(cli._rid(record) == cli._rid(first) for record in found.get("records", []))
    rows.append(("diagnostic write/read/replay", "ok" if same and read_ok else "FAIL",
                 f"same record: {cli._rid(first)[:8]}"))
    cli._print_rows(rows)
    return 0 if same and read_ok else 1
