"""Full-screen Textual application for developer workflows."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable

from textual.app import App
from textual.binding import Binding
from textual.message import Message
from textual.timer import Timer

from .controller import TuiController
from .models import RunActivity
from .screens import DashboardScreen, HelpScreen, SettingsView
from .runtime_session import TuiRuntimeSession
from .setup_screens import SetupRootScreen
from .supervisor import TaskEvent


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
        controller: TuiController | None = None,
        max_concurrency: int = 3,
        *,
        setup_controller: object | None = None,
        setup_controller_factory: Callable[[], object] | None = None,
        runtime_bootstrapper: object | None = None,
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
        if controller is None and (
            runtime_bootstrapper is None
            or (setup_controller is None and setup_controller_factory is None)
        ):
            raise ValueError("TUI bootstrap configuration is invalid")
        if controller is not None and setup_controller is not None:
            raise ValueError("TUI bootstrap configuration is invalid")
        self.setup_controller = setup_controller
        self._setup_controller_factory = setup_controller_factory
        if self.setup_controller is None and self._setup_controller_factory is not None:
            self.setup_controller = self._new_setup_controller()
        self.runtime_bootstrapper = runtime_bootstrapper
        self.runtime_session: TuiRuntimeSession | None = None
        self.controller: TuiController | None = None
        self.supervisor: object | None = None
        self.poll_interval = float(poll_interval)
        self._max_concurrency = max_concurrency
        self.settings = SettingsView(
            max_concurrency=max_concurrency,
            provider_type=provider_type,
            sandbox_configured=sandbox_configured,
        )
        self.activities: dict[str, RunActivity] = {}
        self._accept_events = True
        self._ui_closed = False
        self._transition_lock = asyncio.Lock()
        self._poll_timer: Timer | None = None
        app_ref = weakref.ref(self)

        def sink(event: TaskEvent) -> None:
            app = app_ref()
            if app is not None and app._accept_events:
                app.post_message(TuiTaskMessage(event))

        self._event_sink = sink
        self._dashboard: DashboardScreen | None = None
        self._setup_screen: SetupRootScreen | None = None
        if controller is not None:
            self.runtime_session = TuiRuntimeSession.from_controller(
                controller, max_concurrency, sink
            )
            self._bind_runtime_session(self.runtime_session)

    async def on_mount(self) -> None:
        if self.runtime_session is None:
            assert self.setup_controller is not None
            try:
                handle = await self.setup_controller.activate_existing()
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                handle = None
            if handle is None:
                await self._show_setup()
                return
            try:
                await self._close_setup_controller()
                await self._replace_with_runtime(handle)
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                await self._recover_setup(handle=handle)
            return
        await self._mount_dashboard()

    def _build_runtime_session(self, handle: object) -> TuiRuntimeSession:
        return TuiRuntimeSession.from_handle(
            handle, self._max_concurrency, self._event_sink  # type: ignore[arg-type]
        )

    def _new_setup_controller(self) -> object:
        factory = self._setup_controller_factory
        if factory is None:
            raise RuntimeError("TUI setup recovery is unavailable")
        try:
            controller = factory()
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise RuntimeError("TUI setup recovery is unavailable") from None
        if controller is None:
            raise RuntimeError("TUI setup recovery is unavailable")
        return controller

    async def _show_setup(self) -> None:
        if self.setup_controller is None:
            self.setup_controller = self._new_setup_controller()
        screen = SetupRootScreen(self.setup_controller)
        self._setup_screen = screen
        await self.push_screen(screen, self._setup_done)

    async def _remove_setup_screen(self) -> bool:
        screen = self._setup_screen
        if screen is None:
            return True
        try:
            if screen.is_attached:
                await screen.remove()
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return False
        self._setup_screen = None
        return True

    async def _close_setup_controller(self) -> None:
        controller = self.setup_controller
        if controller is None:
            return
        close = getattr(controller, "aclose", None)
        if callable(close):
            await close()
        else:
            controller.close()
        self.setup_controller = None

    def _build_dashboard(self, session: TuiRuntimeSession) -> DashboardScreen:
        return DashboardScreen(
            session.controller,
            session.supervisor,
            self.settings,
        )

    def _bind_runtime_session(self, session: TuiRuntimeSession) -> None:
        self.runtime_session = session
        self.controller = session.controller
        self.supervisor = session.supervisor
        self._dashboard = self._build_dashboard(session)

    async def _mount_dashboard(self) -> None:
        dashboard = self._dashboard
        if dashboard is None:
            raise RuntimeError("TUI runtime is unavailable")
        await self.push_screen(dashboard)
        await dashboard.refresh_runs()
        self._poll_timer = self.set_interval(
            self.poll_interval, self.refresh_runs
        )

    async def _replace_with_runtime(self, handle: object) -> None:
        session = self._build_runtime_session(handle)
        self._bind_runtime_session(session)
        try:
            await self._mount_dashboard()
        except BaseException:
            self.runtime_session = None
            self.controller = None
            self.supervisor = None
            self._dashboard = None
            await session.close()
            raise

    async def _setup_done(self, handle: object | None) -> None:
        async with self._transition_lock:
            if handle is None or self._ui_closed or self.runtime_session is not None:
                if handle is not None and self.runtime_session is not None:
                    try:
                        await asyncio.wait_for(
                            asyncio.to_thread(handle.close), timeout=5.0
                        )
                    except BaseException:
                        pass
                return
            if not await self._remove_setup_screen():
                return
            await self._finish_setup(handle)

    async def _finish_setup(self, handle: object) -> None:
        session: TuiRuntimeSession | None = None
        try:
            await self._close_setup_controller()
            session = self._build_runtime_session(handle)
            self._bind_runtime_session(session)
            await self._mount_dashboard()
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            await self._recover_setup(handle=handle, session=session)

    async def _recover_setup(
        self,
        *,
        handle: object,
        session: TuiRuntimeSession | None = None,
    ) -> None:
        dashboard = self._dashboard
        if dashboard is not None:
            try:
                dashboard.begin_teardown()
            except BaseException:
                pass
            if self.screen is dashboard:
                try:
                    self.pop_screen()
                except BaseException:
                    pass
            try:
                if dashboard.is_attached:
                    await dashboard.remove()
            except BaseException:
                pass
        if session is not None:
            try:
                await session.close()
            except BaseException:
                pass
        else:
            try:
                await asyncio.wait_for(asyncio.to_thread(handle.close), timeout=5.0)
            except BaseException:
                pass
        self.runtime_session = None
        self.controller = None
        self.supervisor = None
        self._dashboard = None
        if not self._ui_closed:
            self.setup_controller = self._new_setup_controller()
            await self._show_setup()

    async def refresh_runs(self) -> None:
        """Refresh persisted runs without performing workflow mutations."""

        dashboard = self._dashboard
        if dashboard is None:
            return
        mount_generation = dashboard.mount_generation
        if (
            self._ui_closed
            or self.screen is not dashboard
            or not dashboard.owns_refresh(mount_generation)
        ):
            return
        await dashboard.refresh_runs(
            dict(self.activities),
            mount_generation=mount_generation,
        )
        if (
            self._ui_closed
            or self._dashboard is not dashboard
            or self.screen is not dashboard
            or not dashboard.owns_refresh(mount_generation)
        ):
            return

    async def on_tui_task_message(self, message: TuiTaskMessage) -> None:
        """Apply a validated worker event on Textual's UI loop."""

        if self._ui_closed:
            return
        self.activities[message.event.run_id] = message.event.activity
        await self.refresh_runs()

    async def action_quit(self) -> None:
        await self._close_ui()
        self.exit()

    def action_help(self) -> None:
        if self._dashboard is not None and self.screen is self._dashboard:
            self.push_screen(HelpScreen())

    def action_search(self) -> None:
        if self._dashboard is not None and self.screen is self._dashboard:
            self._dashboard.action_search()

    def action_filter(self) -> None:
        if self._dashboard is not None and self.screen is self._dashboard:
            self._dashboard.action_filter()

    async def on_unmount(self) -> None:
        await self._close_ui()

    async def _close_ui(self) -> None:
        if self._dashboard is not None:
            self._dashboard.begin_teardown()
        if self._ui_closed:
            return
        self._ui_closed = True
        self._accept_events = False
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer = None
        if self.runtime_session is not None:
            try:
                if (
                    self.supervisor is not None
                    and self.supervisor is not self.runtime_session.supervisor
                ):
                    await self.supervisor.close()
                await self.runtime_session.close()
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
            self.runtime_session = None
        elif self.setup_controller is not None:
            close = getattr(self.setup_controller, "aclose", None)
            try:
                if callable(close):
                    await close()
                else:
                    self.setup_controller.close()
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
        self.activities.clear()


__all__ = ["DeveloperWorkflowTuiApp", "TuiTaskMessage"]
