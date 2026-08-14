from __future__ import annotations

import asyncio
import os
import shutil
import traceback
from pathlib import Path

import pytest

import src.developer_workflow.codex_runtime as codex_runtime_module
from src.developer_workflow.codex_runtime import (
    NATIVE_CODEX_RELATIVE_PATH,
    OPENAI_AUTHENTICODE_PUBLISHER,
    LockedNativeCodex,
    NativeCodexIdentity,
    discover_locked_native_codex,
)


class FakeRuntimeAdapter:
    def __init__(self, root: Path) -> None:
        self.expected = root / NATIVE_CODEX_RELATIVE_PATH
        self.final = self.expected
        self.identities = [NativeCodexIdentity(7, 11, 6, 13)] * 2
        self.publisher = OPENAI_AUTHENTICODE_PUBLISHER
        self.acceptable = True
        self.descriptor = 71
        self.opened: list[Path] = []
        self.closed: list[int] = []
        self.reads: list[tuple[int, int]] = []
        self.rewinds: list[int] = []
        self.same_file_pairs: list[tuple[Path, Path]] = []
        self.repository_alias: Path | None = None
        self.open_error: BaseException | None = None
        self.verify_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.verified: list[tuple[int, Path]] = []

    def open_locked(self, path: Path) -> int:
        self.opened.append(path)
        if self.open_error is not None:
            raise self.open_error
        return self.descriptor

    def is_disk_regular_non_reparse(self, descriptor: int) -> bool:
        assert descriptor == self.descriptor
        return self.acceptable

    def identity(self, descriptor: int) -> NativeCodexIdentity:
        assert descriptor == self.descriptor
        return self.identities.pop(0)

    def final_path(self, descriptor: int) -> Path:
        assert descriptor == self.descriptor
        return self.final

    def verify_publisher(self, descriptor: int, path: Path) -> str:
        self.verified.append((descriptor, path))
        assert descriptor == self.descriptor
        assert path == self.final
        if self.verify_error is not None:
            raise self.verify_error
        return self.publisher

    def read(self, descriptor: int, size: int) -> bytes:
        self.reads.append((descriptor, size))
        return b"signed"[:size]

    def rewind(self, descriptor: int) -> None:
        self.rewinds.append(descriptor)

    def close(self, descriptor: int) -> None:
        self.closed.append(descriptor)
        if self.close_error is not None:
            raise self.close_error

    def same_file(self, left: Path, right: Path) -> bool:
        self.same_file_pairs.append((left, right))
        if self.repository_alias is not None and right == self.repository_alias:
            return left == self.final or left in self.final.parents
        return _normalized(left) == _normalized(right)


def _normalized(path: Path) -> str:
    return str(path).replace("\\\\?\\", "").replace("/", "\\").casefold()


def _discover(
    root: Path,
    adapter: FakeRuntimeAdapter,
    *,
    repository_roots: tuple[Path, ...] = (),
) -> LockedNativeCodex:
    calls: list[str] = []

    def which(name: str) -> str:
        calls.append(name)
        return str(root / "codex.cmd")

    locked = discover_locked_native_codex(
        which=which,
        repository_roots=repository_roots,
        _adapter=adapter,
    )
    assert calls == ["codex.cmd"]
    return locked


def test_discovers_fixed_native_payload_without_reading_or_executing_shim(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "npm").resolve()
    root.mkdir()
    (root / "codex.cmd").write_text(
        "@node C:\\external-canary\\codex.js %*", encoding="utf-8"
    )
    adapter = FakeRuntimeAdapter(root)

    locked = _discover(root, adapter)

    assert adapter.opened == [root / NATIVE_CODEX_RELATIVE_PATH]
    assert locked.identity == NativeCodexIdentity(7, 11, 6, 13)
    assert locked.size == 6
    assert locked.publisher == OPENAI_AUTHENTICODE_PUBLISHER
    assert adapter.verified == [(71, adapter.final)]
    assert "external-canary" not in repr(locked)
    locked.close()


