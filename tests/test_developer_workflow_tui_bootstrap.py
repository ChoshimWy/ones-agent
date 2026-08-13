from __future__ import annotations

from types import SimpleNamespace

import pytest


class _SetupController:
    def __init__(self, handle: object | None) -> None:
        self.handle = handle
        self.activate_calls = 0
        self.closed = False

    async def activate_existing(self) -> object | None:
        self.activate_calls += 1
        return self.handle

    async def aclose(self) -> None:
        self.closed = True


class _RuntimeSession:
    def __init__(self) -> None:
        self.controller = object()
        self.supervisor = object()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_incomplete_configuration_opens_setup_without_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
    from src.developer_workflow.tui.setup_screens import SetupRootScreen

    setup = _SetupController(None)
    built: list[object] = []
    monkeypatch.setattr(
        DeveloperWorkflowTuiApp,
        "_build_runtime_session",
        lambda self, handle: built.append(handle),
    )
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup, runtime_bootstrapper=object()
    )

    async with app.run_test() as _pilot:
        assert isinstance(app.screen, SetupRootScreen)
        assert built == []
        assert not app.query("#run-list")


@pytest.mark.asyncio
async def test_existing_configuration_opens_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp

    handle = object()
    setup = _SetupController(handle)
    session = _RuntimeSession()
    monkeypatch.setattr(
        DeveloperWorkflowTuiApp,
        "_build_runtime_session",
        lambda self, value: session,
    )
    monkeypatch.setattr(
        DeveloperWorkflowTuiApp,
        "_build_dashboard",
        lambda self, value: SimpleNamespace(refresh_runs=_async_noop),
    )
    pushed: list[object] = []

    async def push(screen: object, *args: object) -> None:
        pushed.append(screen)

    app = DeveloperWorkflowTuiApp(
        setup_controller=setup, runtime_bootstrapper=object()
    )
    monkeypatch.setattr(app, "push_screen", push)
    monkeypatch.setattr(app, "set_interval", lambda *args: None)
    await app.on_mount()

    assert setup.activate_calls == 1
    assert app.runtime_session is session
    assert len(pushed) == 1


async def _async_noop(*args: object, **kwargs: object) -> None:
    return None


@pytest.mark.asyncio
async def test_activation_closes_setup_before_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp

    setup = _SetupController(None)
    session = _RuntimeSession()
    order: list[str] = []

    async def close_setup() -> None:
        order.append("setup-closed")
        setup.closed = True

    async def push_dashboard(screen: object, *args: object) -> None:
        order.append("dashboard")

    setup.aclose = close_setup  # type: ignore[method-assign]
    monkeypatch.setattr(
        DeveloperWorkflowTuiApp,
        "_build_runtime_session",
        lambda self, value: order.append("runtime-built") or session,
    )
    monkeypatch.setattr(
        DeveloperWorkflowTuiApp,
        "_build_dashboard",
        lambda self, value: SimpleNamespace(refresh_runs=_async_noop),
    )
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup, runtime_bootstrapper=object()
    )
    monkeypatch.setattr(app, "push_screen", push_dashboard)
    monkeypatch.setattr(app, "set_interval", lambda *args: None)

    await app._setup_done(object())

    assert order == ["setup-closed", "runtime-built", "dashboard"]
    assert app.runtime_session is session


@pytest.mark.asyncio
async def test_existing_activation_closes_setup_before_runtime_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp

    order: list[str] = []
    setup = _SetupController(object())

    async def close_setup() -> None:
        order.append("setup-closed")
        setup.closed = True

    setup.aclose = close_setup  # type: ignore[method-assign]
    session = _RuntimeSession()
    monkeypatch.setattr(
        DeveloperWorkflowTuiApp,
        "_build_runtime_session",
        lambda self, value: order.append("runtime-built") or session,
    )
    monkeypatch.setattr(
        DeveloperWorkflowTuiApp,
        "_build_dashboard",
        lambda self, value: SimpleNamespace(refresh_runs=_async_noop),
    )
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup,
        setup_controller_factory=lambda: _SetupController(None),
        runtime_bootstrapper=object(),
    )
    monkeypatch.setattr(app, "push_screen", _async_noop)
    monkeypatch.setattr(app, "set_interval", lambda *args: None)

    await app.on_mount()

    assert order == ["setup-closed", "runtime-built"]


