"""Private filesystem roots for persisted workflow evidence and source trees."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import stat
import subprocess
from typing import Iterable


class PrivatePathError(RuntimeError):
    """A workflow root cannot be proven private to trusted local principals."""


_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)


def _has_link_or_reparse_ancestor(path: Path) -> bool:
    current = path
    while True:
        if current.exists() and _is_link_or_reparse(current):
            return True
        if current.parent == current:
            return False
        current = current.parent


def _validate_shape(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    if len(paths) != 3:
        raise PrivatePathError("exactly three private workflow roots are required")
    absolute = tuple(path.absolute() for path in paths)
    if any(not path.is_absolute() for path in absolute):
        raise PrivatePathError("private workflow root is unsafe")
    if any(_has_link_or_reparse_ancestor(path) for path in absolute):
        raise PrivatePathError("private workflow root is unsafe")
    canonical = tuple(path.resolve(strict=False) for path in absolute)
    for index, left in enumerate(canonical):
        for right in canonical[index + 1 :]:
            try:
                nested = left.is_relative_to(right) or right.is_relative_to(left)
            except ValueError:
                nested = False
            if nested:
                raise PrivatePathError("private workflow roots must not overlap")
    return absolute


def _verify_posix(path: Path) -> None:
    if _is_link_or_reparse(path) or not path.is_dir():
        raise PrivatePathError("private workflow root is unsafe")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PrivatePathError("private workflow root is unsafe")


def _prepare_posix(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=False)
    try:
        os.chmod(path, 0o700)
        _verify_posix(path)
    except (OSError, PrivatePathError):
        try:
            path.rmdir()
        except OSError:
            pass
        raise


def _sid_string(pointer: ctypes.c_void_p) -> str:
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSidToStringSidW.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    converted = ctypes.c_void_p()
    if not advapi.ConvertSidToStringSidW(pointer, ctypes.byref(converted)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.cast(converted, ctypes.c_wchar_p).value
    finally:
        kernel.LocalFree(converted)


def _current_user_sid() -> str:
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    advapi.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
    advapi.OpenProcessToken.restype = ctypes.c_int
    advapi.GetTokenInformation.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    advapi.GetTokenInformation.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    token = ctypes.c_void_p()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        required = ctypes.c_ulong()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi.GetTokenInformation(
            token, 1, buffer, required.value, ctypes.byref(required)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
        if sid is None:
            raise OSError("current user SID is unavailable")
        return _sid_string(ctypes.c_void_p(sid))
    finally:
        kernel.CloseHandle(token)


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", ctypes.c_ulong),
        ("AclBytesInUse", ctypes.c_ulong),
        ("AclBytesFree", ctypes.c_ulong),
    ]


def _windows_descriptor(
    path: Path,
) -> tuple[str, tuple[tuple[str, int, int, int], ...], bool]:
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    advapi.GetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_int, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.GetNamedSecurityInfoW.restype = ctypes.c_ulong
    advapi.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int]
    advapi.GetAclInformation.restype = ctypes.c_int
    advapi.GetAce.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = ctypes.c_int
    advapi.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(ctypes.c_ulong)
    ]
    advapi.GetSecurityDescriptorControl.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    result = advapi.GetNamedSecurityInfoW(
        str(path), 1, 0x00000001 | 0x00000004,
        ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
    )
    if result:
        raise ctypes.WinError(result)
    try:
        if owner.value is None or dacl.value is None:
            raise OSError("private directory security descriptor is incomplete")
        control = ctypes.c_ushort()
        revision = ctypes.c_ulong()
        if not advapi.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        dacl_protected = bool(control.value & 0x1000)
        owner_sid = _sid_string(owner)
        information = _AclSizeInformation()
        if not advapi.GetAclInformation(
            dacl, ctypes.byref(information), ctypes.sizeof(information), 2
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        entries: list[tuple[str, int, int, int]] = []
        for index in range(information.AceCount):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            header = ctypes.cast(ace, ctypes.POINTER(ctypes.c_ubyte * 8)).contents
            sid_address = ace.value + 8
            entries.append(
                (
                    _sid_string(ctypes.c_void_p(sid_address)),
                    int.from_bytes(bytes(header[4:8]), "little"),
                    int(header[1]),
                    int(header[0]),
                )
            )
        return owner_sid, tuple(entries), dacl_protected
    finally:
        kernel.LocalFree(descriptor)


def _verify_windows(path: Path, user_sid: str) -> None:
    owner, entries, dacl_protected = _windows_descriptor(path)
    trusted = {user_sid, _SYSTEM_SID, _ADMINISTRATORS_SID}
    principals = {entry[0] for entry in entries}
    full_control = 0x001F01FF
    if (
        owner != user_sid
        or not dacl_protected
        or user_sid not in principals
        or not principals <= trusted
        or any(
            ace_type != 0
            or flags & 0x10
            or flags & 0x03 != 0x03
            or mask & full_control != full_control
            for _, mask, flags, ace_type in entries
        )
    ):
        raise PrivatePathError("private workflow root is unsafe")


def _prepare_windows(path: Path, user_sid: str) -> None:
    existed = path.exists()
    if existed and (_is_link_or_reparse(path) or not path.is_dir()):
        raise PrivatePathError("private workflow root is unsafe")
    if existed:
        try:
            _verify_windows(path, user_sid)
        except OSError:
            raise PrivatePathError("private workflow root is unsafe") from None
        return
    path.mkdir(parents=True, exist_ok=False)
    try:
        if _is_link_or_reparse(path):
            raise PrivatePathError("private workflow root is unsafe")
        completed = subprocess.run(
            [
                "icacls", str(path), "/inheritance:r",
                "/grant:r", f"*{user_sid}:(OI)(CI)F",
                f"*{_SYSTEM_SID}:(OI)(CI)F",
                f"*{_ADMINISTRATORS_SID}:(OI)(CI)F", "/q",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
        )
        if completed.returncode != 0:
            raise PrivatePathError("private workflow root is unsafe")
        _verify_windows(path, user_sid)
    except (OSError, PrivatePathError):
        try:
            path.rmdir()
        except OSError:
            pass
        raise PrivatePathError("private workflow root is unsafe") from None


def prepare_private_roots(paths: Iterable[Path]) -> tuple[Path, ...]:
    """Create/verify three non-overlapping roots with fail-closed local access."""

    roots = _validate_shape(tuple(Path(path) for path in paths))
    for path in roots:
        if path.exists() and (_is_link_or_reparse(path) or not path.is_dir()):
            raise PrivatePathError("private workflow root is unsafe")
    created: list[Path] = []
    if os.name == "nt":
        try:
            user_sid = _current_user_sid()
        except OSError:
            raise PrivatePathError("private workflow root is unsafe") from None
        try:
            for path in roots:
                if path.exists():
                    _verify_windows(path, user_sid)
            for path in roots:
                if not path.exists():
                    _prepare_windows(path, user_sid)
                    created.append(path)
        except (OSError, PrivatePathError):
            for path in reversed(created):
                try:
                    path.rmdir()
                except OSError:
                    pass
            raise PrivatePathError("private workflow root is unsafe") from None
    else:
        try:
            for path in roots:
                if path.exists():
                    _verify_posix(path)
            for path in roots:
                if not path.exists():
                    _prepare_posix(path)
                    created.append(path)
        except (OSError, PrivatePathError):
            for path in reversed(created):
                try:
                    path.rmdir()
                except OSError:
                    pass
            raise PrivatePathError("private workflow root is unsafe") from None
    return tuple(path.resolve(strict=True) for path in roots)


__all__ = ["PrivatePathError", "prepare_private_roots"]
