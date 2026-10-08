"""Claim-key references and platform-protected storage for server administration."""
from __future__ import annotations

import base64
import ctypes
import getpass
import os
import secrets
import stat
from ctypes import wintypes
from pathlib import Path

from ..errors import PmtError


def _error(code, message):
    return PmtError(code, message)


def _local_free(value):
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree(ctypes.cast(value, ctypes.c_void_p))


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data, *, decrypt=False):
    if os.name != "nt":
        raise _error("host_key_unavailable", "DPAPI is available only on Windows")
    source = ctypes.create_string_buffer(data)
    in_blob = _Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    out_blob = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    crypt.restype = wintypes.BOOL
    crypt.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                      ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    flags = 0x1 if decrypt else (0x1 | 0x4)  # UI_FORBIDDEN; LOCAL_MACHINE for protect
    ok = crypt(ctypes.byref(in_blob), None, None, None, None, flags, ctypes.byref(out_blob))
    if not ok:
        raise _error("host_key_unavailable", "Windows could not protect or read the claim key")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _local_free(out_blob.pbData)


def _sid_string(sid):
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    text = ctypes.c_wchar_p()
    if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise _error("host_account_invalid", "Could not resolve the configured service account")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    try:
        return text.value
    finally:
        _local_free(text)


def _account_sid(account=None):
    if os.name != "nt":
        raise _error("host_account_invalid", "Windows account SID resolution is unavailable")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    if account in (None, "current"):
        class TokenUser(ctypes.Structure):
            _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        process = kernel32.GetCurrentProcess()
        token = wintypes.HANDLE()
        kernel32.OpenProcessToken.restype = wintypes.BOOL
        kernel32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi.GetTokenInformation.restype = wintypes.BOOL
        advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        if not kernel32.OpenProcessToken(process, 0x0008, ctypes.byref(token)):
            raise _error("host_account_invalid", "Could not resolve the current service account")
        try:
            size = wintypes.DWORD()
            advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                raise _error("host_account_invalid", "Could not resolve the current service account")
            user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
            return _sid_string(user.sid)
        finally:
            kernel32.CloseHandle(token)
    name = account
    advapi.LookupAccountNameW.restype = wintypes.BOOL
    advapi.LookupAccountNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                                         ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
                                         ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    sid_size, domain_size, use = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
    advapi.LookupAccountNameW(None, name, None, ctypes.byref(sid_size), None, ctypes.byref(domain_size), ctypes.byref(use))
    sid = ctypes.create_string_buffer(sid_size.value)
    domain = ctypes.create_unicode_buffer(domain_size.value or 1)
    if not advapi.LookupAccountNameW(None, name, sid, ctypes.byref(sid_size), domain, ctypes.byref(domain_size), ctypes.byref(use)):
        raise _error("host_account_invalid", "Configured service account does not exist or cannot be resolved")
    return _sid_string(ctypes.cast(sid, ctypes.c_void_p))


def _account_uid(account=None):
    if os.name == "nt": return None
    if account in (None, "current"): return os.getuid()
    import pwd
    try:
        return pwd.getpwnam(account).pw_uid
    except KeyError as exc:
        raise _error("host_account_invalid", "Configured service account does not exist") from exc


def validate_service_account(account):
    if os.name == "nt": return _account_sid(account)
    return _account_uid(account)


def _path_reparse(path):
    path = Path(path)
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if candidate.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            return True
    return False


def _safe_parent(path, account):
    parent = Path(path).parent
    if parent.is_symlink() or getattr(parent.lstat(), "st_file_attributes", 0) & 0x400:
        raise _error("host_key_insecure", "Claim-key directory cannot be a reparse point")
    if os.name == "nt":
        _check_windows_acl(parent, account)
    else:
        info = parent.stat()
        if info.st_uid != _account_uid(account) or stat.S_IMODE(info.st_mode) & 0o077:
            raise _error("host_key_insecure", "Claim-key directory must be owned by the service account with mode 0700")


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]


