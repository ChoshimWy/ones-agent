from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

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
    with pytest.raises(FileNotFoundError):
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
    with pytest.raises(FileNotFoundError, match="ancestor raced"):
        _discover(root, adapter, repository_roots=(repository,))
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


_REAL_NATIVE_CODEX = (
    Path(r"C:\nvm4w\nodejs") / NATIVE_CODEX_RELATIVE_PATH
)


@pytest.mark.skipif(
    os.name != "nt" or not _REAL_NATIVE_CODEX.is_file(),
    reason="signed native npm Codex payload is unavailable",
)
def test_real_windows_native_payload_has_trusted_openai_publisher() -> None:
    locked = discover_locked_native_codex(
        which=lambda name: shutil.which(name),
    )
    try:
        assert locked.publisher == OPENAI_AUTHENTICODE_PUBLISHER
        assert locked.size == _REAL_NATIVE_CODEX.stat().st_size
    finally:
        locked.close()