def test_locked_native_codex_delegates_read_rewind_and_idempotent_close(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "npm").resolve()
    adapter = FakeRuntimeAdapter(root)
    locked = _discover(root, adapter)

    assert locked.read_chunk(4) == b"sign"
    locked.rewind()
    locked.close()
    locked.close()

    assert adapter.reads == [(71, 4)]
    assert adapter.rewinds == [71]
    assert adapter.closed == [71]
    with pytest.raises(ValueError, match="closed"):
        locked.read_chunk(1)


@pytest.mark.parametrize("size", [0, -1, True, 1.5])
def test_locked_native_codex_rejects_invalid_read_sizes(
    tmp_path: Path, size: object,
) -> None:
    root = (tmp_path / "npm").resolve()
    adapter = FakeRuntimeAdapter(root)
    locked = _discover(root, adapter)
    with pytest.raises(ValueError):
        locked.read_chunk(size)  # type: ignore[arg-type]
    locked.close()


@pytest.mark.parametrize(
    "locator",
    [None, "", "relative/codex.cmd", "C:/npm/codex.exe", "bad\x00/codex.cmd"],
)
def test_rejects_missing_or_fake_locator_without_opening(locator: str | None) -> None:
    adapter = FakeRuntimeAdapter(Path("C:/npm"))
    with pytest.raises(OSError):
        discover_locked_native_codex(
            which=lambda name: locator,
            repository_roots=(),
            _adapter=adapter,
        )
    assert adapter.opened == []


def test_rejects_missing_fixed_payload() -> None:
    root = Path("C:/npm")
    adapter = FakeRuntimeAdapter(root)
    adapter.open_error = FileNotFoundError("missing payload canary")
    with pytest.raises(OSError, match="^native Codex payload is unavailable$"):
        _discover(root, adapter)
    assert adapter.closed == []


def test_rejects_final_handle_path_outside_fixed_layout_and_closes_once() -> None:
    root = Path("C:/npm")
    adapter = FakeRuntimeAdapter(root)
    adapter.final = Path("C:/external/codex.exe")
    with pytest.raises(OSError):
        _discover(root, adapter)
    assert adapter.closed == [71]


def test_accepts_extended_case_alias_only_when_same_fixed_file() -> None:
    root = Path("C:/npm")
    adapter = FakeRuntimeAdapter(root)
    adapter.final = Path("\\\\?\\C:\\NPM") / NATIVE_CODEX_RELATIVE_PATH
    locked = _discover(root, adapter)
    locked.close()
    assert adapter.closed == [71]


def test_rejects_non_disk_non_regular_or_reparse_payload_and_closes_once() -> None:
    root = Path("C:/npm")
    adapter = FakeRuntimeAdapter(root)
    adapter.acceptable = False
    with pytest.raises(OSError):
        _discover(root, adapter)
    assert adapter.closed == [71]


def test_rejects_repository_or_worktree_identity_alias() -> None:
    root = Path("C:/npm")
    repository = Path("E:/workspace/ones-agent")
    adapter = FakeRuntimeAdapter(root)
    adapter.repository_alias = repository
    with pytest.raises(OSError):
        _discover(root, adapter, repository_roots=(repository,))
    assert adapter.closed == [71]


def test_repository_containment_identity_error_fails_closed() -> None:
    root = Path("C:/npm")
    repository = Path("E:/workspace/ones-agent")
    adapter = FakeRuntimeAdapter(root)
    original_same_file = adapter.same_file

    def racing_same_file(left: Path, right: Path) -> bool:
        if right == repository:
            raise FileNotFoundError("ancestor raced")
        return original_same_file(left, right)

    adapter.same_file = racing_same_file  # type: ignore[method-assign]
    with pytest.raises(OSError, match="^native Codex payload is unavailable$"):
        _discover(root, adapter, repository_roots=(repository,))
    assert adapter.closed == [71]


def test_default_repository_roots_reject_payload_beneath_current_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = (tmp_path / "external-workspace").resolve()
    root = workspace / "installed"
    root.mkdir(parents=True)
    monkeypatch.chdir(workspace)
    adapter = FakeRuntimeAdapter(root)

    with pytest.raises(OSError, match="^native Codex payload is unavailable$"):
        discover_locked_native_codex(
            which=lambda name: str(root / "codex.cmd"),
            _adapter=adapter,
        )

    assert adapter.closed == [71]


