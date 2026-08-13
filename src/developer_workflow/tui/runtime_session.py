"""Owned resources for one activated TUI runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from ..runtime_bootstrap import RuntimeHandle
from .controller import TuiController
from .run_index import RunIndex
from .supervisor import RunTaskSupervisor, TaskEvent


class TuiRuntimeCloseError(RuntimeError):
    """A runtime session could not be completely closed."""


class TuiRuntimeSession:
    """Own the dashboard controller, task supervisor, index and runtime handle."""

    def __init__(
        self,
        *,
        handle: RuntimeHandle | Any | None,
        controller: TuiController | Any,
        run_index: RunIndex | Any,
        supervisor: RunTaskSupervisor | Any,
        event_sink: Callable[[TaskEvent], None],
    ) -> None:
        self.handle = handle
        self.controller = controller
        self.run_index = run_index
        self.supervisor = supervisor
        self._event_sink: Callable[[TaskEvent], None] | None = event_sink
        self._close_lock = asyncio.Lock()
        self._closed = False

    @classmethod
    def from_handle(
        cls,
        handle: RuntimeHandle,
        max_concurrency: int,
        event_sink: Callable[[TaskEvent], None],
    ) -> "TuiRuntimeSession":
        index = RunIndex(handle.orchestrator.store)
        controller = TuiController(handle.orchestrator, index)
        session: TuiRuntimeSession

        def emit(event: TaskEvent) -> None:
            session.emit(event)

        supervisor = RunTaskSupervisor(max_concurrency, emit)
        session = cls(
            handle=handle,
            controller=controller,
            run_index=index,
            supervisor=supervisor,
            event_sink=event_sink,
        )
        return session

    @classmethod
    def from_controller(
        cls,
        controller: TuiController,
        max_concurrency: int,
        event_sink: Callable[[TaskEvent], None],
    ) -> "TuiRuntimeSession":
        session: TuiRuntimeSession

        def emit(event: TaskEvent) -> None:
            session.emit(event)

        supervisor = RunTaskSupervisor(max_concurrency, emit)
        session = cls(
            handle=None,
            controller=controller,
            run_index=getattr(controller, "_run_index", None),
            supervisor=supervisor,
            event_sink=event_sink,
        )
        return session

    def emit(self, event: TaskEvent) -> None:
        sink = self._event_sink
        if not self._closed and sink is not None:
            sink(event)

    async def close(self) -> None:
        """Stop UI work, controller runtime and shared services exactly once."""

        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._event_sink = None
            failed = False
            try:
                await asyncio.wait_for(self.supervisor.close(), timeout=5.0)
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                failed = True
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self.controller.close), timeout=5.0
                )
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                failed = True
            if self.handle is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(self.handle.close), timeout=5.0
                    )
                except BaseException as error:
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        raise
                    failed = True
            if failed:
                raise TuiRuntimeCloseError("TUI runtime close failed") from None


__all__ = ["TuiRuntimeCloseError", "TuiRuntimeSession"]
