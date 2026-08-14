"""Bounded, non-interactive Codex execution for isolated developer workflows."""

from __future__ import annotations

import json
import hmac
import math
import os
import re
import signal
import secrets
import shlex
import stat
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator

from .codex_runtime import (
    CodexRuntimePreparer,
    LockedPrivateCodex,
    NativeCodexIdentity,
    _PreparedCodexRuntime,
)
from .contracts import (
    AcceptanceCoverage,
    CodexResult,
    CommandResult,
    PreparedWorktree,
    RepositoryChangeClaim,
    RepositoryGroupMapping,
    RepositoryMapping,
    RepositorySnapshot,
    RootCauseEvidence,
)
from .repository import HeadChangedError, WorktreeRepository
from .repository_group import PreparedRepository


class CodexRunnerError(RuntimeError):
    """Base error for a safely rejected Codex execution."""


class UnsafeCodexRunError(CodexRunnerError):
    """The requested execution would cross a local safety boundary."""


class CodexExecutionError(CodexRunnerError):
    """Codex could not be executed successfully."""


class CodexProcessStartError(CodexExecutionError):
    """The requested executable could not be started."""


class CodexTimeoutError(CodexExecutionError):
    """Codex exceeded its execution deadline."""


class CodexOutputError(CodexRunnerError):
    """Codex returned invalid or unsafe structured output."""


_COMMAND_ATTESTATION_NONCE = object()
_COMMAND_ATTESTATION_SECRET = secrets.token_bytes(32)


