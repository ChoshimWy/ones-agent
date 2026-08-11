"""Bounded, per-run task scheduling for the developer workflow TUI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, TypeVar, cast

from .models import RunActivity, TuiDisplayError, safe_tui_text


T = TypeVar("T")

_CLOSED = "workflow task supervisor is closed"
_WRONG_LOOP = "workflow task supervisor requires its running event loop"
_INVALID_CALL = "workflow task call is invalid"
_QUEUED = "workflow action queued"
_STARTED = "workflow action started"
_COMPLETED = "workflow action completed"
_FAILED = "workflow action failed safely"
_CANCELLED = "workflow action cancelled"
_ACTIVITY_MESSAGES = {
    RunActivity.QUEUED: frozenset({_QUEUED}),
    RunActivity.RUNNING: frozenset({_STARTED}),
    RunActivity.IDLE: frozenset({_COMPLETED, _FAILED, _CANCELLED}),
}


class SupervisorClosedError(RuntimeError):
    """Raised when new work is submitted after shutdown begins."""


class SupervisorLoopError(RuntimeError):
    """Raised when the supervisor is used outside its owning event loop."""


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """A fixed-message, display-safe workflow task event."""

    run_id: str
    action: str
    activity: RunActivity
    message: str

    def __post_init__(self) -> None:
        run_id = safe_tui_text(self.run_id, maximum=64)
        action = safe_tui_text(self.action, maximum=64)
        if (
            type(self.activity) is not RunActivity
            or type(self.message) is not str
            or self.message not in _ACTIVITY_MESSAGES[self.activity]
        ):
            raise TuiDisplayError("display value is invalid")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "action", action)

    @classmethod
    def queued(cls, run_id: str, action: str) -> TaskEvent:
        return cls(run_id, action, RunActivity.QUEUED, _QUEUED)

    @classmethod
    def started(cls, run_id: str, action: str) -> TaskEvent:
        return cls(run_id, action, RunActivity.RUNNING, _STARTED)

    @classmethod
    def completed(cls, run_id: str, action: str) -> TaskEvent:
        return cls(run_id, action, RunActivity.IDLE, _COMPLETED)

    @classmethod
    def failed(
        cls,
        run_id: str,
        action: str | BaseException,
        error: BaseException | None = None,
    ) -> TaskEvent:
        del error
        safe_action = "failed" if isinstance(action, BaseException) else action
        return cls(run_id, safe_action, RunActivity.IDLE, _FAILED)

    @classmethod
    def cancelled(cls, run_id: str, action: str) -> TaskEvent:
        return cls(run_id, action, RunActivity.IDLE, _CANCELLED)


@dataclass(slots=True)
class _RunGate:
    lock: asyncio.Lock
    references: int = 0


@dataclass(slots=True)
class _TaskState:
    run_id: str | None
    action: str
    started: bool = False
    cancellation_notified: bool = False


class RunTaskSupervisor:
    """Serialize each run while bounding work across independent runs."""

    def __init__(
        self,
        max_concurrency: int,
        sink: Callable[[TaskEvent], None] = lambda event: None,
    ) -> None:
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 8:
            raise ValueError("max_concurrency must be between 1 and 8")
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            raise SupervisorLoopError(_WRONG_LOOP) from None
        if not callable(sink):
            raise TypeError("workflow task sink is invalid")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._run_gates: dict[str, _RunGate] = {}
        self._tasks: dict[asyncio.Task[object], _TaskState] = {}
        self._sink = sink
        self._closed = False
        self._readonly_sequence = 0

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def task_count(self) -> int:
        return len(self._tasks)

    @property
    def run_lock_count(self) -> int:
        return len(self._run_gates)

    def submit(
        self, run_id: str, action: str, call: Callable[[], T]
    ) -> asyncio.Task[T]:
        """Queue synchronous mutation work on the owning event loop."""

        self._require_loop()
        if self._closed:
            raise SupervisorClosedError(_CLOSED)
        safe_run_id = safe_tui_text(run_id, maximum=64)
        safe_action = safe_tui_text(action, maximum=64)
        if not callable(call):
            raise TypeError(_INVALID_CALL)

        gate = self._run_gates.get(safe_run_id)
        if gate is None:
            gate = _RunGate(asyncio.Lock())
            self._run_gates[safe_run_id] = gate
        gate.references += 1
        state = _TaskState(run_id=safe_run_id, action=safe_action)
        self._emit(TaskEvent.queued(safe_run_id, safe_action))
        task = self._loop.create_task(
            self._execute_mutation(gate, state, call),
            name=f"tui-workflow-{safe_action}",
        )
        self._register(cast(asyncio.Task[object], task), state)
        return task

    async def run_mutation(
        self,
        run_id: str,
        action: str,
        call: Callable[..., T],
        *args: object,
    ) -> T:
        """Submit a synchronous mutation and await its original result."""

        if not callable(call):
            raise TypeError(_INVALID_CALL)
        return await self.submit(run_id, action, lambda: call(*args))

    async def run_readonly(
        self, action: str, call: Callable[..., T], *args: object
    ) -> T:
        """Run synchronous read-only work without acquiring a per-run lock."""

        self._require_loop()
        if self._closed:
            raise SupervisorClosedError(_CLOSED)
        safe_action = safe_tui_text(action, maximum=64)
        if not callable(call):
            raise TypeError(_INVALID_CALL)
        self._readonly_sequence += 1
        internal_key = f"readonly-{self._readonly_sequence}"
        state = _TaskState(run_id=None, action=safe_action)
        task = self._loop.create_task(
            self._execute_readonly(state, call, args),
            name=internal_key,
        )
        self._register(cast(asyncio.Task[object], task), state)
        return await task

    async def close(self) -> None:
        """Reject new work without waiting for started thread operations."""

        self._require_loop()
        if self._closed:
            return
        self._closed = True
        waiting_tasks = tuple(
            task for task, state in self._tasks.items() if not state.started
        )
        for task, state in tuple(self._tasks.items()):
            if not state.started and state.run_id is not None:
                self._notify_cancelled(state)
            task.cancel()
        if waiting_tasks:
            await asyncio.gather(*waiting_tasks, return_exceptions=True)

    async def _execute_mutation(
        self,
        gate: _RunGate,
        state: _TaskState,
        call: Callable[[], T],
    ) -> T:
        try:
            async with gate.lock:
                async with self._semaphore:
                    state.started = True
                    assert state.run_id is not None
                    self._emit(TaskEvent.started(state.run_id, state.action))
                    try:
                        cancelled, result, error = await self._invoke(call)
                    except BaseException:
                        self._emit(TaskEvent.failed(state.run_id, state.action))
                        raise
                    if error is None:
                        self._emit(TaskEvent.completed(state.run_id, state.action))
                    else:
                        self._emit(TaskEvent.failed(state.run_id, state.action))
                    if cancelled:
                        raise asyncio.CancelledError
                    if error is not None:
                        raise error
                    return cast(T, result)
        except asyncio.CancelledError:
            if not state.started:
                self._notify_cancelled(state)
            raise

    async def _execute_readonly(
        self,
        state: _TaskState,
        call: Callable[..., T],
        args: tuple[object, ...],
    ) -> T:
        async with self._semaphore:
            state.started = True
            cancelled, result, error = await self._invoke(lambda: call(*args))
            if cancelled:
                raise asyncio.CancelledError
            if error is not None:
                raise error
            return cast(T, result)

    async def _invoke(
        self, call: Callable[[], T]
    ) -> tuple[bool, T | None, BaseException | None]:
        thread_task = self._loop.create_task(asyncio.to_thread(call))
        cancelled = False
        while not thread_task.done():
            try:
                await asyncio.shield(thread_task)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if (
                    thread_task.done()
                    and current is not None
                    and not current.cancelling()
                ):
                    break
                cancelled = True
            except BaseException:
                # The thread task now owns the original failure; retrieve it below.
                pass
        try:
            result = thread_task.result()
        except BaseException as error:
            return cancelled, None, error
        return cancelled, result, None

    def _register(
        self, task: asyncio.Task[object], state: _TaskState
    ) -> None:
        self._tasks[task] = state
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[object]) -> None:
        state = self._tasks.pop(task, None)
        if task.cancelled() and state is not None and not state.started:
            self._notify_cancelled(state)
        if state is not None and state.run_id is not None:
            gate = self._run_gates.get(state.run_id)
            if gate is not None:
                gate.references -= 1
                if gate.references == 0:
                    self._run_gates.pop(state.run_id, None)
        self._consume_task(task)

    @staticmethod
    def _consume_task(task: asyncio.Task[object]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    def _notify_cancelled(self, state: _TaskState) -> None:
        if state.cancellation_notified or state.run_id is None:
            return
        state.cancellation_notified = True
        self._emit(TaskEvent.cancelled(state.run_id, state.action))

    def _emit(self, event: TaskEvent) -> None:
        try:
            self._sink(event)
        except BaseException:
            pass

    def _require_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise SupervisorLoopError(_WRONG_LOOP) from None
        if loop is not self._loop or not self._loop.is_running():
            raise SupervisorLoopError(_WRONG_LOOP)


__all__ = [
    "RunTaskSupervisor",
    "SupervisorClosedError",
    "SupervisorLoopError",
    "TaskEvent",
]
