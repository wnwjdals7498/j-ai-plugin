"""Canonical source pins and bounded source inspection helpers."""
import hashlib
import os
from pathlib import Path
import subprocess
from dataclasses import asdict, dataclass

from ..errors import PmtError
from ..util import fingerprint


@dataclass(frozen=True)
class SourcePin:
    repository_id: str
    project_id: str
    selected_ref: str | None
    reviewed_commit: str | None
    graph_schema: int
    graph_revision: int
    graph_hash: str
    dirty_state: str
    dirty_fingerprint: str | None = None

    def __post_init__(self):
        if any(not isinstance(value, str) or not value for value in
               (self.repository_id, self.project_id, self.graph_hash)):
            raise PmtError("source_pin_invalid", "SourcePin identifiers and graph_hash are required")
        if type(self.graph_schema) is not int or type(self.graph_revision) is not int or self.graph_schema < 1 or self.graph_revision < 1:
            raise PmtError("source_pin_invalid", "SourcePin graph versions must be positive")
        if any(value is not None and not isinstance(value, str)
               for value in (self.selected_ref, self.reviewed_commit, self.dirty_fingerprint)):
            raise PmtError("source_pin_invalid", "SourcePin refs and fingerprints must be text or null")
        if not isinstance(self.dirty_state, str) or self.dirty_state not in {"clean", "dirty", "unknown"}:
            raise PmtError("source_pin_invalid", "dirty_state must be clean, dirty, or unknown")
        if self.dirty_state == "dirty" and not self.dirty_fingerprint:
            raise PmtError("source_pin_invalid", "Dirty SourcePin requires dirty_fingerprint")
        if self.dirty_state != "dirty" and self.dirty_fingerprint is not None:
            raise PmtError("source_pin_invalid", "dirty_fingerprint is valid only for dirty sources")

    @property
    def source_hash(self):
        return fingerprint(asdict(self))

    @property
    def source_kind(self):
        if self.reviewed_commit is not None:
            return "git"
        if self.selected_ref is not None:
            return "unknown"
        return "non_git"

    def to_dict(self):
        return asdict(self) | {"source_kind": self.source_kind, "source_hash": self.source_hash}


def pin_source(value):
    if isinstance(value, SourcePin):
        return value
    if not isinstance(value, dict):
        raise PmtError("source_pin_invalid", "SourcePin must be an object")
    unknown = set(value) - set(SourcePin.__dataclass_fields__) - {"source_hash", "source_kind"}
    if unknown:
        raise PmtError("source_pin_invalid", "SourcePin has unsupported fields", details={"fields": sorted(unknown)})
    fields = {key: value[key] for key in SourcePin.__dataclass_fields__ if key in value}
    try:
        pin = SourcePin(**fields)
    except TypeError as exc:
        raise PmtError("source_pin_invalid", "SourcePin is missing required fields") from exc
    supplied = value.get("source_hash")
    if supplied is not None and supplied != pin.source_hash:
        raise PmtError("source_pin_invalid", "SourcePin hash does not match its fields")
    supplied_kind = value.get("source_kind")
    if supplied_kind is not None and supplied_kind != pin.source_kind:
        raise PmtError("source_pin_invalid", "SourcePin source_kind does not match its fields")
    return pin


def verify_source_pin(expected, current):
    expected, current = pin_source(expected), pin_source(current)
    if expected.source_hash != current.source_hash:
        raise PmtError("source_conflict", "Source changed since it was reviewed", 3, False,
                       {"expected_source_hash": expected.source_hash,
                        "current_source_hash": current.source_hash})
    return expected.source_hash


