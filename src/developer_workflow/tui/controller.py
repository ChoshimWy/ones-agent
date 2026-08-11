"""Synchronous, display-only boundary for the developer workflow TUI."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
import secrets
from threading import Lock
from typing import Literal

from ..contracts import WorkflowRun, WorkflowState
from ..orchestrator import DeveloperWorkflowOrchestrator, InvalidWorkflowAction
from .models import (
    DangerousActionRequest,
    DefectChoice,
    RunActivity,
    RunDetail,
    RunFilter,
    RunSummary,
    TuiDisplayError,
)
from .run_index import RunIndex


_STALE = "workflow changed; review again"
_CANDIDATE_ERROR = "candidate snapshot is invalid"
_ACTION_ERROR = "workflow action failed safely"
_LIST_ERROR = "workflow list is unavailable"
_DISPLAY_ERROR = "workflow display is unavailable"
_ACTION_UNAVAILABLE = "workflow action is unavailable"


class TuiControllerError(RuntimeError):
    """Fixed-message failure at the interactive application boundary."""


class StaleTuiActionError(TuiControllerError):
    """The authoritative workflow no longer matches the reviewed facts."""


@dataclass(frozen=True, slots=True)
class CandidateSessionView:
    session_id: str
    items: tuple[DefectChoice, ...]


@dataclass(frozen=True, slots=True)
class _CandidateSession:
    project: str
    iteration: str
    assignee: str
    snapshot_token: str
    candidate_ids: frozenset[str]


class TuiController:
    """Adapt workflow services to immutable TUI view models."""

    def __init__(
        self,
        orchestrator: DeveloperWorkflowOrchestrator,
        run_index: RunIndex,
        *,
        max_candidate_sessions: int = 8,
    ) -> None:
        if type(max_candidate_sessions) is not int or max_candidate_sessions <= 0:
            raise TuiControllerError("candidate session capacity is invalid")
        self._orchestrator = orchestrator
        self._run_index = run_index
        self._max_candidate_sessions = max_candidate_sessions
        self._candidate_sessions: OrderedDict[str, _CandidateSession] = OrderedDict()
        self._candidate_lock = Lock()

    def list_runs(
        self,
        filters: RunFilter,
        activities: Mapping[str, RunActivity] | None = None,
    ) -> tuple[RunSummary, ...]:
        try:
            return self._run_index.list(filters, activities)
        except Exception:
            raise TuiControllerError(_LIST_ERROR) from None

    def show(self, run_id: str) -> RunDetail:
        try:
            return self._detail(self._orchestrator.show(run_id))
        except Exception:
            raise TuiControllerError(_DISPLAY_ERROR) from None

    def query_defects(
        self,
        project: str,
        iteration: str,
        assignee: str,
        status_ids: tuple[str, ...],
    ) -> CandidateSessionView:
        try:
            candidates = asyncio.run(
                self._orchestrator.defect_candidates.list_candidates(
                    project, iteration, assignee, status_ids=status_ids
                )
            )
            items = tuple(DefectChoice.from_candidate(item) for item in candidates)
            tokens = {item.snapshot_token for item in candidates}
            if candidates and (
                len(tokens) != 1
                or any(type(token) is not str or not token for token in tokens)
                or len({item.candidate_id for item in items}) != len(items)
            ):
                raise ValueError
        except Exception:
            raise TuiControllerError(_CANDIDATE_ERROR) from None

        session_id = secrets.token_urlsafe(32)
        if not candidates:
            return CandidateSessionView(session_id=session_id, items=())
        session = _CandidateSession(
            project=project,
            iteration=iteration,
            assignee=assignee,
            snapshot_token=next(iter(tokens)),
            candidate_ids=frozenset(item.candidate_id for item in items),
        )
        with self._candidate_lock:
            while session_id in self._candidate_sessions:
                session_id = secrets.token_urlsafe(32)
            self._candidate_sessions[session_id] = session
            while len(self._candidate_sessions) > self._max_candidate_sessions:
                self._candidate_sessions.popitem(last=False)
        return CandidateSessionView(session_id=session_id, items=items)

    def start_defect(self, session_id: str, candidate_id: str) -> RunDetail:
        if (
            type(session_id) is not str
            or not session_id
            or type(candidate_id) is not str
            or not candidate_id
        ):
            raise TuiControllerError(_CANDIDATE_ERROR)
        with self._candidate_lock:
            session = self._candidate_sessions.pop(session_id, None)
        if session is None or candidate_id not in session.candidate_ids:
            raise TuiControllerError(_CANDIDATE_ERROR)
        try:
            run = self._orchestrator.start_defect(
                session.project,
                session.iteration,
                session.assignee,
                session.snapshot_token,
                candidate_id,
            )
            return self._detail(run)
        except Exception:
            raise TuiControllerError(_CANDIDATE_ERROR) from None

    def start_requirement(self, requirement_id: str) -> RunDetail:
        return self._command(self._orchestrator.start_requirement, requirement_id)

    def confirm_repository(
        self, run_id: str, mapping_key: str, expected_version: int
    ) -> RunDetail:
        return self._command(
            self._orchestrator.confirm_repository,
            run_id,
            mapping_key,
            expected_version=expected_version,
        )

    def resume(self, run_id: str, expected_version: int) -> RunDetail:
        try:
            run = self._orchestrator.show(run_id)
        except Exception:
            raise TuiControllerError(_ACTION_ERROR) from None
        if (
            run.state in {WorkflowState.PARTIAL_SUCCESS, WorkflowState.PUBLISHING}
            or (
                run.state is WorkflowState.BLOCKED
                and run.resume_state is WorkflowState.PUBLISHING
            )
        ):
            raise TuiControllerError(_ACTION_UNAVAILABLE)
        return self._command(
            self._orchestrator.resume, run_id, expected_version=expected_version
        )

    def prepare_action(
        self,
        run_id: str,
        action: Literal["approve", "revise", "cancel", "resume-publication"],
    ) -> DangerousActionRequest:
        try:
            return DangerousActionRequest.from_run(
                self._orchestrator.show(run_id), action=action
            )
        except Exception:
            raise TuiControllerError(_ACTION_ERROR) from None

    def approve(self, request: DangerousActionRequest, actor: str) -> RunDetail:
        self._assert_request(request, "approve")
        return self._dangerous(
            self._orchestrator.approve,
            request.run_id,
            actor,
            expected_version=request.version,
        )

    def revise(
        self,
        request: DangerousActionRequest,
        feedback: str,
        scope: Literal["implementation", "repair"] | None,
    ) -> RunDetail:
        self._assert_request(request, "revise")
        return self._dangerous(
            self._orchestrator.revise,
            request.run_id,
            feedback,
            scope=scope,
            expected_version=request.version,
        )

    def cancel(self, request: DangerousActionRequest, actor: str) -> RunDetail:
        self._assert_request(request, "cancel")
        return self._dangerous(
            self._orchestrator.cancel,
            request.run_id,
            actor,
            expected_version=request.version,
        )

    def resume_publication(self, request: DangerousActionRequest) -> RunDetail:
        self._assert_request(request, "resume-publication")
        return self._dangerous(
            self._orchestrator.resume,
            request.run_id,
            expected_version=request.version,
        )

    def _assert_request(self, request: DangerousActionRequest, action: str) -> None:
        if not isinstance(request, DangerousActionRequest) or request.action != action:
            raise TuiControllerError(_ACTION_ERROR)
        try:
            request.assert_current(self._orchestrator.show(request.run_id))
        except Exception:
            raise StaleTuiActionError(_STALE) from None

    def _dangerous(self, command, *args, **kwargs) -> RunDetail:
        try:
            return self._detail(command(*args, **kwargs))
        except InvalidWorkflowAction as exc:
            if str(exc) == _STALE:
                raise StaleTuiActionError(_STALE) from None
            raise TuiControllerError(_ACTION_ERROR) from None
        except Exception:
            raise TuiControllerError(_ACTION_ERROR) from None

    def _command(self, command, *args, **kwargs) -> RunDetail:
        try:
            return self._detail(command(*args, **kwargs))
        except InvalidWorkflowAction as exc:
            if str(exc) == _STALE:
                raise StaleTuiActionError(_STALE) from None
            raise TuiControllerError(_ACTION_ERROR) from None
        except Exception:
            raise TuiControllerError(_ACTION_ERROR) from None

    @staticmethod
    def _detail(run: WorkflowRun) -> RunDetail:
        try:
            return RunDetail.from_run(run)
        except (TuiDisplayError, TypeError, AttributeError):
            raise TuiControllerError(_ACTION_ERROR) from None


__all__ = [
    "CandidateSessionView",
    "StaleTuiActionError",
    "TuiController",
    "TuiControllerError",
]
