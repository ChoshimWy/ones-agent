from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import subprocess
import threading
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
        self.marker_errors: dict[Path, BaseException] = {}
        self.marker_calls: list[Path] = []
        self.marker_identity_race: Path | None = None
        self.marker_identity_calls: dict[Path, int] = {}
        self.resolved_repository_path: Path | None = None
        self.repository_identity_race: Path | None = None
        self.repository_identity_calls: dict[Path, int] = {}

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

    def repository_marker(
        self, root: Path,
    ) -> tuple[str, tuple[int, int, int, int]] | None:
        self.marker_calls.append(root)
        if root in self.marker_errors:
            raise self.marker_errors[root]
        marker = root / ".git"
        if marker.is_dir():
            marker_kind = "directory"
        elif marker.is_file():
            marker_kind = "file"
        else:
            return None
        calls = self.marker_identity_calls.get(root, 0)
        self.marker_identity_calls[root] = calls + 1
        generation = 2 if root == self.marker_identity_race and calls else 1
        return marker_kind, (generation, hash(_normalized(marker)), 0, 0)

    def resolve_repository_path(self, path: Path) -> Path:
        return self.resolved_repository_path or path.resolve(strict=True)

    def repository_path_identity(self, path: Path) -> tuple[int, int, int, int]:
        calls = self.repository_identity_calls.get(path, 0)
        self.repository_identity_calls[path] = calls + 1
        generation = 2 if path == self.repository_identity_race and calls else 1
        return generation, hash(_normalized(path)), 0, 0


def _normalized(path: Path) -> str:
    return str(path).replace("\\\\?\\", "").replace("/", "\\").casefold()


