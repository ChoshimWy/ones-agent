"""Discovery and locking for the signed native Codex npm payload on Windows."""

from __future__ import annotations

import os
import shutil
import stat
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

    def repository_marker(
        self, root: Path,
    ) -> tuple[str, tuple[int, int, int, int]] | None: ...

    def resolve_repository_path(self, path: Path) -> Path: ...

    def repository_path_identity(self, path: Path) -> tuple[int, int, int, int]: ...


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


def _nearest_current_repository_root(adapter: _RuntimeAdapter) -> Path:
    lexical_cwd = Path.cwd()
    physical_cwd = adapter.resolve_repository_path(lexical_cwd)
    if not adapter.same_file(lexical_cwd, physical_cwd):
        raise OSError("unstable current repository identity")

    checked: list[
        tuple[
            Path,
            tuple[int, int, int, int],
            tuple[str, tuple[int, int, int, int]] | None,
        ]
    ] = []
    current = physical_cwd
    repository_root: Path | None = None
    while True:
        path_identity = adapter.repository_path_identity(current)
        marker = adapter.repository_marker(current)
        checked.append((current, path_identity, marker))
        if marker is not None:
            marker_kind, _ = marker
            if marker_kind not in {"directory", "file"}:
                raise OSError("invalid repository marker")
            repository_root = current
            break
        if current.parent == current:
            break
        current = current.parent

    final_cwd = adapter.resolve_repository_path(Path.cwd())
    if not adapter.same_file(physical_cwd, final_cwd):
        raise OSError("unstable current repository identity")
    for path, initial_identity, initial_marker in reversed(checked):
        if (
            adapter.repository_path_identity(path) != initial_identity
            or adapter.repository_marker(path) != initial_marker
        ):
            raise OSError("unstable repository parent identity")
    return repository_root if repository_root is not None else physical_cwd


def _current_repository_roots(adapter: _RuntimeAdapter) -> tuple[Path, ...]:
    worktree = Path(__file__).resolve(strict=True).parents[2]
    candidates = [worktree]
    if worktree.parent.name.casefold() == ".worktrees":
        candidates.append(worktree.parent.parent.resolve(strict=True))
    candidates.append(_nearest_current_repository_root(adapter))
    roots: list[Path] = []
    for candidate in candidates:
        if not adapter.same_file(candidate, candidate):
            raise OSError("unstable repository root identity")
        if any(adapter.same_file(candidate, existing) for existing in roots):
            continue
        roots.append(candidate)
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


def _is_priority_failure(error: BaseException) -> bool:
    return isinstance(error, MemoryError) or not isinstance(error, Exception)


def _is_expected_runtime_failure(error: BaseException) -> bool:
    return isinstance(error, OSError)


def _raise_native_codex_unavailable() -> None:
    raise OSError("native Codex payload is unavailable") from None


def _discover_locked_native_codex_raw(
    *,
    which: Callable[[str], str | None],
    repository_roots: tuple[Path, ...] | None,
    adapter_override: _RuntimeAdapter | None,
) -> LockedNativeCodex:
    raw_locator = which("codex.cmd")
    if (
        type(raw_locator) is not str
        or not raw_locator
        or "\x00" in raw_locator
    ):
        _raise_native_codex_unavailable()
    locator = Path(raw_locator)
    if not locator.is_absolute() or locator.name.casefold() != "codex.cmd":
        _raise_native_codex_unavailable()

    expected = locator.parent / NATIVE_CODEX_RELATIVE_PATH
    adapter = adapter_override if adapter_override is not None else _WindowsRuntimeAdapter()
    descriptor = adapter.open_locked(expected)
    primary_error: BaseException | None = None
    primary_traceback = None
    locked: LockedNativeCodex | None = None
    try:
        roots = (
            _current_repository_roots(adapter)
            if repository_roots is None
            else repository_roots
        )
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
    except BaseException as error:
        primary_error = error
        primary_traceback = error.__traceback__

    if primary_error is None:
        assert locked is not None
        return locked

    cleanup_error: BaseException | None = None
    cleanup_traceback = None
    try:
        adapter.close(descriptor)
    except BaseException as error:
        cleanup_error = error
        cleanup_traceback = error.__traceback__

    if _is_priority_failure(primary_error):
        raise primary_error.with_traceback(primary_traceback)
    if cleanup_error is not None and _is_priority_failure(cleanup_error):
        raise cleanup_error.with_traceback(cleanup_traceback)
    if not _is_expected_runtime_failure(primary_error):
        raise primary_error.with_traceback(primary_traceback)
    if cleanup_error is not None:
        if not _is_expected_runtime_failure(cleanup_error):
            raise cleanup_error.with_traceback(cleanup_traceback)
    raise primary_error.with_traceback(primary_traceback)


