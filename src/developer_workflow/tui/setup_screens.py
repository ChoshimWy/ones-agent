"""Minimal bootstrap screen used before the full setup wizard is mounted."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static


class SetupRootScreen(Screen[object | None]):
    """Host setup safely without constructing or inspecting workflow runtime data."""

    def __init__(self, controller: object) -> None:
        super().__init__(id="setup-root")
        self.controller = controller

    def compose(self) -> ComposeResult:
        yield Static("Runtime configuration is required", id="setup-required")

    def complete(self, handle: object) -> None:
        """Hand a successfully activated runtime back to the application host."""

        if handle is None:
            return
        self.dismiss(handle)


__all__ = ["SetupRootScreen"]
