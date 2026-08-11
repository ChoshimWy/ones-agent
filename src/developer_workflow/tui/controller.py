"""Synchronous, display-only boundary for the developer workflow TUI."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Mapping
from concurrent.futures import CancelledError, Future, wait
from dataclasses import dataclass
import secrets
from threading import Event, Lock, Thread, current_thread
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
_QUERY_UNAVAILABLE = "candidate query is unavailable"
_RUNTIME_CLOSE_TIMEOUT = 5.0


class TuiControllerError(RuntimeError):
    """Fixed-message failure at the interactive application boundary."""


class StaleTuiActionError(TuiControllerError):
    """The authoritative workflow no longer matches the reviewed facts."""


class _AsyncRuntimeError(RuntimeError):
    """Internal fixed-message signal for async runtime lifecycle failures."""


class _AsyncRuntime:
    """Own one stable event loop for all async calls made by a controller."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._ready = Event()
        self._stopped = Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: Thread | None = None
        self._start_error = False
        self._closed = False
        self._futures: set[Future[object]] = set()
        self._coroutines: dict[Future[object], object] = {}

    def submit(self, coroutine):
        try:
            self._ensure_started()
        except Exception:
            coroutine.close()
            raise
        with self._lock:
            thread = self._thread
            loop = self._loop
            if thread is not None and current_thread() is thread:
                coroutine.close()
                raise _AsyncRuntimeError(_QUERY_UNAVAILABLE)
            if self._closed or loop is None or not loop.is_running():
                coroutine.close()
                raise _AsyncRuntimeError(_QUERY_UNAVAILABLE)
            try:
                future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            except Exception:
                coroutine.close()
                raise _AsyncRuntimeError(_QUERY_UNAVAILABLE) from None
            self._futures.add(future)
            self._coroutines[future] = coroutine
        try:
            try:
                return future.result()
            except CancelledError:
                raise _AsyncRuntimeError(_QUERY_UNAVAILABLE) from None
        finally:
            with self._lock:
                self._futures.discard(future)
                self._coroutines.pop(future, None)

    def close(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is not None and current_thread() is thread:
                raise _AsyncRuntimeError(_QUERY_UNAVAILABLE)
            first_close = not self._closed
            self._closed = True
            futures = tuple(self._futures) if first_close else ()
        if not first_close:
            if thread is not None and not self._stopped.wait(_RUNTIME_CLOSE_TIMEOUT):
                raise _AsyncRuntimeError(_QUERY_UNAVAILABLE)
            return
        if thread is None:
            self._stopped.set()
            return
        if not self._ready.wait(_RUNTIME_CLOSE_TIMEOUT):
            raise _AsyncRuntimeError(_QUERY_UNAVAILABLE)
        _, unfinished = wait(futures, timeout=_RUNTIME_CLOSE_TIMEOUT)
        if unfinished:
            for future in unfinished:
                future.cancel()
            wait(unfinished, timeout=_RUNTIME_CLOSE_TIMEOUT)
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        thread.join(_RUNTIME_CLOSE_TIMEOUT)
        if thread.is_alive() or not self._stopped.is_set():
            raise _AsyncRuntimeError(_QUERY_UNAVAILABLE)

    def _ensure_started(self) -> None:
        with self._lock:
            if self._closed or self._start_error:
                raise _AsyncRuntimeError(_QUERY_UNAVAILABLE)
            if self._thread is None:
                self._thread = Thread(
                    target=self._run,
                    name=f"ones-tui-async-{id(self):x}",
                    daemon=True,
                )
                try:
                    self._thread.start()
                except Exception:
                    self._thread = None
                    self._start_error = True
                    self._ready.set()
                    self._stopped.set()
                    raise _AsyncRuntimeError(_QUERY_UNAVAILABLE) from None
        if not self._ready.wait(_RUNTIME_CLOSE_TIMEOUT):
            raise _AsyncRuntimeError(_QUERY_UNAVAILABLE)
        with self._lock:
            if self._start_error or self._loop is None:
                raise _AsyncRuntimeError(_QUERY_UNAVAILABLE)

    def _run(self) -> None:
        loop: asyncio.AbstractEventLoop | None = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            with self._lock:
                self._loop = loop
                closed = self._closed
            self._ready.set()
            if not closed:
                loop.run_forever()
        except Exception:
            with self._lock:
                self._start_error = True
            self._ready.set()
        finally:
            if loop is not None:
                pending = asyncio.all_tasks(loop)
                task_coroutines = {task.get_coro() for task in pending}
                with self._lock:
                    submissions = tuple(
                        (future, self._coroutines.get(future))
                        for future in self._futures
                    )
                for future, coroutine in submissions:
                    future.cancel()
                    if coroutine is not None and coroutine not in task_coroutines:
                        coroutine.close()
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.close()
            self._stopped.set()


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
        self._closed = False
        self._async_runtime = _AsyncRuntime()

    def close(self) -> None:
        """Stop the controller-owned async runtime without closing shared services."""

        with self._candidate_lock:
            self._closed = True
            self._candidate_sessions.clear()
        try:
            self._async_runtime.close()
        except _AsyncRuntimeError:
            raise TuiControllerError(_QUERY_UNAVAILABLE) from None

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
        with self._candidate_lock:
            if self._closed:
                raise TuiControllerError(_QUERY_UNAVAILABLE)
        try:
            candidates = self._async_runtime.submit(
                self._orchestrator.defect_candidates.list_candidates(
                    project,
                    iteration,
                    assignee,
                    status_ids=None if status_ids == () else status_ids,
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
        except _AsyncRuntimeError:
            raise TuiControllerError(_QUERY_UNAVAILABLE) from None
        except Exception:
            raise TuiControllerError(_CANDIDATE_ERROR) from None

        session_id = secrets.token_urlsafe(32)
        with self._candidate_lock:
            if self._closed:
                raise TuiControllerError(_QUERY_UNAVAILABLE)
            if not candidates:
                return CandidateSessionView(session_id=session_id, items=())
            session = _CandidateSession(
                project=project,
                iteration=iteration,
                assignee=assignee,
                snapshot_token=next(iter(tokens)),
                candidate_ids=frozenset(item.candidate_id for item in items),
            )
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
            if self._closed:
                raise TuiControllerError(_QUERY_UNAVAILABLE)
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
            except Exception:
                raise TuiControllerError(_CANDIDATE_ERROR) from None
        try:
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
            run = self._orchestrator.show(request.run_id)
        except Exception:
            raise TuiControllerError(_ACTION_UNAVAILABLE) from None
        try:
            request.assert_current(run)
        except TuiDisplayError as exc:
            if str(exc) == "workflow action is stale":
                raise StaleTuiActionError(_STALE) from None
            raise TuiControllerError(_ACTION_UNAVAILABLE) from None
        except Exception:
            raise TuiControllerError(_ACTION_UNAVAILABLE) from None

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