def _command_attestation_mac(
    prefix: tuple[str, ...],
    path: Path,
    identity: NativeCodexIdentity,
    sha256: str,
    cache_root: Path,
) -> bytes:
    snapshot = json.dumps(
        [
            "codex-command-v1",
            list(prefix),
            str(path),
            sha256,
            str(cache_root),
            identity.volume_serial,
            identity.file_index,
            identity.size,
            identity.mtime_ns,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8", "strict")
    return hmac.digest(_COMMAND_ATTESTATION_SECRET, snapshot, "sha256")


@dataclass(frozen=True, slots=True, init=False, repr=False)
class CodexCommand:
    """An immutable argv prefix produced by the private runtime staging layer."""

    prefix: tuple[str, ...]
    _path: Path = field(repr=False)
    _identity: object = field(repr=False)
    _sha256: str = field(repr=False)
    _cache_root: Path = field(repr=False)
    _lease: LockedPrivateCodex = field(repr=False)
    _seal: bytes = field(repr=False)
    _nonce: object = field(repr=False)

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError("Codex commands cannot be constructed directly")

    @classmethod
    def _from_runtime(cls, runtime: object) -> CodexCommand:
        if (
            type(runtime) is not _PreparedCodexRuntime
            or not runtime._is_attested()
            or runtime.path != runtime.path.resolve(strict=True)
            or runtime.path.name.casefold() != "codex.exe"
        ):
            raise TypeError("Codex runtime attestation is invalid")
        prefix = (str(runtime.path),)
        seal = _command_attestation_mac(
            prefix,
            runtime.path,
            runtime.identity,
            runtime.sha256,
            runtime._cache_root,
        )
        instance = object.__new__(cls)
        object.__setattr__(instance, "prefix", prefix)
        object.__setattr__(instance, "_path", runtime.path)
        object.__setattr__(instance, "_identity", runtime.identity)
        object.__setattr__(instance, "_sha256", runtime.sha256)
        object.__setattr__(instance, "_cache_root", runtime._cache_root)
        object.__setattr__(instance, "_lease", runtime._lease)
        object.__setattr__(instance, "_seal", seal)
        object.__setattr__(instance, "_nonce", _COMMAND_ATTESTATION_NONCE)
        return instance

    def _is_attested(self) -> bool:
        try:
            if (
                self._nonce is not _COMMAND_ATTESTATION_NONCE
                or type(self.prefix) is not tuple
                or not isinstance(self._path, Path)
                or type(self._identity) is not NativeCodexIdentity
                or type(self._sha256) is not str
                or len(self._sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in self._sha256
                )
                or not isinstance(self._cache_root, Path)
                or type(self._lease) is not LockedPrivateCodex
                or not self._lease.verify()
                or self._lease.path != self._path
                or self._lease.identity != self._identity
                or self._lease.sha256 != self._sha256
                or self._lease._cache_root != self._cache_root
                or type(self._seal) is not bytes
                or self.prefix != (str(self._path),)
                or self._path.resolve(strict=True) != self._path
                or self._cache_root.resolve(strict=True) != self._cache_root
                or self._path.name.casefold() != "codex.exe"
                or self._path.parent.name != self._sha256
                or self._path.parent.parent != self._cache_root
            ):
                return False
            expected = _command_attestation_mac(
                self.prefix,
                self._path,
                self._identity,
                self._sha256,
                self._cache_root,
            )
            return hmac.compare_digest(self._seal, expected)
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    def argv(self, *arguments: str) -> list[str]:
        if not self._is_attested():
            raise TypeError("Codex command attestation is invalid")
        if any(
            type(argument) is not str or not argument or "\x00" in argument
            for argument in arguments
        ):
            raise ValueError("Codex command argument is invalid")
        return [*self.prefix, *arguments]

    def close(self) -> None:
        self._lease.close()

    def __copy__(self) -> CodexCommand:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> CodexCommand:
        return self

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("Codex commands cannot be serialized")


def _raise_codex_executable_unavailable() -> None:
    raise CodexProcessStartError("Codex executable is unavailable") from None


def resolve_codex_command(
    *,
    _prepare: Callable[[], _PreparedCodexRuntime] | None = None,
) -> CodexCommand:
    """Return only the verified native executable staged in the private cache."""

    runtime: _PreparedCodexRuntime | None = None
    failed = False
    try:
        runtime = (
            CodexRuntimePreparer().prepare_verified()
            if _prepare is None
            else _prepare()
        )
    except BaseException as error:
        if (
            isinstance(error, MemoryError)
            or not isinstance(error, Exception)
            or not isinstance(error, OSError)
        ):
            raise
        failed = True
    if failed:
        del _prepare, runtime
        _raise_codex_executable_unavailable()
    assert runtime is not None
    return CodexCommand._from_runtime(runtime)


class RepositoryGuard(Protocol):
    def assert_head_unchanged(self, prepared: PreparedWorktree) -> None: ...

    def snapshot(self, prepared: PreparedWorktree, mapping: RepositoryMapping) -> Any: ...

    def contains_sensitive_content(
        self, prepared: PreparedWorktree, mapping: RepositoryMapping,
        secrets: tuple[str, ...],
    ) -> bool: ...


class CommandExecutor(Protocol):
    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        max_output_bytes: int,
        stdin: bytes | None = None,
    ) -> subprocess.CompletedProcess[str]: ...


_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ALLOWED_ENV = {
    "comspec", "lang", "lc_all",
    "no_color", "path", "pathext", "systemroot", "temp", "term", "tmp",
    "tmpdir", "windir",
    "ssl_cert_dir", "ssl_cert_file", "requests_ca_bundle", "curl_ca_bundle",
    "http_proxy", "https_proxy", "no_proxy",
    "codex_api_key", "codex_auth_token",
    "openai_api_key", "openai_api_version", "openai_base_url",
    "openai_organization", "openai_org_id", "openai_project",
}
_SECRET_ENV_TOKENS = (
    "token", "password", "secret", "credential", "authorization", "cookie",
    "api_key", "apikey", "private_key", "access_key", "askpass",
    "connection_string", "dsn",
)


def _is_positive_finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value > 0
    except (OverflowError, TypeError, ValueError):
        return False


def _is_priority_failure(error: BaseException) -> bool:
    return isinstance(error, MemoryError) or not isinstance(error, Exception)


if os.name == "nt":  # pragma: no cover - definitions are exercised on Windows
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    _kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    _kernel32.Thread32First.restype = wintypes.BOOL
    _kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    _kernel32.Thread32Next.restype = wintypes.BOOL
    _kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenThread.restype = wintypes.HANDLE
    _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel32.ResumeThread.restype = wintypes.DWORD


class _ProcessTreeGuard:
    """Own a Windows job when available; POSIX uses the process session."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.handle: Any = None
        if os.name != "nt":
            return
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "could not create process job")
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not _kernel32.SetInformationJobObject(
            handle, 9, ctypes.byref(information), ctypes.sizeof(information)
        ):
            error = ctypes.get_last_error()
            _kernel32.CloseHandle(handle)
            raise OSError(error, "could not configure process job")
        if not _kernel32.AssignProcessToJobObject(handle, wintypes.HANDLE(process._handle)):
            error = ctypes.get_last_error()
            _kernel32.CloseHandle(handle)
            raise OSError(error, "could not assign process job")
        self.handle = handle

    def terminate(self) -> None:
        if os.name == "nt":
            if self.handle:
                _kernel32.TerminateJobObject(self.handle, 1)
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            pass

    def force_kill(self) -> None:
        if os.name == "nt":
            self.terminate()
            return
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except OSError:
            pass

    def close(self) -> None:
        if os.name == "nt" and self.handle:
            _kernel32.CloseHandle(self.handle)
            self.handle = None


def _resume_suspended_process(process: subprocess.Popen[bytes]) -> None:
    """Resume the sole initial Windows thread after job assignment."""

    if os.name != "nt":
        return
    snapshot = _kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "could not enumerate process threads")
    thread_handle: Any = None
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        found = _kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32OwnerProcessID == process.pid:
                thread_handle = _kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                break
            found = _kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        if not thread_handle:
            raise OSError(ctypes.get_last_error(), "could not open suspended process thread")
        previous_suspend_count = _kernel32.ResumeThread(thread_handle)
        if previous_suspend_count == 0xFFFFFFFF:
            raise OSError(ctypes.get_last_error(), "could not resume isolated process")
        if previous_suspend_count != 1:
            raise OSError("isolated process had an unexpected suspend count")
    finally:
        if thread_handle:
            _kernel32.CloseHandle(thread_handle)
        _kernel32.CloseHandle(snapshot)


def _start_isolated_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    pipe_stdin: bool = False,
    merge_stderr: bool = False,
) -> tuple[subprocess.Popen[bytes], _ProcessTreeGuard]:
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x00000004
        )
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if pipe_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            shell=False,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
    except OSError as error:
        raise CodexProcessStartError("Codex process could not be started") from error

    tree: _ProcessTreeGuard | None = None
    try:
        tree = _ProcessTreeGuard(process)
        _resume_suspended_process(process)
        return process, tree
    except BaseException as error:
        cleanup_errors: list[BaseException] = []
        try:
            if tree is not None:
                tree.terminate()
            else:
                process.kill()
        except BaseException as cleanup:
            cleanup_errors.append(cleanup)
        try:
            process.wait(timeout=2)
        except BaseException as cleanup:
            cleanup_errors.append(cleanup)
            try:
                process.kill()
            except BaseException as nested:
                cleanup_errors.append(nested)
            try:
                process.wait(timeout=2)
            except BaseException as nested:
                cleanup_errors.append(nested)
        if tree is not None:
            try:
                tree.close()
            except BaseException as cleanup:
                cleanup_errors.append(cleanup)
        if _is_priority_failure(error):
            raise
        for cleanup in cleanup_errors:
            if _is_priority_failure(cleanup):
                raise cleanup
        if isinstance(error, OSError):
            raise CodexExecutionError(
                "Codex process could not be isolated"
            ) from error
        raise


def _terminate(process: subprocess.Popen[bytes], tree: _ProcessTreeGuard) -> None:
    failures: list[BaseException] = []
    try:
        tree.terminate()
    except BaseException as error:
        failures.append(error)
    try:
        if process.poll() is None:
            process.wait(timeout=2)
    except BaseException as error:
        failures.append(error)
        try:
            tree.force_kill()
        except BaseException as nested:
            failures.append(nested)
        try:
            if process.poll() is None:
                process.wait(timeout=2)
        except BaseException as nested:
            failures.append(nested)
    for failure in failures:
        if _is_priority_failure(failure):
            raise failure


def _bounded_subprocess(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    max_output_bytes: int,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[str]:
    """Capture output incrementally, terminating and reaping on timeout/overflow."""

    if not _is_positive_finite_number(timeout):
        raise ValueError("timeout must be finite and positive")
    if stdin is not None and not isinstance(stdin, bytes):
        raise TypeError("stdin must be bytes or None")
    process, tree = _start_isolated_process(
        command, cwd=cwd, env=env, pipe_stdin=stdin is not None
    )

    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    total = 0
    lock = threading.Lock()
    overflow = threading.Event()

    def feed_stdin() -> None:
        if stdin is None:
            return
        assert process.stdin is not None
        try:
            process.stdin.write(stdin)
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            process.stdin.close()

    def drain(name: str, stream: Any) -> None:
        nonlocal total
        try:
            descriptor = stream.fileno()
            while data := os.read(descriptor, 64 * 1024):
                with lock:
                    total += len(data)
                    if total > max_output_bytes:
                        overflow.set()
                        return
                    chunks[name].append(data)
        finally:
            stream.close()

    assert process.stdout is not None and process.stderr is not None
    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    stdin_thread = threading.Thread(target=feed_stdin, daemon=True)
    for thread in threads:
        thread.start()
    stdin_thread.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if overflow.is_set():
                _terminate(process, tree)
                raise CodexOutputError("Codex output limit exceeded")
            if time.monotonic() >= deadline:
                _terminate(process, tree)
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(0.02)
        for thread in threads:
            thread.join(timeout=0.05)
        if any(thread.is_alive() for thread in threads):
            _terminate(process, tree)
        for thread in threads:
            thread.join(timeout=2)
        stdin_thread.join(timeout=2)
        if any(thread.is_alive() for thread in threads):
            raise CodexExecutionError("Codex process output did not close")
        if stdin_thread.is_alive():
            _terminate(process, tree)
            stdin_thread.join(timeout=2)
            if stdin_thread.is_alive():
                raise CodexExecutionError("Codex process input did not close")
        if overflow.is_set():
            raise CodexOutputError("Codex output limit exceeded")
        try:
            stdout = b"".join(chunks["stdout"]).decode("utf-8", "strict")
            stderr = b"".join(chunks["stderr"]).decode("utf-8", "strict")
        except UnicodeDecodeError as error:
            raise CodexOutputError("Codex returned invalid structured output") from error
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if process.poll() is None:
            _terminate(process, tree)
        tree.close()
        for thread in threads:
            thread.join(timeout=2)
        stdin_thread.join(timeout=2)


def _safe_environment(
    source: Mapping[str, str], isolation_root: Path, codex_home: Path | None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    safe: dict[str, str] = {}
    removed_values: list[str] = []
    for key, value in source.items():
        folded = key.casefold()
        sensitive = any(token in folded for token in _SECRET_ENV_TOKENS)
        if sensitive and len(value) >= 6:
            removed_values.append(value)
        if folded not in _ALLOWED_ENV:
            continue
        if folded in {"http_proxy", "https_proxy", "openai_base_url"}:
            parsed = urlsplit(value)
            if parsed.username is not None or parsed.password is not None:
                for credential in (parsed.username, parsed.password):
                    if credential is not None and len(unquote(credential)) >= 6:
                        removed_values.append(unquote(credential))
                continue
        safe[key] = value
    locations = {
        "HOME": isolation_root / "home",
        "USERPROFILE": isolation_root / "home",
        "APPDATA": isolation_root / "appdata",
        "LOCALAPPDATA": isolation_root / "localappdata",
        "XDG_CONFIG_HOME": isolation_root / "xdg-config",
        "XDG_CACHE_HOME": isolation_root / "xdg-cache",
    }
    for path in set(locations.values()):
        path.mkdir()
    empty_git_config = isolation_root / "empty.gitconfig"
    empty_git_config.write_bytes(b"")
    safe.update({name: str(path) for name, path in locations.items()})
    safe.update({
        "GIT_CONFIG_GLOBAL": str(empty_git_config),
        "GIT_CONFIG_SYSTEM": str(empty_git_config),
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
    })
    if codex_home is not None:
        safe["CODEX_HOME"] = str(codex_home)
    return safe, tuple(removed_values)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _path_allowed(path: str, allowed_paths: tuple[str, ...]) -> bool:
    if not allowed_paths:
        return True
    return any(path == allowed or path.startswith(f"{allowed}/") for allowed in allowed_paths)


def _contains_secret(value: Any, secrets: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(secret in value for secret in secrets)
    if isinstance(value, Mapping):
        return any(
            _contains_secret(key, secrets) or _contains_secret(nested, secrets)
            for key, nested in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return any(_contains_secret(nested, secrets) for nested in value)
    return False


_WRITE_VERBS = {
    "create", "update", "delete", "post", "put", "patch", "transition",
    "assign", "close", "commit", "push", "merge", "reopen", "review",
}
_GIT_GLOBAL_VALUE_OPTIONS = {
    "-c", "-C", "--config-env", "--exec-path", "--git-dir", "--namespace",
    "--super-prefix", "--work-tree",
}


def _executable_name(value: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return name[:-4] if name.endswith(".exe") else name


def _segments(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n(){}!")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = list(lexer)
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and all(character in ";&|\n(){}!" for character in token):
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _option_value(args: list[str], index: int) -> tuple[str | None, int]:
    token = args[index]
    if "=" in token:
        return token.split("=", 1)[1], index + 1
    if index + 1 < len(args):
        return args[index + 1], index + 2
    return None, index + 1


def _git_subcommand(args: list[str]) -> tuple[str | None, list[str]]:
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            index += 1
            break
        option = token.split("=", 1)[0]
        if option in _GIT_GLOBAL_VALUE_OPTIONS:
            _, index = _option_value(args, index)
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    if index >= len(args):
        return None, []
    return args[index].casefold(), args[index + 1 :]


def _git_uses_dynamic_alias(args: list[str]) -> bool:
    index = 0
    while index < len(args):
        token = args[index]
        if token == "-c":
            value, index = _option_value(args, index)
            if value and value.casefold().startswith("alias."):
                return True
            continue
        if token == "--config-env":
            value, index = _option_value(args, index)
            if value and value.casefold().startswith("alias."):
                return True
            continue
        if token.startswith("-calias.") or token.casefold().startswith(
            "--config-env=alias."
        ):
            return True
        index += 1
    return False


def _has_dynamic_token(args: list[str]) -> bool:
    return any(
        token.startswith("$") or "$(" in token or "`" in token for token in args
    )


def _next_non_option(args: list[str]) -> str | None:
    return next((value.casefold() for value in args if not value.startswith("-")), None)


def _curl_writes(args: list[str]) -> bool:
    safe_long_flags = {
        "--compressed", "--fail", "--get", "--head", "--include",
        "--location", "--show-error", "--silent",
    }
    safe_long_value_options = {
        "--connect-timeout", "--max-time", "--retry", "--retry-delay",
        "--user-agent",
    }
    index = 0
    while index < len(args):
        token = args[index]
        folded = token.casefold()
        option = folded.split("=", 1)[0]
        if token == "-X" or option == "--request":
            value, index = _option_value(args, index)
            if value is None or value.casefold() not in {"get", "head"}:
                return True
            continue
        if token.startswith("-X") and len(token) > 2:
            if token[2:].casefold() not in {"get", "head"}:
                return True
            index += 1
            continue
        if (
            token.startswith(("-d", "-F", "-T"))
            or option.startswith("--data")
            or option.startswith("--form")
            or option in {"--upload-file", "--json"}
        ):
            return True
        if token.startswith("-") and not token.startswith("--"):
            if all(character in "fsSLIG" for character in token[1:]):
                index += 1
                continue
            return True
        if option in safe_long_flags:
            index += 1
            continue
        if option in safe_long_value_options:
            _, index = _option_value(args, index)
            continue
        if token.startswith("--"):
            return True
        index += 1
    return False


def _gh_writes(args: list[str]) -> bool:
    index = 0
    while index < len(args):
        token = args[index]
        option = token.split("=", 1)[0]
        if option in {"-R", "--repo", "--hostname"}:
            _, index = _option_value(args, index)
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    subcommand = args[index].casefold() if index < len(args) else None
    remainder = args[index + 1 :] if index < len(args) else []
    if subcommand == "pr":
        action = _next_non_option(remainder)
        return action not in {"view", "status", "list", "checks", "diff"}
    if subcommand != "api":
        read_actions = {
            "repo": {"view", "list"},
            "issue": {"view", "list", "status"},
            "run": {"view", "list", "watch"},
            "workflow": {"view", "list"},
            "release": {"view", "list", "download"},
            "label": {"list"},
            "auth": {"status"},
            "search": {"code", "commits", "issues", "prs", "repos"},
        }
        if subcommand == "status":
            return False
        return (
            subcommand not in read_actions
            or _next_non_option(remainder) not in read_actions[subcommand]
        )
    explicit_get = False
    for index, token in enumerate(remainder):
        option = token.casefold().split("=", 1)[0]
        if option in {"-f", "-F", "--field", "--raw-field", "--input"}:
            return True
        if option in {"-x", "--method"}:
            value, _ = _option_value(remainder, index)
            if value is None or value.casefold() != "get":
                return True
            explicit_get = True
    return not explicit_get


def _contains_write_verb(args: list[str]) -> bool:
    words = re.findall(r"[A-Za-z]+", " ".join(args).casefold())
    return any(word in _WRITE_VERBS for word in words)


def _segment_writes(tokens: list[str], *, depth: int = 0) -> bool:
    if not tokens or depth > 4:
        return depth > 4
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens = tokens[1:]
    while tokens and tokens[0].casefold() in {
        "if", "then", "elif", "else", "while", "until", "do",
    }:
        tokens = tokens[1:]
    if tokens and tokens[0].casefold() in {"fi", "done", "esac"}:
        return False
    if not tokens:
        return False
    executable = _executable_name(tokens[0])
    args = tokens[1:]
    if executable == "env":
        while args and (args[0].startswith("-") or "=" in args[0]):
            args = args[1:]
        return _segment_writes(args, depth=depth + 1)
    if executable in {"command", "nohup"}:
        while args and (args[0] == "--" or args[0].startswith("-")):
            args = args[1:]
        return _segment_writes(args, depth=depth + 1)
    if executable == "sudo":
        value_options = {
            "-u", "--user", "-g", "--group", "-h", "--host", "-p",
            "--prompt", "-C", "--chdir", "-T", "--command-timeout",
        }
        index = 0
        while index < len(args):
            token = args[index]
            if token == "--":
                index += 1
                break
            option = token.split("=", 1)[0]
            if option in value_options:
                _, index = _option_value(args, index)
                continue
            if token.startswith("-"):
                index += 1
                continue
            break
        return _segment_writes(args[index:], depth=depth + 1)
    if executable == "nice":
        index = 0
        while index < len(args):
            token = args[index]
            if token in {"-n", "--adjustment"}:
                _, index = _option_value(args, index)
                continue
            if token.startswith("--adjustment=") or re.fullmatch(r"-[0-9]+", token):
                index += 1
                continue
            break
        return _segment_writes(args[index:], depth=depth + 1)
    if executable in {"timeout", "gtimeout"}:
        index = 0
        while index < len(args):
            token = args[index]
            option = token.split("=", 1)[0]
            if option in {"-k", "--kill-after", "-s", "--signal"}:
                _, index = _option_value(args, index)
                continue
            if token.startswith("-"):
                index += 1
                continue
            break
        if index >= len(args):
            return True
        return _segment_writes(args[index + 1 :], depth=depth + 1)
    if executable in {"sh", "bash", "zsh", "dash", "ksh"}:
        indexes = [
            index
            for index, value in enumerate(args)
            if value.startswith("-")
            and not value.startswith("--")
            and "c" in value.lstrip("-")
        ]
        if not indexes:
            return True
        index = indexes[0]
        return index + 1 >= len(args) or _is_forbidden_command(
            args[index + 1], depth=depth + 1
        )
    if executable == "cmd":
        indexes = [i for i, value in enumerate(args) if value.casefold() in {"/c", "/k"}]
        return not indexes or _is_forbidden_command(
            " ".join(args[indexes[0] + 1 :]), depth=depth + 1
        )
    if executable in {"powershell", "pwsh"}:
        if any(
            value.casefold() in {
                "-e", "-ec", "-enc", "-enco", "-encodedcommand",
                "-f", "-fi", "-fil", "-file",
            }
            for value in args
        ):
            return True
        indexes = [i for i, value in enumerate(args) if value.casefold() in {"-c", "-command"}]
        return not indexes or _is_forbidden_command(
            " ".join(args[indexes[0] + 1 :]), depth=depth + 1
        )
    if executable == "eval":
        return not args or _is_forbidden_command(" ".join(args), depth=depth + 1)
    if executable == "git":
        if _has_dynamic_token(args) or _git_uses_dynamic_alias(args):
            return True
        subcommand, remainder = _git_subcommand(args)
        if subcommand == "remote":
            action = _next_non_option(remainder)
            return action not in {None, "get-url"}
        read_subcommands = {
            "status", "diff", "log", "show", "rev-parse", "grep", "ls-files",
            "ls-tree", "cat-file", "check-ignore", "check-attr", "merge-base",
            "describe", "name-rev", "shortlog", "blame", "version", "help",
            "whatchanged", "for-each-ref", "show-ref",
        }
        if subcommand == "config":
            return not any(
                value.split("=", 1)[0]
                in {
                    "--get", "--get-all", "--get-regexp", "--list",
                    "--show-origin", "--show-scope",
                }
                for value in remainder
            )
        return subcommand not in read_subcommands
    if executable.startswith("git-remote-") or executable in {"git-send-pack", "git-receive-pack"}:
        return True
    if executable == "gh":
        if _has_dynamic_token(args):
            return True
        return _gh_writes(args)
    if executable == "curl":
        if _has_dynamic_token(args):
            return True
        return _curl_writes(args)
    if executable in {"http", "https"}:
        method = _next_non_option(args)
        return (
            method not in {"get", "head"}
            or _has_dynamic_token(args)
            or any("=" in value and not value.startswith("http") for value in args)
        )
    if executable in {"invoke-webrequest", "invoke-restmethod"}:
        for index, token in enumerate(args):
            if token.casefold().split("=", 1)[0] == "-method":
                value, _ = _option_value(args, index)
                return value is None or value.casefold() in {"post", "put", "patch", "delete"}
        return False
    if executable in {"python", "python3", "py", "node", "perl", "ruby"}:
        dynamic_flags = (
            {"-c"}
            if executable in {"python", "python3", "py"}
            else {"-e", "--eval"}
        )
        if any(value.split("=", 1)[0] in dynamic_flags for value in args):
            return True
        if any(value.casefold() in {"--version", "-v"} for value in args):
            return False
        if executable in {"node", "perl", "ruby"}:
            return True
        try:
            module_index = args.index("-m")
        except ValueError:
            return True
        return (
            module_index + 1 >= len(args)
            or args[module_index + 1].casefold()
            not in {"pytest", "unittest", "compileall"}
        )
    if "ones" in executable:
        if _has_dynamic_token(args):
            return True
        words = {
            word.casefold()
            for value in args
            for word in re.findall(r"[A-Za-z]+", value)
        }
        return (
            bool(words & _WRITE_VERBS)
            or not bool(words & {"get", "list", "show", "search", "query", "read"})
        )
    if executable == "uv":
        if not args or args[0].casefold() != "run":
            return not (
                len(args) >= 2
                and args[0].casefold() == "lock"
                and "--check" in args[1:]
            )
        nested = args[1:]
        while nested and nested[0] in {"--frozen", "--locked", "--offline", "--no-sync"}:
            nested = nested[1:]
        return _segment_writes(nested, depth=depth + 1)
    if executable == "make":
        if any(value.startswith("-") for value in args):
            return True
        targets = [value.casefold() for value in args if not value.startswith("-")]
        return not targets or any(
            target not in {"test", "check", "lint"} for target in targets
        )
    if executable == "ruff":
        return _next_non_option(args) != "check"
    if executable in {
        "pytest", "py.test", "mypy", "pyright", "rg", "ripgrep", "grep",
        "cat", "type", "get-content", "select-string", "findstr", "ls",
        "dir", "pwd", "get-childitem", "test-path", "true", "false",
    }:
        return False
    if executable.startswith("$"):
        return True
    return True


def _is_forbidden_command(command: str, *, depth: int = 0) -> bool:
    if ("$(" in command or "`" in command) and re.search(
        r"\b(?:git|gh|curl|ones)\b", command, re.IGNORECASE
    ):
        return True
    try:
        return any(_segment_writes(segment, depth=depth) for segment in _segments(command))
    except ValueError:
        return bool(re.search(r"\b(?:git|gh|curl|ones)\b", command, re.IGNORECASE))


def _is_reparse_or_link(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _environment_value(source: Mapping[str, str], name: str) -> str | None:
    folded_name = name.casefold()
    return next(
        (value for key, value in source.items() if key.casefold() == folded_name),
        None,
    )


def _resolve_codex_home(source: Mapping[str, str]) -> Path | None:
    """Resolve only Codex authentication state before isolating the child HOME."""

    explicit = _environment_value(source, "CODEX_HOME")
    is_explicit = explicit is not None
    if is_explicit:
        raw_path = explicit or ""
    else:
        has_environment_auth = any(
            bool(_environment_value(source, name))
            for name in ("CODEX_API_KEY", "CODEX_AUTH_TOKEN", "OPENAI_API_KEY")
        )
        if has_environment_auth:
            return None
        parent_home = _environment_value(source, "USERPROFILE") or _environment_value(
            source, "HOME"
        )
        if not parent_home:
            return None
        raw_path = str(Path(parent_home) / ".codex")

    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise UnsafeCodexRunError(
            "Codex authentication directory is unsafe or unavailable"
        )
    try:
        candidate.lstat()
    except FileNotFoundError as error:
        if not is_explicit:
            return None
        raise UnsafeCodexRunError(
            "Codex authentication directory is unsafe or unavailable"
        ) from error
    except OSError as error:
        raise UnsafeCodexRunError(
            "Codex authentication directory is unsafe or unavailable"
        ) from error
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_dir() or not resolved.is_absolute():
            raise UnsafeCodexRunError(
                "Codex authentication directory is unsafe or unavailable"
            )
        with os.scandir(resolved):
            pass
        return resolved
    except UnsafeCodexRunError:
        raise
    except OSError as error:
        raise UnsafeCodexRunError(
            "Codex authentication directory is unsafe or unavailable"
        ) from error


def validate_codex_auth_source(source: Mapping[str, str]) -> Path | None:
    """Validate Codex authentication shape without reading credential content."""

    explicit_home = _environment_value(source, "CODEX_HOME")
    if explicit_home is None:
        configured_auth = tuple(
            value
            for name in ("CODEX_API_KEY", "CODEX_AUTH_TOKEN", "OPENAI_API_KEY")
            if (value := _environment_value(source, name))
        )
        if configured_auth:
            if any(
                value != value.strip()
                or len(value) > 65536
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in value
                )
                for value in configured_auth
            ):
                raise UnsafeCodexRunError(
                    "Codex authentication source is unavailable"
                )
            return None

    codex_home = _resolve_codex_home(source)
    if codex_home is None:
        raise UnsafeCodexRunError("Codex authentication source is unavailable")
    auth_file = codex_home / "auth.json"
    try:
        metadata = auth_file.lstat()
        resolved = auth_file.resolve(strict=True)
        if (
            _is_reparse_or_link(auth_file)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > 1024 * 1024
            or resolved.parent != codex_home
        ):
            raise UnsafeCodexRunError("Codex authentication source is unavailable")
    except UnsafeCodexRunError:
        raise
    except OSError:
        raise UnsafeCodexRunError(
            "Codex authentication source is unavailable"
        ) from None
    return codex_home


@dataclass(slots=True)
class CodexRunner:
    run_root: Path
    repository: RepositoryGuard | WorktreeRepository
    command_executor: CommandExecutor = field(default=_bounded_subprocess, repr=False)
    command_resolver: Callable[[], CodexCommand] = field(
        default=resolve_codex_command, repr=False
    )
    environment_provider: Callable[[], Mapping[str, str]] = field(
        default=lambda: os.environ, repr=False
    )
    schema_path: Path = field(
        default_factory=lambda: Path(__file__).with_name("schemas") / "workflow-result.schema.json",
        init=False,
    )
    max_prompt_bytes: int = 1024 * 1024
    max_output_bytes: int = 10 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.run_root.is_absolute():
            raise ValueError("run_root must be absolute")
        canonical_root = self.run_root.resolve(strict=False)
        if os.path.normcase(str(canonical_root)) != os.path.normcase(str(self.run_root)):
            raise ValueError("run_root must be canonical")
        if not self.schema_path.is_absolute():
            self.schema_path = self.schema_path.resolve()
        for value in (self.max_prompt_bytes, self.max_output_bytes):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError("Codex size limits must be positive integers")
        if not callable(self.environment_provider):
            raise ValueError("Codex environment provider is invalid")
        if not callable(self.command_resolver):
            raise ValueError("Codex command resolver is invalid")
        try:
            schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            if _is_reparse_or_link(self.schema_path) or not self.schema_path.is_file():
                raise ValueError("Codex output schema is not a regular file")
        except (OSError, ValueError) as error:
            raise ValueError("Codex output schema is unavailable or invalid") from error

    def run(
        self,
        prepared: PreparedWorktree,
        mapping: RepositoryMapping,
        *,
        run_id: str,
        prompt: str,
        timeout_seconds: float = 1800,
        allow_changes: bool = True,
    ) -> CodexResult:
        self.repository.assert_head_unchanged(prepared)
        try:
            output, removed_secrets = self._invoke(
                run_id=run_id,
                prompt=prompt,
                cwd=prepared.path,
                sandbox="workspace-write" if allow_changes else "read-only",
                timeout_seconds=timeout_seconds,
            )
        finally:
            self.repository.assert_head_unchanged(prepared)

        payload = self._validate_output(output, mapping)
        snapshot_before_scan = self.repository.snapshot(prepared, mapping)
        if snapshot_before_scan.head_commit != prepared.head_commit:
            raise HeadChangedError("worktree HEAD changed")
        if _contains_secret(
            snapshot_before_scan.model_dump(mode="json"), removed_secrets
        ):
            raise CodexOutputError("Codex returned invalid structured output")
        sensitive_content_found = self.repository.contains_sensitive_content(
            prepared, mapping, removed_secrets
        )
        snapshot = self.repository.snapshot(prepared, mapping)
        snapshot_head_changed = snapshot.head_commit != prepared.head_commit
        snapshot_content_changed = snapshot.model_dump(
            mode="json"
        ) != snapshot_before_scan.model_dump(
            mode="json"
        )
        self.repository.assert_head_unchanged(prepared)
        if snapshot_head_changed:
            raise HeadChangedError("worktree HEAD changed")
        if snapshot_content_changed:
            raise CodexOutputError("Codex returned invalid structured output")
        if sensitive_content_found:
            raise CodexOutputError("Codex returned invalid structured output")
        claimed = tuple(payload["changed_files"])
        if set(claimed) != set(snapshot.changed_files) or len(claimed) != len(snapshot.changed_files):
            raise CodexOutputError("Codex returned invalid structured output")
        return self._result_from_payload(payload)

    def run_preflight(
        self,
        *,
        run_id: str,
        prompt: str,
        timeout_seconds: float = 1800,
    ) -> CodexResult:
        """Run source-only structured analysis without creating a worktree."""

        output, _ = self._invoke(
            run_id=run_id,
            prompt=prompt,
            cwd=None,
            sandbox="read-only",
            timeout_seconds=timeout_seconds,
        )
        payload = self._validate_output(output, None)
        return self._result_from_payload(payload)

    def run_group(
        self,
        group: RepositoryGroupMapping,
        prepared: tuple[PreparedRepository, ...],
        *,
        run_id: str,
        prompt: str,
        timeout_seconds: float = 1800,
        allow_changes: bool = True,
    ) -> CodexResult:
        expected_keys = group.topological_keys()
        if tuple(item.repository_key for item in prepared) != expected_keys:
            raise UnsafeCodexRunError("prepared repositories do not match group topology")
        configured = {item.key: item for item in group.repositories}
        if any(item.mapping != configured[item.repository_key] for item in prepared):
            raise UnsafeCodexRunError("prepared repository mapping differs from group")
        parents = {item.prepared.path.parent.resolve(strict=True) for item in prepared}
        if len(parents) != 1:
            raise UnsafeCodexRunError("prepared repositories do not share one workspace")
        workspace = next(iter(parents))
        for item in prepared:
            self.repository.assert_head_unchanged(item.prepared)
        try:
            output, removed_secrets = self._invoke(
                run_id=run_id,
                prompt=prompt,
                cwd=workspace,
                sandbox="workspace-write" if allow_changes else "read-only",
                timeout_seconds=timeout_seconds,
            )
        finally:
            for item in prepared:
                self.repository.assert_head_unchanged(item.prepared)

        payload = self._validate_group_output(output, group)
        before: dict[str, RepositorySnapshot] = {}
        sensitive = False
        for item in prepared:
            snapshot = self.repository.snapshot(item.prepared, item.mapping)
            if snapshot.head_commit != item.prepared.head_commit:
                raise HeadChangedError("worktree HEAD changed")
            before[item.repository_key] = snapshot
            if _contains_secret(snapshot.model_dump(mode="json"), removed_secrets):
                raise CodexOutputError("Codex returned invalid structured output")
            sensitive = self.repository.contains_sensitive_content(
                item.prepared, item.mapping, removed_secrets
            ) or sensitive

        after = {
            item.repository_key: self.repository.snapshot(item.prepared, item.mapping)
            for item in prepared
        }
        for item in prepared:
            self.repository.assert_head_unchanged(item.prepared)
        if any(
            after[key].head_commit != next(
                item.prepared.head_commit for item in prepared if item.repository_key == key
            )
            for key in after
        ):
            raise HeadChangedError("worktree HEAD changed")
        if before != after or sensitive:
            raise CodexOutputError("Codex returned invalid structured output")
        actual = tuple(
            (key, path)
            for key in expected_keys
            for path in after[key].changed_files
        )
        claimed = tuple(
            (item["repository_key"], item["path"])
            for item in payload.get("repository_changes", [])
        )
        if len(claimed) != len(actual) or set(claimed) != set(actual):
            raise CodexOutputError("Codex returned invalid structured output")
        return self._result_from_payload(payload)

    def _invoke(
        self,
        *,
        run_id: str,
        prompt: str,
        cwd: Path | None,
        sandbox: str,
        timeout_seconds: float,
    ) -> tuple[str, tuple[str, ...]]:
        if not _RUN_ID.fullmatch(run_id) or run_id in {".", ".."}:
            raise UnsafeCodexRunError("run_id is not a safe path segment")
        if not _is_positive_finite_number(timeout_seconds):
            raise UnsafeCodexRunError("timeout must be finite and positive")
        if sandbox not in {"workspace-write", "read-only"}:
            raise UnsafeCodexRunError("Codex sandbox mode is invalid")
        prompt_bytes = prompt.encode("utf-8", "strict")
        if not prompt.strip() or len(prompt_bytes) > self.max_prompt_bytes or "\x00" in prompt:
            raise UnsafeCodexRunError("prompt is empty, invalid, or exceeds its size limit")
        try:
            source = self.environment_provider()
            if not isinstance(source, Mapping) or any(
                type(key) is not str or type(value) is not str
                for key, value in source.items()
            ):
                raise TypeError
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            raise UnsafeCodexRunError("Codex environment is unavailable") from None
        codex_home = _resolve_codex_home(source)
        run_directory = self._prepare_run_directory(run_id)
        effective_cwd = cwd or run_directory
        isolation_root = self._prepare_isolation_directory(run_directory)
        safe_env, removed_secrets = _safe_environment(source, isolation_root, codex_home)
        if any(secret in prompt for secret in removed_secrets):
            raise UnsafeCodexRunError("prompt contains credential material")
        self._write_prompt(run_directory / "codex-prompt.txt", prompt_bytes)
        resolved_command = self.command_resolver()
        if (
            type(resolved_command) is not CodexCommand
            or not resolved_command._is_attested()
        ):
            _raise_codex_executable_unavailable()
        execution_error: BaseException | None = None
        execution_traceback = None
        completed: subprocess.CompletedProcess[str] | None = None
        try:
            command = resolved_command.argv(
                "exec", "--cd", str(effective_cwd), "--sandbox", sandbox,
                "--output-schema", str(self.schema_path), "-",
            )
            try:
                completed = self.command_executor(
                    command,
                    cwd=effective_cwd,
                    env=safe_env,
                    timeout=float(timeout_seconds),
                    max_output_bytes=self.max_output_bytes,
                    stdin=prompt_bytes,
                )
            except subprocess.TimeoutExpired as error:
                raise CodexTimeoutError("Codex execution timed out") from error
            except CodexRunnerError:
                raise
            except Exception as error:
                raise CodexExecutionError(
                    "Codex process could not be executed"
                ) from error
        except BaseException as error:
            execution_error = error
            execution_traceback = error.__traceback__
        close_error: BaseException | None = None
        close_traceback = None
        try:
            resolved_command.close()
        except BaseException as error:
            close_error = error
            close_traceback = error.__traceback__
        if execution_error is not None:
            if _is_priority_failure(execution_error):
                raise execution_error.with_traceback(execution_traceback)
            if close_error is not None and _is_priority_failure(close_error):
                raise close_error.with_traceback(close_traceback)
            raise execution_error.with_traceback(execution_traceback)
        if close_error is not None:
            if _is_priority_failure(close_error):
                raise close_error.with_traceback(close_traceback)
            raise CodexExecutionError(
                "Codex process could not be executed"
            ) from None
        assert completed is not None
        if completed.returncode != 0:
            raise CodexExecutionError("Codex exited unsuccessfully")
        if not isinstance(completed.stdout, str) or not isinstance(completed.stderr, str):
            raise CodexOutputError("Codex returned invalid structured output")
        try:
            output_size = len(completed.stdout.encode("utf-8", "strict")) + len(
                completed.stderr.encode("utf-8", "strict")
            )
        except UnicodeEncodeError as error:
            raise CodexOutputError("Codex returned invalid structured output") from error
        if output_size > self.max_output_bytes:
            raise CodexOutputError("Codex output limit exceeded")
        if any(
            secret in completed.stdout or secret in completed.stderr
            for secret in removed_secrets
        ):
            raise CodexOutputError("Codex returned invalid structured output")
        return completed.stdout, removed_secrets

    @staticmethod
    def _result_from_payload(payload: dict[str, Any]) -> CodexResult:
        now = datetime.now(timezone.utc)
        commands = tuple(
            CommandResult(
                command=item["command"], exit_code=item["exit_code"],
                summary=item["summary"], started_at=now, finished_at=now,
            )
            for item in payload["commands"]
        )
        return CodexResult(
            summary=payload["summary"], changed_files=tuple(payload["changed_files"]),
            repository_changes=tuple(
                RepositoryChangeClaim.model_validate(item)
                for item in payload.get("repository_changes", [])
            ),
            commands=commands, evidence=tuple(payload["evidence"]),
            review_findings=tuple(payload["review_findings"]), risks=tuple(payload["risks"]),
            unresolved_items=tuple(payload["unresolved_items"]),
            acceptance_coverage=tuple(
                AcceptanceCoverage.model_validate(item)
                for item in payload.get("acceptance_coverage", [])
            ),
            unrelated_changes_checked=payload.get("unrelated_changes_checked", False),
            root_cause_evidence=tuple(
                RootCauseEvidence.model_validate(item)
                for item in payload.get("root_cause_evidence", [])
            ),
            investigation_suggestions=tuple(
                payload.get("investigation_suggestions", [])
            ),
            behavior_before=payload.get("behavior_before", ""),
            behavior_after=payload.get("behavior_after", ""),
            impact_scope=tuple(payload.get("impact_scope", [])),
            risk_level=payload.get("risk_level", ""),
        )

    def _prepare_run_directory(self, run_id: str) -> Path:
        try:
            self.run_root.mkdir(parents=True, exist_ok=True)
            if _is_reparse_or_link(self.run_root) or not self.run_root.is_dir():
                raise UnsafeCodexRunError("run_root is not a safe directory")
            run_directory = self.run_root / run_id
            run_directory.mkdir(exist_ok=True)
            if _is_reparse_or_link(run_directory) or not run_directory.is_dir():
                raise UnsafeCodexRunError("run directory is not safe")
            if run_directory.resolve(strict=True).parent != self.run_root.resolve(strict=True):
                raise UnsafeCodexRunError("run directory escaped run_root")
            return run_directory
        except UnsafeCodexRunError:
            raise
        except OSError as error:
            raise UnsafeCodexRunError("run directory could not be prepared") from error

    @staticmethod
    def _prepare_isolation_directory(run_directory: Path) -> Path:
        isolation_root = run_directory / f".codex-env-{uuid.uuid4().hex}"
        try:
            isolation_root.mkdir(mode=0o700)
            if (
                _is_reparse_or_link(isolation_root)
                or not isolation_root.is_dir()
                or isolation_root.resolve(strict=True).parent
                != run_directory.resolve(strict=True)
            ):
                raise UnsafeCodexRunError("Codex environment directory is unsafe")
            return isolation_root
        except UnsafeCodexRunError:
            raise
        except OSError as error:
            raise UnsafeCodexRunError(
                "Codex environment directory could not be prepared"
            ) from error

    @staticmethod
    def _write_prompt(path: Path, content: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, flags, 0o600)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise UnsafeCodexRunError("prompt path is not a regular file")
                offset = 0
                while offset < len(content):
                    written = os.write(descriptor, content[offset:])
                    if written <= 0:
                        raise OSError("short prompt write")
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
                descriptor = None
            os.replace(temporary, path)
        except UnsafeCodexRunError:
            raise
        except OSError as error:
            raise UnsafeCodexRunError("prompt could not be persisted") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _validate_output(
        self, text: str, mapping: RepositoryMapping | None
    ) -> dict[str, Any]:
        try:
            payload = self._parse_output(text)
            if payload.get("repository_changes"):
                raise ValueError("single repository output cannot contain group claims")
            if mapping is None and (payload["changed_files"] or payload["commands"]):
                raise ValueError("preflight cannot claim repository effects")
            for path in payload["changed_files"]:
                normalized = PurePosixPath(path).as_posix()
                if mapping is None or normalized != path or not _path_allowed(
                    path, mapping.allowed_paths
                ):
                    raise ValueError("unsafe changed path")
        except Exception as error:
            raise CodexOutputError("Codex returned invalid structured output") from error
        return payload

    def _validate_group_output(
        self, text: str, group: RepositoryGroupMapping
    ) -> dict[str, Any]:
        try:
            payload = self._parse_output(text)
            if payload["changed_files"]:
                raise ValueError("group output must use repository change claims")
            mappings = {item.key: item for item in group.repositories}
            claims = payload.get("repository_changes", [])
            if not isinstance(claims, list):
                raise ValueError("group claims must be a list")
            for claim in claims:
                parsed = RepositoryChangeClaim.model_validate(claim)
                mapping = mappings.get(parsed.repository_key)
                if mapping is None or not _path_allowed(parsed.path, mapping.allowed_paths):
                    raise ValueError("unsafe repository change claim")
        except Exception as error:
            raise CodexOutputError("Codex returned invalid structured output") from error
        return payload

    def _parse_output(self, text: str) -> dict[str, Any]:
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(payload)
        for command in payload["commands"]:
            if _is_forbidden_command(command["command"]):
                raise ValueError("publication command is forbidden")
        return payload


__all__ = [
    "CodexCommand", "CodexExecutionError", "CodexOutputError", "CodexProcessStartError",
    "CodexRunner", "CodexRunnerError", "CodexTimeoutError", "UnsafeCodexRunError",
    "resolve_codex_command",
]
