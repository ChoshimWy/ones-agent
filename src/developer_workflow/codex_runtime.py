"""Discovery and locking for the signed native Codex npm payload on Windows."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


OPENAI_AUTHENTICODE_PUBLISHER = "OpenAI OpCo, LLC"
NATIVE_CODEX_RELATIVE_PATH = Path(
    "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/"
    "vendor/x86_64-pc-windows-msvc/bin/codex.exe"
)


@dataclass(frozen=True, slots=True)
class NativeCodexIdentity:
    volume_serial: int
    file_index: int
    size: int
    mtime_ns: int


class _RuntimeAdapter(Protocol):
    def open_locked(self, path: Path) -> int: ...

    def is_disk_regular_non_reparse(self, descriptor: int) -> bool: ...

    def identity(self, descriptor: int) -> NativeCodexIdentity: ...

    def final_path(self, descriptor: int) -> Path: ...

    def verify_publisher(self, descriptor: int, path: Path) -> str: ...

    def read(self, descriptor: int, size: int) -> bytes: ...

    def rewind(self, descriptor: int) -> None: ...

    def close(self, descriptor: int) -> None: ...

    def same_file(self, left: Path, right: Path) -> bool: ...


@dataclass(slots=True, repr=False)
class LockedNativeCodex:
    descriptor: int = field(repr=False)
    identity: NativeCodexIdentity
    size: int
    publisher: str
    _adapter: _RuntimeAdapter = field(repr=False)
    _closed: bool = field(default=False, repr=False)

    def read_chunk(self, size: int) -> bytes:
        if self._closed:
            raise ValueError("native Codex payload is closed")
        if type(size) is not int or size <= 0:
            raise ValueError("read size must be a positive integer")
        return self._adapter.read(self.descriptor, size)

    def rewind(self) -> None:
        if self._closed:
            raise ValueError("native Codex payload is closed")
        self._adapter.rewind(self.descriptor)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._adapter.close(self.descriptor)


def _final_path_has_fixed_layout(
    final_path: Path,
    locator_root: Path,
    adapter: _RuntimeAdapter,
) -> bool:
    relative_parts = NATIVE_CODEX_RELATIVE_PATH.parts
    if len(final_path.parts) <= len(relative_parts):
        return False
    if tuple(part.casefold() for part in final_path.parts[-len(relative_parts):]) != tuple(
        part.casefold() for part in relative_parts
    ):
        return False
    final_root = final_path.parents[len(relative_parts) - 1]
    return adapter.same_file(final_root, locator_root)


def _current_repository_roots() -> tuple[Path, ...]:
    worktree = Path(__file__).resolve(strict=True).parents[2]
    roots = [worktree]
    if worktree.parent.name.casefold() == ".worktrees":
        roots.append(worktree.parent.parent.resolve(strict=True))
    return tuple(roots)


def _inside_repository_by_identity(
    path: Path,
    repository_roots: tuple[Path, ...],
    adapter: _RuntimeAdapter,
) -> bool:
    current = path
    while True:
        for root in repository_roots:
            if adapter.same_file(current, root):
                return True
        if current.parent == current:
            return False
        current = current.parent


def discover_locked_native_codex(
    *,
    which: Callable[[str], str | None] = shutil.which,
    repository_roots: tuple[Path, ...] | None = None,
    _adapter: _RuntimeAdapter | None = None,
) -> LockedNativeCodex:
    """Return a verified native payload while retaining its source handle lock."""

    raw_locator = which("codex.cmd")
    if (
        type(raw_locator) is not str
        or not raw_locator
        or "\x00" in raw_locator
    ):
        raise OSError("native Codex payload is unavailable")
    locator = Path(raw_locator)
    if not locator.is_absolute() or locator.name.casefold() != "codex.cmd":
        raise OSError("native Codex payload is unavailable")

    expected = locator.parent / NATIVE_CODEX_RELATIVE_PATH
    adapter = _adapter if _adapter is not None else _WindowsRuntimeAdapter()
    roots = _current_repository_roots() if repository_roots is None else repository_roots
    descriptor = adapter.open_locked(expected)
    retained = False
    try:
        if not adapter.is_disk_regular_non_reparse(descriptor):
            raise OSError("native Codex payload is unavailable")
        initial_identity = adapter.identity(descriptor)
        if initial_identity.size < 0:
            raise OSError("native Codex payload is unavailable")
        final_path = adapter.final_path(descriptor)
        if (
            not _final_path_has_fixed_layout(final_path, locator.parent, adapter)
            or not adapter.same_file(final_path, expected)
            or _inside_repository_by_identity(final_path, roots, adapter)
        ):
            raise OSError("native Codex payload is unavailable")
        publisher = adapter.verify_publisher(descriptor, final_path)
        if publisher != OPENAI_AUTHENTICODE_PUBLISHER:
            raise OSError("native Codex payload is unavailable")
        final_identity = adapter.identity(descriptor)
        if final_identity != initial_identity:
            raise OSError("native Codex payload is unavailable")
        locked = LockedNativeCodex(
            descriptor=descriptor,
            identity=initial_identity,
            size=initial_identity.size,
            publisher=publisher,
            _adapter=adapter,
        )
        retained = True
        return locked
    finally:
        if not retained:
            adapter.close(descriptor)


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", _FILETIME),
            ("ftLastAccessTime", _FILETIME),
            ("ftLastWriteTime", _FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class _WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pcwszFilePath", wintypes.LPCWSTR),
            ("hFile", wintypes.HANDLE),
            ("pgKnownSubject", ctypes.c_void_p),
        ]

    class _WINTRUST_DATA(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pPolicyCallbackData", ctypes.c_void_p),
            ("pSIPClientData", ctypes.c_void_p),
            ("dwUIChoice", wintypes.DWORD),
            ("fdwRevocationChecks", wintypes.DWORD),
            ("dwUnionChoice", wintypes.DWORD),
            ("pFile", ctypes.POINTER(_WINTRUST_FILE_INFO)),
            ("dwStateAction", wintypes.DWORD),
            ("hWVTStateData", wintypes.HANDLE),
            ("pwszURLReference", wintypes.LPCWSTR),
            ("dwProvFlags", wintypes.DWORD),
            ("dwUIContext", wintypes.DWORD),
            ("pSignatureSettings", ctypes.c_void_p),
        ]

    class _CRYPT_PROVIDER_CERT(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD),
            ("pCert", ctypes.c_void_p),
            ("fCommercial", wintypes.BOOL),
            ("fTrustedRoot", wintypes.BOOL),
            ("fSelfSigned", wintypes.BOOL),
            ("fTestCert", wintypes.BOOL),
            ("dwRevokedReason", wintypes.DWORD),
            ("dwConfidence", wintypes.DWORD),
            ("dwError", wintypes.DWORD),
            ("pTrustListContext", ctypes.c_void_p),
            ("fTrustListSignerCert", wintypes.BOOL),
            ("pCtlContext", ctypes.c_void_p),
            ("dwCtlError", wintypes.DWORD),
            ("fIsCyclic", wintypes.BOOL),
            ("pChainElement", ctypes.c_void_p),
        ]

    class _CRYPT_PROVIDER_SGNR(ctypes.Structure):
        pass

    _CRYPT_PROVIDER_SGNR._fields_ = [
        ("cbStruct", wintypes.DWORD),
        ("sftVerifyAsOf", _FILETIME),
        ("csCertChain", wintypes.DWORD),
        ("pasCertChain", ctypes.POINTER(_CRYPT_PROVIDER_CERT)),
        ("dwSignerType", wintypes.DWORD),
        ("psSigner", ctypes.c_void_p),
        ("dwError", wintypes.DWORD),
        ("csCounterSigners", wintypes.DWORD),
        ("pasCounterSigners", ctypes.POINTER(_CRYPT_PROVIDER_SGNR)),
        ("pChainContext", ctypes.c_void_p),
    ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _wintrust = ctypes.WinDLL("wintrust", use_last_error=True)
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)

    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.GetFileType.argtypes = [wintypes.HANDLE]
    _kernel32.GetFileType.restype = wintypes.DWORD
    _kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)
    ]
    _kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD
    ]
    _kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    _kernel32.ReadFile.restype = wintypes.BOOL
    _kernel32.SetFilePointerEx.argtypes = [
        wintypes.HANDLE, ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD,
    ]
    _kernel32.SetFilePointerEx.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _wintrust.WinVerifyTrust.argtypes = [
        wintypes.HWND, ctypes.POINTER(_GUID), ctypes.c_void_p
    ]
    _wintrust.WinVerifyTrust.restype = ctypes.c_long
    _wintrust.WTHelperProvDataFromStateData.argtypes = [wintypes.HANDLE]
    _wintrust.WTHelperProvDataFromStateData.restype = ctypes.c_void_p
    _wintrust.WTHelperGetProvSignerFromChain.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ]
    _wintrust.WTHelperGetProvSignerFromChain.restype = ctypes.POINTER(
        _CRYPT_PROVIDER_SGNR
    )

    _crypt32.CertGetNameStringW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD,
    ]
    _crypt32.CertGetNameStringW.restype = wintypes.DWORD


def _raise_last_windows_error(operation: str) -> None:
    if os.name != "nt":
        raise OSError(f"{operation} is only available on Windows")
    error = ctypes.get_last_error()
    raise OSError(error, operation)


class _WindowsRuntimeAdapter:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("native Codex payload is only available on Windows")

    def open_locked(self, path: Path) -> int:
        handle = _kernel32.CreateFileW(
            str(path), 0x80000000, 0x00000001, None, 3, 0x00200000, None
        )
        if handle == _INVALID_HANDLE_VALUE:
            _raise_last_windows_error("could not lock native Codex payload")
        return int(handle)

    def _information(self, descriptor: int) -> _BY_HANDLE_FILE_INFORMATION:
        information = _BY_HANDLE_FILE_INFORMATION()
        if not _kernel32.GetFileInformationByHandle(
            wintypes.HANDLE(descriptor), ctypes.byref(information)
        ):
            _raise_last_windows_error("could not inspect native Codex payload")
        return information

    def is_disk_regular_non_reparse(self, descriptor: int) -> bool:
        if _kernel32.GetFileType(wintypes.HANDLE(descriptor)) != 0x0001:
            return False
        attributes = self._information(descriptor).dwFileAttributes
        return not attributes & (_FILE_ATTRIBUTE_DIRECTORY | _FILE_ATTRIBUTE_REPARSE_POINT)

    def identity(self, descriptor: int) -> NativeCodexIdentity:
        information = self._information(descriptor)
        file_index = (information.nFileIndexHigh << 32) | information.nFileIndexLow
        size = (information.nFileSizeHigh << 32) | information.nFileSizeLow
        mtime_100ns = (
            information.ftLastWriteTime.dwHighDateTime << 32
        ) | information.ftLastWriteTime.dwLowDateTime
        return NativeCodexIdentity(
            volume_serial=information.dwVolumeSerialNumber,
            file_index=file_index,
            size=size,
            mtime_ns=mtime_100ns * 100,
        )

    def final_path(self, descriptor: int) -> Path:
        handle = wintypes.HANDLE(descriptor)
        required = _kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not required:
            _raise_last_windows_error("could not resolve native Codex payload")
        buffer = ctypes.create_unicode_buffer(required + 1)
        written = _kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not written or written >= len(buffer):
            _raise_last_windows_error("could not resolve native Codex payload")
        return Path(buffer.value)

    def verify_publisher(self, descriptor: int, path: Path) -> str:
        return _verify_wintrust_publisher(descriptor, path)

    def read(self, descriptor: int, size: int) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        read = wintypes.DWORD()
        if not _kernel32.ReadFile(
            wintypes.HANDLE(descriptor), buffer, size, ctypes.byref(read), None
        ):
            _raise_last_windows_error("could not read native Codex payload")
        return buffer.raw[: read.value]

    def rewind(self, descriptor: int) -> None:
        if not _kernel32.SetFilePointerEx(
            wintypes.HANDLE(descriptor), 0, None, 0
        ):
            _raise_last_windows_error("could not rewind native Codex payload")

    def close(self, descriptor: int) -> None:
        if not _kernel32.CloseHandle(wintypes.HANDLE(descriptor)):
            _raise_last_windows_error("could not close native Codex payload")

    def same_file(self, left: Path, right: Path) -> bool:
        before_left = left.stat(follow_symlinks=False)
        before_right = right.stat(follow_symlinks=False)
        same = os.path.samefile(left, right)
        after_left = left.stat(follow_symlinks=False)
        after_right = right.stat(follow_symlinks=False)
        for before, after in ((before_left, after_left), (before_right, after_right)):
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise OSError("unstable native Codex path identity")
        return same


def _certificate_publisher(certificate: object) -> str:
    oid = ctypes.c_char_p(b"2.5.4.10")
    required = _crypt32.CertGetNameStringW(certificate, 3, 0, oid, None, 0)
    if required <= 1:
        _raise_last_windows_error("could not read native Codex publisher")
    publisher = ctypes.create_unicode_buffer(required)
    if _crypt32.CertGetNameStringW(
        certificate, 3, 0, oid, publisher, required
    ) != required:
        _raise_last_windows_error("could not read native Codex publisher")
    return publisher.value


def _publisher_from_trust_state(state: object) -> str:
    provider = _wintrust.WTHelperProvDataFromStateData(state)
    if not provider:
        _raise_last_windows_error("could not inspect native Codex trust state")
    signer = _wintrust.WTHelperGetProvSignerFromChain(provider, 0, False, 0)
    if (
        not signer
        or signer.contents.csCertChain < 1
        or not signer.contents.pasCertChain
        or not signer.contents.pasCertChain[0].pCert
    ):
        _raise_last_windows_error("could not inspect native Codex signer")
    return _certificate_publisher(signer.contents.pasCertChain[0].pCert)


def _verify_wintrust_publisher(descriptor: int, path: Path) -> str:
    action = _GUID(
        0x00AAC56B, 0xCD44, 0x11D0,
        (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE),
    )
    file_info = _WINTRUST_FILE_INFO(
        ctypes.sizeof(_WINTRUST_FILE_INFO), str(path), wintypes.HANDLE(descriptor), None
    )
    trust_data = _WINTRUST_DATA()
    trust_data.cbStruct = ctypes.sizeof(_WINTRUST_DATA)
    trust_data.dwUIChoice = 2
    trust_data.fdwRevocationChecks = 0
    trust_data.dwUnionChoice = 1
    trust_data.pFile = ctypes.pointer(file_info)
    trust_data.dwStateAction = 1
    trust_data.dwProvFlags = 0x00001000
    try:
        result = _wintrust.WinVerifyTrust(
            wintypes.HWND(_INVALID_HANDLE_VALUE), ctypes.byref(action), ctypes.byref(trust_data)
        )
        if result != 0:
            raise OSError(result & 0xFFFFFFFF, "native Codex signature is not trusted")
        return _publisher_from_trust_state(trust_data.hWVTStateData)
    finally:
        trust_data.dwStateAction = 2
        _wintrust.WinVerifyTrust(
            wintypes.HWND(_INVALID_HANDLE_VALUE), ctypes.byref(action), ctypes.byref(trust_data)
        )

__all__ = [
    "OPENAI_AUTHENTICODE_PUBLISHER",
    "NATIVE_CODEX_RELATIVE_PATH",
    "LockedNativeCodex",
    "NativeCodexIdentity",
    "discover_locked_native_codex",
]
