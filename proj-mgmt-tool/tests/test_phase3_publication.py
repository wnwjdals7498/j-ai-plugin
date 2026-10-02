from __future__ import annotations

import hashlib
import ctypes
import errno
import os
import uuid

import pytest

from pmt.efficiency import publication
from pmt.errors import PmtError


def _case(tmp_path, *, original=b"old source", candidate=b"new candidate"):
    root = tmp_path / "workspace"
    parent = root / "docs"
    parent.mkdir(parents=True)
    target = parent / "graph.json"
    stage = parent / ".graph.stage"
    if original is not None:
        target.write_bytes(original)
    stage.write_bytes(candidate)
    return root, target, stage, str(uuid.uuid4())


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _publish(root, target, stage, effect, *, expected=b"old source", checkpoint=None):
    return publication.guarded_publish(
        target, stage, _hash(expected) if expected is not None else None,
        effect, root, checkpoint,
    )


def test_existing_target_is_detached_and_candidate_is_published_without_stage_alias(tmp_path):
    root, target, stage, effect = _case(tmp_path)
    receipt = _publish(root, target, stage, effect)
    recovery = root / receipt["recovery_ref"]
    candidate_snapshot = root / receipt["candidate_ref"]
    assert receipt["status"] == "published"
    assert target.read_bytes() == b"new candidate"
    assert recovery.read_bytes() == b"old source"
    assert _hash(candidate_snapshot.read_bytes()) == receipt["candidate_hash"]
    stage.write_bytes(b"later caller edit")
    assert target.read_bytes() == b"new candidate"
    assert recovery.read_bytes() == b"old source"


def test_journal_refs_are_stable_and_resolve_inside_the_owned_root(tmp_path):
    root, target, stage, effect = _case(tmp_path)
    refs = publication.publication_refs(target, effect, root)
    assert refs["target_ref"] == "docs/graph.json"
    assert refs["recovery_ref"] == f"docs/.graph.json.pmt-publish-{effect}.recovery"
    assert refs["candidate_ref"] == "docs/graph.json"
    assert refs["candidate_stage_ref"] == f"docs/.graph.json.pmt-publish-{effect}.candidate-stage"
    assert set(root.rglob("*")) == {target.parent, target, stage}


def test_same_effect_replays_and_different_candidate_conflicts_without_overwrite(tmp_path):
    root, target, stage, effect = _case(tmp_path)
    first = _publish(root, target, stage, effect)
    replay = _publish(root, target, stage, effect)
    assert first["status"] == "published" and replay["status"] == "replayed"
    stage.write_bytes(b"different candidate")
    conflict = _publish(root, target, stage, effect)
    assert conflict["status"] == "conflict"
    assert conflict["phase"] == "effect_id_reused_with_different_inputs"
    assert target.read_bytes() == b"new candidate"
    assert (root / first["recovery_ref"]).read_bytes() == b"old source"


def test_different_expected_hash_for_same_effect_conflicts(tmp_path):
    root, target, stage, effect = _case(tmp_path)
    first = _publish(root, target, stage, effect)
    other = publication.guarded_publish(target, stage, _hash(b"other old"),
                                        effect, root)
    assert first["status"] == "published" and other["status"] == "conflict"
    assert target.read_bytes() == b"new candidate"


def test_wrong_initial_target_is_preserved_as_conflict(tmp_path):
    root, target, stage, effect = _case(tmp_path, original=b"user edit")
    result = _publish(root, target, stage, effect)
    assert result["status"] == "conflict"
    assert result["phase"] == "before_intent"
    assert target.read_bytes() == b"user edit"
    assert stage.read_bytes() == b"new candidate"


def test_absent_target_uses_no_replace_create_and_replays(tmp_path):
    root, target, stage, effect = _case(tmp_path, original=None)
    created = _publish(root, target, stage, effect, expected=None)
    replayed = _publish(root, target, stage, effect, expected=None)
    assert created["status"] == "published"
    assert replayed["status"] == "replayed"
    assert created["recovery_ref"] is None
    assert target.read_bytes() == b"new candidate"


def test_absent_target_race_preserves_new_target_and_stage(tmp_path, monkeypatch):
    root, target, stage, effect = _case(tmp_path, original=None)
    link = publication._link_noreplace
    injected = False

    def create_target_then_link(source, destination):
        nonlocal injected
        if destination == target and not injected:
            injected = True
            destination.write_bytes(b"concurrent user target")
        return link(source, destination)

    monkeypatch.setattr(publication, "_link_noreplace", create_target_then_link)
    result = _publish(root, target, stage, effect, expected=None)
    assert result["status"] == "conflict"
    assert result["phase"] == "new_target_race"
    assert target.read_bytes() == b"concurrent user target"
    assert stage.read_bytes() == b"new candidate"