def _read_key_file(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            return stream.read(1024 * 1024 + 1)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                    ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD]
    handle = kernel32.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise _error("host_key_unavailable", "Claim-key file is unavailable")
    fd = None
    try:
        info = _FileAttributeTagInfo()
        if not kernel32.GetFileInformationByHandleEx(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise _error("host_key_unavailable", "Could not inspect claim-key file")
        if info.attributes & (0x400 | 0x10):
            raise _error("host_key_insecure", "Claim-key path cannot be a reparse point or directory")
        import msvcrt
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        handle = None
        with os.fdopen(fd, "rb") as stream:
            return stream.read(1024 * 1024 + 1)
    finally:
        if handle is not None:
            kernel32.CloseHandle(handle)


def _set_windows_acl(path, account=None):
    """Apply a protected DACL for Administrators, SYSTEM, and run account."""
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    sddl = f"D:P(A;;FA;;;BA)(A;;FA;;;SY)(A;;FA;;;{_account_sid(account)})"
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                                                             ctypes.POINTER(ctypes.c_void_p),
                                                                             ctypes.POINTER(wintypes.DWORD)]
    advapi.SetFileSecurityW.restype = wintypes.BOOL
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise _error("host_key_insecure", "Could not create restrictive claim-key ACL")
    try:
        if not advapi.SetFileSecurityW(str(path), 0x00000004 | 0x80000000, descriptor):
            raise _error("host_key_insecure", "Could not apply restrictive claim-key ACL")
    finally:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree(descriptor)


def _check_windows_acl(path, account=None):
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                                            wintypes.DWORD, ctypes.POINTER(ctypes.c_wchar_p),
                                                                            ctypes.POINTER(wintypes.DWORD)]
    status = advapi.GetNamedSecurityInfoW(str(path), 1, 0x00000004, None, None, None, None, ctypes.byref(descriptor))
    if status != 0:
        raise _error("host_key_insecure", "Could not inspect claim-key ACL")
    sddl = ctypes.c_wchar_p()
    try:
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 0x00000004, ctypes.byref(sddl), None):
            raise _error("host_key_insecure", "Could not inspect claim-key ACL")
        dacl = sddl.value.upper()
        account_sid = _account_sid(account).upper()
        allowed = {"BA", "SY", account_sid}
        import re
        aces = re.findall(r"\(([^)]*)\)", dacl)
        principals = set()
        for ace in aces:
            fields = ace.split(";")
            if len(fields) != 6 or fields[0] != "A":
                raise _error("host_key_insecure", "Claim-key ACL contains an unexpected access rule")
            principals.add(fields[5])
        if not principals or principals - allowed or not allowed.issubset(principals):
            raise _error("host_key_insecure", "Claim-key ACL allows an unexpected account")
    finally:
        _local_free(descriptor)
        if sddl:
            _local_free(sddl)


def _check_file_acl(path, account=None):
    path = Path(path)
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise _error("host_key_insecure", "Claim-key path cannot use symlinks")
        info = path.lstat()
        parent = path.parent.lstat()
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise _error("host_key_insecure", "Claim-key path must be a regular non-reparse file")
        if not stat.S_ISDIR(parent.st_mode) or getattr(parent, "st_file_attributes", 0) & 0x400:
            raise _error("host_key_insecure", "Claim-key directory must be a regular non-reparse directory")
    except OSError as exc:
        raise _error("host_key_unavailable", "Claim-key path is unavailable") from exc
    expected_uid = _account_uid(account)
    if info.st_uid != expected_uid or parent.st_uid != expected_uid:
        raise _error("host_key_insecure", "Claim-key file and directory must belong to the service account")
    if stat.S_IMODE(parent.st_mode) & 0o077:
        raise _error("host_key_insecure", "Claim-key directory is accessible to group or other users")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise _error("host_key_insecure", "Claim-key file is accessible to group or other users")


