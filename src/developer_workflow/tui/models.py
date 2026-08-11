"""Frozen, display-only view models for the developer workflow TUI."""

from __future__ import annotations

import hmac
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from ..approval import approval_fingerprint
from ..contracts import (
    CommandResult,
    DefectCandidate,
    PublicationResult,
    RepositoryMapping,
    RepositoryPublicationResult,
    WorkflowRun,
    WorkflowState,
    WorkflowType,
)


_INVALID_DISPLAY = "display value is invalid"
_INVALID_PR_URL = "PR URL is invalid"
_PUBLICATION_FAILED = "publication failed safely"
_BLOCKED = "workflow blocked safely"
_ACTIONS = {"approve", "revise", "cancel", "resume-publication"}


class TuiDisplayError(ValueError):
    """A fixed-message failure at the untrusted display boundary."""


class RunActivity(str, Enum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"


def safe_tui_text(
    value: str,
    *,
    maximum: int = 4096,
    allow_empty: bool = False,
) -> str:
    """Return strictly encodable, single-line text without format controls."""

    if type(value) is not str or maximum < 1:
        raise TuiDisplayError(_INVALID_DISPLAY)
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise TuiDisplayError(_INVALID_DISPLAY) from None
    if (
        (not value and not allow_empty)
        or len(value) > maximum
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            for character in value
        )
    ):
        raise TuiDisplayError(_INVALID_DISPLAY)
    return value


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    workflow_type: WorkflowType
    work_item_id: str
    state: WorkflowState
    version: int
    updated_at: datetime
    activity: RunActivity
    corrupted: bool = False

    @classmethod
    def from_run(cls, run: WorkflowRun, *, activity: RunActivity) -> RunSummary:
        return cls(
            run_id=safe_tui_text(run.run_id, maximum=64),
            workflow_type=run.type,
            work_item_id=safe_tui_text(run.work_item_id, maximum=256),
            state=run.state,
            version=run.version,
            updated_at=run.updated_at,
            activity=activity,
        )

    @classmethod
    def corrupted_entry(cls, run_id: str) -> RunSummary:
        return cls(
            run_id=safe_tui_text(run_id, maximum=64),
            workflow_type=WorkflowType.REQUIREMENT,
            work_item_id="storage-corrupted",
            state=WorkflowState.BLOCKED,
            version=0,
            updated_at=datetime(1970, 1, 1, tzinfo=UTC),
            activity=RunActivity.IDLE,
            corrupted=True,
        )


@dataclass(frozen=True, slots=True)
class RepositoryView:
    key: str
    role: str
    base_commit: str
    head_commit: str
    tree_hash: str
    changed_files: tuple[str, ...]
    changed_file_count: int
    commit_hash: str
    pushed: bool
    pr_url: str
    error: str


@dataclass(frozen=True, slots=True)
class TestView:
    command: str
    outcome: str
    exit_code: int


@dataclass(frozen=True, slots=True)
class PublicationView:
    repositories: tuple[RepositoryView, ...]
    comment_id: str
    error: str


@dataclass(frozen=True, slots=True)
class HistoryView:
    source: str
    target: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class RunDetail:
    summary: RunSummary
    repositories: tuple[RepositoryView, ...]
    tests: tuple[TestView, ...]
    review: tuple[str, ...]
    publication: PublicationView
    history: tuple[HistoryView, ...]
    blocked_reason: str
    fingerprint: str
    risk_count: int
    unresolved_count: int

    @classmethod
    def from_run(cls, run: WorkflowRun) -> RunDetail:
        return run_detail_from_run(run)


@dataclass(frozen=True, slots=True)
class DefectChoice:
    candidate_id: str
    title: str
    status_id: str
    priority: str

    @classmethod
    def from_candidate(cls, candidate: DefectCandidate) -> DefectChoice:
        return cls(
            candidate_id=safe_tui_text(candidate.uuid, maximum=128),
            title=safe_tui_text(candidate.title, maximum=512),
            status_id=safe_tui_text(candidate.status_id, maximum=128, allow_empty=True),
            priority=safe_tui_text(candidate.priority, maximum=128),
        )


@dataclass(frozen=True, slots=True)
class RunFilter:
    states: tuple[WorkflowState, ...] = ()
    workflow_types: tuple[WorkflowType, ...] = ()
    query: str = ""

    def matches(self, item: RunSummary) -> bool:
        query = safe_tui_text(
            self.query, maximum=256, allow_empty=True
        ).casefold().strip()
        return (
            (not self.states or item.state in self.states)
            and (
                not self.workflow_types
                or item.workflow_type in self.workflow_types
            )
            and (
                not query
                or query in item.work_item_id.casefold()
                or query in item.run_id.casefold()
            )
        )


