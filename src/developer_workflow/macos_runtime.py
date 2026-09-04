"""macOS native executable discovery and private-cache security adapter."""

from __future__ import annotations

import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
from collections.abc import Callable

from .codex_runtime import (
    CODEX_EXECUTABLE_NAME, OPENAI_AUTHENTICODE_PUBLISHER, LockedNativeCodex,
    NativeCodexIdentity, _WindowsCacheRuntimeAdapter, _current_repository_roots,
    _inside_repository_by_identity, _stat_identity,
)
from .private_paths import prepare_private_directory


def _trusted_chain(path: Path) -> None:
    """Reject links, foreign owners and group/world-writable executable paths."""
    for item in (path, *path.parents):
        info = item.lstat()
        if (stat.S_ISLNK(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or info.st_mode & 0o022):
            raise OSError("native runtime path is unsafe")


class MacOSRuntimeAdapter:
    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise OSError("macOS native runtime is unavailable")

    def open_locked(self, path: Path) -> int:
        import fcntl

        _trusted_chain(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            if not self.is_disk_regular_non_reparse(descriptor):
                raise OSError("native runtime is not executable")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def is_disk_regular_non_reparse(self, descriptor: int) -> bool:
        info = os.fstat(descriptor)
        return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and info.st_uid in (0, os.geteuid())
                and not info.st_mode & 0o022 and bool(info.st_mode & 0o111)
                and os.pread(descriptor, 4, 0) in {
                    b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
                    b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca",
                })

    def identity(self, descriptor: int) -> NativeCodexIdentity:
        info = os.fstat(descriptor)
        return NativeCodexIdentity(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    def final_path(self, descriptor: int) -> Path:
        import fcntl

        # Darwin F_GETPATH resolves the open file, not a caller-provided pathname.
        raw = fcntl.fcntl(descriptor, 50, bytes(1024))
        return Path(os.fsdecode(raw.split(b"\0", 1)[0])).resolve(strict=True)

    def verify_publisher(self, descriptor: int, path: Path) -> str:
        _trusted_chain(path)
        before = self.identity(descriptor)
        if not self.same_file(self.final_path(descriptor), path):
            raise OSError("native runtime changed")
        # Apple-trusted Developer ID signature AND the expected organization.
        # An ad-hoc signature or a merely executable shell/JS wrapper is rejected.
        requirement = 'anchor apple generic and certificate leaf[subject.O] = "OpenAI OpCo, LLC"'
        result = subprocess.run(
            ["/usr/bin/codesign", "--verify", "--strict", "--all-architectures",
             "-R", requirement, str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=15, shell=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        )
        if (result.returncode != 0 or self.identity(descriptor) != before
                or not self.same_file(self.final_path(descriptor), path)):
            raise OSError("native runtime signature is invalid")
        return OPENAI_AUTHENTICODE_PUBLISHER

    def read(self, descriptor: int, size: int) -> bytes:
        return os.read(descriptor, size)

    def rewind(self, descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)

    def close(self, descriptor: int) -> None:
        os.close(descriptor)

    def same_file(self, left: Path, right: Path) -> bool:
        return os.path.samefile(left, right)

    def repository_marker(self, root: Path) -> tuple[str, tuple[int, int, int, int]] | None:
        try:
            info = (root / ".git").lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISDIR(info.st_mode):
            return "directory", _stat_identity(info)
        if stat.S_ISREG(info.st_mode):
            return "file", _stat_identity(info)
        raise OSError("invalid repository marker")

    def resolve_repository_path(self, path: Path) -> Path:
        return path.resolve(strict=True)

    def repository_path_identity(self, path: Path) -> tuple[int, int, int, int]:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("invalid repository parent")
        return _stat_identity(info)


class MacOSCacheRuntimeAdapter(_WindowsCacheRuntimeAdapter):
    """Share cache hashing/manifests, replace Windows ACL and handle primitives."""

    def __init__(self, *, _runtime_adapter=None) -> None:
        self._runtime = _runtime_adapter or MacOSRuntimeAdapter()

    def validate_cache_ancestor_chain(self, root: Path) -> None:
        _trusted_chain(root.parent.parent)

    def prepare_private_directory(self, path: Path) -> Path:
        return prepare_private_directory(path)

    def validate_private_directory(self, path: Path) -> None:
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            raise OSError("private runtime directory is unsafe")

    def protect_private_file(self, path: Path) -> None:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise OSError("private runtime file is unsafe")
        executable = (path.name == CODEX_EXECUTABLE_NAME
                      or path.name.startswith(("codex-", "code-mode-host-")))
        os.chmod(path, 0o700 if executable else 0o600, follow_symlinks=False)

    def fsync_directory(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _candidate_paths(locator: Path, machine: str) -> tuple[Path, ...]:
    """Locate npm native payloads without executing their JS/shell launchers."""
    native = {"arm64": ("arm64", "aarch64"), "x86_64": ("x64", "x86_64")}.get(machine)
    if native is None:
        raise OSError("unsupported macOS architecture")
    resolved = locator.resolve(strict=True)
    candidates = [resolved]  # Homebrew or manually installed signed Mach-O.
    if resolved.name == "codex.js" and resolved.parent.name == "bin":
        package = resolved.parent.parent
        architecture, triple_arch = native
        for binary_directory in ("bin", "codex"):
            relative = Path("vendor") / f"{triple_arch}-apple-darwin" / binary_directory / "codex"
            candidates.extend((
                package / "node_modules" / "@openai" / f"codex-darwin-{architecture}" / relative,
                package.parent / f"codex-darwin-{architecture}" / relative,
                package / relative,
            ))
    return tuple(candidates)


def discover_native_codex(*, which: Callable[[str], str | None],
                          repository_roots: tuple[Path, ...] | None) -> LockedNativeCodex:
    adapter = MacOSRuntimeAdapter()
    raw = which("codex")
    if not raw or "\0" in raw or not Path(raw).is_absolute():
        raise OSError("native Codex payload is unavailable")
    roots = _current_repository_roots(adapter) if repository_roots is None else repository_roots
    for candidate in _candidate_paths(Path(raw), platform.machine()):
        descriptor = None
        try:
            descriptor = adapter.open_locked(candidate)
            physical = adapter.final_path(descriptor)
            if _inside_repository_by_identity(physical, roots, adapter):
                raise OSError("repository executable is not trusted")
            identity = adapter.identity(descriptor)
            publisher = adapter.verify_publisher(descriptor, physical)
            return LockedNativeCodex(descriptor, identity, identity.size, publisher, adapter)
        except (OSError, subprocess.TimeoutExpired):
            if descriptor is not None:
                adapter.close(descriptor)
    raise OSError("native Codex payload is unavailable")
