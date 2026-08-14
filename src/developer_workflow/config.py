"""Secret-free JSON configuration for developer workflows."""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path
from string import Formatter
from typing import Any

from pydantic import ConfigDict, Field, StrictInt, field_validator, model_validator

from .contracts import (
    RepositoryGroupMapping,
    RepositoryMapping,
    RepositoryRole,
    WorkflowModel,
    validate_git_ref_name,
)


class ConfigSecretError(ValueError):
    """Raised when a config key could carry credential material."""


class ConfigValidationError(ValueError):
    """Raised when the configuration document cannot be structurally loaded."""


class RepositoryMappingNotFound(LookupError):
    """Raised when no authorized repository mapping matches a request."""


class PublishingProvider(str, Enum):
    GITHUB = "github"
    GITLAB = "gitlab"
    LOCAL_FAKE = "local_fake"


class SandboxPermissionProfileSource(str, Enum):
    MANAGED = "managed"
    BUILTIN_WORKSPACE = "builtin_workspace"


BUILTIN_WORKSPACE_PROFILE = "ones-dev-workspace"


class PublishingConfig(WorkflowModel):
    provider: PublishingProvider
    default_target_branch: str = "main"
    commit_template: str = "{summary}"
    pr_title_template: str = "{summary}"
    pr_body_template: str = "{body}"

    @field_validator("commit_template", "pr_title_template", "pr_body_template")
    @classmethod
    def validate_template(cls, value: str, info: Any) -> str:
        if not value.strip():
            raise ValueError("publishing templates must not be empty")
        allowed_fields = {
            "run_id",
            "work_item_id",
            "number",
            "title",
            "summary",
            "branch",
            "base_branch",
            "pr_url",
        }
        if info.field_name == "pr_body_template":
            allowed_fields.add("body")
        try:
            parsed = Formatter().parse(value)
            for _, field_name, format_spec, conversion in parsed:
                if field_name is None:
                    continue
                if (
                    field_name not in allowed_fields
                    or conversion is not None
                    or bool(format_spec)
                ):
                    raise ValueError("publishing template contains an unsafe field")
        except ValueError as exc:
            raise ValueError("publishing template is invalid") from exc
        return value

    @field_validator("default_target_branch")
    @classmethod
    def validate_target_branch(cls, value: str) -> str:
        return validate_git_ref_name(value)


