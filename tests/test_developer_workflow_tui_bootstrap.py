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
        lambda self, value: session,
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

    assert order == ["setup-closed", "dashboard"]
    assert app.runtime_session is session


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