@pytest.mark.asyncio
async def test_runtime_build_failure_recreates_one_retryable_setup_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
    from src.developer_workflow.tui.setup_screens import SetupRootScreen

    first = _SetupController(None)
    replacement = _SetupController(None)
    handles_closed: list[object] = []
    handle = SimpleNamespace(close=lambda: handles_closed.append(handle))
    pushed: list[object] = []

    async def push(screen: object, *args: object) -> None:
        pushed.append(screen)

    app = DeveloperWorkflowTuiApp(
        setup_controller=first,
        setup_controller_factory=lambda: replacement,
        runtime_bootstrapper=object(),
    )
    monkeypatch.setattr(
        app,
        "_build_runtime_session",
        lambda value: (_ for _ in ()).throw(RuntimeError("secret")),
    )
    monkeypatch.setattr(app, "push_screen", push)

    await app._setup_done(handle)

    assert first.closed
    assert app.setup_controller is replacement
    assert len(pushed) == 1
    assert isinstance(pushed[0], SetupRootScreen)
    assert handles_closed == [handle]


@pytest.mark.asyncio
async def test_dashboard_first_refresh_failure_leaves_only_retryable_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from textual.screen import Screen

    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
    from src.developer_workflow.tui.setup_screens import SetupRootScreen

    class FailingDashboard(Screen[None]):
        async def refresh_runs(self) -> None:
            raise RuntimeError("sensitive refresh failure")

        def begin_teardown(self) -> None:
            return None

    first = _SetupController(None)
    replacement = _SetupController(None)
    session = _RuntimeSession()
    app = DeveloperWorkflowTuiApp(
        setup_controller=first,
        setup_controller_factory=lambda: replacement,
        runtime_bootstrapper=object(),
    )
    monkeypatch.setattr(app, "_build_runtime_session", lambda handle: session)
    monkeypatch.setattr(app, "_build_dashboard", lambda value: FailingDashboard())

    async with app.run_test() as pilot:
        setup_screen = app.screen
        assert isinstance(setup_screen, SetupRootScreen)
        setup_screen.complete(object())
        await pilot.pause()
        await pilot.pause()

        assert isinstance(app.screen, SetupRootScreen)
        assert len(app.query(SetupRootScreen)) == 1
        assert not app.query(FailingDashboard)
        assert first.closed
        assert session.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("removal_failure", ["false", "raise"])
