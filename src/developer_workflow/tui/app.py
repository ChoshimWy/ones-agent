"""Full-screen Textual application for developer workflows."""

from __future__ import annotations

from textual.app import App
from textual.binding import Binding
from textual.message import Message

from .controller import TuiController
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
    ) -> None:
        super().__init__()
        self.controller = controller
        self.settings = SettingsView(
            max_concurrency=max_concurrency,
            provider_type=provider_type,
            sandbox_configured=sandbox_configured,
        )
        self.supervisor = RunTaskSupervisor(
            max_concurrency,
            lambda event: self.post_message(TuiTaskMessage(event)),
        )

    def on_mount(self) -> None:
        self.push_screen(
            DashboardScreen(self.controller, self.supervisor, self.settings)
        )


__all__ = ["DeveloperWorkflowTuiApp", "TuiTaskMessage"]
