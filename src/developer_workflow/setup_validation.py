"""Fail-closed discovery and read-only bootstrap capability checks.

This module deliberately returns only a small, fixed result vocabulary.  Raw
transport, process, credential and filesystem errors never cross its boundary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import inspect
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tomllib
from typing import Any, Callable, Literal, Mapping, Protocol
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, StrictStr, field_validator, model_validator

from .codex_runner import _bounded_subprocess
from .contracts import WorkflowModel
from .private_paths import (
    _ADMINISTRATORS_SID,
    _SYSTEM_SID,
    _current_user_sid,
    _has_link_or_reparse_ancestor,
    _is_link_or_reparse,
    _validate_shape,
    _windows_descriptor,
)
from .requirement_flow import SandboxCommandExecutor
from .setup_models import SetupModel, SetupValidationError


PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_MAX_DOCUMENT_BYTES = 1024 * 1024
_CATEGORIES = Literal[
    "ok",
    "authentication",
    "unreachable",
    "tls",
    "timeout",
    "incompatible",
    "unsafe_path",
    "sandbox",
    "invalid_field",
]


class SetupStep(str, Enum):
    PROFILE = "profile"
    ONES = "ones"
    REPOSITORIES = "repositories"
    PROVIDER = "provider"
    CODEX = "codex"
    PRIVATE_PATHS = "private_paths"
    REVIEW = "review"


class ValidationStatus(str, Enum):
    NOT_CONFIGURED = "not_configured"
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class ConnectionTestResult(WorkflowModel):
    """Non-sensitive result safe for logs and TUI rendering."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    step: SetupStep
    status: ValidationStatus
    category: _CATEGORIES


def _safe_identifier(value: str, field_name: str) -> str:
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


