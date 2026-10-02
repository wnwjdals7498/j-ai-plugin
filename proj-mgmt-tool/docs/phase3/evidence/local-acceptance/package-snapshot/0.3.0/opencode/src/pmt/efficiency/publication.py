"""Recoverable no-replace publication for workspace files.

Callers must hold the active PMT claim and persist their intent before invoking
this helper. It never authorizes a scope or updates the database itself.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import stat
import sys
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..errors import PmtError

_MANIFEST_VERSION = 1
_CHUNK = 1024 * 1024


def _fail(code: str, message: str, *, retryable: bool = False,
          details: Mapping[str, Any] | None = None) -> PmtError:
    return PmtError(code, message, 4 if retryable else 3, retryable, dict(details or {}))


def _reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _path(value: str | Path, field: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise _fail("publication_path_invalid", f"{field} must be an absolute path")
    if not Path(value).is_absolute():
        raise _fail("publication_path_invalid", f"{field} must be absolute")
    result = Path(os.path.abspath(value))
    if not result.is_absolute():
        raise _fail("publication_path_invalid", f"{field} must be absolute")
    return result


def publication_refs(target: str | Path, effect_id: str,
                     owned_root: str | Path) -> dict[str, str | None]:
    """Return stable relative refs for a journal before any filesystem effect."""
    try:
        if str(uuid.UUID(effect_id)) != effect_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise _fail("publication_input_invalid", "effect_id must be a canonical UUID") from exc
    root, dst = _path(owned_root, "owned_root"), _path(target, "target")
    _validate_path(root, dst, leaf="optional")
    suffix = f".pmt-publish-{effect_id}"
    candidate_stage = dst.parent / f".{dst.name}{suffix}.candidate-stage"
    return {"target_ref": str(dst.relative_to(root)).replace(os.sep, "/"),
            "manifest_ref": str((dst.parent / f".{dst.name}{suffix}.json").relative_to(root)).replace(os.sep, "/"),
            "recovery_ref": str((dst.parent / f".{dst.name}{suffix}.recovery").relative_to(root)).replace(os.sep, "/"),
            "candidate_ref": str(dst.relative_to(root)).replace(os.sep, "/"),
            "candidate_stage_ref": str(candidate_stage.relative_to(root)).replace(os.sep, "/")}


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _validate_path(root: Path, path: Path, *, leaf: str) -> None:
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise _fail("publication_scope_invalid", "Publication path is outside owned_root") from exc
    root_info = _lstat(root)
    if (root_info is None or not stat.S_ISDIR(root_info.st_mode)
            or stat.S_ISLNK(root_info.st_mode) or _reparse(root_info)
            or root.resolve(strict=True) != root):
        raise _fail("publication_scope_invalid", "owned_root must be a real existing directory")
    if not parts:
        raise _fail("publication_path_invalid", "File path cannot equal owned_root")
    current = root
    for index, component in enumerate(parts):
        current = current / component
        info = _lstat(current)
        if info is None:
            if index < len(parts) - 1 or leaf == "required":
                raise _fail("publication_path_invalid", "Publication parent or required file is missing")
            continue
        if stat.S_ISLNK(info.st_mode) or _reparse(info):
            raise _fail("publication_link_rejected", "Links and reparse points are not allowed")
        final = index == len(parts) - 1
        if not final and not stat.S_ISDIR(info.st_mode):
            raise _fail("publication_path_invalid", "Publication parent is not a directory")
        if final and leaf in {"required", "optional"} and not stat.S_ISREG(info.st_mode):
            raise _fail("publication_path_invalid", "Publication files must be regular files")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        path_info = path.lstat()
        if stat.S_ISLNK(path_info.st_mode) or _reparse(path_info) or not stat.S_ISREG(path_info.st_mode):
            raise _fail("publication_link_rejected", "Publication input is not a regular file")
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise _fail("publication_path_invalid", "Publication input is not a regular file")
            if (path_info.st_dev, path_info.st_ino) != (before.st_dev, before.st_ino):
                raise _fail("publication_conflict", "Publication path changed before it could be opened")
            for block in iter(lambda: stream.read(_CHUNK), b""):
                digest.update(block)
            after = os.fstat(stream.fileno())
    except PmtError:
        raise
    except OSError as exc:
        raise _fail("publication_io_error", "Could not read publication file", retryable=True) from exc
    current_path = path.lstat()
    if (current_path.st_dev, current_path.st_ino) != (after.st_dev, after.st_ino):
        raise _fail("publication_conflict", "Publication path changed while it was being hashed")
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise _fail("publication_conflict", "File changed while it was being hashed")
    return digest.hexdigest()


def _fsync_dir(path: Path) -> bool:
    if os.name == "nt":
        return False
    unsupported = {errno.EINVAL, errno.ENOSYS, getattr(errno, "ENOTSUP", -1),
                   getattr(errno, "EOPNOTSUPP", -1)}
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        if exc.errno in unsupported:
            return False
        raise _fail("publication_durability_error", "Could not open directory for durability sync",
                    retryable=True, details={"errno": exc.errno}) from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno in unsupported:
            return False
        raise _fail("publication_durability_error", "Directory durability sync failed",
                    retryable=True, details={"errno": exc.errno}) from exc
    finally:
        os.close(fd)
    return True


def _link_noreplace(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except FileExistsError:
        raise
    except (AttributeError, NotImplementedError) as exc:
        raise _fail("publication_unsupported", "No-replace hard links are unsupported") from exc
    except OSError as exc:
        unsupported = {errno.EXDEV, errno.ENOSYS, getattr(errno, "ENOTSUP", -1),
                       getattr(errno, "EOPNOTSUPP", -1)}
        if exc.errno in unsupported or getattr(exc, "winerror", None) in {1, 50}:
            raise _fail("publication_unsupported", "No-replace hard links are unsupported") from exc
        raise _fail("publication_io_error", "Safe hard-link publication failed", retryable=True) from exc


def _rename_noreplace(source: Path, target: Path) -> None:
    if os.name == "nt":
        try:
            os.rename(source, target)  # Windows os.rename refuses an existing target.
        except FileExistsError:
            raise
        except OSError as exc:
            raise _fail("publication_io_error", "Could not detach original target safely", retryable=True) from exc
        return
    if not sys.platform.startswith("linux"):
        raise _fail("publication_unsupported", "Atomic no-replace rename is unavailable on this platform")
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (OSError, AttributeError) as exc:
        raise _fail("publication_unsupported", "Atomic no-replace rename is unavailable") from exc
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1) == 0:
        return
    code = ctypes.get_errno()
    if code == errno.EEXIST:
        raise FileExistsError(code, os.strerror(code), str(target))
    if code in {errno.ENOSYS, errno.EINVAL, getattr(errno, "EOPNOTSUPP", -1)}:
        raise _fail("publication_unsupported", "Atomic no-replace rename is unsupported")
    raise _fail("publication_io_error", "Could not detach original target safely",
                retryable=True, details={"errno": code})


def _write_exclusive(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise
    except OSError as exc:
        raise _fail("publication_io_error", "Could not persist publication intent", retryable=True) from exc


def _atomic_create(path: Path, data: bytes) -> None:
    """Publish metadata under a new name without exposing a partial file."""
    temporary = path.parent / f".{path.name}.{uuid.uuid4()}.tmp"
    _write_exclusive(temporary, data)
    try:
        _link_noreplace(temporary, path)
    except Exception:
        raise
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _snapshot(source: Path, target: Path, wanted_hash: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        temporary = target.parent / f".{target.name}.{uuid.uuid4()}.tmp"
        fd = os.open(temporary, flags, 0o600)
    except FileExistsError:
        if _hash(target) != wanted_hash:
            raise _fail("publication_conflict", "Candidate snapshot differs from this effect")
        return
    except OSError as exc:
        raise _fail("publication_io_error", "Could not stage immutable candidate", retryable=True) from exc
    try:
        try:
            with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
                shutil.copyfileobj(src, dst, _CHUNK)
                dst.flush()
                os.fsync(dst.fileno())
        except OSError as exc:
            raise _fail("publication_io_error", "Could not stage immutable candidate", retryable=True) from exc
        if _hash(temporary) != wanted_hash:
            raise _fail("publication_conflict", "Stage changed while its snapshot was created")
        try:
            _link_noreplace(temporary, target)
        except FileExistsError:
            if _hash(target) != wanted_hash:
                raise _fail("publication_conflict", "Concurrent candidate snapshot differs from this effect")
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_manifest(path: Path) -> dict[str, Any] | None:
    info = _lstat(path)
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode) or _reparse(info) or not stat.S_ISREG(info.st_mode):
        raise _fail("publication_link_rejected", "Publication manifest is not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise _fail("publication_conflict", "Publication manifest is incomplete or invalid") from exc
    if not isinstance(value, dict):
        raise _fail("publication_conflict", "Publication manifest is invalid")
    return value


def _receipt(status: str, data: Mapping[str, Any], phase: str,
             original_hash: str | None = None) -> dict[str, Any]:
    return {"status": status, "effect_id": data["effect_id"], "target_ref": data["target_ref"],
            "expected_hash": data["expected_hash"],
            "original_hash": original_hash if original_hash is not None else data["expected_hash"],
            "candidate_hash": data["candidate_hash"], "recovery_ref": data["recovery_ref"],
            "candidate_ref": data["candidate_ref"], "manifest_ref": data["manifest_ref"],
            "durability_warning": data["durability_warning"], "phase": phase}


def _checkpoint(callback, phase: str, receipt: Mapping[str, Any]) -> None:
    if callback is None:
        return
    try:
        callback(phase, dict(receipt))
    except Exception as exc:
        raise _fail("publication_reconcile_required",
                    "Filesystem effect may have advanced; reconcile using this receipt",
                    retryable=True, details={"phase": phase, "receipt": dict(receipt)}) from exc


def _sync_after(path: Path, data: Mapping[str, Any], phase: str,
                callback: Callable[[str, dict[str, Any]], None] | None) -> None:
    """Sync a namespace effect or report its durability as uncertain."""
    try:
        supported = _fsync_dir(path)
    except PmtError as exc:
        receipt = _receipt("conflict", data, phase)
        receipt["filesystem_status"] = "durability_unknown"
        _checkpoint(callback, "durability_unknown", receipt)
        raise _fail("publication_reconcile_required",
                    "Filesystem effect occurred but directory durability is unknown",
                    retryable=True, details={"phase": phase, "receipt": receipt,
                                             "cause": exc.code}) from exc
    if not supported and not data.get("durability_warning"):
        receipt = _receipt("published", data, phase)
        receipt["durability_warning"] = "directory_fsync_unsupported"
        _checkpoint(callback, "durability_unsupported", receipt)


def _conflict(data, phase: str, original_hash, callback):
    result = _receipt("conflict", data, phase, original_hash)
    _checkpoint(callback, "conflict", result)
    return result


def _replay(data, phase, original_hash, callback):
    result = _receipt("replayed", data, phase, original_hash)
    _checkpoint(callback, "candidate_published", result)
    return result


def guarded_publish(target: str | Path, stage: str | Path, expected_hash: str | None,
                    effect_id: str, owned_root: str | Path,
                    checkpoint: Callable[[str, dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Publish a staged file without replacing a target or losing edited bytes.

    The target and stage must be siblings inside owned_root. Original bytes are
    detached to an immutable effect-specific recovery path. Candidate bytes are
    copied to a private snapshot then hard-linked to target only if that name is
    still absent. Recovery material is intentionally retained for checked cleanup.
    """
    try:
        if str(uuid.UUID(effect_id)) != effect_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise _fail("publication_input_invalid", "effect_id must be a canonical UUID") from exc
    if expected_hash is not None and (not isinstance(expected_hash, str) or len(expected_hash) != 64
                                      or any(c not in "0123456789abcdef" for c in expected_hash)):
        raise _fail("publication_input_invalid", "expected_hash must be a lowercase SHA-256 or null")
    if checkpoint is not None and not callable(checkpoint):
        raise _fail("publication_input_invalid", "checkpoint must be callable or null")

    root, dst, src = _path(owned_root, "owned_root"), _path(target, "target"), _path(stage, "stage")
    _validate_path(root, dst, leaf="optional")
    _validate_path(root, src, leaf="required")
    if dst.parent != src.parent or dst == src:
        raise _fail("publication_path_invalid", "target and stage must be distinct siblings")
    if dst.parent.resolve(strict=True) != dst.parent:
        raise _fail("publication_link_rejected", "Publication parent resolves through an alias")
    try:
        if src.stat().st_dev != dst.parent.stat().st_dev:
            raise _fail("publication_unsupported", "Stage and target are on different filesystems")
    except OSError as exc:
        raise _fail("publication_io_error", "Could not inspect publication filesystem", retryable=True) from exc
    directory_sync_supported = _fsync_dir(dst.parent)

    target_ref, stage_ref = (str(p.relative_to(root)).replace(os.sep, "/") for p in (dst, src))
    suffix = f".pmt-publish-{effect_id}"
    manifest = dst.parent / f".{dst.name}{suffix}.json"
    recovery = dst.parent / f".{dst.name}{suffix}.recovery" if expected_hash is not None else None
    candidate = dst.parent / f".{dst.name}{suffix}.candidate-stage"
    manifest_ref = str(manifest.relative_to(root)).replace(os.sep, "/")
    candidate_hash = _hash(src)
    data = {"manifest_version": _MANIFEST_VERSION, "effect_id": effect_id, "target_ref": target_ref,
            "stage_ref": stage_ref, "expected_hash": expected_hash, "candidate_hash": candidate_hash,
            "recovery_ref": str(recovery.relative_to(root)).replace(os.sep, "/") if recovery else None,
            "candidate_ref": target_ref,
            "candidate_stage_ref": str(candidate.relative_to(root)).replace(os.sep, "/"),
            "manifest_ref": manifest_ref,
            "durability_warning": None if directory_sync_supported else "directory_fsync_unsupported"}
    saved = _read_manifest(manifest)
    if saved is not None:
        if saved != data:
            return _receipt("conflict", data, "effect_id_reused_with_different_inputs")
    else:
        if ((recovery is not None and _lstat(recovery) is not None)
                or _lstat(candidate) is not None):
            raise _fail("publication_conflict", "Effect paths exist without a matching manifest",
                        details={"target_ref": target_ref, "manifest_ref": manifest_ref})
        current = _hash(dst) if _lstat(dst) is not None else None
        if current != expected_hash:
            return _conflict(data, "before_intent", current, checkpoint)
        _validate_path(root, dst, leaf="optional")
        _validate_path(root, src, leaf="required")
        try:
            _atomic_create(manifest, (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
        except FileExistsError:
            saved = _read_manifest(manifest)
            if saved != data:
                return _conflict(data, "manifest_created_by_different_effect", None, checkpoint)
        _sync_after(dst.parent, data, "intent", checkpoint)
    receipt = _receipt("published", data, "intent")
    _checkpoint(checkpoint, "intent", receipt)

    target_hash = _hash(dst) if _lstat(dst) is not None else None
    recovery_hash = _hash(recovery) if recovery is not None and _lstat(recovery) is not None else None
    if target_hash == candidate_hash and (expected_hash is None or recovery_hash == expected_hash):
        stage_info = _lstat(candidate)
        target_info = _lstat(dst)
        if stage_info is not None:
            if (stage_info.st_dev, stage_info.st_ino) != (target_info.st_dev, target_info.st_ino):
                return _conflict(data, "candidate_stage_changed_after_publish", recovery_hash, checkpoint)
            candidate.unlink(missing_ok=True)
            _sync_after(dst.parent, data, "candidate_cleanup", checkpoint)
            target_hash = _hash(dst)
            recovery_hash = _hash(recovery) if recovery is not None else None
            if target_hash != candidate_hash or (recovery is not None and recovery_hash != expected_hash):
                return _conflict(data, "post_cleanup_hash_mismatch", recovery_hash, checkpoint)
        return _replay(data, "candidate_published", recovery_hash, checkpoint)

    if _lstat(candidate) is None:
        _validate_path(root, src, leaf="required")
        _validate_path(root, candidate, leaf="optional")
        _snapshot(src, candidate, candidate_hash)
        _sync_after(dst.parent, data, "candidate_staged", checkpoint)
        _checkpoint(checkpoint, "candidate_staged", _receipt("published", data, "candidate_staged"))
    elif _hash(candidate) != candidate_hash:
        current = _hash(dst) if _lstat(dst) is not None else None
        return _conflict(data, "candidate_snapshot_mismatch", current, checkpoint)

    target_hash = _hash(dst) if _lstat(dst) is not None else None
    recovery_hash = _hash(recovery) if recovery is not None and _lstat(recovery) is not None else None
    if target_hash == candidate_hash:
        if expected_hash is None or recovery_hash == expected_hash:
            return _replay(data, "candidate_published", recovery_hash, checkpoint)
        return _conflict(data, "recovery_changed_after_publish", recovery_hash, checkpoint)

    if expected_hash is not None and recovery_hash is not None:
        if recovery_hash != expected_hash:
            if target_hash is None:
                try:
                    _link_noreplace(recovery, dst)
                    _sync_after(dst.parent, data, "recovery_restored", checkpoint)
                except FileExistsError:
                    pass
            return _conflict(data, "detached_original_changed", recovery_hash, checkpoint)
        if target_hash is not None:
            return _conflict(data, "new_target_exists", recovery_hash, checkpoint)
    elif expected_hash is not None:
        if target_hash is None:
            return _conflict(data, "original_and_recovery_missing", None, checkpoint)
        if target_hash != expected_hash:
            return _conflict(data, "target_changed_before_detach", target_hash, checkpoint)
        try:
            _validate_path(root, dst, leaf="required")
            _validate_path(root, recovery, leaf="optional")
            _rename_noreplace(dst, recovery)
            _sync_after(dst.parent, data, "target_detached", checkpoint)
        except FileExistsError:
            recovery_hash = _hash(recovery) if _lstat(recovery) is not None else None
            current_target = _hash(dst) if _lstat(dst) is not None else None
            if recovery_hash == expected_hash and current_target is None:
                pass
            else:
                return _conflict(data, "recovery_path_already_exists", recovery_hash, checkpoint)
        detached_hash = _hash(recovery)
        receipt = _receipt("published", data, "target_detached", detached_hash)
        _checkpoint(checkpoint, "original_preserved", receipt)
        _checkpoint(checkpoint, "target_detached", receipt)
        if detached_hash != expected_hash:
            try:
                _link_noreplace(recovery, dst)
                _sync_after(dst.parent, data, "recovery_restored", checkpoint)
            except FileExistsError:
                pass
            return _conflict(data, "detached_original_changed", detached_hash, checkpoint)
    elif target_hash is not None:
        return _conflict(data, "new_target_exists", target_hash, checkpoint)

    if recovery is not None:
        recovery_hash = _hash(recovery) if _lstat(recovery) is not None else None
        if recovery_hash != expected_hash:
            if _lstat(dst) is None and recovery_hash is not None:
                try:
                    _link_noreplace(recovery, dst)
                    _sync_after(dst.parent, data, "recovery_restored", checkpoint)
                except FileExistsError:
                    pass
            return _conflict(data, "detached_original_changed", recovery_hash, checkpoint)
    if _lstat(dst) is not None:
        return _conflict(data, "new_target_exists", _hash(dst), checkpoint)
    _validate_path(root, candidate, leaf="required")
    _validate_path(root, dst, leaf="optional")
    try:
        _link_noreplace(candidate, dst)
    except FileExistsError:
        current_hash = _hash(dst) if _lstat(dst) is not None else None
        recovery_hash = _hash(recovery) if recovery is not None and _lstat(recovery) is not None else None
        if current_hash == candidate_hash and (expected_hash is None or recovery_hash == expected_hash):
            return _replay(data, "candidate_published", recovery_hash, checkpoint)
        return _conflict(data, "new_target_race", current_hash, checkpoint)
    except PmtError as exc:
        recovery_hash = _hash(recovery) if recovery is not None and _lstat(recovery) is not None else None
        receipt = _receipt("conflict", data, "candidate_link_unsupported", recovery_hash)
        _checkpoint(checkpoint, "conflict", receipt)
        details = dict(exc.details or {}) | {"phase": "candidate_link_unsupported", "receipt": receipt}
        raise PmtError(exc.code, exc.message, exc.exit_code, exc.retryable, details) from exc
    _sync_after(dst.parent, data, "candidate_published", checkpoint)
    target_hash = _hash(dst)
    recovery_hash = _hash(recovery) if recovery is not None else None
    receipt = _receipt("published", data, "candidate_published", recovery_hash)
    if target_hash != candidate_hash or (recovery is not None and recovery_hash != expected_hash):
        return _conflict(data, "post_publish_hash_mismatch", recovery_hash, checkpoint)
    candidate_info, target_info = _lstat(candidate), _lstat(dst)
    if target_info is None or (candidate_info is not None and
            (candidate_info.st_dev, candidate_info.st_ino) != (target_info.st_dev, target_info.st_ino)):
        return _conflict(data, "candidate_stage_changed_after_publish", recovery_hash, checkpoint)
    if candidate_info is not None:
        candidate.unlink(missing_ok=True)
        _sync_after(dst.parent, data, "candidate_cleanup", checkpoint)
    target_hash = _hash(dst)
    recovery_hash = _hash(recovery) if recovery is not None else None
    if target_hash != candidate_hash or (recovery is not None and recovery_hash != expected_hash):
        return _conflict(data, "post_cleanup_hash_mismatch", recovery_hash, checkpoint)
    receipt = _receipt("published", data, "candidate_published", recovery_hash)
    _checkpoint(checkpoint, "candidate_published", receipt)
    return receipt
