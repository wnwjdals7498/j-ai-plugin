"""Client workspace authorization and source-pin tests using isolated Git trees."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
import stat

import pytest

from pmt.errors import PmtError
from pmt.efficiency.source import inspect_graph_source
from pmt.planning.graph import validate_graph
from pmt.util import canonical_json
from pmt.workspace import ClientWorkspaceResolver, canonical_workspace


def _node(identifier, tree_kind):
    node = {"id": identifier, "tree_kind": tree_kind, "node_kind": "goal", "summary": "Source fixture",
            "premise": "Preserve the source", "product_stage": "prototype",
            "product_scope": {"applies": False, "reason": "Fixture scope"},
            "autonomy": {"authority": "user", "scope": "Test only"}}
    if tree_kind == "requirement":
        node.update(source_refs=["fixture:source"], criteria=["pin matches"], evidence_refs=[])
    else:
        node.update(framework_assignment="stdlib", architecture="Workspace resolver", logging="refs only",
                    tests=["isolated Git"], function_spec={"input": "mapping", "output": "pin",
                    "constraints": "authorize first", "invariants": "stable", "errors": "conflict",
                    "verification": "source hash"}, choice_set={"options": [], "insufficient_reason": "fixture"},
                    choice={"source": "user", "selected": "local", "reason": "fixture", "scope": "local"})
    return node


@pytest.fixture(scope="session")
def workspace_template(request):
    repo_id, project_id = str(uuid.uuid4()), str(uuid.uuid4())
    temporary = tempfile.TemporaryDirectory(prefix="p3-ws-base-")
    request.addfinalizer(temporary.cleanup)
    root = Path(temporary.name) / "repo-a"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "PMT fixture"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "fixture@example.invalid"], check=True)
    graph = {"schema_version": 1, "project_id": project_id, "graph_version": 1,
             "nodes": [_node(str(uuid.uuid4()), "requirement"), _node(str(uuid.uuid4()), "implementation")],
             "relations": [], "provenance": {"request_ref": "fixture"}}
    relative = "docs/pmt-docs/plan.graph.json"
    graph_path = root / relative
    graph_path.parent.mkdir(parents=True)
    graph_path.write_text(canonical_json(graph) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", relative], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
    return {"root": root, "graph": graph, "repo_id": repo_id, "project_id": project_id,
            "relative": relative}


@pytest.fixture
def git_workspace(workspace_template):
    temporary = tempfile.TemporaryDirectory(prefix="p3-ws-")
    root = Path(temporary.name) / "repo-a"
    shutil.copytree(workspace_template["root"], root)
    env = dict(workspace_template) | {"root": root, "graph_path": root / workspace_template["relative"]}
    yield env
    temporary.cleanup()


def _pin(env, workspace=None):
    return inspect_graph_source(workspace or env["root"], (workspace or env["root"]) / env["relative"],
                                env["repo_id"], env["project_id"], graph_scope_id=env["project_id"],
                                validate_graph_fn=validate_graph)["source_pin"]


def _mapping(env, workspace=None, *, branch="main", remote=None):
    result = {"repository_id": env["repo_id"], "project_id": env["project_id"], "branch": branch,
              "local_workspace": str(workspace or env["root"]),
              "relative_graph_path": env["relative"]}
    if remote is not None:
        result["remote"] = remote
    return result


def _context(env, pin, *, scopes=None, branch="main", revision=2):
    branch_key = branch if branch is not None else (
        "detached:" + pin.reviewed_commit if pin.source_kind == "git" else "non-git")
    logical = canonical_workspace(env["repo_id"], branch_key)
    return {"expected_source_pin": pin.to_dict(), "repository_id": env["repo_id"],
            "project_id": env["project_id"], "branch": branch, "canonical_workspace": logical,
            "scopes": scopes if scopes is not None else [{"kind": "workspace", "resource": logical}],
            "run_id": str(uuid.uuid4()), "current_owner": {"actor": "actor-a", "session_id": "sess-a"},
            "revision": revision}


def _authority(request, pin, *, status="authorized", **overrides):
    value = {"status": status, "repository_id": request["repository_id"],
             "project_id": request["project_id"], "branch": request["branch"],
             "canonical_workspace": request["canonical_workspace"], "scopes": request["scopes"],
             "run_id": request["run_id"], "run_revision": request["run_revision"],
             "owner": request["owner"], "source_pin": pin.to_dict(), "context_hash": request["context_hash"]}
    value.update(overrides)
    return value


def test_two_local_checkouts_same_commit_resolve_to_same_logical_source(git_workspace, tmp_path):
    env = git_workspace
    second = tmp_path / "repo-b"
    shutil.copytree(env["root"], second)
    first_pin, second_pin = _pin(env), _pin(env, second)
    assert first_pin.source_hash == second_pin.source_hash
    contexts, requests = [], []

    def authorizer(request):
        requests.append(request)
        return _authority(request, first_pin)

    resolver = ClientWorkspaceResolver(authorizer)
    first = resolver.resolve(_mapping(env), _context(env, first_pin))
    # Match the source in the simulated current server response for the second checkout.
    second_context = _context(env, second_pin)
    second_context["run_id"] = requests[0]["run_id"]
    second_context["revision"] = requests[0]["run_revision"]
    second = ClientWorkspaceResolver(lambda req: _authority(req, second_pin)).resolve(
        _mapping(env, second), second_context)
    assert first["canonical_workspace"] == second["canonical_workspace"]
    assert first["source_pin"].source_hash == second["source_pin"].source_hash
    assert first["workspace"] != second["workspace"]


def test_clean_git_checkouts_with_crlf_worktree_bytes_share_semantic_source_pin(workspace_template, tmp_path):
    base = workspace_template["root"]
    lf_checkout, crlf_checkout = tmp_path / "lf-checkout", tmp_path / "crlf-checkout"
    subprocess.run(["git", "clone", "--quiet", "--no-checkout", str(base), str(lf_checkout)], check=True)
    subprocess.run(["git", "-C", str(lf_checkout), "config", "core.autocrlf", "false"], check=True)
    subprocess.run(["git", "-C", str(lf_checkout), "checkout", "--quiet", "--force", "main"], check=True)
    subprocess.run(["git", "clone", "--quiet", "--no-checkout", str(base), str(crlf_checkout)], check=True)
    subprocess.run(["git", "-C", str(crlf_checkout), "config", "core.autocrlf", "true"], check=True)
    subprocess.run(["git", "-C", str(crlf_checkout), "checkout", "--quiet", "--force", "main"], check=True)
    relative = workspace_template["relative"]
    lf_status = subprocess.run(["git", "-C", str(lf_checkout), "status", "--porcelain=v1", "--", relative],
        check=True, capture_output=True).stdout
    crlf_status = subprocess.run(["git", "-C", str(crlf_checkout), "status", "--porcelain=v1", "--", relative],
        check=True, capture_output=True).stdout
    lf_bytes = (lf_checkout / relative).read_bytes()
    crlf_bytes = (crlf_checkout / relative).read_bytes()
    assert lf_status == crlf_status == b""
    assert lf_bytes != crlf_bytes and b"\r\n" in crlf_bytes
    first = inspect_graph_source(lf_checkout, lf_checkout / relative, workspace_template["repo_id"],
        workspace_template["project_id"], graph_scope_id=workspace_template["project_id"],
        validate_graph_fn=validate_graph)["source_pin"]
    second = inspect_graph_source(crlf_checkout, crlf_checkout / relative, workspace_template["repo_id"],
        workspace_template["project_id"], graph_scope_id=workspace_template["project_id"],
        validate_graph_fn=validate_graph)["source_pin"]
    assert first.reviewed_commit == second.reviewed_commit
    assert first.dirty_state == second.dirty_state == "clean"
    assert first.source_hash == second.source_hash
    assert first.to_dict() == second.to_dict()


def test_graph_edit_or_branch_change_conflicts_with_expected_pin(git_workspace):
    env = git_workspace
    pin = _pin(env)
    context = _context(env, pin)
    calls = []
    resolver = ClientWorkspaceResolver(lambda req: (calls.append(req) or _authority(req, pin)))
    graph_path = env["graph_path"]
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["nodes"][0]["summary"] = "Changed source"
    graph_path.write_text(canonical_json(graph) + "\n", encoding="utf-8")
    with pytest.raises(PmtError, match="Source changed") as caught:
        resolver.resolve(_mapping(env), context)
    assert caught.value.code == "source_conflict"
    assert len(calls) == 1

    subprocess.run(["git", "-C", str(env["root"]), "switch", "-q", "-c", "other"], check=True)
    with pytest.raises(PmtError) as caught:
        ClientWorkspaceResolver(lambda req: _authority(req, pin)).resolve(_mapping(env), context)
    assert caught.value.code == "source_conflict"


def test_authorization_denial_and_rpc_failure_happen_before_filesystem_access(git_workspace, tmp_path):
    env = git_workspace
    missing = tmp_path / "not-created"
    pin = _pin(env)
    context = _context(env, pin)
    mapping = _mapping(env, missing)
    denied = ClientWorkspaceResolver(lambda req: _authority(req, pin, status="denied"))
    with pytest.raises(PmtError) as caught:
        denied.resolve(mapping, context)
    assert caught.value.code == "workspace_authority_stale"
    assert not missing.exists()

    offline = ClientWorkspaceResolver(lambda _req: (_ for _ in ()).throw(ConnectionError("offline")))
    with pytest.raises(PmtError) as caught:
        offline.resolve(mapping, context)
    assert caught.value.code == "workspace_authority_unavailable"
    assert not missing.exists()


def test_authorization_owner_scope_revision_and_source_are_rechecked(git_workspace):
    env = git_workspace
    pin = _pin(env)
    context = _context(env, pin)
    tests = [
        {"owner": {"actor": "someone-else", "session_id": "sess-a"}},
        {"run_revision": context["revision"] + 1},
        {"source_pin": pin.to_dict() | {"graph_hash": "0" * 64}},
        {"context_hash": "0" * 64},
    ]
    for override in tests:
        resolver = ClientWorkspaceResolver(lambda req, o=override: _authority(req, pin, **o))
        with pytest.raises(PmtError):
            resolver.resolve(_mapping(env), context)

    no_scope_context = _context(env, pin, scopes=[])
    # It is rejected locally before any authorization callback can grant broader access.
    called = []
    with pytest.raises(PmtError) as caught:
        ClientWorkspaceResolver(lambda req: (called.append(req), _authority(req, pin))[1]).resolve(
            _mapping(env), no_scope_context)
    assert caught.value.code == "workspace_scope_denied"
    assert not called


def test_scope_revocation_and_remote_branch_mapping_are_denied(git_workspace):
    env = git_workspace
    pin = _pin(env)
    context = _context(env, pin)
    revoked = ClientWorkspaceResolver(lambda req: _authority(req, pin, scopes=[]))
    with pytest.raises(PmtError) as caught:
        revoked.resolve(_mapping(env), context)
    assert caught.value.code == "workspace_authority_stale"

    with pytest.raises(PmtError) as caught:
        ClientWorkspaceResolver(lambda req: _authority(req, pin)).resolve(
            _mapping(env, branch="other"), context)
    assert caught.value.code == "source_conflict"

    actual_origin = subprocess.run(["git", "-C", str(env["root"]), "remote", "get-url", "origin"],
                                   capture_output=True, text=True)
    assert actual_origin.returncode != 0
    with pytest.raises(PmtError) as caught:
        ClientWorkspaceResolver(lambda req: _authority(req, pin)).resolve(
            _mapping(env, remote="https://expected.invalid/repo.git"), context)
    assert caught.value.code == "repository_remote_mismatch"


def test_canonical_workspace_is_branch_hash_and_has_no_physical_path(git_workspace, tmp_path):
    env = git_workspace
    pin = _pin(env)
    a = canonical_workspace(env["repo_id"], "main")
    b = canonical_workspace(env["repo_id"], "feature/topic")
    assert a.startswith(f"pmt://{env['repo_id']}/") and a != b
    assert str(env["root"]) not in a and str(tmp_path) not in a
    assert canonical_workspace(env["repo_id"], "main") == canonical_workspace(env["repo_id"], "main")
    assert pin.selected_ref == "main"


def test_path_traversal_and_symlink_are_rejected(git_workspace, tmp_path):
    env = git_workspace
    pin = _pin(env)
    context = _context(env, pin)
    resolver = ClientWorkspaceResolver(lambda req: _authority(req, pin))
    with pytest.raises(PmtError) as caught:
        resolver.resolve(_mapping(env) | {"relative_graph_path": "../secret.json"}, context)
    assert caught.value.code == "workspace_mapping_invalid"

    outside = tmp_path / "outside.json"
    outside.write_text(env["graph_path"].read_text(encoding="utf-8"), encoding="utf-8")
    link = env["root"] / "docs" / "pmt-docs" / "linked.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable in this test environment")
    with pytest.raises(PmtError) as caught:
        resolver.resolve(_mapping(env) | {"relative_graph_path": "docs/pmt-docs/linked.json"}, context)
    assert caught.value.code in {"resource_link_rejected", "workspace_path_invalid"}


def test_explicit_non_git_is_supported_but_broken_git_is_not_misclassified(git_workspace):
    env = git_workspace
    def clear_readonly(function, path, _exc):
        Path(path).chmod(Path(path).stat().st_mode | stat.S_IWRITE)
        function(path)
    shutil.rmtree(env["root"] / ".git", onerror=clear_readonly)
    pin = _pin(env)
    assert pin.source_kind == "non_git" and pin.reviewed_commit is None
    context = _context(env, pin, branch=None)
    resolver = ClientWorkspaceResolver(lambda req: _authority(req, pin))
    resolved = resolver.resolve(_mapping(env, branch=None), context)
    assert resolved["source_pin"].source_hash == pin.source_hash
    assert resolved["canonical_workspace"] == canonical_workspace(env["repo_id"], "non-git")

    (env["root"] / ".git").mkdir()
    with pytest.raises(PmtError) as caught:
        resolver.resolve(_mapping(env, branch=None), context)
    assert caught.value.code == "git_source_unavailable"