async def test_setup_removal_failure_closes_handle_and_rebinds_retryable_setup(
    monkeypatch: pytest.MonkeyPatch,
    removal_failure: str,
) -> None:
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
    from src.developer_workflow.tui.setup_screens import SetupRootScreen

    first = _SetupController(None)
    replacement = _SetupController(None)
    closes: list[str] = []
    handle = SimpleNamespace(close=lambda: closes.append("handle"))
    app = DeveloperWorkflowTuiApp(
        setup_controller=first,
        setup_controller_factory=lambda: replacement,
        runtime_bootstrapper=object(),
    )

    async def fail_remove() -> bool:
        if removal_failure == "raise":
            raise RuntimeError("sensitive remove failure")
        return False

    async with app.run_test() as pilot:
        old_screen = app.screen
        assert isinstance(old_screen, SetupRootScreen)
        monkeypatch.setattr(app, "_remove_setup_screen", fail_remove)
        old_screen.complete(handle)
        await pilot.pause()
        await pilot.pause()

        assert closes == ["handle"]
        assert first.closed
        assert app.setup_controller is replacement
        assert isinstance(app.screen, SetupRootScreen)
        assert app.screen.controller is replacement
        assert len(app.query(SetupRootScreen)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_stage", ["session", "dashboard", "bind", "push", "refresh"]
)
async def test_runtime_transition_failure_has_single_handle_owner_and_one_setup(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    from textual.screen import Screen

    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
    from src.developer_workflow.tui.setup_screens import SetupRootScreen

    closes: list[str] = []
    handle = SimpleNamespace(close=lambda: closes.append("handle"))
    first = _SetupController(None)
    replacement = _SetupController(None)

    class OwnedSession(_RuntimeSession):
        async def close(self) -> None:
            if not self.closed:
                self.closed = True
                handle.close()

    session = OwnedSession()

    class Dashboard(Screen[None]):
        async def refresh_runs(self) -> None:
            if failure_stage == "refresh":
                raise RuntimeError("sensitive refresh failure")

        def begin_teardown(self) -> None:
            return None

    app = DeveloperWorkflowTuiApp(
        setup_controller=first,
        setup_controller_factory=lambda: replacement,
        runtime_bootstrapper=object(),
    )
    original_push = app.push_screen

    def build(value: object) -> _RuntimeSession:
        if failure_stage == "session":
            raise RuntimeError("sensitive session failure")
        return session

    def dashboard(value: object) -> Dashboard:
        if failure_stage == "dashboard":
            raise RuntimeError("sensitive dashboard failure")
        return Dashboard()

    original_bind = app._bind_runtime_session

    def bind(value: object) -> None:
        if failure_stage == "bind":
            raise RuntimeError("sensitive bind failure")
        original_bind(value)  # type: ignore[arg-type]

    monkeypatch.setattr(app, "_build_runtime_session", build)
    monkeypatch.setattr(app, "_build_dashboard", dashboard)
    monkeypatch.setattr(app, "_bind_runtime_session", bind)

    async with app.run_test() as pilot:
        if failure_stage == "push":
            async def fail_dashboard_push(screen: object, *args: object):
                if isinstance(screen, Dashboard):
                    raise RuntimeError("sensitive push failure")
                return await original_push(screen, *args)

            monkeypatch.setattr(app, "push_screen", fail_dashboard_push)
        setup_screen = app.screen
        assert isinstance(setup_screen, SetupRootScreen)
        setup_screen.complete(handle)
        await pilot.pause()
        await pilot.pause()

        assert closes == ["handle"]
        assert app.runtime_session is None
        assert isinstance(app.screen, SetupRootScreen)
        assert len(app.query(SetupRootScreen)) == 1
        assert not app.query(Dashboard)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_stage", ["session", "dashboard", "bind", "push", "refresh"]
)
async def test_existing_runtime_transition_failure_transfers_ownership_once(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    from textual.screen import Screen

    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
    from src.developer_workflow.tui.setup_screens import SetupRootScreen

    closes: list[str] = []
    handle = SimpleNamespace(close=lambda: closes.append("handle"))
    first = _SetupController(handle)
    replacement = _SetupController(None)

    class OwnedSession(_RuntimeSession):
        async def close(self) -> None:
            if not self.closed:
                self.closed = True
                handle.close()

    session = OwnedSession()

    class Dashboard(Screen[None]):
        async def refresh_runs(self) -> None:
            if failure_stage == "refresh":
                raise RuntimeError("sensitive refresh failure")

        def begin_teardown(self) -> None:
            return None

    app = DeveloperWorkflowTuiApp(
        setup_controller=first,
        setup_controller_factory=lambda: replacement,
        runtime_bootstrapper=object(),
    )

    def build(value: object) -> _RuntimeSession:
        if failure_stage == "session":
            raise RuntimeError("sensitive session failure")
        return session

    def dashboard(value: object) -> Dashboard:
        if failure_stage == "dashboard":
            raise RuntimeError("sensitive dashboard failure")
        return Dashboard()

    original_bind = app._bind_runtime_session

    def bind(value: object) -> None:
        if failure_stage == "bind":
            raise RuntimeError("sensitive bind failure")
        original_bind(value)  # type: ignore[arg-type]

    monkeypatch.setattr(app, "_build_runtime_session", build)
    monkeypatch.setattr(app, "_build_dashboard", dashboard)
    monkeypatch.setattr(app, "_bind_runtime_session", bind)
    if failure_stage == "push":
        original_push = app.push_screen

        async def fail_dashboard_push(screen: object, *args: object):
            if isinstance(screen, Dashboard):
                raise RuntimeError("sensitive push failure")
            return await original_push(screen, *args)

        monkeypatch.setattr(app, "push_screen", fail_dashboard_push)

    async with app.run_test() as pilot:
        await pilot.pause()

        assert closes == ["handle"]
        assert first.closed
        assert app.runtime_session is None
        assert isinstance(app.screen, SetupRootScreen)
        assert len(app.query(SetupRootScreen)) == 1
        assert not app.query(Dashboard)


@pytest.mark.asyncio
async def test_activation_callback_after_exit_closes_unclaimed_handle() -> None:
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp

    closes: list[str] = []
    handle = SimpleNamespace(close=lambda: closes.append("handle"))
    app = DeveloperWorkflowTuiApp(
        setup_controller=_SetupController(None),
        runtime_bootstrapper=object(),
    )
    app._ui_closed = True

    await app._setup_done(handle)

    assert closes == ["handle"]


@pytest.mark.asyncio
async def test_close_wins_while_setup_close_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp

    entered = asyncio.Event()
    release = asyncio.Event()
    closes: list[str] = []
    builds: list[object] = []

    class BlockingSetup(_SetupController):
        close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1
            entered.set()
            await release.wait()
            self.closed = True

    setup = BlockingSetup(None)
    handle = SimpleNamespace(close=lambda: closes.append("handle"))
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup,
        setup_controller_factory=lambda: _SetupController(None),
        runtime_bootstrapper=object(),
    )
    monkeypatch.setattr(
        app,
        "_build_runtime_session",
        lambda value: builds.append(value) or _RuntimeSession(),
    )

    transition = asyncio.create_task(app._setup_done(handle))
    await entered.wait()
    closing = asyncio.create_task(app._close_ui())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(transition, closing)

    assert builds == []
    assert closes == ["handle"]
    assert setup.close_calls == 1
    assert app.runtime_session is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fatal_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("fatal_stage", ["remove", "setup-close", "session-build"])
