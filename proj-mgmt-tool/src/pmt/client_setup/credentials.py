"""Per-user protected storage for the Host credential."""
from __future__ import annotations

import ctypes
import os
import stat
import tempfile
from contextlib import contextmanager, nullcontext
from ctypes import wintypes
from pathlib import Path

from ..errors import PmtError

ENV_NAME = "PMT_HOST_CREDENTIAL"
_FILENAME = "host-credential"


def _paths(config_root):
    directory = Path(config_root) / "secrets"
    return directory, directory / (_FILENAME + (".dpapi" if os.name == "nt" else ""))


def has_credential_store(config_root):
    _directory, path = _paths(config_root)
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


@contextmanager
def _credential_lock(config_root):
    """Use the same per-ConfigRoot lock as storage profile publication."""
    from ..storage_config import _config_lock
    root = Path(config_root)
    root.mkdir(parents=True, exist_ok=True)
    with _config_lock(root):
        yield


def _protect_windows(data, *, decrypt=False):
    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    raw = ctypes.create_string_buffer(data)
    source = BLOB(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)))
    target = BLOB()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(BLOB), wintypes.LPCWSTR, ctypes.POINTER(BLOB),
                                       wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(BLOB)]
    crypt.CryptProtectData.restype = wintypes.BOOL
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(BLOB), ctypes.POINTER(wintypes.LPWSTR),
                                         ctypes.POINTER(BLOB), wintypes.LPVOID, wintypes.LPVOID,
                                         wintypes.DWORD, ctypes.POINTER(BLOB)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    ui_forbidden = 0x1
    if decrypt:
        description = wintypes.LPWSTR()
        ok = crypt.CryptUnprotectData(ctypes.byref(source), ctypes.byref(description), None, None, None,
                                      ui_forbidden, ctypes.byref(target))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), "PMT Host credential", None, None, None,
                                    ui_forbidden, ctypes.byref(target))
    if not ok:
        raise OSError(ctypes.get_last_error(), "Windows credential protection failed")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(target.pbData)
        if decrypt and description.value:
            kernel.LocalFree(description)


def _is_reparse_or_symlink(path, info):
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _validate_unix_metadata(directory_stat, file_stat, uid):
    """Pure metadata policy, testable on hosts without POSIX ownership APIs."""
    if directory_stat.st_uid != uid or file_stat is not None and file_stat.st_uid != uid:
        raise PmtError("credential_store_insecure", "Host credential store ownership is not private")
    if stat.S_IMODE(directory_stat.st_mode) & 0o077:
        raise PmtError("credential_store_insecure", "Host credential directory permissions are too broad")
    if file_stat is not None and stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise PmtError("credential_store_insecure", "Host credential file permissions are too broad")


def _validate_paths(directory, path, *, creating=False):
    try:
        directory_info = directory.lstat()
    except FileNotFoundError:
        if not creating:
            raise
        directory_info = None
    if directory_info is not None and (not stat.S_ISDIR(directory_info.st_mode)
                                       or _is_reparse_or_symlink(directory, directory_info)):
        raise PmtError("credential_store_insecure", "Host credential directory is not a private directory")
    try:
        file_info = path.lstat()
    except FileNotFoundError:
        file_info = None
    if file_info is not None and (not stat.S_ISREG(file_info.st_mode) or _is_reparse_or_symlink(path, file_info)):
        raise PmtError("credential_store_insecure", "Host credential file is not a regular file")
    if os.name != "nt" and directory_info is not None:
        uid = os.getuid()
        _validate_unix_metadata(directory_info, file_info, uid)
    return directory_info, file_info


def _atomic_write(directory, path, data):
    fd, temporary = tempfile.mkstemp(prefix=".host-credential-", dir=str(directory))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Recheck immediately before replacement to reject swapped links/reparse points.
        _validate_paths(directory, path, creating=False)
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def store_credential(config_root, credential):
    """Store credential protected for the current OS user."""
    if not isinstance(credential, str) or not credential:
        raise PmtError("credential_unavailable", "A nonempty Host credential is required")
    directory, path = _paths(config_root)
    try:
        with _credential_lock(config_root):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory_info, file_info = _validate_paths(directory, path, creating=False)
            if os.name != "nt":
                if directory_info is not None and directory_info.st_uid != os.getuid():
                    raise PmtError("credential_store_insecure", "Host credential directory has another owner")
                if file_info is not None and file_info.st_uid != os.getuid():
                    raise PmtError("credential_store_insecure", "Host credential file has another owner")
                os.chmod(directory, 0o700)
            data = credential.encode("utf-8")
            if os.name == "nt":
                data = _protect_windows(data)
            _atomic_write(directory, path, data)
    except PmtError:
        raise
    except (OSError, UnicodeError, ctypes.ArgumentError, AttributeError) as exc:
        raise PmtError("credential_store_unreadable", "Host credential could not be stored") from exc
    return path


