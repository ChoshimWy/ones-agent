"""Full-screen Textual application for developer workflows."""

from __future__ import annotations

import weakref

from textual.app import App
from textual.binding import Binding
from textual.message import Message
from textual.timer import Timer

from .controller import TuiController
from .models import RunActivity
from .screens import DashboardScreen, SettingsView
from .supervisor import RunTaskSupervisor, TaskEvent


class TuiTaskMessage(Message):
    """Transport a safe supervisor event onto Textual's UI loop."""

    def __init__(self, event: TaskEvent) -> None:
        super().__init__()
        self.event = event


class DeveloperWorkflowTuiApp(App[None]):
    """Keyboard-first, mouse-capable full-screen workflow console."""

    CSS_PATH = "tui.tcss"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("?", "help", "Help"),
        Binding("n", "new_run", "New"),
        Binding("/", "search", "Search"),
        Binding("f", "filter", "Filter"),
    ]

    def __init__(
        self,
        controller: TuiController,
        max_concurrency: int,
        *,
        provider_type: str = "configured",
        sandbox_configured: bool = True,
        poll_interval: float = 2.0,
    ) -> None:
        if (
            isinstance(poll_interval, bool)
            or not isinstance(poll_interval, (int, float))
            or poll_interval <= 0
        ):
            raise ValueError("poll_interval must be positive")
        super().__init__()
        self.controller = controller
        self.poll_interval = float(poll_interval)
        self.settings = SettingsView(
            max_concurrency=max_concurrency,
            provider_type=provider_type,
            sandbox_configured=sandbox_configured,
        )
        self.activities: dict[str, RunActivity] = {}
        self._accept_events = True
        self._ui_closed = False
        self._poll_timer: Timer | None = None
        app_ref = weakref.ref(self)

        def sink(event: TaskEvent) -> None:
            app = app_ref()
            if app is not None and app._accept_events:
                app.post_message(TuiTaskMessage(event))

        self.supervisor = RunTaskSupervisor(
            max_concurrency,
            sink,
        )
        self._dashboard = DashboardScreen(
            self.controller,
            self.supervisor,
            self.settings,
        )

    def on_mount(self) -> None:
        self.push_screen(self._dashboard)
        self._poll_timer = self.set_interval(
            self.poll_interval, self.refresh_runs
        )

    async def refresh_runs(self) -> None:
        """Refresh persisted runs without performing workflow mutations."""

        if self._ui_closed or not self._dashboard.is_mounted:
            return
        await self._dashboard.refresh_runs(dict(self.activities))

    async def on_tui_task_message(self, message: TuiTaskMessage) -> None:
        """Apply a validated worker event on Textual's UI loop."""

        if self._ui_closed:
            return
        self.activities[message.event.run_id] = message.event.activity
        await self.refresh_runs()

    async def action_quit(self) -> None:
        await self._close_ui()
        self.exit()

    async def on_unmount(self) -> None:
        await self._close_ui()

    async def _close_ui(self) -> None:
        if self._ui_closed:
            return
        self._ui_closed = True
        self._accept_events = False
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer = None
        await self.supervisor.close()
        self.activities.clear()


__all__ = ["DeveloperWorkflowTuiApp", "TuiTaskMessage"]