async def test_transition_fatal_closes_activation_handle_once_and_preserves_fatal(
    monkeypatch: pytest.MonkeyPatch,
    fatal_type: type[BaseException],
    fatal_stage: str,
) -> None:
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp

    closes: list[str] = []
    handle = SimpleNamespace(close=lambda: closes.append("handle"))
    setup = _SetupController(None)

    if fatal_stage == "setup-close":
        async def fatal_close() -> None:
            raise fatal_type("original fatal")

        setup.aclose = fatal_close  # type: ignore[method-assign]

    app = DeveloperWorkflowTuiApp(
        setup_controller=setup,
        setup_controller_factory=lambda: _SetupController(None),
        runtime_bootstrapper=object(),
    )
    if fatal_stage == "remove":
        async def fatal_remove() -> bool:
            raise fatal_type("original fatal")

        monkeypatch.setattr(app, "_remove_setup_screen", fatal_remove)
    if fatal_stage == "session-build":
        monkeypatch.setattr(
            app,
            "_build_runtime_session",
            lambda value: (_ for _ in ()).throw(fatal_type("original fatal")),
        )

    with pytest.raises(fatal_type, match="original fatal"):
        await app._setup_done(handle)

    assert closes == ["handle"]
    assert app.runtime_session is None


@pytest.mark.asyncio
async def test_close_wins_during_recovery_session_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from textual.screen import Screen

    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp

    entered = asyncio.Event()
    release = asyncio.Event()
    closes: list[str] = []
    factories: list[str] = []
    handle = SimpleNamespace(close=lambda: closes.append("handle"))

    class BlockingSession(_RuntimeSession):
        async def close(self) -> None:
            if self.closed:
                return
            self.closed = True
            entered.set()
            await release.wait()
            handle.close()

    class FailingDashboard(Screen[None]):
        async def refresh_runs(self) -> None:
            raise RuntimeError("sensitive refresh failure")

        def begin_teardown(self) -> None:
            return None

    session = BlockingSession()

    def replacement() -> _SetupController:
        factories.append("factory")
        return _SetupController(None)

    app = DeveloperWorkflowTuiApp(
        setup_controller=_SetupController(None),
        setup_controller_factory=replacement,
        runtime_bootstrapper=object(),
    )
    monkeypatch.setattr(app, "_build_runtime_session", lambda value: session)
    monkeypatch.setattr(app, "_build_dashboard", lambda value: FailingDashboard())

    transition = asyncio.create_task(app._setup_done(handle))
    await entered.wait()
    closing = asyncio.create_task(app._close_ui())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(transition, closing)

    assert closes == ["handle"]
    assert factories == []
    assert app.runtime_session is None