class DeveloperWorkflowConfig(WorkflowModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    run_root: Path
    worktree_root: Path
    mirror_root: Path
    sandbox_permission_profile: str
    sandbox_permission_profile_source: SandboxPermissionProfileSource = (
        SandboxPermissionProfileSource.MANAGED
    )
    max_codex_attempts: int = Field(ge=1, le=10)
    tui_max_concurrency: StrictInt = Field(default=3, ge=1, le=8)
    repositories: tuple[RepositoryMapping, ...] = Field(default_factory=tuple)
    repository_groups: tuple[RepositoryGroupMapping, ...] = Field(default_factory=tuple)
    publishing: PublishingConfig

    @field_validator("sandbox_permission_profile")
    @classmethod
    def validate_sandbox_permission_profile(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is None:
            raise ValueError(
                "sandbox_permission_profile must name an installed managed Codex permissions profile"
            )
        return value

    @classmethod
    def validate_sandbox_permission_profile_binding(
        cls, profile: str, source: SandboxPermissionProfileSource
    ) -> None:
        if (
            profile == BUILTIN_WORKSPACE_PROFILE
            and source is not SandboxPermissionProfileSource.BUILTIN_WORKSPACE
        ) or (
            profile != BUILTIN_WORKSPACE_PROFILE
            and source is not SandboxPermissionProfileSource.MANAGED
        ):
            raise ValueError("sandbox permission profile source is invalid")

    @model_validator(mode="after")
    def validate_unique_repositories(self) -> DeveloperWorkflowConfig:
        self.validate_sandbox_permission_profile_binding(
            self.sandbox_permission_profile,
            self.sandbox_permission_profile_source,
        )
        groups = self.normalized_groups()
        keys = [mapping.key for mapping in groups]
        pairs = [(mapping.project_id, mapping.iteration_id) for mapping in groups]
        if not groups:
            raise ValueError("at least one repository mapping or group is required")
        if len(keys) != len(set(keys)):
            raise ValueError("repository mapping and group keys must be unique")
        if len(pairs) != len(set(pairs)):
            raise ValueError("project and iteration mappings must be unique")
        return self

    def normalized_groups(self) -> tuple[RepositoryGroupMapping, ...]:
        legacy = tuple(
            RepositoryGroupMapping(
                key=mapping.key,
                project_id=mapping.project_id,
                iteration_id=mapping.iteration_id,
                primary_repository=mapping.key,
                repositories=(
                    mapping.validated_update(
                        role=RepositoryRole.PRIMARY,
                        depends_on=(),
                    ),
                ),
            )
            for mapping in self.repositories
        )
        return (*legacy, *self.repository_groups)

    @classmethod
    def load(cls, path: str | Path) -> DeveloperWorkflowConfig:
        config_path = Path(path).resolve()
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        _reject_secret_keys(raw)
        if not isinstance(raw, dict):
            raise ConfigValidationError("configuration root must be an object")
        if "sandbox_permission_profile" not in raw:
            raise ConfigValidationError(
                "configuration requires sandbox_permission_profile naming an installed "
                "managed Codex permissions profile"
            )
        required_paths = ("run_root", "worktree_root", "mirror_root")
        missing = [field_name for field_name in required_paths if field_name not in raw]
        if missing:
            raise ConfigValidationError(
                f"configuration is missing required path fields: {', '.join(missing)}"
            )
        for field_name in required_paths:
            try:
                raw_path = Path(raw[field_name])
            except TypeError as exc:
                raise ConfigValidationError(f"{field_name} must be a path string") from exc
            if not raw_path.is_absolute():
                raw_path = config_path.parent / raw_path
            # Preserve the lexical path so the production private-root boundary
            # can detect a symlink/reparse point in any existing ancestor.
            raw[field_name] = raw_path.absolute()
        return cls.model_validate(raw)

    def resolve_repository(self, project_id: str, iteration_id: str) -> RepositoryMapping:
        exact = next(
            (
                mapping
                for mapping in self.repositories
                if mapping.project_id == project_id
                and mapping.iteration_id == iteration_id
            ),
            None,
        )
        if exact is not None:
            return exact
        default = next(
            (
                mapping
                for mapping in self.repositories
                if mapping.project_id == project_id and mapping.iteration_id == "*"
            ),
            None,
        )
        if default is not None:
            return default
        raise RepositoryMappingNotFound(f"no mapping for {project_id}/{iteration_id}")

    def resolve_mapping_key(
        self, key: str, project_id: str, iteration_id: str
    ) -> RepositoryMapping:
        mapping = next(
            (candidate for candidate in self.repositories if candidate.key == key), None
        )
        if (
            mapping is None
            or mapping.project_id != project_id
            or mapping.iteration_id not in {iteration_id, "*"}
        ):
            raise RepositoryMappingNotFound(
                f"mapping {key!r} is not valid for {project_id}/{iteration_id}"
            )
        return mapping

    def resolve_repository_group(
        self, project_id: str, iteration_id: str
    ) -> RepositoryGroupMapping:
        groups = self.normalized_groups()
        exact = next(
            (
                group
                for group in groups
                if group.project_id == project_id and group.iteration_id == iteration_id
            ),
            None,
        )
        if exact is not None:
            return exact
        default = next(
            (
                group
                for group in groups
                if group.project_id == project_id and group.iteration_id == "*"
            ),
            None,
        )
        if default is not None:
            return default
        raise RepositoryMappingNotFound(f"no mapping for {project_id}/{iteration_id}")

    def resolve_group_key(
        self, key: str, project_id: str, iteration_id: str
    ) -> RepositoryGroupMapping:
        group = next(
            (candidate for candidate in self.normalized_groups() if candidate.key == key),
            None,
        )
        if (
            group is None
            or group.project_id != project_id
            or group.iteration_id not in {iteration_id, "*"}
        ):
            raise RepositoryMappingNotFound(
                f"mapping {key!r} is not valid for {project_id}/{iteration_id}"
            )
        return group


_SECRET_TOKENS = {
    "password",
    "token",
    "secret",
    "pat",
    "credential",
    "authorization",
    "cookie",
}


def _key_tokens(key: object) -> tuple[str, ...]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(key))
    return tuple(token for token in re.split(r"[^A-Za-z0-9]+", text.casefold()) if token)


def _is_secret_key(key: object) -> bool:
    compact = str(key).casefold()
    if compact in _SECRET_TOKENS or compact in {"apikey", "privatekey"}:
        return True
    tokens = _key_tokens(key)
    if any(token in _SECRET_TOKENS for token in tokens):
        return True
    pairs = set(zip(tokens, tokens[1:]))
    return ("api", "key") in pairs or ("private", "key") in pairs


def _reject_secret_keys(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key)
            if _is_secret_key(key):
                location = ".".join((*path, key_text))
                raise ConfigSecretError(f"secret-bearing config key is forbidden: {location}")
            _reject_secret_keys(nested, (*path, key_text))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_secret_keys(nested, (*path, str(index)))


__all__ = [
    "ConfigSecretError",
    "ConfigValidationError",
    "DeveloperWorkflowConfig",
    "BUILTIN_WORKSPACE_PROFILE",
    "PublishingConfig",
    "PublishingProvider",
    "RepositoryMappingNotFound",
    "SandboxPermissionProfileSource",
]
