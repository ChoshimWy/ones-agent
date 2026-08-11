"""Persistable contracts for the isolated developer workflows."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal, Mapping
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from src.contracts import DefectRecord, RequirementRecord, WikiPageRef, WikiPageSnapshot
from .command_utils import CommandArgvError, parse_command_argv


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp suitable for persistence."""

    return datetime.now(UTC)


def _non_empty(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    return value


def validate_git_ref_name(value: str) -> str:
    """Validate the shared safe subset used for configured Git branches."""

    if (
        not value
        or value != value.strip()
        or value.startswith("-")
        or value == "@"
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
        or any(character in "~^:?*[\\" for character in value)
        or ".." in value
        or "@{" in value
        or value.endswith((".", "/"))
    ):
        raise ValueError("branch is not a safe Git ref")
    segments = value.split("/")
    if any(
        not segment
        or segment in {".", ".."}
        or segment.startswith(".")
        or segment.casefold().endswith(".lock")
        for segment in segments
    ):
        raise ValueError("branch contains an unsafe Git ref segment")
    return value


class WorkflowModel(BaseModel):
    """Strict JSON-friendly base for workflow-owned models."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @field_validator("*", mode="after")
    @classmethod
    def normalize_datetimes(cls, value: Any) -> Any:
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(UTC)

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> WorkflowModel:
        data = self.model_dump(round_trip=True)
        if deep:
            data = deepcopy(data)
        if update:
            data.update(deepcopy(dict(update)) if deep else dict(update))
        return type(self).model_validate(data)

    def validated_update(self, **updates: Any) -> WorkflowModel:
        """Return a fully validated replacement with the supplied fields."""

        return self.model_copy(update=updates)


class WorkflowState(str, Enum):
    CREATED = "CREATED"
    READING_ONES = "READING_ONES"
    VALIDATING = "VALIDATING"
    PREPARING_REPO = "PREPARING_REPO"
    IMPLEMENTING = "IMPLEMENTING"
    TESTING = "TESTING"
    AI_REVIEW = "AI_REVIEW"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"


class WorkflowType(str, Enum):
    REQUIREMENT = "requirement"
    DEFECT = "defect"


class DefectCheckpoint(str, Enum):
    NONE = "NONE"
    ROOT_VERIFIED = "ROOT_VERIFIED"
    REPRODUCTION_PREPARED = "REPRODUCTION_PREPARED"
    REPRODUCTION_FAILED = "REPRODUCTION_FAILED"
    REPAIR_APPLIED = "REPAIR_APPLIED"
    FINAL_TESTED = "FINAL_TESTED"


class CommandOutcome(str, Enum):
    PASSED = "passed"
    TEST_FAILED = "test_failed"
    COMMAND_ERROR = "command_error"
    TIMEOUT = "timeout"
    SANDBOX_ERROR = "sandbox_error"


class RepositoryMapping(WorkflowModel):
    key: str
    project_id: str
    iteration_id: str
    repo_url: str
    repo_name: str
    base_branch: str = "main"
    test_commands: tuple[str, ...] = Field(default_factory=tuple)
    lint_commands: tuple[str, ...] = Field(default_factory=tuple)
    build_commands: tuple[str, ...] = Field(default_factory=tuple)
    allowed_paths: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("project_id", "iteration_id")
    @classmethod
    def validate_non_empty_fields(cls, value: str, info: Any) -> str:
        return _non_empty(value, info.field_name)

    @field_validator("base_branch")
    @classmethod
    def validate_base_branch(cls, value: str) -> str:
        return validate_git_ref_name(value)

    @field_validator("key", "repo_name")
    @classmethod
    def validate_safe_segment(cls, value: str, info: Any) -> str:
        _non_empty(value, info.field_name)
        if not re.fullmatch(r"[A-Za-z0-9._-]+", value) or value in {".", ".."}:
            raise ValueError(f"{info.field_name} must be one safe ASCII path segment")
        if info.field_name == "repo_name" and value.casefold().endswith(".lock"):
            raise ValueError("repo_name must not end with .lock")
        return value

    @field_validator("repo_url")
    @classmethod
    def validate_repo_url(cls, value: str) -> str:
        _non_empty(value, "repo_url")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("repo_url must not contain control characters")
        parsed = urlparse(value)
        if parsed.query or parsed.fragment:
            raise ValueError("repo_url must not contain query or fragment")
        if parsed.scheme in {"http", "https"}:
            if not parsed.hostname or parsed.username is not None or parsed.password is not None:
                raise ValueError("repo_url must not contain userinfo")
            return value
        if parsed.scheme == "ssh":
            if not parsed.hostname or parsed.password is not None:
                raise ValueError("ssh repo_url must not contain credential userinfo")
            return value
        if re.fullmatch(r"git@[^\s:]+:.+", value):
            return value
        if (
            Path(value).is_absolute()
            or PureWindowsPath(value).is_absolute()
            or PurePosixPath(value).is_absolute()
        ):
            return value
        raise ValueError("repo_url must be HTTP(S), SSH, git@, or an absolute local path")

    @field_validator("test_commands", "lint_commands", "build_commands")
    @classmethod
    def validate_commands(cls, commands: tuple[str, ...]) -> tuple[str, ...]:
        for command in commands:
            if not command.strip() or "\x00" in command:
                raise ValueError("commands must not be blank or contain NUL")
        return commands

    @field_validator("allowed_paths")
    @classmethod
    def validate_allowed_paths(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        normalized_paths: list[str] = []
        for raw_path in paths:
            if (
                not raw_path.strip()
                or any(ord(character) < 32 or ord(character) == 127 for character in raw_path)
                or ":" in raw_path
                or "\\" in raw_path
            ):
                raise ValueError("allowed_paths entries contain unsafe characters")
            windows_path = PureWindowsPath(raw_path)
            normalized = raw_path
            posix_path = PurePosixPath(normalized)
            raw_parts = normalized.split("/")
            if (
                windows_path.is_absolute()
                or bool(windows_path.drive)
                or posix_path.is_absolute()
                or any(part in {"", ".", ".."} for part in raw_parts)
            ):
                raise ValueError("allowed_paths must be repository-relative without traversal")
            normalized_paths.append(posix_path.as_posix())
        return tuple(normalized_paths)

    @model_validator(mode="after")
    def validate_unique_command_argv(self) -> RepositoryMapping:
        try:
            argv = [
                parse_command_argv(command)
                for command in (*self.lint_commands, *self.build_commands, *self.test_commands)
            ]
        except CommandArgvError as error:
            raise ValueError("repository command cannot be parsed") from error
        if len(argv) != len(set(argv)):
            raise ValueError("repository commands must be unique by canonical argv")
        return self


class DefectCandidate(WorkflowModel):
    """Frozen, neutral candidate summary safe to display to callers."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, frozen=True)

    uuid: str
    key: str
    number: str
    title: str
    priority: str
    status: str
    updated_at: str
    snapshot_token: str
    status_id: str = ""

    @field_validator("uuid", "key", "number", "title", "priority", "status", "updated_at", "snapshot_token")
    @classmethod
    def validate_candidate_text(cls, value: str, info: Any) -> str:
        return _non_empty(value, info.field_name)

    @field_validator("status_id")
    @classmethod
    def validate_status_id(cls, value: str) -> str:
        if value == "":
            return value
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
            raise ValueError("status_id is invalid")
        return value



class RootCauseSupportingPoint(WorkflowModel):
    """One observable support point used by the actionable-evidence gate."""

    kind: Literal["defect", "repo_resolution", "code", "cross_file"]
    description: str
    source: str
    file_path: str = ""
    snippet: str = ""
    start_line: StrictInt | None = None
    end_line: StrictInt | None = None
    direct_root_cause: StrictBool = False

    @field_validator("description", "source")
    @classmethod
    def validate_required_support_text(cls, value: str, info: Any) -> str:
        return _non_empty(value, info.field_name)

    @field_validator("file_path")
    @classmethod
    def validate_optional_support_path(cls, value: str) -> str:
        if value:
            RepositorySnapshot._validate_repository_path(value)
        return value

    @model_validator(mode="after")
    def validate_support_shape(self) -> RootCauseSupportingPoint:
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("support line range must include both endpoints")
        if self.start_line is not None and (
            self.start_line <= 0
            or self.end_line is None
            or self.end_line < self.start_line
        ):
            raise ValueError("support line range is invalid")
        if self.kind in {"code", "cross_file"} and (
            not self.file_path
            or not (self.snippet.strip() or self.start_line is not None)
        ):
            raise ValueError("repository support requires a file and observable content")
        return self


class RootCauseEvidence(WorkflowModel):
    """Repository-backed evidence required before a defect may be modified."""

    file_path: str
    location: str
    start_line: StrictInt | None = None
    end_line: StrictInt | None = None
    symbol: str = ""
    mechanism: str
    code_excerpt: str = ""
    call_chain: tuple[str, ...] = Field(default_factory=tuple)
    reproduction_test: str
    test_selector: str
    reproduction_command: str
    confidence: StrictFloat = Field(ge=0.0, le=1.0)
    insufficient_evidence: StrictBool
    impacted_files: tuple[str, ...] = Field(min_length=1)
    fix_steps: tuple[str, ...] = Field(min_length=1)
    supporting_points: tuple[RootCauseSupportingPoint, ...] = Field(min_length=1)

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, value: str) -> str:
        RepositorySnapshot._validate_repository_path(value)
        return value

    @field_validator("reproduction_test")
    @classmethod
    def validate_reproduction_path(cls, value: str) -> str:
        RepositorySnapshot._validate_repository_path(value)
        if value.startswith("-"):
            raise ValueError("reproduction path cannot be an option")
        return value

    @field_validator("test_selector")
    @classmethod
    def validate_test_selector(cls, value: str) -> str:
        if (
            not value
            or "\x00" in value
            or value.startswith("-")
            or any(character.isspace() for character in value)
        ):
            raise ValueError("test_selector is invalid")
        path, *node_parts = value.split("::")
        RepositorySnapshot._validate_repository_path(path)
        if path.startswith("-") or any(
            not part
            or part.startswith("-")
            or re.fullmatch(r"[A-Za-z0-9_.\-\[\]/]+", part) is None
            for part in node_parts
        ):
            raise ValueError("test_selector is invalid")
        return value

    @field_validator("reproduction_command")
    @classmethod
    def validate_reproduction_command(cls, value: str) -> str:
        return _non_empty(value, "reproduction_command")

    @field_validator("impacted_files")
    @classmethod
    def validate_impacted_files(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        for path in paths:
            RepositorySnapshot._validate_repository_path(path)
        if len(paths) != len(set(paths)):
            raise ValueError("impacted_files must be unique")
        return paths

    @field_validator("fix_steps")
    @classmethod
    def validate_fix_steps(cls, steps: tuple[str, ...]) -> tuple[str, ...]:
        if any(not step.strip() or "\x00" in step for step in steps):
            raise ValueError("fix_steps must be concrete non-empty text")
        return steps

    @field_validator("location", "mechanism")
    @classmethod
    def validate_required_text(cls, value: str, info: Any) -> str:
        return _non_empty(value, info.field_name)

    @field_validator("symbol", "code_excerpt")
    @classmethod
    def validate_optional_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("evidence text must not contain NUL")
        return value

    @field_validator("call_chain")
    @classmethod
    def validate_call_chain(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or "\x00" in item for item in value):
            raise ValueError("call_chain entries must not be empty")
        return value

    @model_validator(mode="after")
    def validate_location_and_support(self) -> RootCauseEvidence:
        if self.test_selector.split("::", 1)[0] != self.reproduction_test:
            raise ValueError("test_selector must target reproduction_test")
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("evidence line range must include both endpoints")
        if self.start_line is not None and (
            self.start_line <= 0 or self.end_line is None or self.end_line < self.start_line
        ):
            raise ValueError("evidence line range is invalid")
        if self.start_line is None and not self.symbol.strip():
            raise ValueError("evidence location must include a line range or symbol")
        if not (self.code_excerpt.strip() or self.call_chain or self.reproduction_test):
            raise ValueError("root cause evidence needs code, call-chain, or reproduction support")
        return self


class PreparedWorktree(WorkflowModel):
    path: Path
    branch: str
    base_commit: str
    head_commit: str
    mirror_path: Path

    @field_validator("branch")
    @classmethod
    def validate_branch_ref(cls, value: str) -> str:
        return validate_git_ref_name(value)

    @field_validator("base_commit", "head_commit")
    @classmethod
    def validate_commit_oid(cls, value: str) -> str:
        if len(value) not in {40, 64} or not re.fullmatch(r"[0-9a-f]+", value):
            raise ValueError("commit identity must be a canonical full object ID")
        return value

    @model_validator(mode="after")
    def validate_absolute_distinct_paths(self) -> PreparedWorktree:
        if not self.path.is_absolute() or not self.mirror_path.is_absolute():
            raise ValueError("prepared paths must be absolute")
        if self.path.resolve(strict=False) == self.mirror_path.resolve(strict=False):
            raise ValueError("worktree and mirror paths must be distinct")
        return self


class RepositorySnapshot(WorkflowModel):
    head_commit: str
    diff_sha256: str
    changed_files: tuple[str, ...] = Field(default_factory=tuple)
    patch: str = ""
    untracked_hashes: dict[str, str] = Field(default_factory=dict)
    is_clean: StrictBool = True

    @field_validator("head_commit")
    @classmethod
    def validate_head_commit_oid(cls, value: str) -> str:
        if len(value) not in {40, 64} or not re.fullmatch(r"[0-9a-f]+", value):
            raise ValueError("commit identity must be a canonical full object ID")
        return value

    @field_validator("diff_sha256")
    @classmethod
    def validate_diff_hash(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("diff_sha256 must be a canonical SHA-256 digest")
        return value

    @field_validator("changed_files")
    @classmethod
    def validate_changed_file_paths(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        for path in paths:
            cls._validate_repository_path(path)
        return paths

    @field_validator("untracked_hashes")
    @classmethod
    def validate_untracked_entries(cls, hashes: dict[str, str]) -> dict[str, str]:
        for path, digest in hashes.items():
            cls._validate_repository_path(path)
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("untracked hash must be a canonical SHA-256 digest")
        return hashes

    @staticmethod
    def _validate_repository_path(value: str) -> None:
        parts = value.split("/")
        if (
            not value
            or value.startswith("/")
            or ":" in value
            or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or any(part in {"", ".", ".."} for part in parts)
            or parts[0].casefold() == ".git"
        ):
            raise ValueError("changed path must be a safe repository-relative POSIX path")

    @model_validator(mode="after")
    def validate_clean_consistency(self) -> RepositorySnapshot:
        has_evidence = bool(self.patch or self.changed_files or self.untracked_hashes)
        if self.is_clean == has_evidence:
            raise ValueError("is_clean must match snapshot evidence")
        if self.is_clean and self.diff_sha256 != hashlib.sha256(b"").hexdigest():
            raise ValueError("clean snapshot must use the empty SHA-256 digest")
        return self


class CommandResult(WorkflowModel):
    command: str
    argv: tuple[str, ...] = Field(default_factory=tuple)
    exit_code: StrictInt
    summary: str
    started_at: datetime
    finished_at: datetime
    outcome: CommandOutcome | None = None
    output_sha256: str = Field(default_factory=lambda: hashlib.sha256(b"").hexdigest())

    @model_validator(mode="after")
    def validate_time_range(self) -> CommandResult:
        if any(not item or "\x00" in item for item in self.argv):
            raise ValueError("command argv entries must be non-empty")
        if self.outcome is None:
            object.__setattr__(self, "outcome", (
                CommandOutcome.PASSED if self.exit_code == 0 else CommandOutcome.COMMAND_ERROR
            ))
        if re.fullmatch(r"[0-9a-f]{64}", self.output_sha256) is None:
            raise ValueError("output_sha256 must be a canonical SHA-256 digest")
        if (self.outcome is CommandOutcome.PASSED) != (self.exit_code == 0):
            raise ValueError("command outcome must agree with exit_code")
        if self.outcome is CommandOutcome.TEST_FAILED and self.exit_code != 1:
            raise ValueError("test_failed requires the pytest failure exit code")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must be on or after started_at")
        return self


class AcceptanceCoverage(WorkflowModel):
    criterion_id: str
    criterion_text: str
    files: tuple[str, ...] = Field(min_length=1)
    tests: tuple[str, ...] = Field(min_length=1)

    @field_validator("criterion_id")
    @classmethod
    def validate_criterion_id(cls, value: str) -> str:
        if re.fullmatch(r"AC-[1-9][0-9]*", value) is None:
            raise ValueError("criterion_id must use AC-N")
        return value

    @field_validator("criterion_text")
    @classmethod
    def validate_criterion_text(cls, value: str) -> str:
        return _non_empty(value, "criterion_text")

    @field_validator("files")
    @classmethod
    def validate_files(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        for path in paths:
            RepositorySnapshot._validate_repository_path(path)
        if len(paths) != len(set(paths)):
            raise ValueError("coverage files must be unique")
        return paths

    @field_validator("tests")
    @classmethod
    def validate_tests(cls, commands: tuple[str, ...]) -> tuple[str, ...]:
        if any(not command.strip() or "\x00" in command for command in commands):
            raise ValueError("coverage tests must be non-empty configured commands")
        if len(commands) != len(set(commands)):
            raise ValueError("coverage tests must be unique")
        return commands


class CodexResult(WorkflowModel):
    summary: str = ""
    changed_files: tuple[str, ...] = Field(default_factory=tuple)
    commands: tuple[CommandResult, ...] = Field(default_factory=tuple)
    evidence: tuple[str, ...] = Field(default_factory=tuple)
    review_findings: tuple[str, ...] = Field(default_factory=tuple)
    risks: tuple[str, ...] = Field(default_factory=tuple)
    unresolved_items: tuple[str, ...] = Field(default_factory=tuple)
    acceptance_coverage: tuple[AcceptanceCoverage, ...] = Field(default_factory=tuple)
    unrelated_changes_checked: StrictBool = False
    root_cause_evidence: tuple[RootCauseEvidence, ...] = Field(default_factory=tuple)
    investigation_suggestions: tuple[str, ...] = Field(default_factory=tuple)
    behavior_before: str = ""
    behavior_after: str = ""
    impact_scope: tuple[str, ...] = Field(default_factory=tuple)
    risk_level: str = ""
    session_id: str = ""

    @field_validator("impact_scope")
    @classmethod
    def validate_impact_scope(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        for path in paths:
            RepositorySnapshot._validate_repository_path(path)
        if len(paths) != len(set(paths)):
            raise ValueError("impact scope paths must be unique")
        return paths

    @field_validator("risk_level")
    @classmethod
    def validate_risk_level(cls, value: str) -> str:
        if value and value not in {"low", "medium", "high"}:
            raise ValueError("risk_level must be low, medium, or high")
        return value


class StateEvent(WorkflowModel):
    source: WorkflowState
    target: WorkflowState
    reason: str
    occurred_at: datetime


class ApprovalPackage(WorkflowModel):
    work_item_id: str
    work_item_title: str = ""
    work_item_status: str = ""
    source_versions: dict[str, str] = Field(default_factory=dict)
    wiki_hashes: dict[str, str] = Field(default_factory=dict)
    wiki_snapshots: tuple[WikiPageSnapshot, ...] = Field(default_factory=tuple)
    repository: RepositoryMapping | None = None
    repo_url: str = ""
    base_branch: str = ""
    base_commit: str = ""
    head_commit: str = ""
    diff_hash: str = ""
    diff_summary: str = ""
    branch: str = ""
    changed_files: tuple[str, ...] = Field(default_factory=tuple)
    coverage: dict[str, str] = Field(default_factory=dict)
    evidence: tuple[str, ...] = Field(default_factory=tuple)
    tests: tuple[CommandResult, ...] = Field(default_factory=tuple)
    review: tuple[str, ...] = Field(default_factory=tuple)
    risks: tuple[str, ...] = Field(default_factory=tuple)
    unresolved_items: tuple[str, ...] = Field(default_factory=tuple)
    manual_checks: tuple[str, ...] = Field(default_factory=tuple)
    unrelated_changes_checked: StrictBool = False
    root_cause_evidence: tuple[RootCauseEvidence, ...] = Field(default_factory=tuple)
    behavior_before: str = ""
    behavior_after: str = ""
    impact_scope: tuple[str, ...] = Field(default_factory=tuple)
    risk_level: str = ""
    pre_fix_tests: tuple[CommandResult, ...] = Field(default_factory=tuple)
    reproduction_command: str = ""
    reproduction_test_sha256: str = ""
    commit_message: str = ""
    pr_title: str = ""
    pr_body: str = ""
    fingerprint: str = ""
    approved_by: str | None = None
    approved_at: datetime | None = None

    @field_validator("impact_scope")
    @classmethod
    def validate_defect_impact_scope(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        return CodexResult.validate_impact_scope(paths)

    @field_validator("risk_level")
    @classmethod
    def validate_defect_risk_level(cls, value: str) -> str:
        return CodexResult.validate_risk_level(value)


class PublicationResult(WorkflowModel):
    approved_fingerprint: str = ""
    repo_url: str = ""
    provider: str = ""
    provider_host: str = ""
    expected_parent: str = ""
    expected_tree: str = ""
    commit_message: str = ""
    commit_hash: str = ""
    remote_branch: str = ""
    push_completed_at: datetime | None = None
    pr_marker: str = ""
    pr_base: str = ""
    pr_head: str = ""
    pr_title: str = ""
    pr_body: str = ""
    pr_url: str = ""
    comment_marker: str = ""
    comment_id: str = ""
    error: str = ""

    @field_validator("approved_fingerprint")
    @classmethod
    def validate_publication_fingerprint(cls, value: str) -> str:
        if value and re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("publication fingerprint must be canonical SHA-256")
        return value

    @field_validator("expected_parent", "expected_tree", "commit_hash")
    @classmethod
    def validate_publication_oid(cls, value: str) -> str:
        if value and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None:
            raise ValueError("publication object identity must be canonical")
        return value

    @field_validator("remote_branch", "pr_base", "pr_head")
    @classmethod
    def validate_publication_ref(cls, value: str) -> str:
        return validate_git_ref_name(value) if value else value

    @model_validator(mode="after")
    def validate_publication_checkpoint(self) -> PublicationResult:
        intent_values = (
            self.approved_fingerprint,
            self.repo_url,
            self.provider,
            self.provider_host,
            self.expected_parent,
            self.expected_tree,
            self.commit_message,
            self.remote_branch,
            self.pr_marker,
            self.pr_base,
            self.pr_head,
            self.pr_title,
            self.pr_body,
            self.comment_marker,
        )
        if any(intent_values) and not all(
            isinstance(value, str) and value.strip() for value in intent_values
        ):
            raise ValueError("publication intent must be complete and immutable")
        if self.provider and self.provider not in {"github", "gitlab"}:
            raise ValueError("publication provider is invalid")
        if self.provider_host and self.provider_host != self.provider_host.casefold():
            raise ValueError("publication provider host must be canonical")
        if self.commit_hash and not (
            self.approved_fingerprint
            and self.expected_parent
            and self.expected_tree
            and self.commit_message.strip()
        ):
            raise ValueError("commit fact requires a complete persisted commit intent")
        if self.push_completed_at is not None and not (
            self.commit_hash and self.remote_branch
        ):
            raise ValueError("push fact requires commit and remote branch intent")
        if self.pr_url and not (
            self.push_completed_at and self.pr_marker and self.pr_base and self.pr_head
        ):
            raise ValueError("PR fact requires a complete persisted PR intent")
        if self.comment_id and not (self.pr_url and self.comment_marker):
            raise ValueError("comment fact requires PR URL and comment marker intent")
        return self


class RevisionRecord(WorkflowModel):
    feedback: str
    occurred_at: datetime


class WorkflowRun(WorkflowModel):
    run_id: str
    type: WorkflowType
    state: WorkflowState = WorkflowState.CREATED
    version: StrictInt = 0
    history: tuple[StateEvent, ...] = Field(default_factory=tuple)
    work_item_id: str = ""
    project_id: str = ""
    iteration_id: str = ""
    assignee_id: str = ""
    candidate_id: str = ""
    requirement: RequirementRecord | None = None
    defect: DefectRecord | None = None
    wiki_snapshots: tuple[WikiPageSnapshot, ...] = Field(default_factory=tuple)
    repository: RepositoryMapping | None = None
    repository_candidates: tuple[RepositoryMapping, ...] = Field(default_factory=tuple)
    prepared_worktree: PreparedWorktree | None = None
    tested_snapshot: RepositorySnapshot | None = None
    pre_fix_snapshot: RepositorySnapshot | None = None
    pre_fix_test_results: tuple[CommandResult, ...] = Field(default_factory=tuple)
    reproduction_test_sha256: str = ""
    defect_checkpoint: DefectCheckpoint = DefectCheckpoint.NONE
    root_cause_evidence: tuple[RootCauseEvidence, ...] = Field(default_factory=tuple)
    investigation_suggestions: tuple[str, ...] = Field(default_factory=tuple)
    defect_preflight: CodexResult | None = None
    behavior_before: str = ""
    behavior_after: str = ""
    impact_scope: tuple[str, ...] = Field(default_factory=tuple)
    risk_level: str = ""
    acceptance_coverage: tuple[AcceptanceCoverage, ...] = Field(default_factory=tuple)
    base_commit: str = ""
    head_commit: str = ""
    branch: str = ""
    worktree_path: str = ""
    codex_results: tuple[CodexResult, ...] = Field(default_factory=tuple)
    changed_files: tuple[str, ...] = Field(default_factory=tuple)
    test_results: tuple[CommandResult, ...] = Field(default_factory=tuple)
    review: CodexResult | None = None
    approval: ApprovalPackage | None = None
    publication: PublicationResult = Field(default_factory=PublicationResult)
    resume_state: WorkflowState | None = None
    blocked_reason: str = ""
    error: str = ""
    retry_count: StrictInt = 0
    revisions: tuple[RevisionRecord, ...] = Field(default_factory=tuple)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_single_selected_defect(self) -> WorkflowRun:
        if self.reproduction_test_sha256 and re.fullmatch(
            r"[0-9a-f]{64}", self.reproduction_test_sha256
        ) is None:
            raise ValueError("reproduction_test_sha256 must be a canonical SHA-256 digest")
        if self.type is WorkflowType.DEFECT and (
            not self.work_item_id
            or not self.candidate_id
            or self.candidate_id != self.work_item_id
            or (self.defect is not None and self.defect.defect_id != self.work_item_id)
        ):
            raise ValueError("a defect run must contain exactly its selected work item")
        if self.type is not WorkflowType.DEFECT and self.defect is not None:
            raise ValueError("a defect snapshot is only valid on a defect run")
        return self

    @property
    def workflow_type(self) -> WorkflowType:
        """Compatibility name used by workflow dispatchers."""

        return self.type

    @classmethod
    def new(cls, workflow_type: WorkflowType | str, work_item_id: str) -> WorkflowRun:
        _non_empty(work_item_id, "work_item_id")
        now = utc_now()
        return cls(
            run_id=uuid.uuid4().hex,
            type=workflow_type,
            state=WorkflowState.CREATED,
            version=0,
            history=(),
            work_item_id=work_item_id,
            created_at=now,
            updated_at=now,
        )

    @classmethod
    def new_defect(
        cls,
        project_id: str,
        iteration_id: str,
        assignee_id: str,
        candidate_id: str,
    ) -> WorkflowRun:
        for name, value in (
            ("project_id", project_id),
            ("iteration_id", iteration_id),
            ("assignee_id", assignee_id),
            ("candidate_id", candidate_id),
        ):
            _non_empty(value, name)
        now = utc_now()
        return cls(
            run_id=uuid.uuid4().hex,
            type=WorkflowType.DEFECT,
            state=WorkflowState.CREATED,
            version=0,
            history=(),
            work_item_id=candidate_id,
            project_id=project_id,
            iteration_id=iteration_id,
            assignee_id=assignee_id,
            candidate_id=candidate_id,
            created_at=now,
            updated_at=now,
        )

    def for_revision(self, feedback: str) -> WorkflowRun:
        _non_empty(feedback, "feedback")
        data = self.model_dump()
        if self.approval is not None:
            data["approval"] = {
                **self.approval.model_dump(),
                "fingerprint": "",
                "approved_by": None,
                "approved_at": None,
            }
        data.update(
            state=WorkflowState.BLOCKED,
            resume_state=WorkflowState.IMPLEMENTING,
            revisions=(
                *data["revisions"],
                RevisionRecord(feedback=feedback, occurred_at=utc_now()).model_dump(),
            ),
            updated_at=utc_now(),
        )
        return type(self).model_validate(data)

    def with_approval(
        self, approved_by: str, approved_at: datetime | None = None
    ) -> WorkflowRun:
        _non_empty(approved_by, "approved_by")
        if self.state is not WorkflowState.WAITING_APPROVAL:
            raise ValueError("approval is only valid in WAITING_APPROVAL")
        if self.approval is None:
            raise ValueError("an approval package is required")
        normalized_approved_at = approved_at or utc_now()
        if (
            normalized_approved_at.tzinfo is None
            or normalized_approved_at.utcoffset() is None
        ):
            raise ValueError("approved_at must be timezone-aware")
        normalized_approved_at = normalized_approved_at.astimezone(UTC)
        data = self.model_dump()
        data["approval"] = {
            **self.approval.model_dump(),
            "approved_by": approved_by,
            "approved_at": normalized_approved_at,
        }
        data["updated_at"] = utc_now()
        return type(self).model_validate(data)


__all__ = [
    "ApprovalPackage",
    "AcceptanceCoverage",
    "CodexResult",
    "CommandResult",
    "CommandOutcome",
    "DefectRecord",
    "DefectCandidate",
    "DefectCheckpoint",
    "PublicationResult",
    "PreparedWorktree",
    "RepositoryMapping",
    "RepositorySnapshot",
    "RequirementRecord",
    "RootCauseEvidence",
    "RootCauseSupportingPoint",
    "RevisionRecord",
    "StateEvent",
    "WikiPageRef",
    "WikiPageSnapshot",
    "WorkflowRun",
    "WorkflowModel",
    "WorkflowState",
    "WorkflowType",
    "utc_now",
    "validate_git_ref_name",
]