@pytest.mark.asyncio
async def test_concurrent_activation_callback_builds_one_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp

    setup = _SetupController(None)
    builds: list[object] = []

    def build(self: object, handle: object) -> _RuntimeSession:
        builds.append(handle)
        return _RuntimeSession()

    monkeypatch.setattr(DeveloperWorkflowTuiApp, "_build_runtime_session", build)
    monkeypatch.setattr(
        DeveloperWorkflowTuiApp,
        "_build_dashboard",
        lambda self, value: SimpleNamespace(refresh_runs=_async_noop),
    )
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup, runtime_bootstrapper=object()
    )
    monkeypatch.setattr(app, "push_screen", _async_noop)
    monkeypatch.setattr(app, "set_interval", lambda *args: None)

    await asyncio.gather(app._setup_done(object()), app._setup_done(object()))

    assert len(builds) == 1


@pytest.mark.asyncio
async def test_runtime_session_close_is_ordered_idempotent_and_clears_sink() -> None:
    from src.developer_workflow.tui.runtime_session import TuiRuntimeSession

    order: list[str] = []
    emitted: list[object] = []
    handle = SimpleNamespace(close=lambda: order.append("handle"))
    controller = SimpleNamespace(close=lambda: order.append("controller"))

    class Supervisor:
        async def close(self) -> None:
            order.append("supervisor")

    session = TuiRuntimeSession(
        handle=handle,
        controller=controller,
        run_index=object(),
        supervisor=Supervisor(),
        event_sink=emitted.append,
    )
    session.emit(object())
    await session.close()
    session.emit(object())
    await session.close()

    assert len(emitted) == 1
    assert order == ["supervisor", "controller", "handle"]


@pytest.mark.asyncio
async def test_runtime_session_close_drains_later_resources_after_failure() -> None:
    from src.developer_workflow.tui.runtime_session import (
        TuiRuntimeCloseError,
        TuiRuntimeSession,
    )

    order: list[str] = []
    handle = SimpleNamespace(close=lambda: order.append("handle"))

    def close_controller() -> None:
        order.append("controller")
        raise RuntimeError("sensitive backend failure")

    controller = SimpleNamespace(close=close_controller)

    class Supervisor:
        async def close(self) -> None:
            order.append("supervisor")

    session = TuiRuntimeSession(
        handle=handle,
        controller=controller,
        run_index=object(),
        supervisor=Supervisor(),
        event_sink=lambda event: None,
    )

    with pytest.raises(TuiRuntimeCloseError) as raised:
        await session.close()

    assert str(raised.value) == "TUI runtime close failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert order == ["supervisor", "controller", "handle"]


def test_tui_dispatches_before_legacy_config_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow.cli import main
    from src.developer_workflow.config import DeveloperWorkflowConfig

    calls: list[object] = []
    monkeypatch.setattr(
        DeveloperWorkflowConfig,
        "load",
        lambda path: (_ for _ in ()).throw(AssertionError("legacy load called")),
    )

    code = main(
        ["tui", "--config", "missing.json"],
        tui_host_factory=lambda path: calls.append(path) or (object(), object()),
        tui_runner=lambda setup, bootstrap: calls.append((setup, bootstrap)),
    )

    assert code == 0
    assert calls[0].name == "missing.json"
    assert len(calls) == 2
