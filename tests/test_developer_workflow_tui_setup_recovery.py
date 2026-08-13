from __future__ import annotations

from types import SimpleNamespace

import pytest


def _controller_for_store(store: object):
    from tests.test_developer_workflow_setup_controller import (
        FakeBootstrap,
        FakeRuntimeBuilder,
    )
    from src.developer_workflow.setup_controller import SetupController

    return SetupController(
        profile_id="managed-profile",
        store=store,  # type: ignore[arg-type]
        runtime_builder=FakeRuntimeBuilder(),
        runtime_bootstrap=FakeBootstrap(),
    )


class _RecoveryController:
    def __init__(self, *, previous: bool = True, orphans: tuple[str, ...] = ()) -> None:
        self.recovery_state = SimpleNamespace(
            previous_available=previous,
            orphan_count=len(orphans),
            error_category="activation_recovery_required",
            owner_generation="b" * 32,
        )
        self.orphans = orphans
        self.calls: list[tuple[str, object]] = []

    def list_orphan_generations(self) -> tuple[str, ...]:
        return self.orphans

    def restore_pending(self, generation: str) -> None:
        self.calls.append(("restore", generation))

    def discard_pending(self, generation: str) -> None:
        self.calls.append(("discard", generation))

    def cleanup_orphans(self, generations: tuple[str, ...]) -> None:
        self.calls.append(("cleanup", generations))


def _screen_app(screen: object):
    from textual.app import App

    class ScreenApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(screen)  # type: ignore[arg-type]

    return ScreenApp()


@pytest.mark.asyncio
async def test_recovery_plain_enter_never_mutates_and_actions_require_confirmation() -> None:
    from src.developer_workflow.tui.setup_screens import SetupRecoveryScreen

    controller = _RecoveryController(orphans=("c" * 32,))
    screen = SetupRecoveryScreen(controller)
    async with _screen_app(screen).run_test() as pilot:
        screen.query_one("#restore-previous").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert controller.calls == []

        await pilot.click("#restore-previous")
        await pilot.pause()
        assert controller.calls == []
        await pilot.click("#confirm-recovery-action")
        await pilot.pause()
        assert controller.calls == [("restore", screen.owner_generation)]


@pytest.mark.asyncio
async def test_recovery_summary_is_fixed_and_never_exposes_backend_values() -> None:
    from src.developer_workflow.tui.setup_screens import SetupRecoveryScreen

    controller = _RecoveryController(orphans=("d" * 32,))
    screen = SetupRecoveryScreen(controller, owner_generation="a" * 32)
    async with _screen_app(screen).run_test() as pilot:
        rendered = "\n".join(
            str(widget.render()) for widget in pilot.app.screen.query("Static")
        )
        assert "a" * 32 not in rendered
        assert "d" * 32 not in rendered
        assert "Previous configuration: available" in rendered
        assert "Orphan credentials: 1" in rendered


@pytest.mark.asyncio
async def test_setup_activation_button_opens_confirmation_before_callback() -> None:
    from src.developer_workflow.setup_validation import SetupStep, ValidationStatus
    from src.developer_workflow.tui.setup_screens import SetupWizardScreen

    from tests.test_developer_workflow_tui_bootstrap import (
        _WizardSetupController,
    )

    controller = _WizardSetupController()
    for step in tuple(SetupStep)[:-1]:
        controller._results[step] = controller._results[step].model_copy(
            update={"status": ValidationStatus.PASSED, "category": "ok"}
        )
    controller.current_step = SetupStep.REVIEW
    calls: list[str] = []

    async def activate() -> object:
        calls.append("activate")
        return object()

    screen = SetupWizardScreen(controller, activation_callback=activate)
    async with _screen_app(screen).run_test() as pilot:
        await pilot.click("#activate-runtime")
        await pilot.pause()
        assert calls == []
        await pilot.press("enter")
        await pilot.pause()
        assert calls == []
        await pilot.click("#confirm-setup-activation")
        await pilot.pause()
        assert calls == ["activate"]


@pytest.mark.asyncio
async def test_reconfigure_closes_runtime_before_creating_secret_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp

    order: list[str] = []

    class Session:
        controller = object()
        supervisor = object()

        async def close(self) -> None:
            order.append("runtime-close")

    class Setup:
        def load_active_public_draft(self) -> None:
            order.append("public-draft")

    app = DeveloperWorkflowTuiApp(
        setup_controller_factory=lambda: order.append("factory") or Setup(),
        runtime_bootstrapper=object(),
    )
    order.clear()
    app.runtime_session = Session()  # type: ignore[assignment]
    app.controller = app.runtime_session.controller  # type: ignore[assignment]
    app.supervisor = app.runtime_session.supervisor
    app._dashboard = SimpleNamespace(begin_teardown=lambda: order.append("teardown"))

    async def remove() -> None:
        order.append("remove")

    async def show() -> None:
        order.append("show-setup")

    monkeypatch.setattr(app, "_remove_dashboard", remove)
    monkeypatch.setattr(app, "_show_setup", show)

    await app._begin_reconfigure()

    assert order.index("runtime-close") < order.index("factory")
    assert order.index("public-draft") < order.index("show-setup")
    assert app.runtime_session is None