def test_edit_immediately_before_detach_is_restored_and_reported(tmp_path, monkeypatch):
    root, target, stage, effect = _case(tmp_path)
    rename = publication._rename_noreplace

    def edit_then_rename(source, destination):
        source.write_bytes(b"edit before detach")
        return rename(source, destination)

    monkeypatch.setattr(publication, "_rename_noreplace", edit_then_rename)
    result = _publish(root, target, stage, effect)
    recovery = root / result["recovery_ref"]
    assert result["status"] == "conflict"
    assert result["phase"] == "detached_original_changed"
    assert target.read_bytes() == b"edit before detach"
    assert recovery.read_bytes() == b"edit before detach"
    assert stage.read_bytes() == b"new candidate"


def test_new_target_created_after_detach_is_preserved_with_recovery(tmp_path, monkeypatch):
    root, target, stage, effect = _case(tmp_path)
    rename = publication._rename_noreplace

    def create_new_target(source, destination):
        result = rename(source, destination)
        target.write_bytes(b"concurrent new target")
        return result

    monkeypatch.setattr(publication, "_rename_noreplace", create_new_target)
    result = _publish(root, target, stage, effect)
    assert result["status"] == "conflict"
    assert result["phase"] == "new_target_exists"
    assert target.read_bytes() == b"concurrent new target"
    assert (root / result["recovery_ref"]).read_bytes() == b"old source"
    assert stage.read_bytes() == b"new candidate"


def test_old_open_handle_edit_after_detach_is_preserved_and_conflicts(tmp_path, monkeypatch):
    root, target, stage, effect = _case(tmp_path)
    rename = publication._rename_noreplace

    def edit_detached_file(source, destination):
        result = rename(source, destination)
        destination.write_bytes(b"old handle edit")
        return result

    monkeypatch.setattr(publication, "_rename_noreplace", edit_detached_file)
    result = _publish(root, target, stage, effect)
    recovery = root / result["recovery_ref"]
    assert result["status"] == "conflict"
    assert result["phase"] == "detached_original_changed"
    assert target.read_bytes() == b"old handle edit"
    assert recovery.read_bytes() == b"old handle edit"
    assert stage.read_bytes() == b"new candidate"


@pytest.mark.parametrize("stop_phase", [
    "intent", "candidate_staged", "original_preserved", "target_detached", "candidate_published",
])
def test_checkpoint_interruption_is_reconciled_from_filesystem_hashes(tmp_path, stop_phase):
    root, target, stage, effect = _case(tmp_path)
    seen = []

    def stop_once(phase, receipt):
        seen.append((phase, receipt["phase"]))
        if phase == stop_phase:
            raise RuntimeError("simulated journal interruption")

    with pytest.raises(PmtError, match="reconcile") as raised:
        _publish(root, target, stage, effect, checkpoint=stop_once)
    assert raised.value.code == "publication_reconcile_required"
    assert raised.value.details["phase"] == stop_phase
    recovered = _publish(root, target, stage, effect)
    assert recovered["status"] in {"published", "replayed"}
    assert target.read_bytes() == b"new candidate"
    assert (root / recovered["recovery_ref"]).read_bytes() == b"old source"