def _run_git(workspace: Path, *args, timeout=15, check=True):
    try:
        completed = subprocess.run(["git", "-C", str(workspace), *args], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PmtError("git_source_unavailable", "Local Git source could not be inspected", 4, True) from exc
    if check and completed.returncode != 0:
        raise PmtError("git_source_unavailable", "Local Git source does not support the requested inspection", 3)
    return completed


def inspect_graph_source(workspace, graph_path, repository_id, project_id, *,
                         graph_scope_id=None, git_runner=None, validate_graph_fn=None,
                         pre_read_validator=None):
    """Read and pin one claimed graph file; callers must authorize before calling.

    The helper deliberately performs no claim or mapping grant. It only inspects
    the supplied, already-authorized local source and returns derived data.
    """
    from ..util import fingerprint, strict_json_loads
    if validate_graph_fn is None:
        from ..planning.graph import validate_graph as validate_graph_fn
    runner = git_runner or _run_git
    workspace, graph_path = Path(workspace), Path(graph_path)
    root_result = runner(workspace, "rev-parse", "--show-toplevel", check=False)
    is_git = root_result.returncode == 0
    if not is_git and any((parent / ".git").exists() for parent in (workspace, *workspace.parents)):
        raise PmtError("git_source_unavailable", "Git metadata exists but the repository cannot be inspected", 3)
    repo_root, git_relative, head, selected_ref, origin = None, None, None, None, None
    if is_git:
        try:
            repo_root = Path(os.fsdecode(root_result.stdout).strip()).resolve(strict=True)
            git_relative = graph_path.resolve(strict=True).relative_to(repo_root).as_posix()
        except (OSError, ValueError) as exc:
            raise PmtError("repository_path_mismatch", "Graph file is outside the checked-out Git repository", 3) from exc
        origin_result = runner(workspace, "remote", "get-url", "origin", check=False)
        origin = os.fsdecode(origin_result.stdout).strip() if origin_result.returncode == 0 else None
        head = os.fsdecode(runner(workspace, "rev-parse", "--verify", "HEAD").stdout).strip()
        branch_result = runner(workspace, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        selected_ref = os.fsdecode(branch_result.stdout).strip() if branch_result.returncode == 0 else None
    if pre_read_validator is not None:
        pre_read_validator({"repo_root": repo_root, "git_relative_path": git_relative,
                            "git_origin": origin, "is_git": is_git,
                            "reviewed_commit": head, "selected_ref": selected_ref})
    try:
        wire = graph_path.read_bytes()
    except OSError as exc:
        raise PmtError("graph_source_read_failed", "Graph source could not be read", 4, True) from exc
    if len(wire) > 8 * 1024 * 1024:
        raise PmtError("graph_source_too_large", "Graph source exceeds 8 MiB")
    graph = strict_json_loads(wire, max_bytes=8 * 1024 * 1024)
    report = validate_graph_fn(graph, graph_scope_id or project_id, complete=False)
    working_hash = hashlib.sha256(wire).hexdigest()
    if is_git:
        baseline = runner(workspace, "show", f"HEAD:{git_relative}", check=False)
        status = runner(workspace, "status", "--porcelain=v1", "--untracked-files=all", "--", git_relative).stdout
        status_text = os.fsdecode(status)
        if status_text.strip():
            dirty_state = "dirty"
            dirty_fingerprint = fingerprint({"path": git_relative, "status": status_text,
                                             "working_file_sha256": working_hash})
        elif baseline.returncode != 0:
            dirty_state, dirty_fingerprint = "unknown", None
        else:
            try:
                baseline_graph = strict_json_loads(baseline.stdout, max_bytes=8 * 1024 * 1024)
                baseline_report = validate_graph_fn(baseline_graph, graph_scope_id or project_id, complete=False)
            except PmtError:
                dirty_state, dirty_fingerprint = "unknown", None
            else:
                # Git may check out the same committed JSON with CRLF on one
                # client. Pin semantic canonical graph content; retain raw bytes
                # only when Git itself reports a working-tree change.
                dirty = baseline_report["sha256"] != report["sha256"]
                dirty_state = "dirty" if dirty else "clean"
                dirty_fingerprint = fingerprint({"path": git_relative, "status": status_text,
                    "working_file_sha256": working_hash}) if dirty else None
    else:
        dirty_state, dirty_fingerprint = "unknown", None
    pin = SourcePin(repository_id, project_id, selected_ref, head,
                    report["schema_version"], report["graph_version"], report["sha256"],
                    dirty_state, dirty_fingerprint)
    return {"repo_root": repo_root, "git_relative_path": git_relative, "git_origin": origin,
            "is_git": is_git, "wire": wire, "raw_sha256": working_hash, "graph": graph,
            "report": report, "source_pin": pin}