def test_default_repository_root_identity_race_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = (tmp_path / "racing-workspace").resolve()
    root = workspace / "installed"
    root.mkdir(parents=True)
    monkeypatch.chdir(workspace)
    adapter = FakeRuntimeAdapter(root)
    original_same_file = adapter.same_file

    def racing_same_file(left: Path, right: Path) -> bool:
        if left == workspace or right == workspace:
            raise FileNotFoundError("cwd-identity-race-canary")
        return original_same_file(left, right)

    adapter.same_file = racing_same_file  # type: ignore[method-assign]
    with pytest.raises(OSError, match="^native Codex payload is unavailable$"):
        discover_locked_native_codex(
            which=lambda name: str(root / "codex.cmd"),
            _adapter=adapter,
        )


@pytest.mark.parametrize("error_type", [AssertionError, TypeError, AttributeError])
def test_internal_runtime_failures_propagate_after_close(
    error_type: type[Exception],
) -> None:
    root = Path("C:/npm")
    adapter = FakeRuntimeAdapter(root)
    internal = error_type("internal-runtime-canary")
    adapter.verify_error = internal

    with pytest.raises(error_type) as caught:
        _discover(root, adapter)

    assert caught.value is internal
    assert adapter.closed == [71]


def test_internal_cleanup_failure_beats_expected_runtime_failure() -> None:
    root = Path("C:/npm")
    adapter = FakeRuntimeAdapter(root)
    internal = TypeError("internal-close-canary")
    adapter.verify_error = OSError("expected-trust-failure")
    adapter.close_error = internal

    with pytest.raises(TypeError) as caught:
        _discover(root, adapter)

    assert caught.value is internal
    assert adapter.closed == [71]


def test_rejects_identity_race_after_signature_verification() -> None:
    root = Path("C:/npm")
    adapter = FakeRuntimeAdapter(root)
    adapter.identities = [
        NativeCodexIdentity(7, 11, 6, 13),
        NativeCodexIdentity(7, 12, 6, 14),
    ]
    with pytest.raises(OSError):
        _discover(root, adapter)
    assert adapter.closed == [71]


@pytest.mark.parametrize(
    "publisher",
    ["OpenAI, L.L.C.", "OpenAI OpCo, LLC ", "openai opco, llc", ""],
)
def test_rejects_wrong_authenticode_publisher(publisher: str) -> None:
    root = Path("C:/npm")
    adapter = FakeRuntimeAdapter(root)
    adapter.publisher = publisher
    with pytest.raises(OSError):
        _discover(root, adapter)
    assert adapter.closed == [71]


def test_rejects_invalid_signature_or_wintrust_failure_and_closes_once() -> None:
    root = Path("C:/npm")
    adapter = FakeRuntimeAdapter(root)
    adapter.verify_error = OSError("wintrust-api-canary")
    with pytest.raises(OSError):
        _discover(root, adapter)
    assert adapter.closed == [71]


_PRIORITY_FAILURES = (
    MemoryError,
    asyncio.CancelledError,
    KeyboardInterrupt,
    SystemExit,
    GeneratorExit,
)


@pytest.mark.parametrize("primary_type", _PRIORITY_FAILURES)
def test_primary_memory_or_control_flow_beats_ordinary_close_failure(
    primary_type: type[BaseException],
) -> None:
    root = Path("C:/npm")
    adapter = FakeRuntimeAdapter(root)
    primary = primary_type("primary-control")
    adapter.verify_error = primary
    adapter.close_error = OSError("ordinary-close-canary")

    with pytest.raises(primary_type) as caught:
        _discover(root, adapter)

    assert caught.value is primary
    assert adapter.closed == [71]