@dataclass(frozen=True, slots=True)
class DangerousActionRequest:
    run_id: str
    version: int
    action: Literal["approve", "revise", "cancel", "resume-publication"]
    fingerprint: str
    work_item_id: str
    repositories: tuple[RepositoryView, ...]
    changed_file_count: int
    test_count: int
    risk_count: int
    unresolved_count: int

    def __post_init__(self) -> None:
        if self.action not in _ACTIONS:
            raise TuiDisplayError("workflow action is invalid")

    @classmethod
    def from_run(
        cls,
        run: WorkflowRun,
        *,
        action: str,
        expected_version: int | None = None,
    ) -> DangerousActionRequest:
        if action not in _ACTIONS:
            raise TuiDisplayError("workflow action is invalid")
        if expected_version is not None and expected_version != run.version:
            raise TuiDisplayError("workflow action is stale")
        detail = run_detail_from_run(run)
        return cls(
            run_id=detail.summary.run_id,
            version=detail.summary.version,
            action=action,
            fingerprint=detail.fingerprint,
            work_item_id=detail.summary.work_item_id,
            repositories=detail.repositories,
            changed_file_count=sum(
                item.changed_file_count for item in detail.repositories
            ),
            test_count=len(detail.tests),
            risk_count=detail.risk_count,
            unresolved_count=detail.unresolved_count,
        )

    def assert_current(self, run: WorkflowRun) -> None:
        current = type(self).from_run(
            run, action=self.action, expected_version=self.version
        )
        if current != self:
            raise TuiDisplayError("workflow action is stale")


def run_detail_from_run(run: WorkflowRun) -> RunDetail:
    approval = run.approval
    fingerprint = _safe_fingerprint(approval.fingerprint) if approval else ""
    signed = bool(
        approval
        and fingerprint
        and approval.approved_by
        and approval.approved_at
        and hmac.compare_digest(fingerprint, approval_fingerprint(approval))
    )
    repositories: tuple[RepositoryView, ...]
    tests: tuple[TestView, ...]
    if run.repository_group is not None:
        repositories, tests = _group_views(run, signed=signed)
    else:
        repositories, tests = _single_repository_views(run, signed=signed)

    single_publication_bound = bool(
        signed and run.publication.approved_fingerprint == fingerprint
    )
    publication_error = bool(
        single_publication_bound and run.publication.error
    )
    comment = (
        "delivered"
        if single_publication_bound and run.publication.comment_id
        else ""
    )
    group_publication_bound = bool(
        signed
        and run.group_publication is not None
        and all(
            item.approved_fingerprint == fingerprint
            for item in run.group_publication.repositories
        )
    )
    if group_publication_bound and run.group_publication is not None:
        publication_error = bool(run.group_publication.error)
        comment = "delivered" if run.group_publication.comment_id else ""

    review_count = len(approval.review) if approval is not None else 0
    return RunDetail(
        summary=RunSummary.from_run(run, activity=RunActivity.IDLE),
        repositories=repositories,
        tests=tests,
        review=tuple("review recorded" for _ in range(review_count)),
        publication=PublicationView(
            repositories=repositories,
            comment_id=comment,
            error=_PUBLICATION_FAILED if publication_error else "",
        ),
        history=tuple(
            HistoryView(
                source=event.source.value,
                target=event.target.value,
                occurred_at=event.occurred_at,
            )
            for event in run.history
        ),
        blocked_reason=_BLOCKED if run.blocked_reason or run.error else "",
        fingerprint=fingerprint,
        risk_count=len(approval.risks) if approval is not None else 0,
        unresolved_count=(
            len(approval.unresolved_items) if approval is not None else 0
        ),
    )


def _single_repository_views(
    run: WorkflowRun, *, signed: bool
) -> tuple[tuple[RepositoryView, ...], tuple[TestView, ...]]:
    mapping = run.repository
    prepared = run.prepared_worktree
    if mapping is None or prepared is None:
        return (), tuple(_test_view(item) for item in run.test_results)

    approval = run.approval
    publication = run.publication
    publication_bound = bool(
        signed
        and approval is not None
        and publication.approved_fingerprint == approval.fingerprint
    )
    if run.tested_snapshot is not None:
        final_paths = run.tested_snapshot.changed_files
    elif approval is not None:
        final_paths = approval.changed_files
    else:
        final_paths = ()
    changed_files = tuple(_safe_repository_path(path) for path in final_paths)
    repository = _repository_view(
        mapping=mapping,
        base_commit=prepared.base_commit,
        head_commit=prepared.head_commit,
        tree_hash=publication.expected_tree if publication_bound else "",
        changed_files=changed_files,
        publication=publication if publication_bound else None,
    )
    return (repository,), tuple(_test_view(item) for item in run.test_results)