def _check_unix_owner(directory, path):
    directory_info, file_info = _validate_paths(directory, path)
    _validate_unix_metadata(directory_info, file_info, os.getuid())


def snapshot_credential(config_root):
    directory, path = _paths(config_root)
    try:
        with _credential_lock(config_root):
            _validate_paths(directory, path, creating=True)
            return path.read_bytes() if path.exists() else None
    except PmtError:
        raise
    except OSError as exc:
        raise PmtError("credential_store_unreadable", "Host credential store cannot be read") from exc


def stage_credential(config_root, credential):
    """Replace a credential under the ConfigRoot lock and return rollback bytes."""
    if not isinstance(credential, str) or not credential:
        raise PmtError("credential_unavailable", "A nonempty Host credential is required")
    directory, path = _paths(config_root)
    try:
        with _credential_lock(config_root):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory_info, file_info = _validate_paths(directory, path, creating=False)
            if os.name != "nt":
                if directory_info.st_uid != os.getuid() or file_info is not None and file_info.st_uid != os.getuid():
                    raise PmtError("credential_store_insecure", "Host credential store has another owner")
                os.chmod(directory, 0o700)
            try:
                old_data = path.read_bytes()
            except FileNotFoundError:
                old_data = None
            old_value = None
            if old_data is not None:
                decoded = _protect_windows(old_data, decrypt=True) if os.name == "nt" else old_data
                old_value = decoded.decode("utf-8")
            if old_value == credential:
                return old_data, old_data, False
            data = credential.encode("utf-8")
            if os.name == "nt":
                data = _protect_windows(data)
            _atomic_write(directory, path, data)
            return old_data, path.read_bytes(), True
    except PmtError:
        raise
    except (OSError, UnicodeError, ValueError, ctypes.ArgumentError, AttributeError) as exc:
        raise PmtError("credential_store_unreadable", "Host credential could not be staged") from exc


def restore_credential(config_root, snapshot, *, expected_current=None, _lock_held=False):
    directory, path = _paths(config_root)
    try:
        with (nullcontext() if _lock_held else _credential_lock(config_root)):
            _validate_paths(directory, path, creating=True)
            try:
                current = path.read_bytes()
            except FileNotFoundError:
                current = None
            if expected_current is not None and current != expected_current:
                return False
            if snapshot is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                return True
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            info, file_info = _validate_paths(directory, path)
            if os.name != "nt":
                if info.st_uid != os.getuid() or file_info is not None and file_info.st_uid != os.getuid():
                    raise PmtError("credential_store_insecure", "Host credential store has another owner")
                os.chmod(directory, 0o700)
            _atomic_write(directory, path, snapshot)
            return True
    except PmtError:
        raise
    except OSError as exc:
        raise PmtError("credential_store_unreadable", "Host credential store could not be restored") from exc


def load_credential(config_root, environ=None):
    """Load into the process environment, honoring an explicitly supplied value."""
    environ = os.environ if environ is None else environ
    explicit = environ.get(ENV_NAME)
    if isinstance(explicit, str) and explicit:
        return explicit
    _directory, path = _paths(config_root)
    try:
        _validate_paths(_directory, path)
        if os.name != "nt":
            _check_unix_owner(_directory, path)
        data = path.read_bytes()
        if os.name == "nt":
            data = _protect_windows(data, decrypt=True)
        value = data.decode("utf-8")
        if not value:
            raise ValueError("empty credential")
    except PmtError:
        raise
    except FileNotFoundError as exc:
        raise PmtError("credential_unavailable", "Host credential is unavailable") from exc
    except (OSError, UnicodeError, ValueError, ctypes.ArgumentError, AttributeError) as exc:
        raise PmtError("credential_store_unreadable", "Host credential store cannot be read") from exc
    environ[ENV_NAME] = value
    return value