@pytest.mark.parametrize("cleanup_type", _PRIORITY_FAILURES)
def test_close_memory_or_control_flow_beats_ordinary_primary_failure(
    cleanup_type: type[BaseException],
) -> None:
    root = Path("C:/npm")
    adapter = FakeRuntimeAdapter(root)
    cleanup = cleanup_type("cleanup-control")
    adapter.verify_error = OSError("ordinary-primary-canary")
    adapter.close_error = cleanup

    with pytest.raises(cleanup_type) as caught:
        _discover(root, adapter)

    assert caught.value is cleanup
    assert adapter.closed == [71]


@pytest.mark.parametrize("primary_type", _PRIORITY_FAILURES)
@pytest.mark.parametrize("cleanup_type", _PRIORITY_FAILURES)
def test_primary_memory_or_control_flow_beats_cleanup_control_flow(
    primary_type: type[BaseException],
    cleanup_type: type[BaseException],
) -> None:
    root = Path("C:/npm")
    adapter = FakeRuntimeAdapter(root)
    primary = primary_type("primary-control")
    adapter.verify_error = primary
    adapter.close_error = cleanup_type("cleanup-control")

    with pytest.raises(primary_type) as caught:
        _discover(root, adapter)

    assert caught.value is primary
    assert adapter.closed == [71]


