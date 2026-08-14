"""Discovery and locking for the signed native Codex npm payload on Windows."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import stat
import subprocess
import threading
import uuid
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


class _CacheRuntimeAdapter(Protocol):
    def validate_cache_ancestor_chain(self, root: Path) -> None: ...

    def prepare_private_directory(self, path: Path) -> Path: ...

    def validate_private_directory(self, path: Path) -> None: ...

    def protect_private_file(self, path: Path) -> None: ...

    def inspect_private_executable(
        self, path: Path,
    ) -> tuple[NativeCodexIdentity, str]: ...

    def validate_private_file(self, path: Path) -> tuple[int, int]: ...

    def read_private_text(self, path: Path) -> str: ...

    def fsync_directory(self, path: Path) -> None: ...

    def smoke(
        self, executable: Path, *, environment: dict[str, str], timeout: float,
    ) -> None: ...


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

    def current_identity(self) -> NativeCodexIdentity:
        if self._closed:
            raise ValueError("native Codex payload is closed")
        return self._adapter.identity(self.descriptor)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._adapter.close(self.descriptor)


_RUNTIME_ATTESTATION_NONCE = object()
_RUNTIME_ATTESTATION_SECRET = secrets.token_bytes(32)


def _runtime_attestation_mac(
    path: Path,
    identity: NativeCodexIdentity,
    sha256: str,
    cache_root: Path,
) -> bytes:
    snapshot = json.dumps(
        [
            "prepared-codex-runtime-v1",
            str(cache_root),
            str(path),
            sha256,
            identity.volume_serial,
            identity.file_index,
            identity.size,
            identity.mtime_ns,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8", "strict")
    return hmac.digest(_RUNTIME_ATTESTATION_SECRET, snapshot, "sha256")


@dataclass(frozen=True, slots=True, init=False, repr=False)
class _PreparedCodexRuntime:
    path: Path
    identity: NativeCodexIdentity
    sha256: str
    _cache_root: Path = field(repr=False)
    _seal: bytes = field(repr=False)
    _nonce: object = field(repr=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("verified Codex runtimes cannot be constructed directly")

    @classmethod
    def _issue(
        cls,
        path: Path,
        identity: NativeCodexIdentity,
        sha256: str,
        cache_root: Path,
        *,
        nonce: object,
    ) -> _PreparedCodexRuntime:
        if nonce is not _RUNTIME_ATTESTATION_NONCE:
            raise TypeError("invalid Codex runtime attestation")
        canonical = path.resolve(strict=True)
        canonical_root = cache_root.resolve(strict=True)
        if (
            canonical.name.casefold() != "codex.exe"
            or not _is_sha256(sha256)
            or type(identity) is not NativeCodexIdentity
            or any(
                type(value) is not int
                for value in (
                    identity.volume_serial,
                    identity.file_index,
                    identity.size,
                    identity.mtime_ns,
                )
            )
            or identity.size < 0
            or canonical.parent.name != sha256
            or canonical.parent.parent != canonical_root
        ):
            raise ValueError("invalid Codex runtime attestation")
        seal = _runtime_attestation_mac(
            canonical, identity, sha256, canonical_root,
        )
        instance = object.__new__(cls)
        object.__setattr__(instance, "path", canonical)
        object.__setattr__(instance, "identity", identity)
        object.__setattr__(instance, "sha256", sha256)
        object.__setattr__(instance, "_cache_root", canonical_root)
        object.__setattr__(instance, "_seal", seal)
        object.__setattr__(instance, "_nonce", nonce)
        return instance

    def _is_attested(self) -> bool:
        try:
            if (
                self._nonce is not _RUNTIME_ATTESTATION_NONCE
                or not isinstance(self.path, Path)
                or type(self.identity) is not NativeCodexIdentity
                or not _is_sha256(self.sha256)
                or not isinstance(self._cache_root, Path)
                or type(self._seal) is not bytes
                or self.path.resolve(strict=True) != self.path
                or self._cache_root.resolve(strict=True) != self._cache_root
                or self.path.name.casefold() != "codex.exe"
                or self.path.parent.name != self.sha256
                or self.path.parent.parent != self._cache_root
            ):
                return False
            expected = _runtime_attestation_mac(
                self.path, self.identity, self.sha256, self._cache_root,
            )
            return hmac.compare_digest(self._seal, expected)
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    def __copy__(self) -> _PreparedCodexRuntime:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> _PreparedCodexRuntime:
        return self

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("verified Codex runtimes cannot be serialized")


_CACHE_SCHEMA_KEYS = frozenset(
    {"publisher", "schema_version", "sha256", "size", "target"}
)
_QUARANTINE_SCHEMA_KEYS = frozenset({"schema_version", "sha256"})
_QUARANTINE_PREFIX = ".quarantine-"
_QUARANTINE_SUFFIX = ".json"


class CodexRuntimePreparer:
    """Stage a verified native payload into a private content-addressed cache."""

    def __init__(
        self,
        *,
        cache_root: Path | None = None,
        discover: Callable[[], LockedNativeCodex] | None = None,
        _cache_adapter: _CacheRuntimeAdapter | None = None,
        chunk_size: int = 1024 * 1024,
        smoke_timeout: float = 5.0,
    ) -> None:
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError("chunk size must be a positive integer")
        if not isinstance(smoke_timeout, (int, float)) or smoke_timeout <= 0:
            raise ValueError("smoke timeout must be positive")
        if cache_root is None:
            local_app_data = os.environ.get("LOCALAPPDATA")
            if not local_app_data:
                raise OSError("private Codex runtime is unavailable")
            cache_root = Path(local_app_data) / "ones-dev" / "codex-runtime"
        if "\x00" in os.fspath(cache_root):
            raise OSError("private Codex runtime is unavailable")
        self._cache_root = Path(cache_root)
        self._discover = discover
        self._cache_adapter = _cache_adapter
        self._chunk_size = chunk_size
        self._smoke_timeout = float(smoke_timeout)

    def prepare(self) -> Path:
        adapter = self._cache_adapter or _WindowsCacheRuntimeAdapter()
        root = self._prepare_root(adapter)
        self._cleanup_stale_owned_entries(root, adapter)
        quarantined, rebuildable = self._scan_quarantines(root, adapter)
        cached = self._valid_cached_executables(
            root, adapter, excluded=quarantined,
        )
        discovery = self._discover or discover_locked_native_codex
        try:
            locked = discovery()
        except BaseException as error:
            if _is_priority_failure(error) or not isinstance(error, OSError):
                raise
            if cached:
                return cached[sorted(cached)[-1]]
            raise OSError("private Codex runtime is unavailable") from None
        result: Path | None = None
        primary: BaseException | None = None
        primary_traceback = None
        try:
            try:
                source_sha256 = self._hash_locked_source(locked)
                matching = cached.get(source_sha256)
                if source_sha256 in quarantined and source_sha256 not in rebuildable:
                    raise OSError("private Codex runtime is quarantined")
                result = (
                    matching
                    if matching is not None
                    else self._stage_locked_source(
                        root,
                        locked,
                        source_sha256,
                        adapter,
                        quarantine=rebuildable.get(source_sha256),
                    )
                )
            except BaseException as error:
                if _is_priority_failure(error) or not isinstance(error, OSError):
                    raise
                if cached:
                    result = cached[sorted(cached)[-1]]
                else:
                    raise OSError(
                        "private Codex runtime is unavailable"
                    ) from None
        except BaseException as error:
            primary = error
            primary_traceback = error.__traceback__
        cleanup: BaseException | None = None
        cleanup_traceback = None
        try:
            locked.close()
        except BaseException as error:
            cleanup = error
            cleanup_traceback = error.__traceback__
        if primary is not None:
            if _is_priority_failure(primary):
                raise primary.with_traceback(primary_traceback)
            if cleanup is not None and _is_priority_failure(cleanup):
                raise cleanup.with_traceback(cleanup_traceback)
            raise primary.with_traceback(primary_traceback)
        if cleanup is not None:
            raise cleanup.with_traceback(cleanup_traceback)
        assert result is not None
        return result

    def prepare_verified(self) -> _PreparedCodexRuntime:
        path = self.prepare()
        adapter = self._cache_adapter or _WindowsCacheRuntimeAdapter()
        adapter.validate_private_file(path)
        identity, publisher = adapter.inspect_private_executable(path)
        sha256 = path.parent.name
        if (
            publisher != OPENAI_AUTHENTICODE_PUBLISHER
            or identity.size != path.stat(follow_symlinks=False).st_size
            or _hash_file(path) != sha256
        ):
            raise OSError("private Codex runtime is unavailable")
        final_identity, final_publisher = adapter.inspect_private_executable(path)
        if final_identity != identity or final_publisher != publisher:
            raise OSError("private Codex runtime is unavailable")
        return _PreparedCodexRuntime._issue(
            path,
            identity,
            sha256,
            path.parent.parent,
            nonce=_RUNTIME_ATTESTATION_NONCE,
        )

    def _hash_locked_source(self, locked: LockedNativeCodex) -> str:
        digest = hashlib.sha256()
        copied = 0
        locked.rewind()
        while True:
            chunk = locked.read_chunk(self._chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            copied += len(chunk)
        if copied != locked.size or locked.current_identity() != locked.identity:
            raise OSError("native Codex payload changed")
        return digest.hexdigest()

    def _stage_locked_source(
        self,
        root: Path,
        locked: LockedNativeCodex,
        source_sha256: str,
        adapter: _CacheRuntimeAdapter,
        *,
        quarantine: Path | None = None,
    ) -> Path:
        staging = root / f".staging-{uuid.uuid4().hex}"
        final_directory = root / source_sha256
        write_ahead = root / (
            f"{_QUARANTINE_PREFIX}{source_sha256}{_QUARANTINE_SUFFIX}"
        )
        published_final: Path | None = None
        write_ahead_started = False
        clear_started = False
        final_validated = False
        result: Path | None = None
        primary: BaseException | None = None
        primary_traceback = None
        try:
            staging.mkdir(exist_ok=False)
            adapter.prepare_private_directory(staging)
            temporary = staging / f"codex-{uuid.uuid4().hex}.tmp"
            digest = hashlib.sha256()
            copied = 0
            locked.rewind()
            with temporary.open("xb", buffering=0) as destination:
                while True:
                    chunk = locked.read_chunk(self._chunk_size)
                    if not chunk:
                        break
                    _write_all(destination, chunk)
                    digest.update(chunk)
                    copied += len(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            if copied != locked.size or locked.current_identity() != locked.identity:
                raise OSError("native Codex payload changed")
            adapter.protect_private_file(temporary)
            sha256 = digest.hexdigest()
            if sha256 != source_sha256:
                raise OSError("native Codex payload changed")
            executable = staging / "codex.exe"
            os.replace(temporary, executable)
            adapter.fsync_directory(staging)
            self._validate_executable(
                executable, sha256=sha256, size=copied, adapter=adapter,
            )
            manifest = {
                "publisher": OPENAI_AUTHENTICODE_PUBLISHER,
                "schema_version": 1,
                "sha256": sha256,
                "size": copied,
                "target": "codex.exe",
            }
            manifest_temp = staging / f"manifest-{uuid.uuid4().hex}.tmp"
            with manifest_temp.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(manifest, stream, ensure_ascii=True, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            adapter.protect_private_file(manifest_temp)
            os.replace(manifest_temp, staging / "manifest.json")
            adapter.fsync_directory(staging)
            self._validate_staging_ready(
                staging, sha256=sha256, size=copied, adapter=adapter,
            )
            adapter.smoke(
                executable,
                environment=_sanitized_smoke_environment(),
                timeout=self._smoke_timeout,
            )
            write_ahead_started = True
            self._publish_quarantine(root, sha256, adapter)
            if final_directory.exists():
                adapter.fsync_directory(root)
                try:
                    result = self._validate_cache_directory(
                        final_directory, sha256, adapter, smoke=False,
                    )
                except BaseException as error:
                    if _is_priority_failure(error) or not isinstance(
                        error, (OSError, ValueError, json.JSONDecodeError)
                    ):
                        raise
                    if quarantine is None:
                        raise
                    _remove_owned_tree(final_directory)
                    adapter.fsync_directory(root)
                    os.replace(staging, final_directory)
                    published_final = final_directory
                    adapter.fsync_directory(root)
                    result = self._validate_cache_directory(
                        final_directory, sha256, adapter, smoke=False,
                    )
            else:
                os.replace(staging, final_directory)
                published_final = final_directory
                adapter.fsync_directory(root)
                result = self._validate_cache_directory(
                    final_directory, sha256, adapter, smoke=False,
                )
            final_validated = True
            try:
                initial_identity = adapter.validate_private_file(write_ahead)
            except FileNotFoundError:
                # A concurrent preparer may have durably validated this exact
                # content-addressed final and removed the shared marker.
                pass
            else:
                record = _read_strict_manifest(write_ahead, adapter)
                if (
                    adapter.validate_private_file(write_ahead) != initial_identity
                    or not _is_valid_quarantine(record, sha256)
                ):
                    raise OSError("invalid private runtime quarantine")
                clear_started = True
                write_ahead.unlink()
                adapter.fsync_directory(root)
        except BaseException as error:
            primary = error
            primary_traceback = error.__traceback__
        quarantine_ready = False
        marker_missing_after_concurrent_commit = False
        marker_probe_error: BaseException | None = None
        marker_exists = True
        if write_ahead_started and primary is not None and not clear_started:
            try:
                marker_exists = write_ahead.exists()
            except BaseException as error:
                marker_probe_error = error
        concurrent_probe_error: BaseException | None = None
        if (
            write_ahead_started
            and primary is not None
            and not clear_started
            and marker_probe_error is None
            and not marker_exists
            and final_directory.exists()
        ):
            try:
                adapter.fsync_directory(root)
                self._validate_cache_directory(
                    final_directory, source_sha256, adapter, smoke=False,
                )
            except BaseException as error:
                concurrent_probe_error = error
            else:
                marker_missing_after_concurrent_commit = True
        if (
            write_ahead_started
            and primary is not None
            and not marker_missing_after_concurrent_commit
        ):
            try:
                self._publish_quarantine(root, source_sha256, adapter)
                quarantine_ready = True
            except BaseException as error:
                quarantine_error = error
            else:
                quarantine_error = None
        else:
            quarantine_error = None
        cleanup_paths = [staging]
        if (
            quarantine_ready
            and published_final is not None
            and primary is not None
            and not clear_started
        ):
            cleanup_paths.append(published_final)
        cleanup = _attempt_owned_cleanup(tuple(cleanup_paths))
        cleanup = _prefer_cleanup_error(cleanup, marker_probe_error)
        cleanup = _prefer_cleanup_error(cleanup, concurrent_probe_error)
        cleanup = _prefer_cleanup_error(cleanup, quarantine_error)
        if (
            published_final is not None
            and primary is not None
            and quarantine_ready
            and not clear_started
        ):
            if published_final.exists():
                retry_error = _attempt_owned_cleanup((published_final,))
                cleanup = _prefer_cleanup_error(cleanup, retry_error)
            try:
                adapter.fsync_directory(root)
            except BaseException as error:
                cleanup = _prefer_cleanup_error(cleanup, error)
            if published_final.exists():
                cleanup = _prefer_cleanup_error(
                    cleanup,
                    OSError("attempted private runtime could not be removed"),
                )
        cleanup_traceback = cleanup.__traceback__ if cleanup is not None else None
        if primary is not None:
            if _is_priority_failure(primary):
                raise primary.with_traceback(primary_traceback)
            if cleanup is not None and _is_priority_failure(cleanup):
                raise cleanup.with_traceback(cleanup_traceback)
            raise primary.with_traceback(primary_traceback)
        if cleanup is not None:
            raise cleanup.with_traceback(cleanup_traceback)
        assert result is not None
        return result

    def _prepare_root(self, adapter: _CacheRuntimeAdapter) -> Path:
        root = self._cache_root
        if not root.is_absolute():
            raise OSError("private Codex runtime is unavailable")
        parent = root.parent
        if parent.parent == parent:
            raise OSError("private Codex runtime is unavailable")
        adapter.validate_cache_ancestor_chain(root)
        adapter.prepare_private_directory(parent)
        prepared = adapter.prepare_private_directory(root)
        adapter.validate_cache_ancestor_chain(prepared)
        adapter.validate_private_directory(parent)
        adapter.validate_private_directory(prepared)
        return prepared

    def _valid_cached_executables(
        self,
        root: Path,
        adapter: _CacheRuntimeAdapter,
        *,
        excluded: set[str] | frozenset[str] = frozenset(),
    ) -> dict[str, Path]:
        valid: dict[str, Path] = {}
        try:
            candidates = tuple(root.iterdir())
        except OSError:
            raise OSError("private Codex runtime is unavailable") from None
        for directory in candidates:
            digest = directory.name
            if not (
                len(digest) == 64
                and all(character in "0123456789abcdef" for character in digest)
            ) or digest in excluded:
                continue
            try:
                valid[digest] = self._validate_cache_directory(
                    directory, digest, adapter,
                )
            except BaseException as error:
                if _is_priority_failure(error) or not isinstance(
                    error, (OSError, ValueError, json.JSONDecodeError)
                ):
                    raise
        return valid

    def _scan_quarantines(
        self, root: Path, adapter: _CacheRuntimeAdapter,
    ) -> tuple[set[str], dict[str, Path]]:
        blocked: set[str] = set()
        valid: dict[str, Path] = {}
        for candidate in tuple(root.iterdir()):
            digest = _quarantine_digest(candidate.name)
            if digest is None:
                continue
            blocked.add(digest)
            try:
                initial_identity = adapter.validate_private_file(candidate)
                record = _read_strict_manifest(candidate, adapter)
                if (
                    adapter.validate_private_file(candidate) != initial_identity
                    or not _is_valid_quarantine(record, digest)
                ):
                    raise OSError("invalid private runtime quarantine")
                valid[digest] = candidate
            except BaseException as error:
                if _is_priority_failure(error) or not isinstance(
                    error, (OSError, ValueError, json.JSONDecodeError)
                ):
                    raise
        return blocked, valid

    def _publish_quarantine(
        self, root: Path, digest: str, adapter: _CacheRuntimeAdapter,
    ) -> Path:
        if not _is_sha256(digest):
            raise OSError("invalid private runtime quarantine")
        final = root / f"{_QUARANTINE_PREFIX}{digest}{_QUARANTINE_SUFFIX}"
        temporary = root / (
            f"{_QUARANTINE_PREFIX}{digest}-{uuid.uuid4().hex}.tmp"
        )
        result: Path | None = None
        primary: BaseException | None = None
        primary_traceback = None
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    {"schema_version": 1, "sha256": digest},
                    stream,
                    ensure_ascii=True,
                    sort_keys=True,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            adapter.protect_private_file(temporary)
            os.replace(temporary, final)
            adapter.fsync_directory(root)
            initial_identity = adapter.validate_private_file(final)
            record = _read_strict_manifest(final, adapter)
            if (
                adapter.validate_private_file(final) != initial_identity
                or not _is_valid_quarantine(record, digest)
            ):
                raise OSError("invalid private runtime quarantine")
            result = final
        except BaseException as error:
            primary = error
            primary_traceback = error.__traceback__
        cleanup: BaseException | None = None
        cleanup_traceback = None
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except BaseException as error:
            cleanup = error
            cleanup_traceback = error.__traceback__
        if primary is not None:
            if _is_priority_failure(primary):
                raise primary.with_traceback(primary_traceback)
            if cleanup is not None and _is_priority_failure(cleanup):
                raise cleanup.with_traceback(cleanup_traceback)
            raise primary.with_traceback(primary_traceback)
        if cleanup is not None:
            raise cleanup.with_traceback(cleanup_traceback)
        assert result is not None
        return result

    def _validate_staging_ready(
        self,
        directory: Path,
        *,
        sha256: str,
        size: int,
        adapter: _CacheRuntimeAdapter,
    ) -> None:
        adapter.validate_private_directory(directory)
        manifest_path = directory / "manifest.json"
        initial_identity = adapter.validate_private_file(manifest_path)
        manifest = _read_strict_manifest(manifest_path, adapter)
        if (
            adapter.validate_private_file(manifest_path) != initial_identity
            or not _is_valid_manifest(manifest, sha256)
            or manifest["size"] != size
        ):
            raise OSError("private Codex runtime is unavailable")
        self._validate_executable(
            directory / "codex.exe",
            sha256=sha256,
            size=size,
            adapter=adapter,
        )

    def _validate_cache_directory(
        self,
        directory: Path,
        digest: str,
        adapter: _CacheRuntimeAdapter,
        *,
        smoke: bool = True,
    ) -> Path:
        adapter.validate_private_directory(directory)
        manifest_path = directory / "manifest.json"
        initial_manifest_identity = adapter.validate_private_file(manifest_path)
        manifest = _read_strict_manifest(manifest_path, adapter)
        if adapter.validate_private_file(manifest_path) != initial_manifest_identity:
            raise OSError("manifest changed")
        if not _is_valid_manifest(manifest, digest):
            raise OSError("invalid manifest")
        executable = directory / "codex.exe"
        self._validate_executable(
            executable,
            sha256=digest,
            size=manifest["size"],
            adapter=adapter,
        )
        if smoke:
            adapter.smoke(
                executable,
                environment=_sanitized_smoke_environment(),
                timeout=self._smoke_timeout,
            )
        resolved = executable.resolve(strict=True)
        if not resolved.is_relative_to(self._cache_root.resolve(strict=True)):
            raise OSError("cache target escaped")
        return resolved

    def _cleanup_stale_owned_entries(
        self, root: Path, adapter: _CacheRuntimeAdapter,
    ) -> None:
        for candidate in tuple(root.iterdir()):
            name = candidate.name
            random_stage = (
                name.startswith(".staging-")
                and len(name) == len(".staging-") + 32
                and all(character in "0123456789abcdef" for character in name[9:])
            )
            deterministic_name = (
                len(name) == 64
                and all(character in "0123456789abcdef" for character in name)
            )
            if random_stage:
                continue
            if not deterministic_name:
                continue
            try:
                adapter.validate_private_directory(candidate)
                try:
                    (candidate / "manifest.json").lstat()
                except FileNotFoundError:
                    pass
                else:
                    continue
                children = tuple(candidate.iterdir())
                if any(not _is_owned_staging_child(child) for child in children):
                    continue
                for child in children:
                    adapter.validate_private_file(child)
                cleanup = _attempt_owned_cleanup((candidate,))
                if cleanup is not None:
                    raise cleanup
                adapter.fsync_directory(root)
            except BaseException as error:
                if _is_priority_failure(error):
                    raise
                if not isinstance(error, (OSError, ValueError)):
                    raise

    @staticmethod
    def _validate_executable(
        executable: Path,
        *,
        sha256: str,
        size: int,
        adapter: _CacheRuntimeAdapter,
    ) -> None:
        adapter.validate_private_file(executable)
        first_identity, publisher = adapter.inspect_private_executable(executable)
        if (
            publisher != OPENAI_AUTHENTICODE_PUBLISHER
            or first_identity.size != size
            or _hash_file(executable) != sha256
        ):
            raise OSError("private Codex runtime is unavailable")
        final_identity, final_publisher = adapter.inspect_private_executable(executable)
        if final_identity != first_identity or final_publisher != publisher:
            raise OSError("private Codex runtime is unavailable")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _write_all(stream: object, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = stream.write(remaining)  # type: ignore[attr-defined]
        if type(written) is not int or written <= 0 or written > len(remaining):
            raise OSError("private Codex runtime write failed")
        remaining = remaining[written:]


def _is_owned_staging_child(path: Path) -> bool:
    name = path.name
    return (
        name in {"codex.exe", "manifest.json"}
        or name.startswith("codex-") and name.endswith(".tmp")
        or name.startswith("manifest-") and name.endswith(".tmp")
    )


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest member")
        result[key] = value
    return result


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _quarantine_digest(name: str) -> str | None:
    if not (
        name.startswith(_QUARANTINE_PREFIX)
        and name.endswith(_QUARANTINE_SUFFIX)
    ):
        return None
    digest = name[len(_QUARANTINE_PREFIX) : -len(_QUARANTINE_SUFFIX)]
    return digest if _is_sha256(digest) else None


def _invalid_json_constant(value: str) -> None:
    raise ValueError("invalid manifest constant")


def _read_strict_manifest(
    path: Path, adapter: _CacheRuntimeAdapter,
) -> dict[str, object]:
    metadata = path.stat(follow_symlinks=False)
    if metadata.st_size > 4096:
        raise ValueError("manifest is oversized")
    raw = adapter.read_private_text(path)
    parsed = json.loads(
        raw,
        object_pairs_hook=_strict_json_object,
        parse_constant=_invalid_json_constant,
    )
    if type(parsed) is not dict:
        raise ValueError("manifest must be an object")
    return parsed


def _is_valid_manifest(manifest: dict[str, object], directory_name: str) -> bool:
    return (
        frozenset(manifest) == _CACHE_SCHEMA_KEYS
        and type(manifest.get("schema_version")) is int
        and manifest["schema_version"] == 1
        and type(manifest.get("sha256")) is str
        and manifest["sha256"] == directory_name
        and type(manifest.get("publisher")) is str
        and manifest["publisher"] == OPENAI_AUTHENTICODE_PUBLISHER
        and type(manifest.get("target")) is str
        and manifest["target"] == "codex.exe"
        and type(manifest.get("size")) is int
        and manifest["size"] >= 0
    )


def _is_valid_quarantine(record: dict[str, object], digest: str) -> bool:
    return (
        frozenset(record) == _QUARANTINE_SCHEMA_KEYS
        and type(record.get("schema_version")) is int
        and record["schema_version"] == 1
        and type(record.get("sha256")) is str
        and record["sha256"] == digest
    )


def _sanitized_smoke_environment() -> dict[str, str]:
    keep = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in keep and type(value) is str and "\x00" not in value
    }


def _remove_owned_tree(path: Path) -> None:
    if not path.name.startswith(".staging-") and not (
        len(path.name) == 64
        and all(character in "0123456789abcdef" for character in path.name)
    ):
        return
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(metadata.st_mode) or attributes & reparse:
        if stat.S_ISDIR(metadata.st_mode):
            path.rmdir()
        else:
            path.unlink()
        return
    shutil.rmtree(path)


def _attempt_owned_cleanup(paths: tuple[Path, ...]) -> BaseException | None:
    first: BaseException | None = None
    priority: BaseException | None = None
    for path in paths:
        try:
            _remove_owned_tree(path)
        except BaseException as error:
            if first is None:
                first = error
            if priority is None and _is_priority_failure(error):
                priority = error
    return priority or first


def _prefer_cleanup_error(
    current: BaseException | None, candidate: BaseException | None,
) -> BaseException | None:
    if candidate is None:
        return current
    if current is None or (
        _is_priority_failure(candidate) and not _is_priority_failure(current)
    ):
        return candidate
    return current


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


def _current_repository_chain_roots(adapter: _RuntimeAdapter) -> tuple[Path, ...]:
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
    repository_roots: list[Path] = []
    while True:
        path_identity = adapter.repository_path_identity(current)
        marker = adapter.repository_marker(current)
        checked.append((current, path_identity, marker))
        if marker is not None:
            marker_kind, _ = marker
            if marker_kind not in {"directory", "file"}:
                raise OSError("invalid repository marker")
            repository_roots.append(current)
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
    return tuple(repository_roots) if repository_roots else (physical_cwd,)


def _current_repository_roots(adapter: _RuntimeAdapter) -> tuple[Path, ...]:
    worktree = Path(__file__).resolve(strict=True).parents[2]
    candidates = [worktree]
    if worktree.parent.name.casefold() == ".worktrees":
        candidates.append(worktree.parent.parent.resolve(strict=True))
    candidates.extend(_current_repository_chain_roots(adapter))
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
    _kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _kernel32.FlushFileBuffers.restype = wintypes.BOOL

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


_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"
_TRUSTED_INSTALLER_SID = (
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
)
_ANCESTOR_REPLACEMENT_RIGHTS = (
    0x00000040  # FILE_DELETE_CHILD
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x10000000  # GENERIC_ALL
    | 0x40000000  # GENERIC_WRITE
)


def _validate_windows_cache_ancestor_acl(
    *,
    owner: str,
    entries: tuple[tuple[str, int, int, int], ...],
    user_sid: str,
    dacl_protected: bool,
    trusted_installer_allowed: bool,
) -> None:
    if type(dacl_protected) is not bool or type(trusted_installer_allowed) is not bool:
        raise OSError("private runtime ancestor ACL is unsafe")
    trusted = {
        user_sid,
        _SYSTEM_SID,
        _ADMINISTRATORS_SID,
    }
    if trusted_installer_allowed:
        trusted.add(_TRUSTED_INSTALLER_SID)
    if owner not in trusted:
        raise OSError("private runtime ancestor owner is unsafe")
    for sid, mask, flags, ace_type in entries:
        if ace_type == 1:
            continue
        if ace_type != 0:
            raise OSError("private runtime ancestor ACE is unsafe")
        if flags & 0x08 or sid in trusted:
            continue
        if mask & _ANCESTOR_REPLACEMENT_RIGHTS:
            raise OSError("private runtime ancestor ACL is unsafe")


def _validate_windows_private_directory_acl(
    *,
    owner: str,
    entries: tuple[tuple[str, int, int, int], ...],
    user_sid: str,
    dacl_protected: bool,
) -> None:
    if type(dacl_protected) is not bool:
        raise OSError("private runtime directory ACL is unsafe")
    trusted = {user_sid, _SYSTEM_SID, _ADMINISTRATORS_SID}
    full_control = 0x001F01FF
    principals = {sid for sid, _, _, _ in entries}
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
        raise OSError("private runtime directory ACL is unsafe")


def _windows_current_profile_directory() -> Path:
    if os.name != "nt":
        raise OSError("Windows profile directory is unavailable")
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    userenv.GetUserProfileDirectoryW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    userenv.GetUserProfileDirectoryW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        length = wintypes.DWORD()
        userenv.GetUserProfileDirectoryW(token, None, ctypes.byref(length))
        if length.value == 0:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(length.value)
        if not userenv.GetUserProfileDirectoryW(
            token, buffer, ctypes.byref(length),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return Path(buffer.value).resolve(strict=True)
    finally:
        kernel.CloseHandle(token)


def _trusted_installer_allowed_for_ancestor(current: Path, root: Path) -> bool:
    if current.parent == current:
        return True
    try:
        canonical_profile = _windows_current_profile_directory()
        standard_local = (canonical_profile / "AppData" / "Local").resolve(
            strict=True,
        )
        return (
            root.parent.parent.resolve(strict=True) == standard_local
            and (current == standard_local or current in standard_local.parents)
        )
    except OSError:
        return False


class _WindowsCacheRuntimeAdapter:
    def __init__(self, *, _runtime_adapter: _RuntimeAdapter | None = None) -> None:
        if os.name != "nt":
            raise OSError("private Codex runtime is only available on Windows")
        self._runtime = _runtime_adapter or _WindowsRuntimeAdapter()

    def validate_cache_ancestor_chain(self, root: Path) -> None:
        from .private_paths import (
            _current_user_sid,
            _is_link_or_reparse,
            _windows_descriptor,
        )

        user_sid = _current_user_sid()
        current = root.parent.parent
        while True:
            before = current.stat(follow_symlinks=False)
            if _is_link_or_reparse(current) or not stat.S_ISDIR(before.st_mode):
                raise OSError("private runtime ancestor is unsafe")
            canonical = current.resolve(strict=True)
            canonical_stat = canonical.stat(follow_symlinks=False)
            if (before.st_dev, before.st_ino) != (
                canonical_stat.st_dev,
                canonical_stat.st_ino,
            ):
                raise OSError("private runtime ancestor is unstable")
            descriptor = _windows_descriptor(canonical)
            _validate_windows_cache_ancestor_acl(
                owner=descriptor[0],
                entries=descriptor[1],
                user_sid=user_sid,
                dacl_protected=descriptor[2],
                trusted_installer_allowed=(
                    _trusted_installer_allowed_for_ancestor(canonical, root)
                ),
            )
            after = current.stat(follow_symlinks=False)
            if (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or _windows_descriptor(canonical) != descriptor
            ):
                raise OSError("private runtime ancestor is unstable")
            if current.parent == current:
                return
            current = current.parent

    def prepare_private_directory(self, path: Path) -> Path:
        from .private_paths import PrivatePathError, prepare_private_directory

        try:
            return prepare_private_directory(path)
        except (OSError, PrivatePathError):
            raise OSError("private runtime directory is unsafe") from None

    def validate_private_directory(self, path: Path) -> None:
        from .private_paths import (
            PrivatePathError,
            _current_user_sid,
            _windows_descriptor,
            prepare_private_directory,
        )

        try:
            before = path.stat(follow_symlinks=False)
            prepared = prepare_private_directory(path)
            descriptor = _windows_descriptor(prepared)
            _validate_windows_private_directory_acl(
                owner=descriptor[0],
                entries=descriptor[1],
                user_sid=_current_user_sid(),
                dacl_protected=descriptor[2],
            )
            after = prepared.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(after.st_mode)
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or _windows_descriptor(prepared) != descriptor
            ):
                raise OSError("private runtime directory is unsafe")
        except (OSError, PrivatePathError):
            raise OSError("private runtime directory is unsafe") from None

    def protect_private_file(self, path: Path) -> None:
        from .setup_store import _protect_private_file

        _protect_private_file(path)

    def validate_private_file(self, path: Path) -> tuple[int, int]:
        from .setup_store import _validate_regular_file

        return _validate_regular_file(path)

    def read_private_text(self, path: Path) -> str:
        from .setup_store import _read_nofollow

        return _read_nofollow(path)

    def inspect_private_executable(
        self, path: Path,
    ) -> tuple[NativeCodexIdentity, str]:
        descriptor = self._runtime.open_locked(path)
        primary: BaseException | None = None
        primary_traceback = None
        result: tuple[NativeCodexIdentity, str] | None = None
        try:
            if not self._runtime.is_disk_regular_non_reparse(descriptor):
                raise OSError("private Codex executable is unsafe")
            initial = self._runtime.identity(descriptor)
            final_path = self._runtime.final_path(descriptor)
            if not self._runtime.same_file(final_path, path):
                raise OSError("private Codex executable changed")
            publisher = self._runtime.verify_publisher(descriptor, final_path)
            if publisher != OPENAI_AUTHENTICODE_PUBLISHER:
                raise OSError("private Codex publisher is invalid")
            if self._runtime.identity(descriptor) != initial:
                raise OSError("private Codex executable changed")
            result = initial, publisher
        except BaseException as error:
            primary = error
            primary_traceback = error.__traceback__
        cleanup: BaseException | None = None
        cleanup_traceback = None
        try:
            self._runtime.close(descriptor)
        except BaseException as error:
            cleanup = error
            cleanup_traceback = error.__traceback__
        if primary is not None:
            if _is_priority_failure(primary):
                raise primary.with_traceback(primary_traceback)
            if cleanup is not None and _is_priority_failure(cleanup):
                raise cleanup.with_traceback(cleanup_traceback)
            raise primary.with_traceback(primary_traceback)
        if cleanup is not None:
            raise cleanup.with_traceback(cleanup_traceback)
        assert result is not None
        return result

    def fsync_directory(self, path: Path) -> None:
        handle = _kernel32.CreateFileW(
            str(path),
            0x40000000,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            _raise_last_windows_error("could not open private runtime directory")
        descriptor = int(handle)
        primary: BaseException | None = None
        try:
            if not _kernel32.FlushFileBuffers(wintypes.HANDLE(descriptor)):
                _raise_last_windows_error("could not flush private runtime directory")
        except BaseException as error:
            primary = error
        try:
            self._runtime.close(descriptor)
        except BaseException:
            if primary is None:
                raise
        if primary is not None:
            raise primary

    def smoke(
        self, executable: Path, *, environment: dict[str, str], timeout: float,
    ) -> None:
        _run_bounded_smoke(executable, environment=environment, timeout=timeout)


def _run_bounded_smoke(
    executable: Path, *, environment: dict[str, str], timeout: float,
) -> None:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [str(executable), "--version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        env=environment,
        creationflags=creationflags,
    )
    output = bytearray()
    overflow = threading.Event()
    reader_errors: list[BaseException] = []

    def drain() -> None:
        try:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    return
                if len(output) + len(chunk) > 64 * 1024:
                    overflow.set()
                    return
                output.extend(chunk)
        except BaseException as error:
            reader_errors.append(error)

    reader: threading.Thread | None = None
    try:
        reader = threading.Thread(
            target=drain,
            daemon=True,
            name="ones-dev-codex-smoke-output",
        )
        reader.start()
    except BaseException as error:
        cleanup_errors = _cleanup_failed_smoke_start(process, reader)
        selected = _select_smoke_failure(error, [], cleanup_errors)
        assert selected is not None
        raise selected
    assert reader is not None
    primary: BaseException | None = None
    primary_traceback = None
    returncode: int | None = None
    try:
        returncode = process.wait(timeout=timeout)
    except BaseException as error:
        primary = (
            OSError("private Codex runtime smoke test failed")
            if isinstance(error, subprocess.TimeoutExpired)
            else error
        )
        primary_traceback = primary.__traceback__

    if primary is None:
        try:
            reader.join(timeout=1.0)
        except BaseException as error:
            primary = error
            primary_traceback = error.__traceback__
    failed = (
        primary is not None
        or bool(reader_errors)
        or reader.is_alive()
        or overflow.is_set()
        or returncode != 0
    )
    cleanup_errors: list[BaseException] = []
    if failed:
        try:
            process.kill()
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            process.wait(timeout=1.0)
        except BaseException as error:
            cleanup_errors.append(error)
    try:
        if process.stdout is not None:
            process.stdout.close()
    except BaseException as error:
        cleanup_errors.append(error)
    try:
        reader.join(timeout=1.0)
    except BaseException as error:
        cleanup_errors.append(error)
    if reader.is_alive():
        cleanup_errors.append(OSError("private Codex smoke reader did not stop"))

    operational = list(reader_errors)
    if failed and primary is None and not operational:
        operational.append(OSError("private Codex runtime smoke test failed"))
    selected = _select_smoke_failure(primary, operational, cleanup_errors)
    if selected is not None:
        if primary is selected and primary_traceback is not None:
            raise selected.with_traceback(primary_traceback)
        raise selected


def _select_smoke_failure(
    primary: BaseException | None,
    operational: list[BaseException],
    cleanup: list[BaseException],
) -> BaseException | None:
    if primary is not None and _is_priority_failure(primary):
        return primary
    for error in operational:
        if _is_priority_failure(error):
            return error
    for error in cleanup:
        if _is_priority_failure(error):
            return error
    if primary is not None:
        return primary
    if operational:
        return operational[0]
    if cleanup:
        return cleanup[0]
    return None


def _cleanup_failed_smoke_start(
    process: subprocess.Popen[bytes], reader: threading.Thread | None,
) -> list[BaseException]:
    errors: list[BaseException] = []
    try:
        process.kill()
    except BaseException as error:
        errors.append(error)
    try:
        process.wait(timeout=1.0)
    except BaseException as error:
        errors.append(error)
    try:
        if process.stdout is not None:
            process.stdout.close()
    except BaseException as error:
        errors.append(error)
    if reader is not None:
        try:
            reader.join(timeout=1.0)
        except BaseException as error:
            errors.append(error)
    return errors

__all__ = [
    "OPENAI_AUTHENTICODE_PUBLISHER",
    "NATIVE_CODEX_RELATIVE_PATH",
    "LockedNativeCodex",
    "NativeCodexIdentity",
    "CodexRuntimePreparer",
    "discover_locked_native_codex",
]