class FakeLockedSource:
    def __init__(self, payload: bytes = b"signed-native") -> None:
        self.payload = payload
        self.offset = 0
        self.identity = NativeCodexIdentity(3, 5, len(payload), 7)
        self.size = len(payload)
        self.publisher = OPENAI_AUTHENTICODE_PUBLISHER
        self.closed = False
        self.read_calls = 0

    def read_chunk(self, size: int) -> bytes:
        self.read_calls += 1
        chunk = self.payload[self.offset : self.offset + max(1, size // 2)]
        self.offset += len(chunk)
        return chunk

    def rewind(self) -> None:
        self.offset = 0

    def current_identity(self) -> NativeCodexIdentity:
        return self.identity

    def close(self) -> None:
        self.closed = True


class FakeCacheAdapter:
    def __init__(self) -> None:
        self.events: list[tuple[str, Path | tuple[str, ...]]] = []
        self.publisher = OPENAI_AUTHENTICODE_PUBLISHER
        self.smoke_error: BaseException | None = None
        self.fsync_error: BaseException | None = None
        self.directory_error: BaseException | None = None
        self.ancestor_error: BaseException | None = None

    def prepare_private_directory(self, path: Path) -> Path:
        self.events.append(("prepare-directory", path))
        path.mkdir(exist_ok=True)
        return path.resolve(strict=True)

    def validate_cache_ancestor_chain(self, root: Path) -> None:
        self.events.append(("validate-ancestors", root))
        if self.ancestor_error is not None:
            raise self.ancestor_error

    def validate_private_directory(self, path: Path) -> None:
        self.events.append(("validate-directory", path))
        if self.directory_error is not None and len(path.name) == 64:
            raise self.directory_error
        if not path.is_dir() or path.is_symlink():
            raise OSError("unsafe directory")

    def protect_private_file(self, path: Path) -> None:
        self.events.append(("protect-file", path))

    def inspect_private_executable(
        self, path: Path,
    ) -> tuple[NativeCodexIdentity, str]:
        self.events.append(("inspect-executable", path))
        metadata = path.stat()
        return (
            NativeCodexIdentity(
                metadata.st_dev, metadata.st_ino, metadata.st_size,
                metadata.st_mtime_ns,
            ),
            self.publisher,
        )

    def validate_private_file(self, path: Path) -> tuple[int, int]:
        self.events.append(("validate-file", path))
        metadata = path.stat()
        return metadata.st_dev, metadata.st_ino

    def read_private_text(self, path: Path) -> str:
        self.events.append(("read-file", path))
        return path.read_text("utf-8")

    def fsync_directory(self, path: Path) -> None:
        self.events.append(("fsync-directory", path))
        if self.fsync_error is not None:
            raise self.fsync_error

    def smoke(
        self, executable: Path, *, environment: dict[str, str], timeout: float,
    ) -> None:
        self.events.append(("smoke", (str(executable), *sorted(environment))))
        if self.smoke_error is not None:
            raise self.smoke_error


def test_preparer_streams_source_and_publishes_manifest_last(tmp_path: Path) -> None:
    from src.developer_workflow.codex_runtime import CodexRuntimePreparer

    source = FakeLockedSource()
    adapter = FakeCacheAdapter()
    root = tmp_path / "ones-dev" / "codex-runtime"

    executable = CodexRuntimePreparer(
        cache_root=root,
        discover=lambda: source,  # type: ignore[arg-type]
        _cache_adapter=adapter,  # type: ignore[arg-type]
        chunk_size=4,
    ).prepare()

    digest = __import__("hashlib").sha256(source.payload).hexdigest()
    assert executable == (root / digest / "codex.exe").resolve(strict=True)
    assert executable.read_bytes() == source.payload
    manifest = json.loads((executable.parent / "manifest.json").read_text("utf-8"))
    assert manifest == {
        "publisher": OPENAI_AUTHENTICODE_PUBLISHER,
        "schema_version": 1,
        "sha256": digest,
        "size": len(source.payload),
        "target": "codex.exe",
    }
    assert source.closed
    assert source.read_calls > 1
    assert not tuple(root.glob(".staging-*"))


def test_preparer_smokes_only_valid_manifest_staging_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ones-dev" / "codex-runtime"

    class OrderingAdapter(FakeCacheAdapter):
        def smoke(
            self, executable: Path, *, environment: dict[str, str], timeout: float,
        ) -> None:
            assert executable.parent.name.startswith(".staging-")
            manifest = executable.parent / "manifest.json"
            assert manifest.is_file()
            super().smoke(executable, environment=environment, timeout=timeout)

    executable = _prepare_runtime(root, FakeLockedSource(), OrderingAdapter())
    assert executable.parent.name == hashlib.sha256(b"signed-native").hexdigest()


def test_preparer_revalidates_staging_manifest_before_smoke(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ones-dev" / "codex-runtime"

    class CorruptingAdapter(FakeCacheAdapter):
        smoked = False

        def fsync_directory(self, path: Path) -> None:
            super().fsync_directory(path)
            manifest = path / "manifest.json"
            if path.name.startswith(".staging-") and manifest.is_file():
                value = json.loads(manifest.read_text("utf-8"))
                value["target"] = "../outside.exe"
                manifest.write_text(json.dumps(value), encoding="utf-8")

        def smoke(
            self, executable: Path, *, environment: dict[str, str], timeout: float,
        ) -> None:
            self.smoked = True

    adapter = CorruptingAdapter()
    with pytest.raises(OSError, match="private Codex runtime is unavailable"):
        _prepare_runtime(root, FakeLockedSource(), adapter)
    assert not adapter.smoked
    assert not tuple(root.glob(".staging-*"))


@pytest.mark.parametrize("failure_point", ["manifest_fsync", "root_fsync"])
def test_postpublish_fsync_failure_removes_attempted_final_and_allows_retry(
    tmp_path: Path, failure_point: str,
) -> None:
    root = tmp_path / "ones-dev" / "codex-runtime"

    class PostPublishFailureAdapter(FakeCacheAdapter):
        def fsync_directory(self, path: Path) -> None:
            super().fsync_directory(path)
            manifest_exists = (path / "manifest.json").is_file()
            final_exists = any(
                child.is_dir() and len(child.name) == 64
                for child in root.iterdir()
            )
            if failure_point == "manifest_fsync" and path.name.startswith(
                ".staging-"
            ) and manifest_exists:
                raise OSError("post-manifest fsync")
            if failure_point == "root_fsync" and path == root and final_exists:
                raise OSError("post-rename root fsync")

    adapter = PostPublishFailureAdapter()
    with pytest.raises(OSError, match="private Codex runtime is unavailable"):
        _prepare_runtime(root, FakeLockedSource(), adapter)
    assert not tuple(root.glob(".staging-*"))
    assert not tuple(path for path in root.iterdir() if len(path.name) == 64)

    executable = _prepare_runtime(root, FakeLockedSource(), FakeCacheAdapter())
    assert executable.is_file()


def test_postrename_cleanup_retries_transient_final_delete_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "ones-dev" / "codex-runtime"

    class RootFailure(FakeCacheAdapter):
        def fsync_directory(self, path: Path) -> None:
            super().fsync_directory(path)
            if path == root and any(
                child.is_dir() and len(child.name) == 64
                for child in root.iterdir()
            ):
                raise OSError("root fsync failed")

    original = codex_runtime_module._remove_owned_tree
    final_attempts = 0

    def transient(path: Path) -> None:
        nonlocal final_attempts
        if len(path.name) == 64:
            final_attempts += 1
            if final_attempts == 1:
                raise OSError("transient delete")
        original(path)

    monkeypatch.setattr(codex_runtime_module, "_remove_owned_tree", transient)
    with pytest.raises(OSError, match="private Codex runtime is unavailable"):
        _prepare_runtime(root, FakeLockedSource(), RootFailure())
    assert final_attempts >= 2
    assert not tuple(path for path in root.iterdir() if len(path.name) == 64)


@pytest.mark.parametrize("stale_kind", ["random", "deterministic"])
def test_preparer_cleans_private_owned_incomplete_cache_and_retries(
    tmp_path: Path, stale_kind: str,
) -> None:
    payload = b"retry-payload"
    digest = hashlib.sha256(payload).hexdigest()
    root = tmp_path / "ones-dev" / "codex-runtime"
    root.mkdir(parents=True)
    stale = root / (
        ".staging-" + ("a" * 32) if stale_kind == "random" else digest
    )
    stale.mkdir()
    (stale / "codex.exe").write_bytes(b"incomplete")

    executable = _prepare_runtime(
        root, FakeLockedSource(payload), FakeCacheAdapter()
    )

    assert executable == (root / digest / "codex.exe").resolve(strict=True)
    if stale_kind == "random":
        assert stale.is_dir()
    else:
        assert stale == executable.parent


def test_preparer_ignores_potentially_active_random_staging_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ones-dev" / "codex-runtime"
    root.mkdir(parents=True)
    active = root / (".staging-" + "b" * 32)
    active.mkdir()
    (active / "codex.exe").write_bytes(b"possibly-active")

    executable = _prepare_runtime(
        root, FakeLockedSource(b"other-version"), FakeCacheAdapter()
    )

    assert executable.is_file()
    assert active.is_dir()
    assert (active / "codex.exe").read_bytes() == b"possibly-active"


def test_incomplete_hash_directory_is_validated_before_manifest_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow.codex_runtime import CodexRuntimePreparer

    root = tmp_path / "ones-dev" / "codex-runtime"
    root.mkdir(parents=True)
    candidate = root / ("c" * 64)
    candidate.mkdir()
    adapter = FakeCacheAdapter()
    adapter.directory_error = OSError("simulated reparse")
    original_exists = Path.exists

    def guarded_exists(path: Path) -> bool:
        if path == candidate / "manifest.json":
            raise AssertionError("manifest lookup traversed an unvalidated directory")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", guarded_exists)
    CodexRuntimePreparer(
        cache_root=root,
        discover=FakeLockedSource,
        _cache_adapter=adapter,  # type: ignore[arg-type]
    )._cleanup_stale_owned_entries(root, adapter)  # type: ignore[arg-type]
    assert candidate.is_dir()


def _prepare_runtime(
    root: Path, source: FakeLockedSource, adapter: FakeCacheAdapter,
) -> Path:
    from src.developer_workflow.codex_runtime import CodexRuntimePreparer

    return CodexRuntimePreparer(
        cache_root=root,
        discover=lambda: source,  # type: ignore[arg-type]
        _cache_adapter=adapter,  # type: ignore[arg-type]
        chunk_size=3,
    ).prepare()


def test_preparer_uses_valid_cache_when_source_is_unavailable(tmp_path: Path) -> None:
    from src.developer_workflow.codex_runtime import CodexRuntimePreparer

    root = tmp_path / "ones-dev" / "codex-runtime"
    adapter = FakeCacheAdapter()
    existing = _prepare_runtime(root, FakeLockedSource(b"trusted-old"), adapter)

    def missing() -> LockedNativeCodex:
        raise OSError("sensitive missing source")

    reused = CodexRuntimePreparer(
        cache_root=root,
        discover=missing,
        _cache_adapter=adapter,  # type: ignore[arg-type]
    ).prepare()

    assert reused == existing
    assert reused.read_bytes() == b"trusted-old"


def test_preparer_reuses_matching_cache_without_replacing_executable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ones-dev" / "codex-runtime"
    adapter = FakeCacheAdapter()
    first = _prepare_runtime(root, FakeLockedSource(b"same-version"), adapter)
    initial_identity = first.stat().st_ino
    source = FakeLockedSource(b"same-version")

    second = _prepare_runtime(root, source, adapter)

    assert second == first
    assert second.stat().st_ino == initial_identity
    assert source.closed


def test_preparer_stages_new_hash_when_source_changes(tmp_path: Path) -> None:
    root = tmp_path / "ones-dev" / "codex-runtime"
    adapter = FakeCacheAdapter()
    old = _prepare_runtime(root, FakeLockedSource(b"version-one"), adapter)
    new = _prepare_runtime(root, FakeLockedSource(b"version-two"), adapter)

    assert old != new
    assert old.read_bytes() == b"version-one"
    assert new.read_bytes() == b"version-two"
    assert (old.parent / "manifest.json").is_file()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: {**manifest, "target": "../../outside.exe"},
        lambda manifest: {**manifest, "unexpected": True},
        lambda manifest: {**manifest, "size": True},
        lambda manifest: {**manifest, "sha256": "0" * 64},
        lambda manifest: {**manifest, "publisher": "Different Publisher"},
    ],
)
def test_preparer_never_uses_corrupt_or_path_injecting_manifest(
    tmp_path: Path, mutate: object,
) -> None:
    from src.developer_workflow.codex_runtime import CodexRuntimePreparer

    root = tmp_path / "ones-dev" / "codex-runtime"
    adapter = FakeCacheAdapter()
    executable = _prepare_runtime(root, FakeLockedSource(), adapter)
    manifest_path = executable.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest_path.write_text(
        json.dumps(mutate(manifest)),  # type: ignore[operator]
        encoding="utf-8",
    )
    adapter.events.clear()

    with pytest.raises(OSError, match="private Codex runtime is unavailable"):
        CodexRuntimePreparer(
            cache_root=root,
            discover=lambda: (_ for _ in ()).throw(OSError("missing")),
            _cache_adapter=adapter,  # type: ignore[arg-type]
        ).prepare()

    inspected = [
        value for event, value in adapter.events if event == "inspect-executable"
    ]
    assert all(Path(value).is_relative_to(root) for value in inspected)


def test_preparer_rejects_source_identity_change_and_cleans_owned_temps(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ones-dev" / "codex-runtime"
    adapter = FakeCacheAdapter()
    source = FakeLockedSource()
    source.identity = NativeCodexIdentity(3, 5, source.size, 99)
    initial = source.identity

    def changed_identity() -> NativeCodexIdentity:
        return NativeCodexIdentity(
            initial.volume_serial, initial.file_index, initial.size, 100,
        )

    source.current_identity = changed_identity  # type: ignore[method-assign]
    with pytest.raises(OSError):
        _prepare_runtime(root, source, adapter)

    assert source.closed
    assert not tuple(root.glob(".staging-*"))
    assert not tuple(path for path in root.iterdir() if len(path.name) == 64)


def test_cache_adapter_inspects_locked_target_and_closes_handle(
    tmp_path: Path,
) -> None:
    target = (tmp_path / "codex.exe").resolve()
    target.write_bytes(b"signed")
    runtime = FakeRuntimeAdapter(tmp_path)
    runtime.expected = target
    runtime.final = target
    adapter = codex_runtime_module._WindowsCacheRuntimeAdapter(  # type: ignore[attr-defined]
        _runtime_adapter=runtime,
    )

    identity, publisher = adapter.inspect_private_executable(target)

    assert identity == NativeCodexIdentity(7, 11, 6, 13)
    assert publisher == OPENAI_AUTHENTICODE_PUBLISHER
    assert runtime.verified == [(71, target)]
    assert runtime.closed == [71]


def test_smoke_environment_drops_credentials_and_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_API_KEY", "must-not-leak")
    monkeypatch.setenv("PATH", "C:/attacker")
    monkeypatch.setenv("SYSTEMROOT", "C:/Windows")

    environment = codex_runtime_module._sanitized_smoke_environment()

    assert environment["SYSTEMROOT"] == "C:/Windows"
    assert "PATH" not in environment
    assert "CODEX_API_KEY" not in environment


@pytest.mark.parametrize("unsafe", ["signature", "directory"])
def test_preparer_rejects_untrusted_cached_executable_or_directory(
    tmp_path: Path, unsafe: str,
) -> None:
    from src.developer_workflow.codex_runtime import CodexRuntimePreparer

    root = tmp_path / "ones-dev" / "codex-runtime"
    adapter = FakeCacheAdapter()
    _prepare_runtime(root, FakeLockedSource(), adapter)
    if unsafe == "signature":
        adapter.publisher = "Wrong Publisher"
    else:
        adapter.directory_error = OSError("unsafe ACL")

    with pytest.raises(OSError, match="private Codex runtime is unavailable"):
        CodexRuntimePreparer(
            cache_root=root,
            discover=lambda: (_ for _ in ()).throw(OSError("missing")),
            _cache_adapter=adapter,  # type: ignore[arg-type]
        ).prepare()


@pytest.mark.parametrize("failure_point", ["fsync", "replace", "cancel"])
def test_failed_stage_cleans_owned_temps_and_closes_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    root = tmp_path / "ones-dev" / "codex-runtime"
    adapter = FakeCacheAdapter()
    source = FakeLockedSource()
    if failure_point == "fsync":
        adapter.fsync_error = OSError("disk full")
    elif failure_point == "replace":
        monkeypatch.setattr(
            codex_runtime_module.os,
            "replace",
            lambda *args: (_ for _ in ()).throw(OSError("replace failed")),
        )
    else:
        original_read = source.read_chunk
        calls = 0

        def cancelled(size: int) -> bytes:
            nonlocal calls
            calls += 1
            if calls > 4:
                raise asyncio.CancelledError()
            return original_read(size)

        source.read_chunk = cancelled  # type: ignore[method-assign]

    with pytest.raises(
        asyncio.CancelledError if failure_point == "cancel" else OSError
    ):
        _prepare_runtime(root, source, adapter)

    assert source.closed
    assert not tuple(root.glob(".staging-*"))
    assert not tuple(path for path in root.iterdir() if len(path.name) == 64)


def test_control_flow_beats_source_close_failure_and_cleans_temps(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ones-dev" / "codex-runtime"

    class CancellingSource(FakeLockedSource):
        def read_chunk(self, size: int) -> bytes:
            raise asyncio.CancelledError()

        def close(self) -> None:
            raise OSError("close-sensitive-path")

    with pytest.raises(asyncio.CancelledError):
        _prepare_runtime(root, CancellingSource(), FakeCacheAdapter())
    assert not tuple(root.glob(".staging-*"))


def test_partial_destination_writes_are_completed() -> None:
    class PartialWriter:
        def __init__(self) -> None:
            self.value = bytearray()

        def write(self, data: bytes | memoryview) -> int:
            raw = bytes(data)
            count = max(1, len(raw) // 2)
            self.value.extend(raw[:count])
            return count

    writer = PartialWriter()
    codex_runtime_module._write_all(writer, b"complete-payload")
    assert bytes(writer.value) == b"complete-payload"


def test_owned_cleanup_attempts_every_path_before_raising_priority_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / ".staging-one"
    version = tmp_path / ("a" * 64)
    calls: list[Path] = []
    priority = MemoryError("cleanup-memory")

    def remove(path: Path) -> None:
        calls.append(path)
        if path == staging:
            raise priority
        raise OSError("cleanup-disk")

    monkeypatch.setattr(codex_runtime_module, "_remove_owned_tree", remove)

    assert codex_runtime_module._attempt_owned_cleanup((staging, version)) is priority
    assert calls == [staging, version]


def test_failed_new_version_preserves_and_returns_old_trusted_cache(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ones-dev" / "codex-runtime"
    adapter = FakeCacheAdapter()
    old = _prepare_runtime(root, FakeLockedSource(b"trusted-old"), adapter)
    adapter.fsync_error = OSError("new-version-fsync-failed")

    fallback = _prepare_runtime(root, FakeLockedSource(b"unusable-new"), adapter)

    assert fallback == old
    assert old.read_bytes() == b"trusted-old"
    assert (old.parent / "manifest.json").is_file()


def test_preparer_rejects_nul_cache_root_before_filesystem_access() -> None:
    from src.developer_workflow.codex_runtime import CodexRuntimePreparer

    with pytest.raises(OSError, match="private Codex runtime is unavailable"):
        CodexRuntimePreparer(cache_root=Path("C:/unsafe\x00root"))


@pytest.mark.parametrize(
    "dangerous_mask",
    [0x00000040, 0x00010000, 0x00040000, 0x00080000, 0x40000000, 0x10000000, 0x001301BF],
)
def test_windows_cache_ancestor_rejects_untrusted_replacement_rights(
    dangerous_mask: int,
) -> None:
    with pytest.raises(OSError, match="ancestor"):
        codex_runtime_module._validate_windows_cache_ancestor_acl(
            owner="S-1-5-18",
            entries=(("S-1-1-0", dangerous_mask, 0, 0),),
            user_sid="S-1-5-21-current",
        )


def test_windows_cache_ancestor_allows_read_add_only_and_inherit_only_rights() -> None:
    codex_runtime_module._validate_windows_cache_ancestor_acl(
        owner="S-1-5-18",
        entries=(
            ("S-1-1-0", 0x001200A9, 0, 0),
            ("S-1-5-11", 0x00000004, 0, 0),
            ("S-1-5-11", 0xE0010000, 0x08, 0),
        ),
        user_sid="S-1-5-21-current",
    )


def test_windows_cache_ancestor_rejects_unknown_ace_shape() -> None:
    with pytest.raises(OSError, match="ancestor"):
        codex_runtime_module._validate_windows_cache_ancestor_acl(
            owner="S-1-5-18",
            entries=(("S-1-1-0", 0x1F01FF, 0, 5),),
            user_sid="S-1-5-21-current",
        )


def test_unsafe_cache_ancestor_fails_before_creating_private_root(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.codex_runtime import CodexRuntimePreparer

    root = tmp_path / "ones-dev" / "codex-runtime"
    adapter = FakeCacheAdapter()
    adapter.ancestor_error = OSError("unsafe ancestor")
    with pytest.raises(OSError, match="unsafe ancestor"):
        CodexRuntimePreparer(
            cache_root=root,
            discover=FakeLockedSource,
            _cache_adapter=adapter,  # type: ignore[arg-type]
        ).prepare()
    assert not root.exists()


@pytest.mark.skipif(os.name != "nt", reason="real LOCALAPPDATA ACL is Windows-only")
def test_real_localappdata_ancestor_chain_is_safe_without_staging() -> None:
    local_app_data = Path(os.environ["LOCALAPPDATA"])
    root = local_app_data / "ones-dev" / "codex-runtime"
    adapter = codex_runtime_module._WindowsCacheRuntimeAdapter()

    adapter.validate_cache_ancestor_chain(root)

    assert local_app_data.is_dir()


def test_cache_ancestor_chain_rejects_reparse_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow import private_paths

    root = tmp_path / "ones-dev" / "codex-runtime"
    target = root.parent.parent
    monkeypatch.setattr(private_paths, "_current_user_sid", lambda: "user")
    monkeypatch.setattr(
        private_paths,
        "_windows_descriptor",
        lambda path: ("user", (("user", 0x1F01FF, 0, 0),), True),
    )
    monkeypatch.setattr(
        private_paths, "_is_link_or_reparse", lambda path: path == target,
    )
    adapter = codex_runtime_module._WindowsCacheRuntimeAdapter(
        _runtime_adapter=FakeRuntimeAdapter(tmp_path),
    )
    with pytest.raises(OSError, match="ancestor"):
        adapter.validate_cache_ancestor_chain(root)


def test_cache_ancestor_chain_rejects_identity_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow import private_paths

    root = tmp_path / "ones-dev" / "codex-runtime"
    target = root.parent.parent
    original_stat = Path.stat
    calls = 0

    def racing_stat(path: Path, *args: object, **kwargs: object) -> object:
        nonlocal calls
        metadata = original_stat(path, *args, **kwargs)
        if path == target:
            calls += 1
            if calls >= 3:
                class Changed:
                    st_mode = metadata.st_mode
                    st_dev = metadata.st_dev
                    st_ino = metadata.st_ino + 1

                return Changed()
        return metadata

    monkeypatch.setattr(private_paths, "_current_user_sid", lambda: "user")
    monkeypatch.setattr(
        private_paths,
        "_windows_descriptor",
        lambda path: ("user", (("user", 0x1F01FF, 0, 0),), True),
    )
    monkeypatch.setattr(private_paths, "_is_link_or_reparse", lambda path: False)
    monkeypatch.setattr(Path, "stat", racing_stat)
    adapter = codex_runtime_module._WindowsCacheRuntimeAdapter(
        _runtime_adapter=FakeRuntimeAdapter(tmp_path),
    )
    with pytest.raises(OSError, match="unstable"):
        adapter.validate_cache_ancestor_chain(root)


def test_zero_length_destination_write_fails_closed() -> None:
    class StalledWriter:
        def write(self, data: bytes | memoryview) -> int:
            return 0

    with pytest.raises(OSError, match="write failed"):
        codex_runtime_module._write_all(StalledWriter(), b"payload")


def test_smoke_uses_absolute_native_argv_shell_false_and_bounded_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = (tmp_path / "codex.exe").resolve()
    calls: list[tuple[object, dict[str, object]]] = []

    class FakeProcess:
        stdout = io.BytesIO(b"codex-cli 1.2.3\n")

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            raise AssertionError("successful smoke must not be killed")

    def popen(argv: object, **kwargs: object) -> FakeProcess:
        calls.append((argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr(codex_runtime_module.subprocess, "Popen", popen)
    codex_runtime_module._run_bounded_smoke(
        executable,
        environment={"SYSTEMROOT": "C:/Windows"},
        timeout=2.0,
    )

    argv, kwargs = calls[0]
    assert argv == [str(executable), "--version"]
    assert kwargs["shell"] is False
    assert kwargs["env"] == {"SYSTEMROOT": "C:/Windows"}


def test_smoke_kills_process_when_output_exceeds_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[bool] = []

    class FakeProcess:
        stdout = io.BytesIO(b"x" * (64 * 1024 + 1))

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            killed.append(True)

    monkeypatch.setattr(
        codex_runtime_module.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    with pytest.raises(OSError, match="smoke test failed"):
        codex_runtime_module._run_bounded_smoke(
            (tmp_path / "codex.exe").resolve(), environment={}, timeout=2.0,
        )
    assert killed


@pytest.mark.parametrize(
    "primary",
    [
        asyncio.CancelledError(), KeyboardInterrupt(), SystemExit(),
        GeneratorExit(), MemoryError("memory"),
        subprocess.TimeoutExpired(["codex.exe", "--version"], 2.0),
        OSError("wait failed"),
    ],
)
def test_smoke_failure_always_kills_reaps_closes_and_joins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary: BaseException,
) -> None:
    events: list[str] = []

    class Pipe(io.BytesIO):
        def close(self) -> None:
            events.append("close-pipe")
            super().close()

    class Process:
        stdout = Pipe(b"")
        wait_calls = 0

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls += 1
            events.append(f"wait-{self.wait_calls}")
            if self.wait_calls == 1:
                raise primary
            return 1

        def kill(self) -> None:
            events.append("kill")

    process = Process()
    monkeypatch.setattr(
        codex_runtime_module.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    expected = (
        OSError if isinstance(primary, subprocess.TimeoutExpired) else type(primary)
    )
    with pytest.raises(expected):
        codex_runtime_module._run_bounded_smoke(
            (tmp_path / "codex.exe").resolve(), environment={}, timeout=2.0,
        )

    assert events[:3] == ["wait-1", "kill", "wait-2"]
    assert "close-pipe" in events
    assert process.stdout.closed
    assert not any(
        thread.name == "ones-dev-codex-smoke-output" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_smoke_reader_failure_is_propagated_after_process_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader_error = RuntimeError("reader failed")
    events: list[str] = []

    class Pipe:
        closed = False

        def read(self, size: int) -> bytes:
            raise reader_error

        def close(self) -> None:
            self.closed = True
            events.append("close-pipe")

    class Process:
        stdout = Pipe()

        def wait(self, timeout: float | None = None) -> int:
            events.append("wait")
            return 0

        def kill(self) -> None:
            events.append("kill")

    process = Process()
    monkeypatch.setattr(
        codex_runtime_module.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )

    with pytest.raises(RuntimeError) as caught:
        codex_runtime_module._run_bounded_smoke(
            (tmp_path / "codex.exe").resolve(), environment={}, timeout=2.0,
        )
    assert caught.value is reader_error
    assert "kill" in events and "close-pipe" in events
    assert not any(
        thread.name == "ones-dev-codex-smoke-output" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_smoke_primary_control_flow_beats_cleanup_memory_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = asyncio.CancelledError()

    class Process:
        stdout = io.BytesIO(b"")
        calls = 0

        def wait(self, timeout: float | None = None) -> int:
            self.calls += 1
            if self.calls == 1:
                raise primary
            return 1

        def kill(self) -> None:
            raise MemoryError("cleanup memory")

    monkeypatch.setattr(
        codex_runtime_module.subprocess,
        "Popen",
        lambda *args, **kwargs: Process(),
    )
    with pytest.raises(asyncio.CancelledError) as caught:
        codex_runtime_module._run_bounded_smoke(
            (tmp_path / "codex.exe").resolve(), environment={}, timeout=2.0,
        )
    assert caught.value is primary


def test_smoke_thread_start_failure_still_kills_reaps_and_closes_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = MemoryError("thread start failed")
    events: list[str] = []

    class Process:
        stdout = io.BytesIO(b"")

        def wait(self, timeout: float | None = None) -> int:
            events.append("wait")
            return 1

        def kill(self) -> None:
            events.append("kill")

    class Thread:
        name = "ones-dev-codex-smoke-output"

        def start(self) -> None:
            raise primary

        def join(self, timeout: float | None = None) -> None:
            events.append("join")

        def is_alive(self) -> bool:
            return False

    process = Process()
    monkeypatch.setattr(
        codex_runtime_module.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(
        codex_runtime_module.threading,
        "Thread",
        lambda *args, **kwargs: Thread(),
    )
    with pytest.raises(MemoryError) as caught:
        codex_runtime_module._run_bounded_smoke(
            (tmp_path / "codex.exe").resolve(), environment={}, timeout=2.0,
        )
    assert caught.value is primary
    assert events[:2] == ["kill", "wait"]
    assert "join" in events
    assert process.stdout.closed


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


@pytest.mark.parametrize("marker_kind", ["directory", "file"])
def test_default_roots_protect_nearest_repository_root_from_subdirectory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker_kind: str,
) -> None:
    repository = (tmp_path / f"external-{marker_kind}-repository").resolve()
    cwd = repository / "nested" / "work"
    root = repository / "sibling-install"
    cwd.mkdir(parents=True)
    root.mkdir()
    marker = repository / ".git"
    if marker_kind == "directory":
        marker.mkdir()
    else:
        marker.write_text("gitdir: ../metadata/worktree", encoding="utf-8")
    monkeypatch.chdir(cwd)
    adapter = FakeRuntimeAdapter(root)

    with pytest.raises(OSError, match="^native Codex payload is unavailable$"):
        discover_locked_native_codex(
            which=lambda name: str(root / "codex.cmd"),
            _adapter=adapter,
        )

    assert repository in adapter.marker_calls
    assert adapter.closed == [71]


def test_default_roots_protect_outer_repository_beyond_nested_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    outer_repository = (tmp_path / "outer-repository").resolve()
    inner_repository = outer_repository / "nested-repository"
    cwd = inner_repository / "work"
    root = outer_repository / "sibling-install"
    cwd.mkdir(parents=True)
    root.mkdir()
    (outer_repository / ".git").mkdir()
    (inner_repository / ".git").write_text(
        "gitdir: ../metadata/nested", encoding="utf-8"
    )
    monkeypatch.chdir(cwd)
    adapter = FakeRuntimeAdapter(root)

    with pytest.raises(OSError, match="^native Codex payload is unavailable$"):
        discover_locked_native_codex(
            which=lambda name: str(root / "codex.cmd"),
            _adapter=adapter,
        )

    assert inner_repository in adapter.marker_calls
    assert outer_repository in adapter.marker_calls
    assert adapter.closed == [71]


def test_default_roots_collect_every_repository_marker_on_physical_parent_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    outer_repository = (tmp_path / "outer-collection-repository").resolve()
    inner_repository = outer_repository / "nested-collection-repository"
    cwd = inner_repository / "work"
    cwd.mkdir(parents=True)
    (outer_repository / ".git").mkdir()
    (inner_repository / ".git").mkdir()
    monkeypatch.chdir(cwd)
    adapter = FakeRuntimeAdapter(tmp_path / "external-install")

    roots = codex_runtime_module._current_repository_roots(adapter)

    assert inner_repository in roots
    assert outer_repository in roots
    assert inner_repository in adapter.marker_calls
    assert outer_repository in adapter.marker_calls


def test_outer_repository_marker_permission_error_after_inner_marker_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    outer_repository = (tmp_path / "outer-permission-repository").resolve()
    inner_repository = outer_repository / "nested-permission-repository"
    cwd = inner_repository / "work"
    root = (tmp_path / "external-install").resolve()
    cwd.mkdir(parents=True)
    root.mkdir()
    (outer_repository / ".git").mkdir()
    (inner_repository / ".git").mkdir()
    monkeypatch.chdir(cwd)
    adapter = FakeRuntimeAdapter(root)
    adapter.marker_errors[outer_repository] = PermissionError(
        "outer-marker-permission-canary"
    )

    with pytest.raises(OSError, match="^native Codex payload is unavailable$") as caught:
        discover_locked_native_codex(
            which=lambda name: str(root / "codex.cmd"),
            _adapter=adapter,
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert outer_repository in adapter.marker_calls
    assert adapter.closed == [71]


def test_no_repository_marker_protects_only_physical_cwd_not_volume_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = (tmp_path / "markerless" / "nested").resolve()
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    adapter = FakeRuntimeAdapter(tmp_path / "external-install")

    roots = codex_runtime_module._current_repository_roots(adapter)

    assert cwd in roots
    assert Path(cwd.anchor) not in roots


@pytest.mark.parametrize("marker_error", [
    PermissionError("marker-permission-canary"),
    OSError("marker-replacement-canary"),
])
def test_default_repository_marker_error_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker_error: OSError,
) -> None:
    repository = (tmp_path / "external-racing-repository").resolve()
    cwd = repository / "nested"
    root = repository / "sibling-install"
    cwd.mkdir(parents=True)
    root.mkdir()
    monkeypatch.chdir(cwd)
    adapter = FakeRuntimeAdapter(root)
    adapter.marker_errors[repository] = marker_error

    with pytest.raises(OSError, match="^native Codex payload is unavailable$") as caught:
        discover_locked_native_codex(
            which=lambda name: str(root / "codex.cmd"),
            _adapter=adapter,
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert adapter.closed == [71]


def test_default_roots_follow_physical_cwd_alias_to_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lexical_cwd = (tmp_path / "junction-alias" / "nested").resolve()
    repository = (tmp_path / "physical-repository").resolve()
    physical_cwd = repository / "nested"
    root = repository / "sibling-install"
    lexical_cwd.mkdir(parents=True)
    physical_cwd.mkdir(parents=True)
    root.mkdir()
    (repository / ".git").mkdir()
    monkeypatch.chdir(lexical_cwd)
    adapter = FakeRuntimeAdapter(root)
    adapter.resolved_repository_path = physical_cwd
    original_same_file = adapter.same_file

    def alias_aware_same_file(left: Path, right: Path) -> bool:
        if {_normalized(left), _normalized(right)} == {
            _normalized(lexical_cwd),
            _normalized(physical_cwd),
        }:
            return True
        return original_same_file(left, right)

    adapter.same_file = alias_aware_same_file  # type: ignore[method-assign]

    with pytest.raises(OSError, match="^native Codex payload is unavailable$"):
        discover_locked_native_codex(
            which=lambda name: str(root / "codex.cmd"),
            _adapter=adapter,
        )

    assert repository in adapter.marker_calls
    assert adapter.closed == [71]


def test_default_repository_parent_chain_identity_race_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = (tmp_path / "racing-parent-repository").resolve()
    cwd = repository / "nested" / "work"
    root = (tmp_path / "external-install").resolve()
    cwd.mkdir(parents=True)
    root.mkdir()
    (repository / ".git").mkdir()
    monkeypatch.chdir(cwd)
    adapter = FakeRuntimeAdapter(root)
    adapter.repository_identity_race = repository / "nested"

    with pytest.raises(OSError, match="^native Codex payload is unavailable$") as caught:
        discover_locked_native_codex(
            which=lambda name: str(root / "codex.cmd"),
            _adapter=adapter,
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert adapter.closed == [71]


def test_default_repository_marker_identity_race_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = (tmp_path / "racing-marker-repository").resolve()
    cwd = repository / "nested"
    root = (tmp_path / "external-install").resolve()
    cwd.mkdir(parents=True)
    root.mkdir()
    (repository / ".git").mkdir()
    monkeypatch.chdir(cwd)
    adapter = FakeRuntimeAdapter(root)
    adapter.marker_identity_race = repository

    with pytest.raises(OSError, match="^native Codex payload is unavailable$") as caught:
        discover_locked_native_codex(
            which=lambda name: str(root / "codex.cmd"),
            _adapter=adapter,
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert adapter.closed == [71]


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


@pytest.mark.parametrize("primary_type", [AssertionError, TypeError, AttributeError])
@pytest.mark.parametrize("cleanup_type", _PRIORITY_FAILURES)
def test_close_memory_or_control_flow_beats_internal_primary_failure(
    primary_type: type[Exception],
    cleanup_type: type[BaseException],
) -> None:
    root = Path("C:/npm")
    adapter = FakeRuntimeAdapter(root)
    adapter.verify_error = primary_type("internal-primary-canary")
    cleanup = cleanup_type("cleanup-control")
    adapter.close_error = cleanup

    with pytest.raises(cleanup_type) as caught:
        _discover(root, adapter)

    assert caught.value is cleanup
    assert cleanup.__cause__ is None
    assert cleanup.__context__ is None
    assert adapter.closed == [71]


@pytest.mark.parametrize("primary_type", [AssertionError, TypeError, AttributeError])
def test_internal_primary_failure_beats_ordinary_close_failure(
    primary_type: type[Exception],
) -> None:
    root = Path("C:/npm")
    adapter = FakeRuntimeAdapter(root)
    primary = primary_type("internal-primary-canary")
    adapter.verify_error = primary
    adapter.close_error = OSError("ordinary-close-canary")

    with pytest.raises(primary_type) as caught:
        _discover(root, adapter)

    assert caught.value is primary
    assert primary.__cause__ is None
    assert primary.__context__ is None
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


@pytest.mark.skipif(os.name != "nt", reason="WinTrust API is Windows-only")
@pytest.mark.parametrize("primary_type", [AssertionError, TypeError, AttributeError])
@pytest.mark.parametrize("cleanup_type", _PRIORITY_FAILURES)
def test_wintrust_close_memory_or_control_beats_internal_signer_failure(
    monkeypatch: pytest.MonkeyPatch,
    primary_type: type[Exception],
    cleanup_type: type[BaseException],
) -> None:
    actions: list[int] = []
    primary = primary_type("internal-signer-canary")
    cleanup = cleanup_type("trust-close-control")

    def fake_winverifytrust(hwnd: object, action: object, data: object) -> int:
        trust_data = codex_runtime_module.ctypes.cast(
            data,
            codex_runtime_module.ctypes.POINTER(codex_runtime_module._WINTRUST_DATA),
        ).contents
        actions.append(trust_data.dwStateAction)
        if trust_data.dwStateAction == 1:
            trust_data.hWVTStateData = 99
            return 0
        raise cleanup

    def fail_signer(state: object) -> str:
        raise primary

    monkeypatch.setattr(
        codex_runtime_module._wintrust, "WinVerifyTrust", fake_winverifytrust
    )
    monkeypatch.setattr(codex_runtime_module, "_publisher_from_trust_state", fail_signer)

    with pytest.raises(cleanup_type) as caught:
        codex_runtime_module._verify_wintrust_publisher(73, Path("C:/fixed/codex.exe"))

    assert caught.value is cleanup
    assert cleanup.__cause__ is None
    assert cleanup.__context__ is None
    assert actions == [1, 2]


@pytest.mark.skipif(os.name != "nt", reason="WinTrust API is Windows-only")
@pytest.mark.parametrize("primary_type", [AssertionError, TypeError, AttributeError])
def test_wintrust_internal_signer_failure_beats_ordinary_close_failure(
    monkeypatch: pytest.MonkeyPatch,
    primary_type: type[Exception],
) -> None:
    actions: list[int] = []
    primary = primary_type("internal-signer-canary")

    def fake_winverifytrust(hwnd: object, action: object, data: object) -> int:
        trust_data = codex_runtime_module.ctypes.cast(
            data,
            codex_runtime_module.ctypes.POINTER(codex_runtime_module._WINTRUST_DATA),
        ).contents
        actions.append(trust_data.dwStateAction)
        if trust_data.dwStateAction == 1:
            trust_data.hWVTStateData = 99
            return 0
        raise OSError("ordinary-close-canary")

    def fail_signer(state: object) -> str:
        raise primary

    monkeypatch.setattr(
        codex_runtime_module._wintrust, "WinVerifyTrust", fake_winverifytrust
    )
    monkeypatch.setattr(codex_runtime_module, "_publisher_from_trust_state", fail_signer)

    with pytest.raises(primary_type) as caught:
        codex_runtime_module._verify_wintrust_publisher(73, Path("C:/fixed/codex.exe"))

    assert caught.value is primary
    assert primary.__cause__ is None
    assert primary.__context__ is None
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