class OnesProbeInput(SetupModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    team_id: StrictStr
    project_id: StrictStr
    status_id: StrictStr
    item_id: StrictStr
    issue_type_id: StrictStr | None = None

    @field_validator("team_id", "project_id", "status_id", "item_id", "issue_type_id")
    @classmethod
    def validate_ids(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _safe_identifier(value, info.field_name)


class ProviderProbeInput(SetupModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    host: StrictStr
    api_url: StrictStr

    @model_validator(mode="after")
    def validate_binding(self) -> ProviderProbeInput:
        parsed = urlsplit(self.api_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.hostname.casefold() != self.host.casefold()
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("provider endpoint is invalid")
        return self


class RepositoryProbeInput(SetupModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    path: Path
    remote_url: StrictStr

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("repository path must be absolute")
        return value

    @field_validator("remote_url")
    @classmethod
    def validate_remote(cls, value: str) -> str:
        if re.fullmatch(r"git@[^\s:]+:[^\s]+", value):
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "ssh"} or parsed.username or parsed.password:
            raise ValueError("repository remote is invalid")
        return value


class CodexProbeInput(SetupModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    profile: StrictStr
    worktree: Path

    @field_validator("profile")
    @classmethod
    def validate_profile(cls, value: str) -> str:
        if PROFILE_RE.fullmatch(value) is None:
            raise ValueError("profile is invalid")
        return value

    @field_validator("worktree")
    @classmethod
    def validate_worktree(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("worktree must be absolute")
        return value


class PrivatePathsProbeInput(SetupModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    paths: tuple[Path, ...] = Field(min_length=3, max_length=3)

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        if any(not path.is_absolute() for path in value):
            raise ValueError("private paths must be absolute")
        return value


class DoctorRunner(Protocol):
    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]: ...


class SandboxExecutorFactory(Protocol):
    def __call__(self, profile: str) -> Any: ...


class ProviderTransport(Protocol):
    def get(self, url: str, *, timeout: float) -> Any: ...


class GitReadOnlyRunner(Protocol):
    def run(self, argv: tuple[str, ...], *, cwd: Path, timeout: float) -> Any: ...


class CodexAuthMetadata(Protocol):
    def metadata(self) -> Mapping[str, object]: ...


def _default_doctor_runner(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    max_output_bytes: int,
    shell: bool,
) -> subprocess.CompletedProcess[str]:
    if shell is not False:
        raise ValueError("doctor shell boundary is invalid")
    return _bounded_subprocess(
        argv,
        cwd=cwd,
        env=env,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
    )


def _clean_doctor_environment() -> dict[str, str]:
    allowed = {"comspec", "path", "pathext", "systemroot", "windir", "lang", "lc_all"}
    return {
        key: value
        for key, value in os.environ.items()
        if key.casefold() in allowed
        and not any(word in key.casefold() for word in ("token", "secret", "password", "key"))
    }


def _default_file_security(path: Path, admin: bool) -> bool:
    try:
        if not path.is_absolute() or _has_link_or_reparse_ancestor(path) or _is_link_or_reparse(path):
            return False
        metadata = path.stat()
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
            return False
        if os.name != "nt":
            return metadata.st_uid == os.geteuid() and stat.S_IMODE(metadata.st_mode) & 0o077 == 0
        owner, entries, protected = _windows_descriptor(path)
        user_sid = _current_user_sid()
        trusted = {_SYSTEM_SID, _ADMINISTRATORS_SID} if admin else {
            user_sid,
            _SYSTEM_SID,
            _ADMINISTRATORS_SID,
        }
        required_owners = {_SYSTEM_SID, _ADMINISTRATORS_SID} if admin else {user_sid}
        principals = {entry[0] for entry in entries}
        return (
            protected
            and owner in required_owners
            and bool(entries)
            and principals <= trusted
            and all(ace_type == 0 and flags & 0x10 == 0 for _, _, flags, ace_type in entries)
        )
    except (OSError, TypeError, ValueError):
        return False


def _read_secure_bytes(path: Path, *, admin: bool, security: Callable[[Path, bool], bool]) -> bytes:
    try:
        canonical = path.resolve(strict=True)
        if canonical != path.absolute() or not security(canonical, admin):
            raise SetupValidationError("managed profile source is unsafe")
        before = canonical.stat()
        if before.st_size > _MAX_DOCUMENT_BYTES:
            raise SetupValidationError("managed profile source is invalid")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(canonical, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino, opened.st_size) != (
                before.st_dev,
                before.st_ino,
                before.st_size,
            ):
                raise SetupValidationError("managed profile source is unsafe")
            data = os.read(descriptor, _MAX_DOCUMENT_BYTES + 1)
        finally:
            os.close(descriptor)
        after = canonical.stat()
        if len(data) > _MAX_DOCUMENT_BYTES or (after.st_dev, after.st_ino, after.st_size) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
        ):
            raise SetupValidationError("managed profile source is unsafe")
        return data
    except SetupValidationError:
        raise
    except (OSError, TypeError, ValueError):
        raise SetupValidationError("managed profile source is unsafe") from None


@dataclass(slots=True)
class ManagedProfileCatalog:
    """Discover only configured profiles that pass the real sandbox probe."""

    codex_doctor: DoctorRunner = _default_doctor_runner
    trusted_admin_catalog: Path | None = None
    probe_worktree: Path | None = None
    executor_factory: SandboxExecutorFactory = SandboxCommandExecutor
    file_security: Callable[[Path, bool], bool] = _default_file_security
    timeout_seconds: float = 20.0
    max_output_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if (
            not callable(self.codex_doctor)
            or not callable(self.executor_factory)
            or not callable(self.file_security)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or not 1 <= self.max_output_bytes <= _MAX_DOCUMENT_BYTES
        ):
            raise SetupValidationError("managed profile catalog is invalid")

    def list_profiles(self) -> tuple[str, ...]:
        config_path = self._config_path_from_doctor()
        user_profiles = self._read_user_profiles(config_path)
        admin_profiles = self._read_admin_profiles()
        if len(user_profiles) != len(set(user_profiles)) or len(admin_profiles) != len(set(admin_profiles)):
            raise SetupValidationError("managed profile catalog is invalid")
        if set(user_profiles) & set(admin_profiles):
            raise SetupValidationError("managed profile catalog conflicts")
        candidates = tuple(sorted((*user_profiles, *admin_profiles)))
        worktree = (self.probe_worktree or Path.cwd()).resolve(strict=True)
        if not worktree.is_dir():
            raise SetupValidationError("managed profile worktree is unavailable")
        available: list[str] = []
        for name in candidates:
            try:
                executor = self.executor_factory(name)
                completed = executor(
                    ["git", "status", "--short"],
                    cwd=worktree,
                    env=_clean_doctor_environment(),
                    timeout=self.timeout_seconds,
                    max_output_bytes=64 * 1024,
                )
                if completed.returncode != 0:
                    raise RuntimeError
            except Exception:
                continue
            available.append(name)
        return tuple(available)

    def require_selected(self, selected: str) -> str:
        if type(selected) is not str or PROFILE_RE.fullmatch(selected) is None:
            raise SetupValidationError("managed profile selection is invalid")
        if selected not in self.list_profiles():
            raise SetupValidationError("managed profile is unavailable")
        return selected

    def _config_path_from_doctor(self) -> Path:
        try:
            completed = self.codex_doctor(
                ["codex", "doctor", "--json"],
                cwd=(self.probe_worktree or Path.cwd()).resolve(strict=True),
                env=_clean_doctor_environment(),
                timeout=self.timeout_seconds,
                max_output_bytes=self.max_output_bytes,
                shell=False,
            )
            if (
                completed.returncode not in {0, 1}
                or type(completed.stdout) is not str
                or type(completed.stderr) is not str
                or len(completed.stdout.encode("utf-8", errors="strict")) > self.max_output_bytes
                or len(completed.stderr.encode("utf-8", errors="strict")) > self.max_output_bytes
            ):
                raise ValueError
            document = json.loads(completed.stdout)
            if type(document) is not dict or set(document) != {
                "schemaVersion", "generatedAt", "overallStatus", "codexVersion", "checks"
            } or document["schemaVersion"] != 1 or type(document["checks"]) is not dict:
                raise ValueError
            check = document["checks"].get("config.load")
            if type(check) is not dict or not {
                "id", "category", "status", "summary", "details", "remediation", "durationMs"
            }.issubset(check) or check.get("id") != "config.load" or check.get("status") != "ok":
                raise ValueError
            details = check["details"]
            if type(details) is not dict or details.get("config.toml parse") != "ok":
                raise ValueError
            home = Path(details["CODEX_HOME"]).resolve(strict=True)
            config = Path(details["config.toml"]).resolve(strict=True)
            if config.name != "config.toml" or config.parent != home:
                raise ValueError
            return config
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            raise SetupValidationError("Codex profile discovery is unavailable") from None

    def _read_user_profiles(self, path: Path) -> tuple[str, ...]:
        raw = _read_secure_bytes(path, admin=False, security=self.file_security)
        try:
            document = tomllib.loads(raw.decode("utf-8", errors="strict"))
            permissions = document.get("permissions")
            if type(permissions) is not dict:
                raise ValueError
            names: list[str] = []
            allowed = {"description", "extends", "workspace_roots", "filesystem", "network"}
            for name, profile in permissions.items():
                if PROFILE_RE.fullmatch(name) is None or type(profile) is not dict or not set(profile) <= allowed:
                    raise ValueError
                names.append(name)
            return tuple(names)
        except (UnicodeError, tomllib.TOMLDecodeError, TypeError, ValueError):
            raise SetupValidationError("Codex permissions table is invalid") from None

    def _read_admin_profiles(self) -> tuple[str, ...]:
        path = self.trusted_admin_catalog
        if path is None:
            program_data = os.environ.get("PROGRAMDATA")
            if not program_data:
                return ()
            candidate = Path(program_data) / "ones-dev" / "managed-sandbox-profiles.json"
            if not candidate.exists():
                return ()
            path = candidate
        if not self.file_security(path.parent.resolve(strict=True), True):
            raise SetupValidationError("administrator profile catalog is unsafe")
        raw = _read_secure_bytes(path, admin=True, security=self.file_security)
        try:
            document = json.loads(raw.decode("utf-8", errors="strict"))
            if type(document) is not dict or set(document) != {"schema_version", "profiles"}:
                raise ValueError
            profiles = document["profiles"]
            if document["schema_version"] != 1 or type(profiles) is not list or any(
                type(name) is not str or PROFILE_RE.fullmatch(name) is None for name in profiles
            ):
                raise ValueError
            if len(profiles) != len(set(profiles)):
                raise ValueError
            return tuple(profiles)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise SetupValidationError("administrator profile catalog is invalid") from None


async def _await_call(call: Any) -> Any:
    return await call if inspect.isawaitable(call) else call


def _result(step: SetupStep, category: _CATEGORIES = "ok") -> ConnectionTestResult:
    return ConnectionTestResult(
        step=step,
        status=ValidationStatus.PASSED if category == "ok" else ValidationStatus.FAILED,
        category=category,
    )


def _failure_category(error: Exception, *, default: _CATEGORIES = "unreachable") -> _CATEGORIES:
    name = type(error).__name__.casefold()
    if isinstance(error, (asyncio.TimeoutError, TimeoutError, subprocess.TimeoutExpired)) or "timeout" in name:
        return "timeout"
    if "ssl" in name or "tls" in name or "certificate" in name:
        return "tls"
    if "auth" in name or "permission" in name or "forbidden" in name or "unauthorized" in name:
        return "authentication"
    if isinstance(error, (TypeError, ValueError)):
        return "incompatible"
    return default


@dataclass(slots=True)
class SetupValidator:
    """Orchestrate bounded probes that cannot perform business writes."""

    ones_gateway: Any = None
    ones_transport: Any = None
    provider_transport: ProviderTransport | None = None
    git_runner: GitReadOnlyRunner | None = None
    command_executor: Any = None
    codex_auth_metadata: CodexAuthMetadata | Callable[[], Mapping[str, object]] | None = None
    profile_catalog: Any = None
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise SetupValidationError("setup validator timeout is invalid")

    async def probe_ones(self, probe: OnesProbeInput) -> ConnectionTestResult:
        gateway = self.ones_gateway if self.ones_gateway is not None else self.ones_transport
        if gateway is None:
            return _result(SetupStep.ONES, "invalid_field")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                settings = getattr(gateway, "settings", None)
                configured_team = getattr(settings, "team_id", probe.team_id)
                if configured_team != probe.team_id:
                    return _result(SetupStep.ONES, "invalid_field")
                if hasattr(gateway, "authenticate"):
                    await _await_call(gateway.authenticate())
                if hasattr(gateway, "get_team"):
                    await _await_call(gateway.get_team(probe.team_id))
                if hasattr(gateway, "get_project"):
                    await _await_call(gateway.get_project(probe.project_id))
                else:
                    projects = await _await_call(gateway.list_projects(include_archived=False))
                    if not any(str(item.get("uuid", item.get("id", ""))) == probe.project_id for item in projects):
                        raise ValueError
                if hasattr(gateway, "get_status"):
                    await _await_call(gateway.get_status(probe.status_id))
                else:
                    statuses = await _await_call(
                        gateway.list_defect_statuses(probe.project_id, probe.issue_type_id or probe.status_id)
                    )
                    if not any(str(getattr(item, "id", "")) == probe.status_id for item in statuses):
                        raise ValueError
                await _await_call(gateway.list_comments(probe.item_id, page_size=1))
            return _result(SetupStep.ONES)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            return _result(SetupStep.ONES, _failure_category(error))

    async def probe_provider(self, probe: ProviderProbeInput) -> ConnectionTestResult:
        if self.provider_transport is None:
            return _result(SetupStep.PROVIDER, "invalid_field")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                response = await _await_call(
                    self.provider_transport.get(probe.api_url, timeout=self.timeout_seconds)
                )
            status = response if type(response) is int else getattr(response, "status_code", None)
            if status in {401, 403}:
                return _result(SetupStep.PROVIDER, "authentication")
            if type(status) is not int or not 200 <= status < 300:
                return _result(SetupStep.PROVIDER, "incompatible")
            return _result(SetupStep.PROVIDER)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            return _result(SetupStep.PROVIDER, _failure_category(error))

    async def probe_repository(self, probe: RepositoryProbeInput) -> ConnectionTestResult:
        if self.git_runner is None:
            return _result(SetupStep.REPOSITORIES, "invalid_field")
        try:
            root = probe.path.resolve(strict=True)
            if not root.is_dir() or _has_link_or_reparse_ancestor(root):
                return _result(SetupStep.REPOSITORIES, "unsafe_path")
            identity = root.stat()

            async def run(arguments: tuple[str, ...]) -> str:
                call = self.git_runner.run(arguments, cwd=root, timeout=self.timeout_seconds)
                completed = await _await_call(call)
                if isinstance(completed, subprocess.CompletedProcess):
                    if completed.returncode != 0:
                        raise RuntimeError
                    return completed.stdout
                if type(completed) is not str:
                    raise RuntimeError
                return completed

            before = (
                await run(("git", "rev-parse", "HEAD")),
                await run(("git", "status", "--porcelain=v1", "--untracked-files=all")),
                await run(("git", "diff", "--cached", "--binary", "--no-ext-diff")),
            )
            await run(("git", "ls-remote", "--refs", probe.remote_url))
            after = (
                await run(("git", "rev-parse", "HEAD")),
                await run(("git", "status", "--porcelain=v1", "--untracked-files=all")),
                await run(("git", "diff", "--cached", "--binary", "--no-ext-diff")),
            )
            final = root.stat()
            if before != after or (identity.st_dev, identity.st_ino) != (final.st_dev, final.st_ino):
                return _result(SetupStep.REPOSITORIES, "unsafe_path")
            return _result(SetupStep.REPOSITORIES)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            return _result(SetupStep.REPOSITORIES, _failure_category(error))

    async def probe_codex(self, probe: CodexProbeInput) -> ConnectionTestResult:
        try:
            metadata_source = self.codex_auth_metadata
            metadata = metadata_source.metadata() if hasattr(metadata_source, "metadata") else metadata_source()
            metadata = await _await_call(metadata)
            if type(metadata) is not dict or metadata.get("configured") is not True or set(metadata) - {"configured", "mode"}:
                return _result(SetupStep.CODEX, "authentication")
            if self.profile_catalog is None:
                return _result(SetupStep.CODEX, "sandbox")
            self.profile_catalog.require_selected(probe.profile)
            executor = self.command_executor or SandboxCommandExecutor(permission_profile=probe.profile)
            completed = executor(
                ["git", "status", "--short"],
                cwd=probe.worktree,
                env={},
                timeout=self.timeout_seconds,
                max_output_bytes=64 * 1024,
            )
            if completed.returncode != 0:
                return _result(SetupStep.CODEX, "sandbox")
            return _result(SetupStep.CODEX)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            return _result(SetupStep.CODEX, _failure_category(error, default="sandbox"))

    async def probe_private_paths(self, probe: PrivatePathsProbeInput) -> ConnectionTestResult:
        try:
            _validate_shape(probe.paths, require_three=True)
            for candidate in probe.paths:
                current = candidate
                while not current.exists() and current.parent != current:
                    current = current.parent
                if not current.exists() or _has_link_or_reparse_ancestor(current):
                    raise ValueError
                resolved = current.resolve(strict=True)
                if not resolved.is_dir():
                    raise ValueError
            return _result(SetupStep.PRIVATE_PATHS)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            return _result(SetupStep.PRIVATE_PATHS, "unsafe_path")


__all__ = [
    "CodexProbeInput",
    "CodexAuthMetadata",
    "ConnectionTestResult",
    "ManagedProfileCatalog",
    "OnesProbeInput",
    "PrivatePathsProbeInput",
    "PROFILE_RE",
    "ProviderProbeInput",
    "ProviderTransport",
    "GitReadOnlyRunner",
    "RepositoryProbeInput",
    "SetupStep",
    "SetupValidator",
    "ValidationStatus",
]