def discover_locked_native_codex(
    *,
    which: Callable[[str], str | None] = shutil.which,
    repository_roots: tuple[Path, ...] | None = None,
    _adapter: _RuntimeAdapter | None = None,
) -> LockedNativeCodex:
    """Return a verified native payload while retaining its source handle lock."""

    failed = False
    try:
        return _discover_locked_native_codex_raw(
            which=which,
            repository_roots=repository_roots,
            adapter_override=_adapter,
        )
    except BaseException as error:
        if _is_priority_failure(error) or not _is_expected_runtime_failure(error):
            raise
        failed = True
    if failed:
        del which, repository_roots, _adapter
        _raise_native_codex_unavailable()
    raise AssertionError("unreachable")


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


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


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

    def repository_marker(
        self, root: Path,
    ) -> tuple[str, tuple[int, int, int, int]] | None:
        marker = root / ".git"
        before_root = root.stat(follow_symlinks=False)
        try:
            before_marker = marker.stat(follow_symlinks=False)
        except FileNotFoundError:
            after_root = root.stat(follow_symlinks=False)
            try:
                marker.stat(follow_symlinks=False)
            except FileNotFoundError:
                if _stat_identity(before_root) != _stat_identity(after_root):
                    raise OSError("unstable repository marker identity")
                return None
            raise OSError("unstable repository marker identity") from None

        attributes = getattr(before_marker, "st_file_attributes", 0)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError("invalid repository marker")
        if stat.S_ISDIR(before_marker.st_mode):
            marker_kind = "directory"
        elif stat.S_ISREG(before_marker.st_mode):
            marker_kind = "file"
        else:
            raise OSError("invalid repository marker")

        after_marker = marker.stat(follow_symlinks=False)
        after_root = root.stat(follow_symlinks=False)
        if (
            _stat_identity(before_root) != _stat_identity(after_root)
            or _stat_identity(before_marker) != _stat_identity(after_marker)
        ):
            raise OSError("unstable repository marker identity")
        return marker_kind, _stat_identity(after_marker)

    def resolve_repository_path(self, path: Path) -> Path:
        before = path.stat(follow_symlinks=False)
        resolved = path.resolve(strict=True)
        if not self.same_file(path, resolved):
            raise OSError("unstable current repository identity")
        after = path.stat(follow_symlinks=False)
        if _stat_identity(before) != _stat_identity(after):
            raise OSError("unstable current repository identity")
        return resolved

    def repository_path_identity(self, path: Path) -> tuple[int, int, int, int]:
        value = path.stat(follow_symlinks=False)
        attributes = getattr(value, "st_file_attributes", 0)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT or not stat.S_ISDIR(value.st_mode):
            raise OSError("invalid repository parent")
        return _stat_identity(value)


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


def _raise_signature_not_trusted() -> None:
    raise OSError("native Codex signature is not trusted") from None


def _verify_wintrust_publisher_raw(descriptor: int, path: Path) -> str:
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
    publisher: str | None = None
    primary_error: BaseException | None = None
    primary_traceback = None
    try:
        result = _wintrust.WinVerifyTrust(
            wintypes.HWND(_INVALID_HANDLE_VALUE), ctypes.byref(action), ctypes.byref(trust_data)
        )
        if result != 0:
            raise OSError(result & 0xFFFFFFFF, "native Codex signature is not trusted")
        publisher = _publisher_from_trust_state(trust_data.hWVTStateData)
        if type(publisher) is not str or not publisher:
            raise OSError("native Codex signature publisher is invalid")
    except BaseException as error:
        primary_error = error
        primary_traceback = error.__traceback__

    cleanup_error: BaseException | None = None
    cleanup_traceback = None
    try:
        trust_data.dwStateAction = 2
        close_result = _wintrust.WinVerifyTrust(
            wintypes.HWND(_INVALID_HANDLE_VALUE), ctypes.byref(action), ctypes.byref(trust_data)
        )
        if close_result != 0:
            raise OSError(close_result & 0xFFFFFFFF, "could not close Codex trust state")
    except BaseException as error:
        cleanup_error = error
        cleanup_traceback = error.__traceback__

    if primary_error is not None:
        if _is_priority_failure(primary_error):
            raise primary_error.with_traceback(primary_traceback)
        if cleanup_error is not None and _is_priority_failure(cleanup_error):
            raise cleanup_error.with_traceback(cleanup_traceback)
        if not _is_expected_runtime_failure(primary_error):
            raise primary_error.with_traceback(primary_traceback)
        if cleanup_error is not None and not _is_expected_runtime_failure(cleanup_error):
            raise cleanup_error.with_traceback(cleanup_traceback)
        raise primary_error.with_traceback(primary_traceback)
    if cleanup_error is not None:
        raise cleanup_error.with_traceback(cleanup_traceback)
    assert publisher is not None
    return publisher


def _verify_wintrust_publisher(descriptor: int, path: Path) -> str:
    failed = False
    try:
        return _verify_wintrust_publisher_raw(descriptor, path)
    except BaseException as error:
        if _is_priority_failure(error) or not _is_expected_runtime_failure(error):
            raise
        failed = True
    if failed:
        del descriptor, path
        _raise_signature_not_trusted()
    raise AssertionError("unreachable")

__all__ = [
    "OPENAI_AUTHENTICODE_PUBLISHER",
    "NATIVE_CODEX_RELATIVE_PATH",
    "LockedNativeCodex",
    "NativeCodexIdentity",
    "discover_locked_native_codex",
]