def _group_views(
    run: WorkflowRun, *, signed: bool
) -> tuple[tuple[RepositoryView, ...], tuple[TestView, ...]]:
    approval = run.approval
    approvals = {
        item.repository_key: item
        for item in (
            approval.repositories
            if signed and approval is not None
            else ()
        )
    }
    publications = {
        item.repository_key: item
        for item in (
            run.group_publication.repositories
            if signed and run.group_publication is not None
            else ()
        )
        if approval is not None
        and item.approved_fingerprint == approval.fingerprint
    }
    repository_views: list[RepositoryView] = []
    test_views: list[TestView] = []
    for evidence in run.repository_evidence:
        key = evidence.repository_key
        signed_evidence = approvals.get(key)
        publication = publications.get(key)
        changed_files = tuple(
            _safe_repository_path(path) for path in evidence.changed_files
        )
        repository_views.append(
            _repository_view(
                mapping=evidence.mapping,
                base_commit=evidence.prepared_worktree.base_commit,
                head_commit=evidence.prepared_worktree.head_commit,
                tree_hash=signed_evidence.tree_hash if signed_evidence else "",
                changed_files=changed_files,
                publication=publication,
            )
        )
        test_views.extend(_test_view(item) for item in evidence.test_results)
    test_views.extend(_test_view(item) for item in run.integration_test_results)
    return tuple(repository_views), tuple(test_views)


def _repository_view(
    *,
    mapping: RepositoryMapping,
    base_commit: str,
    head_commit: str,
    tree_hash: str,
    changed_files: tuple[str, ...],
    publication: PublicationResult | RepositoryPublicationResult | None,
) -> RepositoryView:
    return RepositoryView(
        key=safe_tui_text(mapping.key, maximum=128),
        role=mapping.role.value,
        base_commit=safe_tui_text(base_commit, maximum=64),
        head_commit=safe_tui_text(head_commit, maximum=64),
        tree_hash=safe_tui_text(tree_hash, maximum=64, allow_empty=True),
        changed_files=changed_files,
        changed_file_count=len(changed_files),
        commit_hash=(
            safe_tui_text(publication.commit_hash, maximum=64, allow_empty=True)
            if publication is not None
            else ""
        ),
        pushed=bool(publication and publication.push_completed_at),
        pr_url=(
            _safe_pr_url(publication.pr_url)
            if publication is not None and publication.pr_url
            else ""
        ),
        error=(
            _PUBLICATION_FAILED
            if publication is not None and publication.error
            else ""
        ),
    )


def _test_view(result: CommandResult) -> TestView:
    outcome = result.outcome
    if outcome is None:
        raise TuiDisplayError(_INVALID_DISPLAY)
    return TestView(
        command="test command",
        outcome=outcome.value,
        exit_code=result.exit_code,
    )


def _safe_repository_path(value: str) -> str:
    checked = safe_tui_text(value, maximum=1024)
    parts = checked.split("/")
    if (
        checked.startswith("/")
        or ":" in checked
        or "\\" in checked
        or any(part in {"", ".", ".."} for part in parts)
        or parts[0].casefold() == ".git"
    ):
        raise TuiDisplayError(_INVALID_DISPLAY)
    return checked


def _safe_fingerprint(value: str) -> str:
    checked = safe_tui_text(value, maximum=64, allow_empty=True)
    if checked and re.fullmatch(r"[0-9a-f]{64}", checked) is None:
        raise TuiDisplayError(_INVALID_DISPLAY)
    return checked


def _safe_pr_url(value: str) -> str:
    try:
        checked = safe_tui_text(value)
        parsed = urlsplit(checked)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
        port = parsed.port
        hostname = parsed.hostname.casefold()
        if ":" in hostname:
            hostname = f"[{hostname}]"
        netloc = f"{hostname}:{port}" if port is not None else hostname
        sanitized = urlunsplit((parsed.scheme, netloc, parsed.path or "/", "", ""))
        return safe_tui_text(sanitized)
    except (TuiDisplayError, ValueError, UnicodeError):
        raise TuiDisplayError(_INVALID_PR_URL) from None
