"""Strict, secret-free bootstrap configuration and explicit runtime inputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Literal, Mapping
import unicodedata
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, StrictInt, StrictStr, field_validator

from .config import DeveloperWorkflowConfig, PublishingConfig
from .contracts import RepositoryGroupMapping, RepositoryMapping, WorkflowModel


class SetupValidationError(ValueError):
    """Raised when bootstrap configuration cannot safely be activated."""


class SecretKind(str, Enum):
    ONES_EMAIL = "ones_email"
    ONES_PASSWORD = "ones_password"
    PROVIDER_TOKEN = "provider_token"
    CODEX_API_KEY = "codex_api_key"
    CODEX_AUTH_TOKEN = "codex_auth_token"
    GIT_ASKPASS = "git_askpass"
    GIT_SSH = "git_ssh"
    GIT_SSH_COMMAND = "git_ssh_command"
    SSH_ASKPASS = "ssh_askpass"
    SSH_AUTH_SOCK = "ssh_auth_sock"


_BIDI_CONTROLS = {
    "LRE",
    "RLE",
    "LRO",
    "RLO",
    "PDF",
    "LRI",
    "RLI",
    "FSI",
    "PDI",
}
_BIDI_CONTROL_CHARACTERS = {"\u061c", "\u200e", "\u200f"}


def _validated_text(value: str, field_name: str, *, maximum: int = 4096) -> str:
    if not value or value != value.strip():
        raise ValueError(f"{field_name} is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise ValueError(f"{field_name} is invalid") from None
    if (
        len(value) > maximum
        or len(encoded) > maximum * 4
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            or unicodedata.bidirectional(character) in _BIDI_CONTROLS
            or character in _BIDI_CONTROL_CHARACTERS
            for character in value
        )
    ):
        raise ValueError(f"{field_name} is invalid")
    return value


def _validated_https_url(value: str, field_name: str) -> str:
    _validated_text(value, field_name)
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(f"{field_name} must be a credential-free HTTPS URL") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 1 <= port <= 65535
    ):
        raise ValueError(f"{field_name} must be a credential-free HTTPS URL")
    return value


def _validated_absolute_path(value: Path | None, field_name: str) -> Path | None:
    if value is not None:
        _validated_text(str(value), field_name)
        if not value.is_absolute():
            raise ValueError(f"{field_name} must be absolute")
    return value


class RuntimePublicConfig(WorkflowModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ones_base_url: StrictStr
    ones_team_id: StrictStr
    ones_issue_type_id: StrictStr
    ones_comment_list_path_template: StrictStr
    provider_host: StrictStr
    provider_api_url: StrictStr
    git_author_name: StrictStr
    git_author_email: StrictStr
    codex_auth_mode: Literal["credential", "file"]
    codex_home: Path | None = None

    @field_validator(
        "ones_team_id",
        "ones_issue_type_id",
        "git_author_name",
    )
    @classmethod
    def validate_plain_text(cls, value: str, info: Any) -> str:
        return _validated_text(value, info.field_name, maximum=320)

    @field_validator("ones_base_url", "provider_api_url")
    @classmethod
    def validate_https_urls(cls, value: str, info: Any) -> str:
        return _validated_https_url(value, info.field_name)

    @field_validator("provider_host")
    @classmethod
    def validate_provider_host(cls, value: str) -> str:
        _validated_text(value, "provider_host", maximum=253)
        parsed = urlsplit(f"//{value}")
        if (
            parsed.hostname != value
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("provider_host must be a bare host name")
        return value

    @field_validator("ones_comment_list_path_template")
    @classmethod
    def validate_comment_path_template(cls, value: str) -> str:
        _validated_text(value, "ones_comment_list_path_template")
        parsed = urlsplit(value)
        if (
            not value.startswith("/")
            or value.startswith("//")
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("ones_comment_list_path_template must be an absolute URL path")
        return value

    @field_validator("git_author_email")
    @classmethod
    def validate_git_author_email(cls, value: str) -> str:
        _validated_text(value, "git_author_email", maximum=320)
        if re.fullmatch(r"[^@\s<>]+@[^@\s<>]+", value) is None:
            raise ValueError("git_author_email is invalid")
        return value

    @field_validator("codex_home")
    @classmethod
    def validate_codex_home(cls, value: Path | None) -> Path | None:
        return _validated_absolute_path(value, "codex_home")


class WorkflowDraft(WorkflowModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    run_root: Path | None = None
    mirror_root: Path | None = None
    worktree_root: Path | None = None
    sandbox_permission_profile: StrictStr | None = None
    max_codex_attempts: StrictInt = Field(default=3, ge=1, le=10)
    tui_max_concurrency: StrictInt = Field(default=3, ge=1, le=8)
    repositories: tuple[RepositoryMapping, ...] = Field(default_factory=tuple)
    repository_groups: tuple[RepositoryGroupMapping, ...] = Field(default_factory=tuple)
    publishing: PublishingConfig | None = None

    @field_validator("run_root", "mirror_root", "worktree_root")
    @classmethod
    def validate_absolute_paths(cls, value: Path | None, info: Any) -> Path | None:
        return _validated_absolute_path(value, info.field_name)

    @field_validator("sandbox_permission_profile")
    @classmethod
    def validate_sandbox_permission_profile(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return DeveloperWorkflowConfig.validate_sandbox_permission_profile(value)


class SetupDraft(WorkflowModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    runtime: RuntimePublicConfig | None = None
    workflow: WorkflowDraft = Field(default_factory=WorkflowDraft)
    detected_secret_kinds: tuple[SecretKind, ...] = Field(default_factory=tuple)


class ActiveSetup(WorkflowModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    generation: StrictStr
    runtime: RuntimePublicConfig
    workflow: DeveloperWorkflowConfig
    credential_kinds: tuple[SecretKind, ...]

    @field_validator("generation")
    @classmethod
    def validate_generation(cls, value: str) -> str:
        _validated_text(value, "generation", maximum=32)
        if re.fullmatch(r"[0-9a-f]{32}", value) is None:
            raise ValueError("generation must be 32 lowercase hexadecimal characters")
        return value


class SetupDocument(WorkflowModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal[1] = 1
    profile_id: StrictStr
    active: ActiveSetup | None = None
    previous: ActiveSetup | None = None
    draft: SetupDraft = Field(default_factory=SetupDraft)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_strict_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @field_validator("profile_id")
    @classmethod
    def validate_profile_id(cls, value: str) -> str:
        _validated_text(value, "profile_id", maximum=128)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is None:
            raise ValueError("profile_id is invalid")
        return value


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeSecrets:
    values: Mapping[SecretKind, str] = field(repr=False)

    def __post_init__(self) -> None:
        copied: dict[SecretKind, str] = {}
        for kind, value in self.values.items():
            if type(kind) is not SecretKind or type(value) is not str:
                raise SetupValidationError("runtime credential is invalid")
            copied[kind] = value
        object.__setattr__(self, "values", MappingProxyType(copied))

    def require(self, kind: SecretKind) -> str:
        value = self.values.get(kind, "")
        if not value:
            raise SetupValidationError("runtime credential is unavailable")
        return value


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeInputs:
    public: RuntimePublicConfig
    secrets: RuntimeSecrets = field(repr=False)


__all__ = [
    "ActiveSetup",
    "RuntimeInputs",
    "RuntimePublicConfig",
    "RuntimeSecrets",
    "SecretKind",
    "SetupDocument",
    "SetupDraft",
    "SetupValidationError",
    "WorkflowDraft",
]