def test_controller_recovery_uses_owner_generation_cas_and_fixed_errors(
    tmp_path,
) -> None:
    from src.developer_workflow.setup_controller import SetupActionError
    from src.developer_workflow.setup_models import SetupDocument
    from src.developer_workflow.setup_store import SetupStoreError
    from tests.test_developer_workflow_setup_controller import _candidate_for_store

    pending = _candidate_for_store(tmp_path, "b" * 32)

    class Store:
        document = SetupDocument(
            profile_id="managed-profile",
            active=pending,
            activation_owner_generation=pending.generation,
        )
        restored: list[str] = []

        def load_or_empty(self, *, profile_id: str):
            return self.document

        def orphan_generations(self) -> tuple[str, ...]:
            return ("c" * 32,)

        def restore_previous(self, profile_id: str, generation: str):
            if generation != self.document.activation_owner_generation:
                raise SetupStoreError("SECRET superseded generation")
            self.restored.append(generation)
            self.document = self.document.validated_update(
                active=None, previous=None, activation_owner_generation=None
            )
            return self.document

    store = Store()
    controller = _controller_for_store(store)
    state = controller.recovery_state
    assert state.owner_generation == "b" * 32
    assert state.previous_available is False
    assert state.orphan_count == 1
    assert "SECRET" not in repr(state)

    with pytest.raises(SetupActionError) as raised:
        controller.restore_pending("a" * 32)
    assert str(raised.value) == "activation recovery failed"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert store.restored == []

    controller.restore_pending("b" * 32)
    assert store.restored == ["b" * 32]


def test_reconfigure_loads_only_public_active_draft(tmp_path) -> None:
    from src.developer_workflow.setup_models import SetupDocument
    from tests.test_developer_workflow_setup_controller import _candidate_for_store

    active = _candidate_for_store(tmp_path, "a" * 32)

    class Store:
        def load_or_empty(self, *, profile_id: str):
            return SetupDocument(profile_id=profile_id, active=active)

    controller = _controller_for_store(Store())
    controller.load_active_public_draft()

    assert controller.draft.runtime == active.runtime
    assert controller.draft.workflow.model_dump(mode="python") == (
        active.workflow.model_dump(mode="python")
    )
    assert controller.state.secret_count == 0
    assert "persisted-password" not in repr(controller.draft)


def test_orphan_cleanup_requires_exact_fresh_snapshot() -> None:
    from src.developer_workflow.setup_controller import SetupActionError

    class Store:
        current = ("c" * 32, "d" * 32)
        cleaned: list[tuple[str, ...]] = []

        def orphan_generations(self) -> tuple[str, ...]:
            return self.current

        def cleanup_orphan_generations(self, generations: tuple[str, ...]) -> None:
            self.cleaned.append(generations)

    store = Store()
    controller = _controller_for_store(store)
    stale = controller.list_orphan_generations()
    store.current = ("d" * 32,)
    with pytest.raises(SetupActionError, match="^credential cleanup failed$"):
        controller.cleanup_orphans(stale)
    assert store.cleaned == []
    controller.cleanup_orphans(store.current)
    assert store.cleaned == [("d" * 32,)]


@pytest.mark.asyncio
async def test_pending_startup_mounts_recovery_without_runtime_or_secret_read() -> None:
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
    from src.developer_workflow.tui.setup_screens import SetupRecoveryScreen

    calls: list[str] = []

    class Setup:
        recovery_state = SimpleNamespace(
            owner_generation="b" * 32,
            previous_available=True,
            orphan_count=1,
            error_category="activation_recovery_required",
        )

        async def activate_existing(self) -> None:
            calls.append("inspect-pointer")
            return None

        async def aclose(self) -> None:
            calls.append("setup-close")

    setup = Setup()
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup,
        runtime_bootstrapper=object(),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, SetupRecoveryScreen)
        assert calls == ["inspect-pointer"]
        assert app.runtime_session is None


@pytest.mark.asyncio
async def test_review_ctrl_enter_opens_confirmation_without_activating() -> None:
    from src.developer_workflow.setup_validation import SetupStep, ValidationStatus
    from src.developer_workflow.tui.setup_screens import SetupWizardScreen
    from tests.test_developer_workflow_tui_bootstrap import _WizardSetupController

    controller = _WizardSetupController()
    for step in tuple(SetupStep)[:-1]:
        controller._results[step] = controller._results[step].model_copy(
            update={"status": ValidationStatus.PASSED, "category": "ok"}
        )
    controller.current_step = SetupStep.REVIEW
    calls: list[str] = []

    async def activate() -> None:
        calls.append("activate")

    screen = SetupWizardScreen(controller, activation_callback=activate)
    async with _screen_app(screen).run_test() as pilot:
        await pilot.press("ctrl+enter")
        await pilot.pause()
        assert pilot.app.screen.id == "setup-activation-confirmation"
        assert calls == []


@pytest.mark.asyncio
async def test_reconfigure_close_failure_exits_without_secret_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp

    calls: list[str] = []

    class Session:
        controller = object()
        supervisor = object()

        async def close(self) -> None:
            calls.append("runtime-close")
            raise RuntimeError("SENSITIVE runtime close")

    app = DeveloperWorkflowTuiApp(
        setup_controller_factory=lambda: calls.append("factory") or object(),
        runtime_bootstrapper=object(),
    )
    calls.clear()
    app.runtime_session = Session()  # type: ignore[assignment]
    app._dashboard = SimpleNamespace(begin_teardown=lambda: None)

    async def remove() -> None:
        calls.append("remove")

    async def close_ui() -> None:
        calls.append("safe-exit")
        app._ui_closed = True

    monkeypatch.setattr(app, "_remove_dashboard", remove)
    monkeypatch.setattr(app, "_close_ui", close_ui)
    monkeypatch.setattr(app, "exit", lambda: calls.append("exit"))

    await app._begin_reconfigure()

    assert calls == ["runtime-close", "remove", "safe-exit", "exit"]
    assert app.runtime_session is None
    assert app.setup_controller is not None  # startup controller; no replacement made