def store_key(value, path, kind, *, account=None):
    path = Path(path)
    if _path_reparse(path) or _path_reparse(path.parent):
        raise _error("host_key_insecure", "Claim-key path cannot contain symlinks or reparse points")
    if kind == "dpapi" and os.name != "nt":
        raise _error("host_key_unavailable", "DPAPI is available only on Windows")
    parent_created = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    expected_uid = _account_uid(account) if os.name != "nt" else None
    if parent_created:
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
            if expected_uid != os.getuid():
                if os.geteuid() != 0:
                    raise _error("host_account_invalid", "Writing a claim key for another account requires that account or administrator privileges")
                os.chown(path.parent, expected_uid, -1)
        else:
            _set_windows_acl(path.parent, account)
    _safe_parent(path, account)
    if path.exists():
        raise _error("host_key_exists", "Claim-key destination already exists; preserved")
    if kind == "file":
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(value); stream.flush(); os.fsync(stream.fileno())
        except Exception:
            try: path.unlink()
            except OSError: pass
            raise
        try:
            if os.name == "nt": _set_windows_acl(path, account)
            else:
                os.chmod(path, 0o600)
                if expected_uid != os.getuid(): os.chown(path, expected_uid, -1)
        except Exception:
            try: path.unlink()
            except OSError: pass
            raise
    elif kind == "dpapi":
        protected = _dpapi(value)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(protected); stream.flush(); os.fsync(stream.fileno())
        except Exception:
            try: path.unlink()
            except OSError: pass
            raise
        try: _set_windows_acl(path, account)
        except Exception:
            try: path.unlink()
            except OSError: pass
            raise
    else:
        raise _error("config_invalid", "Unsupported claim-key storage kind")
    return {"kind": kind, "path": str(path)}


def read_key(source, *, account=None):
    kind = source.get("kind")
    if kind == "env":
        encoded = os.environ.get(source.get("name", ""))
        if not encoded:
            raise _error("host_key_unavailable", "Claim-key environment reference is unavailable")
        try: raw = encoded.encode("ascii", "strict")
        except UnicodeError as exc: raise _error("host_key_unavailable", "Claim-key environment reference is invalid") from exc
    elif kind in {"file", "dpapi"}:
        path = Path(source["path"])
        try:
            _safe_parent(path, account)
            if os.name == "nt":
                if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                    raise _error("host_key_insecure", "Claim-key path cannot be a reparse point")
                _check_windows_acl(path, account)
                _check_windows_acl(path.parent, account)
            else:
                _check_file_acl(path, account)
            if _path_reparse(path) or _path_reparse(path.parent):
                raise _error("host_key_insecure", "Claim-key path cannot contain symlinks or reparse points")
            raw = _read_key_file(path)
            if len(raw) > 1024 * 1024:
                raise _error("host_key_insecure", "Claim-key file exceeds the size limit")
        except PmtError:
            raise
        except OSError as exc:
            raise _error("host_key_unavailable", "Claim-key file is unavailable") from exc
        if kind == "dpapi":
            raw = _dpapi(raw, decrypt=True)
    else:
        raise _error("host_key_unavailable", "Claim-key source kind is invalid")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (ValueError, base64.binascii.Error, UnicodeError) as exc:
        raise _error("host_key_unavailable", "Claim key is not valid base64") from exc
    if len(decoded) < 32:
        raise _error("host_key_unavailable", "Claim key is shorter than 32 bytes")
    return decoded


def create_key_reference(root, key_id, *, kind=None, account=None):
    import re
    if not isinstance(key_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key_id):
        raise _error("config_invalid", "Claim key id is invalid")
    kind = kind or ("dpapi" if os.name == "nt" else "file")
    suffix = ".dpapi" if kind == "dpapi" else ".key"
    path = Path(root) / "secrets" / f"claim-{key_id}{suffix}"
    value = base64.b64encode(secrets.token_bytes(48))
    return store_key(value, path, kind, account=account)


def check_source(source, *, account=None):
    read_key(source, account=account)
    return {"ok": True, "kind": source["kind"]}