def test_ordinary_primary_and_close_failures_are_fixed_and_scrubbed() -> None:
    primary_canary = "ordinary-primary-path-canary"
    cleanup_canary = "ordinary-close-path-canary"
    root = Path("C:/npm")
    adapter = FakeRuntimeAdapter(root)
    adapter.verify_error = OSError(primary_canary)
    adapter.close_error = OSError(cleanup_canary)

    with pytest.raises(OSError) as caught:
        _discover(root, adapter)

    assert str(caught.value) == "native Codex payload is unavailable"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert adapter.closed == [71]
    rendered = "".join(traceback.format_exception(caught.value))
    assert primary_canary not in rendered
    assert cleanup_canary not in rendered
    captured = traceback.TracebackException.from_exception(
        caught.value, capture_locals=True
    )
    module_path = Path(codex_runtime_module.__file__).resolve()
    project_locals = "\n".join(
        value
        for frame in captured.stack
        if Path(frame.filename).resolve() == module_path
        for value in (frame.locals or {}).values()
    )
    assert primary_canary not in project_locals
    assert cleanup_canary not in project_locals


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing semantics")
def test_real_windows_locked_handle_blocks_write_and_delete_until_close(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.codex_runtime import _WindowsRuntimeAdapter

    source = tmp_path / "codex.exe"
    source.write_bytes(b"signed-payload-placeholder")
    adapter = _WindowsRuntimeAdapter()
    descriptor = adapter.open_locked(source)
    try:
        with pytest.raises(PermissionError):
            source.open("r+b")
        with pytest.raises(PermissionError):
            source.unlink()
    finally:
        adapter.close(descriptor)

    with source.open("r+b") as stream:
        assert stream.read(6) == b"signed"


@pytest.mark.skipif(os.name != "nt", reason="WinTrust API is Windows-only")
def test_wintrust_verify_failure_closes_trust_state_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions: list[int] = []

    def fake_winverifytrust(hwnd: object, action: object, data: object) -> int:
        trust_data = codex_runtime_module.ctypes.cast(
            data,
            codex_runtime_module.ctypes.POINTER(codex_runtime_module._WINTRUST_DATA),
        ).contents
        actions.append(trust_data.dwStateAction)
        return 0x800B0100 if trust_data.dwStateAction == 1 else 0

    monkeypatch.setattr(
        codex_runtime_module._wintrust, "WinVerifyTrust", fake_winverifytrust
    )

    with pytest.raises(OSError, match="native Codex signature is not trusted") as caught:
        codex_runtime_module._verify_wintrust_publisher(73, Path("C:/fixed/codex.exe"))

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert actions == [1, 2]


@pytest.mark.skipif(os.name != "nt", reason="WinTrust API is Windows-only")
def test_wintrust_signer_failure_closes_state_and_preserves_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions: list[int] = []
    signer_failure = RuntimeError("signer-read-canary")

    def fake_winverifytrust(hwnd: object, action: object, data: object) -> int:
        trust_data = codex_runtime_module.ctypes.cast(
            data,
            codex_runtime_module.ctypes.POINTER(codex_runtime_module._WINTRUST_DATA),
        ).contents
        actions.append(trust_data.dwStateAction)
        if trust_data.dwStateAction == 1:
            trust_data.hWVTStateData = 99
        return 0

    def fail_signer(state: object) -> str:
        raise signer_failure

    monkeypatch.setattr(
        codex_runtime_module._wintrust, "WinVerifyTrust", fake_winverifytrust
    )
    monkeypatch.setattr(codex_runtime_module, "_publisher_from_trust_state", fail_signer)

    with pytest.raises(RuntimeError) as caught:
        codex_runtime_module._verify_wintrust_publisher(73, Path("C:/fixed/codex.exe"))

    assert caught.value is signer_failure
    assert actions == [1, 2]


@pytest.mark.skipif(os.name != "nt", reason="WinTrust API is Windows-only")
def test_wintrust_success_with_close_failure_is_fixed_and_does_not_return_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions: list[int] = []

    def fake_winverifytrust(hwnd: object, action: object, data: object) -> int:
        trust_data = codex_runtime_module.ctypes.cast(
            data,
            codex_runtime_module.ctypes.POINTER(codex_runtime_module._WINTRUST_DATA),
        ).contents
        actions.append(trust_data.dwStateAction)
        if trust_data.dwStateAction == 1:
            trust_data.hWVTStateData = 99
            return 0
        return 0x80004005

    monkeypatch.setattr(
        codex_runtime_module._wintrust, "WinVerifyTrust", fake_winverifytrust
    )
    monkeypatch.setattr(
        codex_runtime_module,
        "_publisher_from_trust_state",
        lambda state: OPENAI_AUTHENTICODE_PUBLISHER,
    )

    with pytest.raises(OSError) as caught:
        codex_runtime_module._verify_wintrust_publisher(73, Path("C:/fixed/codex.exe"))

    assert str(caught.value) == "native Codex signature is not trusted"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert actions == [1, 2]


@pytest.mark.skipif(os.name != "nt", reason="WinTrust API is Windows-only")
def test_wintrust_verify_and_close_failures_are_fixed_and_close_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions: list[int] = []

    def fake_winverifytrust(hwnd: object, action: object, data: object) -> int:
        trust_data = codex_runtime_module.ctypes.cast(
            data,
            codex_runtime_module.ctypes.POINTER(codex_runtime_module._WINTRUST_DATA),
        ).contents
        actions.append(trust_data.dwStateAction)
        return 0x800B0100 if trust_data.dwStateAction == 1 else 0x80004005

    monkeypatch.setattr(
        codex_runtime_module._wintrust, "WinVerifyTrust", fake_winverifytrust
    )

    with pytest.raises(OSError) as caught:
        codex_runtime_module._verify_wintrust_publisher(73, Path("C:/fixed/codex.exe"))

    assert str(caught.value) == "native Codex signature is not trusted"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert actions == [1, 2]


@pytest.mark.skipif(os.name != "nt", reason="WinTrust API is Windows-only")
def test_wintrust_ordinary_signer_and_close_failures_are_fixed_and_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer_canary = "signer-path-canary"
    actions: list[int] = []

    def fake_winverifytrust(hwnd: object, action: object, data: object) -> int:
        trust_data = codex_runtime_module.ctypes.cast(
            data,
            codex_runtime_module.ctypes.POINTER(codex_runtime_module._WINTRUST_DATA),
        ).contents
        actions.append(trust_data.dwStateAction)
        if trust_data.dwStateAction == 1:
            trust_data.hWVTStateData = 99
            return 0
        return 0x80004005

    def fail_signer(state: object) -> str:
        raise OSError(signer_canary)

    monkeypatch.setattr(
        codex_runtime_module._wintrust, "WinVerifyTrust", fake_winverifytrust
    )
    monkeypatch.setattr(codex_runtime_module, "_publisher_from_trust_state", fail_signer)

    with pytest.raises(OSError) as caught:
        codex_runtime_module._verify_wintrust_publisher(73, Path("C:/fixed/codex.exe"))

    assert str(caught.value) == "native Codex signature is not trusted"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert signer_canary not in "".join(traceback.format_exception(caught.value))
    assert actions == [1, 2]


@pytest.mark.skipif(os.name != "nt", reason="WinTrust API is Windows-only")
@pytest.mark.parametrize("primary_type", _PRIORITY_FAILURES)
def test_wintrust_primary_memory_or_control_beats_close_failure(
    monkeypatch: pytest.MonkeyPatch,
    primary_type: type[BaseException],
) -> None:
    actions: list[int] = []
    primary = primary_type("trust-primary-control")

    def fake_winverifytrust(hwnd: object, action: object, data: object) -> int:
        trust_data = codex_runtime_module.ctypes.cast(
            data,
            codex_runtime_module.ctypes.POINTER(codex_runtime_module._WINTRUST_DATA),
        ).contents
        actions.append(trust_data.dwStateAction)
        if trust_data.dwStateAction == 1:
            trust_data.hWVTStateData = 99
            return 0
        return 0x80004005

    def fail_signer(state: object) -> str:
        raise primary

    monkeypatch.setattr(
        codex_runtime_module._wintrust, "WinVerifyTrust", fake_winverifytrust
    )
    monkeypatch.setattr(codex_runtime_module, "_publisher_from_trust_state", fail_signer)

    with pytest.raises(primary_type) as caught:
        codex_runtime_module._verify_wintrust_publisher(73, Path("C:/fixed/codex.exe"))

    assert caught.value is primary
    assert actions == [1, 2]


@pytest.mark.skipif(os.name != "nt", reason="WinTrust API is Windows-only")
@pytest.mark.parametrize("error_type", [AssertionError, TypeError, AttributeError])
def test_wintrust_internal_signer_failure_propagates_after_close(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    actions: list[int] = []
    internal = error_type("internal-signer-canary")

    def fake_winverifytrust(hwnd: object, action: object, data: object) -> int:
        trust_data = codex_runtime_module.ctypes.cast(
            data,
            codex_runtime_module.ctypes.POINTER(codex_runtime_module._WINTRUST_DATA),
        ).contents
        actions.append(trust_data.dwStateAction)
        if trust_data.dwStateAction == 1:
            trust_data.hWVTStateData = 99
            return 0
        return 0x80004005

    def fail_signer(state: object) -> str:
        raise internal

    monkeypatch.setattr(
        codex_runtime_module._wintrust, "WinVerifyTrust", fake_winverifytrust
    )
    monkeypatch.setattr(codex_runtime_module, "_publisher_from_trust_state", fail_signer)

    with pytest.raises(error_type) as caught:
        codex_runtime_module._verify_wintrust_publisher(73, Path("C:/fixed/codex.exe"))

    assert caught.value is internal
    assert actions == [1, 2]


_REAL_CODEX_LOCATOR = shutil.which("codex.cmd")
_REAL_NATIVE_CODEX = (
    Path(_REAL_CODEX_LOCATOR).parent / NATIVE_CODEX_RELATIVE_PATH
    if _REAL_CODEX_LOCATOR is not None
    else None
)


@pytest.mark.skipif(
    os.name != "nt"
    or _REAL_NATIVE_CODEX is None
    or not _REAL_NATIVE_CODEX.is_file(),
    reason="signed native npm Codex payload is unavailable",
)
def test_real_windows_native_payload_has_trusted_openai_publisher() -> None:
    assert _REAL_CODEX_LOCATOR is not None
    expected = Path(_REAL_CODEX_LOCATOR).parent / NATIVE_CODEX_RELATIVE_PATH
    locked = discover_locked_native_codex(
        which=lambda name: _REAL_CODEX_LOCATOR if name == "codex.cmd" else None,
    )
    try:
        assert locked.publisher == OPENAI_AUTHENTICODE_PUBLISHER
        assert locked.size == expected.stat().st_size
    finally:
        locked.close()