def test_outside_root_and_linked_stage_are_rejected(tmp_path):
    root, target, stage, effect = _case(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    with pytest.raises(PmtError) as outside_error:
        publication.guarded_publish(outside, stage, None, effect, root)
    assert outside_error.value.code == "publication_scope_invalid"

    link = target.parent / "linked-stage"
    try:
        link.symlink_to(stage)
    except OSError:
        pytest.skip("This Windows account cannot create symlinks")
    with pytest.raises(PmtError) as link_error:
        publication.guarded_publish(target, link, _hash(b"old source"), str(uuid.uuid4()), root)
    assert link_error.value.code == "publication_link_rejected"


def test_reparse_point_is_rejected_by_metadata_check(tmp_path, monkeypatch):
    root, target, stage, effect = _case(tmp_path)
    monkeypatch.setattr(publication, "_reparse", lambda _info: True)
    with pytest.raises(PmtError) as raised:
        _publish(root, target, stage, effect)
    assert raised.value.code == "publication_scope_invalid"


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing-lock behavior")
def test_open_target_handle_blocks_detach_without_losing_bytes_and_retry_succeeds(tmp_path):
    root, target, stage, effect = _case(tmp_path)
    with target.open("r+b") as open_handle:
        with pytest.raises(PmtError) as raised:
            _publish(root, target, stage, effect)
        assert raised.value.code == "publication_io_error"
        assert raised.value.retryable is True
        assert target.read_bytes() == b"old source"
        assert stage.read_bytes() == b"new candidate"
    recovered = _publish(root, target, stage, effect)
    assert recovered["status"] == "published"
    assert target.read_bytes() == b"new candidate"
    assert (root / recovered["recovery_ref"]).read_bytes() == b"old source"


def test_unsupported_no_replace_link_never_uses_replace_fallback(tmp_path, monkeypatch):
    root, target, stage, effect = _case(tmp_path)
    real_link = publication._link_noreplace
    calls = 0
    replace_calls = []

    def deny_candidate(source, destination):
        nonlocal calls
        calls += 1
        if destination == target:
            raise PmtError("publication_unsupported", "no hardlinks", 3, False)
        return real_link(source, destination)

    monkeypatch.setattr(publication, "_link_noreplace", deny_candidate)
    monkeypatch.setattr(publication.os, "replace", lambda *args: replace_calls.append(args))
    with pytest.raises(PmtError) as raised:
        _publish(root, target, stage, effect)
    assert raised.value.code == "publication_unsupported"
    assert raised.value.details["receipt"]["recovery_ref"]
    assert calls >= 1
    assert replace_calls == []
    assert not target.exists()
    assert (root / f"docs/.graph.json.pmt-publish-{effect}.recovery").read_bytes() == b"old source"
    assert stage.read_bytes() == b"new candidate"


def test_directory_fsync_unsupported_is_visible_in_receipt(tmp_path, monkeypatch):
    root, target, stage, effect = _case(tmp_path)
    monkeypatch.setattr(publication, "_fsync_dir", lambda _path: False)
    checkpoints = []
    receipt = _publish(root, target, stage, effect,
                       checkpoint=lambda phase, details: checkpoints.append((phase, details)))
    assert receipt["status"] == "published"
    assert receipt["durability_warning"] == "directory_fsync_unsupported"
    assert any(details.get("durability_warning") == "directory_fsync_unsupported"
               for _phase, details in checkpoints)


def test_directory_fsync_io_failure_returns_reconcile_receipt_after_detach(tmp_path, monkeypatch):
    root, target, stage, effect = _case(tmp_path)
    real_fsync = publication._fsync_dir
    calls = 0
    checkpoints = []

    def fail_after_detach(path):
        nonlocal calls
        calls += 1
        if calls == 4:  # capability probe, manifest, and candidate snapshot have completed
            raise PmtError("publication_durability_error", "injected directory fsync failure", 4, True)
        return real_fsync(path)

    monkeypatch.setattr(publication, "_fsync_dir", fail_after_detach)
    with pytest.raises(PmtError) as raised:
        _publish(root, target, stage, effect,
                 checkpoint=lambda phase, details: checkpoints.append((phase, details)))
    assert raised.value.code == "publication_reconcile_required"
    receipt = raised.value.details["receipt"]
    assert receipt["filesystem_status"] == "durability_unknown"
    assert any(phase == "durability_unknown" for phase, _details in checkpoints)
    assert not target.exists()
    assert (root / receipt["recovery_ref"]).read_bytes() == b"old source"
    monkeypatch.setattr(publication, "_fsync_dir", real_fsync)
    replay = _publish(root, target, stage, effect)
    assert replay["status"] == "published"
    assert target.read_bytes() == b"new candidate"


@pytest.mark.parametrize("result,errno_value,expected", [
    (0, 0, "success"),
    (-1, errno.EEXIST, "exists"),
    (-1, errno.ENOSYS, "unsupported"),
])
def test_linux_renameat2_path_is_exercised_with_a_stubbed_ctypes_symbol(
        tmp_path, monkeypatch, result, errno_value, expected):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    calls = []

    class FakeFunction:
        argtypes = None
        restype = None

        def __call__(self, *args):
            calls.append(args)
            if result:
                ctypes.set_errno(errno_value)
            return result

    class FakeLib:
        renameat2 = FakeFunction()

    monkeypatch.setattr(publication.os, "name", "posix")
    monkeypatch.setattr(publication.sys, "platform", "linux")
    monkeypatch.setattr(publication.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLib())
    if expected == "success":
        publication._rename_noreplace(source, destination)
    elif expected == "exists":
        with pytest.raises(FileExistsError):
            publication._rename_noreplace(source, destination)
    else:
        with pytest.raises(PmtError) as raised:
            publication._rename_noreplace(source, destination)
        assert raised.value.code == "publication_unsupported"
    assert calls == [(-100, os.fsencode(source), -100, os.fsencode(destination), 1)]
    assert FakeLib.renameat2.argtypes == [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    assert FakeLib.renameat2.restype is ctypes.c_int


def test_preexisting_recovery_path_is_never_overwritten(tmp_path):
    root, target, stage, effect = _case(tmp_path)
    recovery = target.parent / f".{target.name}.pmt-publish-{effect}.recovery"
    recovery.write_bytes(b"do not overwrite")
    with pytest.raises(PmtError) as raised:
        _publish(root, target, stage, effect)
    assert raised.value.code == "publication_conflict"
    assert target.read_bytes() == b"old source"
    assert recovery.read_bytes() == b"do not overwrite"
