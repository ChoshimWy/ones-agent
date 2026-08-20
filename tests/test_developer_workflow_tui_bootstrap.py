from __future__ import annotations

from types import SimpleNamespace
from types import MappingProxyType

import pytest


class _WizardSetupController:
    def __init__(self) -> None:
        from src.developer_workflow.setup_validation import (
            ConnectionTestResult,
            SetupStep,
            ValidationStatus,
        )

        self.current_step = SetupStep.PROFILE
        self.calls: list[tuple[SetupStep, object]] = []
        self.cancel_calls = 0
        self.runtime_calls: list[object] = []
        self.workflow_calls: list[object] = []
        self.secret_calls: list[tuple[object, str]] = []
        self.repository_calls: list[dict[str, object]] = []
        self._results = {
            step: ConnectionTestResult(
                step=step,
                status=ValidationStatus.NOT_CONFIGURED,
                category="invalid_field",
            )
            for step in SetupStep
        }

    async def list_managed_profiles(self) -> tuple[str, ...]:
        return ("managed-profile", "restricted-profile")

    @property
    def state(self) -> object:
        return SimpleNamespace(
            current_step=self.current_step,
            results=tuple(self._results.values()),
            repository_count=0,
            repository_group_count=0,
            secret_count=0,
            review_confirmed=False,
            closed=False,
            error_category=None,
        )

    def result_for(self, step: object) -> object:
        return self._results[step]

    async def test_step(self, step: object, probe: object = None) -> object:
        from src.developer_workflow.setup_validation import (
            ConnectionTestResult,
            ValidationStatus,
        )

        self.calls.append((step, probe))
        result = ConnectionTestResult(
            step=step,
            status=ValidationStatus.PASSED,
            category="ok",
        )
        self._results[step] = result
        return result

    def cancel_edit(self) -> None:
        self.cancel_calls += 1

    def set_secret(self, kind: object, value: str) -> None:
        self.secret_calls.append((kind, value))

    def apply_runtime(self, runtime: object, **kwargs: object) -> None:
        self.runtime_calls.append(runtime)

    def apply_workflow(self, workflow: object, **kwargs: object) -> None:
        self.workflow_calls.append(workflow)

    def add_repository(self, **fields: object) -> object:
        self.repository_calls.append(fields)
        return SimpleNamespace(key=fields["key"])


def _wizard_app(controller: object):
    from textual.app import App

    from src.developer_workflow.tui.setup_screens import SetupWizardScreen

    class WizardApp(App[None]):
        CSS_PATH = "../src/developer_workflow/tui/tui.tcss"

        def on_mount(self) -> None:
            self.push_screen(SetupWizardScreen(controller))

    return WizardApp()


async def _wait_until(pilot: object, predicate, *, attempts: int = 80) -> None:
    """Yield to Textual deterministically until an observable condition holds."""

    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause()
    assert predicate()


async def _open_workspace_profile_modal(pilot: object) -> None:
    from textual.widgets import Button

    await _wait_until(
        pilot,
        lambda: pilot.app.screen.id == "setup-wizard"
        and all(
            not (item.id or "").startswith("setup-workspace-profile-confirmation-")
            for item in pilot.app.screen_stack
        ),
    )
    button = pilot.app.screen.query_one("#create-workspace-profile", Button)
    assert not button.disabled
    pilot.app.screen._request_builtin_workspace_profile()

    def modal_ready() -> bool:
        if not (pilot.app.screen.id or "").startswith(
            "setup-workspace-profile-confirmation-"
        ):
            return False
        buttons = list(pilot.app.screen.query("#confirm-workspace-profile"))
        return len(buttons) == 1 and buttons[0].region.height > 0

    await _wait_until(
        pilot,
        modal_ready,
    )


class _RuntimeProfileController(_WizardSetupController):
    def __init__(self, outcomes: list[object] | None = None) -> None:
        from src.developer_workflow.setup_models import SetupDraft, WorkflowDraft

        super().__init__()
        self.draft = SetupDraft(workflow=WorkflowDraft())
        self.profile_confirm_calls = 0
        self.profile_confirm_outcomes = list(outcomes or [True])

    async def list_managed_profiles(self) -> tuple[str, ...]:
        return ()

    async def confirm_builtin_workspace_profile(self) -> str:
        from src.developer_workflow.config import (
            BUILTIN_WORKSPACE_PROFILE,
            SandboxPermissionProfileSource,
        )
        from src.developer_workflow.setup_models import WorkflowDraft

        self.profile_confirm_calls += 1
        outcome = self.profile_confirm_outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if hasattr(outcome, "wait"):
            await outcome.wait()
        workflow_data = self.draft.workflow.model_dump(mode="python", round_trip=True)
        workflow_data.update(
            sandbox_permission_profile=BUILTIN_WORKSPACE_PROFILE,
            sandbox_permission_profile_source=(
                SandboxPermissionProfileSource.BUILTIN_WORKSPACE
            ),
        )
        self.draft = self.draft.model_copy(
            update={"workflow": WorkflowDraft.model_validate(workflow_data)}
        )
        return BUILTIN_WORKSPACE_PROFILE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "width,layout", [(60, "one"), (99, "two"), (100, "two"), (120, "three")]
)
async def test_setup_layout_preserves_explicit_actions(width: int, layout: str) -> None:
    controller = _WizardSetupController()
    async with _wizard_app(controller).run_test(size=(width, 32)) as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert screen.has_class(layout)
        assert screen.query_one("#test-connection").display
        assert screen.query_one("#next-step").display
        assert screen.query_one("#back-step").display
        assert screen.query_one("#cancel-setup").display


@pytest.mark.asyncio
async def test_setup_plain_enter_does_not_test_advance_or_activate() -> None:
    from src.developer_workflow.setup_validation import SetupStep, ValidationStatus

    controller = _WizardSetupController()
    controller.current_step = SetupStep.ONES
    controller._results[SetupStep.PROFILE] = controller._results[
        SetupStep.PROFILE
    ].model_copy(update={"status": ValidationStatus.PASSED, "category": "ok"})
    async with _wizard_app(controller).run_test() as pilot:
        password = pilot.app.screen.query_one("#ones-password")
        password.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert controller.calls == []
        assert pilot.app.screen.current_step is SetupStep.ONES


@pytest.mark.asyncio
async def test_setup_test_uses_immutable_transient_snapshot_and_authoritative_result() -> None:
    from src.developer_workflow.setup_validation import SetupStep, ValidationStatus

    controller = _WizardSetupController()
    controller.current_step = SetupStep.ONES
    controller._results[SetupStep.PROFILE] = controller._results[
        SetupStep.PROFILE
    ].model_copy(update={"status": ValidationStatus.PASSED, "category": "ok"})
    async with _wizard_app(controller).run_test() as pilot:
        password = pilot.app.screen.query_one("#ones-password")
        password.value = "wizard-secret-value"
        await pilot.app.screen.action_test_connection()
        await pilot.pause()
        assert len(controller.calls) == 1
        step, probe = controller.calls[0]
        assert step is SetupStep.ONES
        assert isinstance(probe, MappingProxyType)
        assert "ones-password" not in probe
        assert pilot.app.screen.query_one("#next-step").disabled is False
        assert password.value == ""


@pytest.mark.asyncio
async def test_setup_view_and_renderables_never_contain_sensitive_values() -> None:
    from src.developer_workflow.setup_validation import SetupStep, ValidationStatus
    from src.developer_workflow.tui.setup_models import build_setup_step_view

    controller = _WizardSetupController()
    controller.current_step = SetupStep.ONES
    view = build_setup_step_view(controller.state, SetupStep.ONES)
    assert "secret" not in repr(view).casefold()
    assert "password" not in repr(view).casefold()
    assert "token" not in repr(view).casefold()
    async with _wizard_app(controller).run_test() as pilot:
        password = pilot.app.screen.query_one("#ones-password")
        password.value = "SENSITIVE-WIZARD-CANARY"
        assert password.password is True
        rendered = "\n".join(str(widget.render()) for widget in pilot.app.screen.query("Static"))
        assert "SENSITIVE-WIZARD-CANARY" not in rendered


@pytest.mark.asyncio
async def test_setup_cancel_clears_secret_and_controller_exactly_once() -> None:
    from src.developer_workflow.setup_validation import SetupStep

    controller = _WizardSetupController()
    controller.current_step = SetupStep.ONES
    async with _wizard_app(controller).run_test() as pilot:
        password = pilot.app.screen.query_one("#ones-password")
        password.value = "SENSITIVE-CANCEL-CANARY"
        await pilot.click("#cancel-setup")
        await pilot.pause()
        assert password.value == ""
        assert controller.cancel_calls == 1


@pytest.mark.asyncio
async def test_setup_has_seven_independent_steps_and_cannot_skip_controller_gate() -> None:
    from src.developer_workflow.setup_validation import SetupStep, ValidationStatus

    controller = _WizardSetupController()
    async with _wizard_app(controller).run_test() as pilot:
        screen = pilot.app.screen
        assert len(screen.query(".setup-step")) == 7
        await pilot.click("#nav-review")
        await pilot.pause()
        assert screen.current_step is SetupStep.PROFILE
        assert screen.query_one("#profile-step").display
        assert not screen.query_one("#review-step").display
        controller._results[SetupStep.PROFILE] = controller._results[
            SetupStep.PROFILE
        ].model_copy(update={"status": ValidationStatus.PASSED, "category": "ok"})
        await pilot.click("#nav-ones")
        await pilot.pause()
        assert screen.current_step is SetupStep.ONES


@pytest.mark.asyncio
async def test_setup_review_activation_uses_explicit_host_callback() -> None:
    from textual.app import App

    from src.developer_workflow.setup_validation import SetupStep, ValidationStatus
    from src.developer_workflow.tui.setup_screens import SetupWizardScreen

    controller = _WizardSetupController()
    for step in tuple(SetupStep)[:-1]:
        controller._results[step] = controller._results[step].model_copy(
            update={"status": ValidationStatus.PASSED, "category": "ok"}
        )
    controller.current_step = SetupStep.REVIEW
    activated: list[bool] = []

    async def activate() -> None:
        activated.append(True)
        return None

    class WizardApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(
                SetupWizardScreen(controller, activation_callback=activate)
            )

    async with WizardApp().run_test() as pilot:
        await pilot.click("#activate-runtime")
        await pilot.pause()
        assert activated == []
        await pilot.click("#confirm-setup-activation")
        await pilot.pause()
        assert activated == [True]


@pytest.mark.asyncio
async def test_setup_duplicate_test_is_suppressed_and_unmounted_result_is_discarded() -> None:
    import asyncio

    from textual.app import App

    from src.developer_workflow.setup_validation import (
        ConnectionTestResult,
        SetupStep,
        ValidationStatus,
    )
    from src.developer_workflow.tui.setup_screens import SetupWizardScreen

    release = asyncio.Event()

    class BlockingController(_WizardSetupController):
        async def test_step(self, step: object, probe: object = None) -> object:
            self.calls.append((step, probe))
            await release.wait()
            return ConnectionTestResult(
                step=step, status=ValidationStatus.PASSED, category="ok"
            )

    controller = BlockingController()
    controller.current_step = SetupStep.ONES
    controller._results[SetupStep.PROFILE] = controller._results[
        SetupStep.PROFILE
    ].model_copy(update={"status": ValidationStatus.PASSED, "category": "ok"})

    class WizardApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(SetupWizardScreen(controller))

    async with WizardApp().run_test() as pilot:
        await pilot.pause()
        first = asyncio.create_task(pilot.app.screen.action_test_connection())
        while not controller.calls:
            await asyncio.sleep(0)
        await pilot.app.screen.action_test_connection()
        assert len(controller.calls) == 1
        screen = pilot.app.screen
        await screen.remove()
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        assert controller.cancel_calls == 1


@pytest.mark.asyncio
async def test_setup_consumes_secrets_before_probe_without_recording_them() -> None:
    from src.developer_workflow.setup_models import SecretKind
    from src.developer_workflow.setup_validation import SetupStep, ValidationStatus

    controller = _WizardSetupController()
    controller.current_step = SetupStep.ONES
    controller._results[SetupStep.PROFILE] = controller._results[
        SetupStep.PROFILE
    ].model_copy(update={"status": ValidationStatus.PASSED, "category": "ok"})
    async with _wizard_app(controller).run_test() as pilot:
        screen = pilot.app.screen
        screen.query_one("#ones-email").value = "private@example.invalid"
        screen.query_one("#ones-password").value = "FRAME-SECRET-CANARY"
        for widget_id, value in {
            "#ones-team-id": "team-1",
            "#ones-project-id": "project-1",
            "#ones-status-id": "status-1",
            "#ones-item-id": "item-1",
        }.items():
            screen.query_one(widget_id).value = value
        await pilot.click("#test-connection")
        await pilot.pause()
        assert controller.secret_calls == [
            (SecretKind.ONES_EMAIL, "private@example.invalid"),
            (SecretKind.ONES_PASSWORD, "FRAME-SECRET-CANARY"),
        ]
        _, probe = controller.calls[-1]
        assert set(probe) == {
            "ones-team-id",
            "ones-project-id",
            "ones-status-id",
            "ones-item-id",
            "ones-issue-type-id",
        }
        assert "FRAME-SECRET-CANARY" not in repr(probe)
        assert "private@example.invalid" not in repr(probe)
        assert screen.query_one("#ones-email").value == ""
        assert screen.query_one("#ones-password").value == ""


@pytest.mark.asyncio
async def test_setup_codex_applies_complete_public_runtime() -> None:
    from src.developer_workflow.setup_validation import SetupStep, ValidationStatus

    controller = _WizardSetupController()
    controller.runtime_public_fields = MappingProxyType(
        {
            "ones_base_url": "https://ones.example.invalid",
            "ones_team_id": "team-1",
            "ones_issue_type_id": "defect-1",
            "provider_host": "git.example.invalid",
            "provider_api_url": "https://git.example.invalid/api",
            "git_author_name": "ONES Developer",
            "git_author_email": "developer@example.invalid",
            "provider": "github",
        }
    )
    controller.current_step = SetupStep.CODEX
    for step in tuple(SetupStep)[:4]:
        controller._results[step] = controller._results[step].model_copy(
            update={"status": ValidationStatus.PASSED, "category": "ok"}
        )
    async with _wizard_app(controller).run_test() as pilot:
        screen = pilot.app.screen
        values = {
            "#codex-auth-mode": "credential",
            "#codex-profile": "managed-profile",
            "#codex-worktree": "C:/safe/probe",
        }
        for widget_id, value in values.items():
            screen.query_one(widget_id).value = value
        await screen.action_test_connection()
        await pilot.pause()
        assert len(controller.runtime_calls) == 1
        runtime = controller.runtime_calls[0]
        assert runtime.ones_team_id == "team-1"
        assert runtime.provider_host == "git.example.invalid"
        assert runtime.codex_auth_mode == "credential"


@pytest.mark.asyncio
async def test_setup_profile_and_repository_use_controller_builders() -> None:
    from src.developer_workflow.setup_validation import SetupStep, ValidationStatus

    controller = _WizardSetupController()
    async with _wizard_app(controller).run_test() as pilot:
        screen = pilot.app.screen
        screen.query_one("#sandbox-profile").value = "managed-profile"
        await pilot.click("#test-connection")
        await pilot.pause()
        assert len(controller.workflow_calls) == 1
        assert controller.workflow_calls[0].sandbox_permission_profile == "managed-profile"

        controller.current_step = SetupStep.REPOSITORIES
        for step in (SetupStep.PROFILE, SetupStep.ONES):
            controller._results[step] = controller._results[step].model_copy(
                update={"status": ValidationStatus.PASSED, "category": "ok"}
            )
        screen.current_step = SetupStep.REPOSITORIES
        screen._render_state()
        values = {
            "#repository-key": "primary",
            "#repository-project-id": "project-1",
            "#repository-iteration-id": "iteration-1",
            "#repository-name": "primary",
            "#repository-path": "C:/safe/repository",
            "#repository-url": "https://git.example.invalid/team/primary.git",
            "#repository-branch": "main",
        }
        for widget_id, value in values.items():
            screen.query_one(widget_id).value = value
        await screen.action_test_connection()
        await pilot.pause()
        assert controller.repository_calls[0]["key"] == "primary"
        assert controller.repository_calls[0]["project_id"] == "project-1"


@pytest.mark.asyncio
async def test_setup_remote_only_repository_preserves_absent_source_path() -> None:
    from src.developer_workflow.setup_validation import SetupStep, ValidationStatus

    controller = _WizardSetupController()
    controller.current_step = SetupStep.REPOSITORIES
    for step in (SetupStep.PROFILE, SetupStep.ONES):
        controller._results[step] = controller._results[step].model_copy(
            update={"status": ValidationStatus.PASSED, "category": "ok"}
        )
    async with _wizard_app(controller).run_test() as pilot:
        screen = pilot.app.screen
        for widget_id, value in {
            "#repository-key": "remote",
            "#repository-project-id": "project-1",
            "#repository-iteration-id": "iteration-1",
            "#repository-name": "remote",
            "#repository-path": "   ",
            "#repository-url": "https://git.example.invalid/team/remote.git",
            "#repository-branch": "main",
        }.items():
            screen.query_one(widget_id).value = value

        await screen.action_test_connection()
        await pilot.pause()

        assert controller.repository_calls[0]["source_path"] is None


@pytest.mark.asyncio
async def test_setup_profiles_are_catalog_backed_and_shared_between_steps() -> None:
    from textual.widgets import Select

    controller = _WizardSetupController()
    async with _wizard_app(controller).run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        profile = screen.query_one("#sandbox-profile", Select)
        codex = screen.query_one("#codex-profile", Select)

        assert tuple(value for _, value in profile._options if type(value) is str) == (
            "managed-profile",
            "restricted-profile",
        )
        profile.value = "restricted-profile"
        await pilot.pause()
        assert codex.value == "restricted-profile"


@pytest.mark.asyncio
async def test_setup_rejects_stale_draft_profile_and_blocks_empty_catalog() -> None:
    from src.developer_workflow.setup_models import SetupDraft, WorkflowDraft
    from textual.widgets import Button, Select

    class EmptyCatalogController(_WizardSetupController):
        draft = SetupDraft(
            workflow=WorkflowDraft(sandbox_permission_profile="removed-profile")
        )

        async def list_managed_profiles(self) -> tuple[str, ...]:
            return ()

    async with _wizard_app(EmptyCatalogController()).run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert screen.query_one("#sandbox-profile", Select).disabled
        assert screen.query_one("#test-connection", Button).disabled


@pytest.mark.asyncio
async def test_empty_profile_catalog_offers_explicit_runtime_creation() -> None:
    from textual.widgets import Button, Select

    controller = _RuntimeProfileController()
    async with _wizard_app(controller).run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert screen.query_one("#sandbox-profile", Select).disabled
        assert screen.query_one("#codex-profile", Select).disabled
        create = screen.query_one("#create-workspace-profile", Button)
        assert create.disabled is False
        assert create.label.plain == "Create safe workspace profile"

        await _open_workspace_profile_modal(pilot)
        modal = pilot.app.screen
        rendered = "\n".join(str(widget.render()) for widget in modal.query("Static"))
        assert "ones-dev workspace profile" in rendered
        for requirement in (
            "inside-write",
            "outside-deny",
            "network-deny",
            "environment isolation",
            "299 MB",
            "private cache",
        ):
            assert requirement in rendered
        assert modal.query_one("#confirm-workspace-profile", Button).variant == "warning"
        assert modal.query_one("#confirm-workspace-profile", Button).label.plain == "Confirm"
        assert modal.query_one("#cancel-workspace-profile", Button)


@pytest.mark.asyncio
async def test_runtime_profile_modal_requires_explicit_confirmation() -> None:
    controller = _RuntimeProfileController()
    async with _wizard_app(controller).run_test() as pilot:
        await _open_workspace_profile_modal(pilot)
        modal = pilot.app.screen
        await pilot.press("enter")
        await pilot.pause()
        assert pilot.app.screen is modal
        assert controller.profile_confirm_calls == 0

        await pilot.click("#cancel-workspace-profile")
        await pilot.pause()
        assert pilot.app.screen.id == "setup-wizard"
        assert controller.profile_confirm_calls == 0

        await _open_workspace_profile_modal(pilot)
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen.id == "setup-wizard"
        assert controller.profile_confirm_calls == 0


@pytest.mark.asyncio
async def test_runtime_profile_success_reads_authoritative_draft_and_enables_test() -> None:
    from src.developer_workflow.config import BUILTIN_WORKSPACE_PROFILE
    from textual.widgets import Button, Select

    controller = _RuntimeProfileController()
    async with _wizard_app(controller).run_test() as pilot:
        await _open_workspace_profile_modal(pilot)
        await pilot.click("#confirm-workspace-profile")
        await _wait_until(pilot, lambda: controller.profile_confirm_calls == 1)
        await _wait_until(
            pilot,
            lambda: pilot.app.screen.query_one(
                "#sandbox-profile", Select
            ).value == BUILTIN_WORKSPACE_PROFILE,
        )
        screen = pilot.app.screen
        assert screen.query_one("#codex-profile", Select).value == BUILTIN_WORKSPACE_PROFILE
        assert screen.query_one("#sandbox-profile", Select).disabled is False
        assert screen.query_one("#codex-profile", Select).disabled is False
        assert screen.query_one("#test-connection", Button).disabled is False
        assert screen.query_one("#next-step", Button).disabled is True
        assert screen.query_one("#create-workspace-profile", Button).disabled is False


@pytest.mark.asyncio
async def test_runtime_profile_failure_is_redacted_and_retry_can_succeed() -> None:
    from src.developer_workflow.config import BUILTIN_WORKSPACE_PROFILE
    from textual.widgets import Button, Select, Static

    canary = "PROFILE-RAW-FAILURE-CANARY C:/private/hash-secret"
    controller = _RuntimeProfileController([RuntimeError(canary), True])
    async with _wizard_app(controller).run_test() as pilot:
        await _open_workspace_profile_modal(pilot)
        await pilot.click("#confirm-workspace-profile")
        await _wait_until(
            pilot,
            lambda: controller.profile_confirm_calls == 1
            and pilot.app.screen.query_one(
                "#create-workspace-profile", Button
            ).label.plain == "Retry safe workspace profile",
        )
        screen = pilot.app.screen
        assert screen.query_one("#setup-notice", Static).renderable == (
            "Safe workspace profile could not be verified"
        )
        rendered = "\n".join(
            str(widget.render()) for widget in screen.query("Static, Button")
        )
        assert canary not in rendered
        assert "C:/private" not in rendered
        assert canary not in repr(screen)
        assert canary not in repr(screen._state())
        retry = screen.query_one("#create-workspace-profile", Button)
        assert retry.disabled is False

        await _open_workspace_profile_modal(pilot)
        await pilot.click("#confirm-workspace-profile")
        await _wait_until(pilot, lambda: controller.profile_confirm_calls == 2)
        await _wait_until(
            pilot,
            lambda: screen.query_one("#sandbox-profile", Select).value
            == BUILTIN_WORKSPACE_PROFILE,
        )
        assert screen.query_one("#codex-profile", Select).value == BUILTIN_WORKSPACE_PROFILE


@pytest.mark.asyncio
async def test_runtime_profile_does_not_trust_success_return_without_draft_binding() -> None:
    from textual.widgets import Button, Select, Static

    class ForgedSuccessController(_RuntimeProfileController):
        async def confirm_builtin_workspace_profile(self) -> str:
            self.profile_confirm_calls += 1
            return "ones-dev-workspace"

    controller = ForgedSuccessController()
    async with _wizard_app(controller).run_test() as pilot:
        await _open_workspace_profile_modal(pilot)
        await pilot.click("#confirm-workspace-profile")
        await _wait_until(
            pilot,
            lambda: pilot.app.screen.query_one(
                "#create-workspace-profile", Button
            ).label.plain == "Retry safe workspace profile",
        )
        screen = pilot.app.screen
        assert screen.query_one("#sandbox-profile", Select).disabled
        assert screen.query_one("#codex-profile", Select).disabled
        assert screen.query_one("#setup-notice", Static).renderable == (
            "Safe workspace profile could not be verified"
        )


@pytest.mark.asyncio
async def test_runtime_profile_duplicate_confirmation_is_single_flight() -> None:
    import asyncio

    from textual.widgets import Button

    release = asyncio.Event()
    controller = _RuntimeProfileController([release])
    async with _wizard_app(controller).run_test() as pilot:
        screen = pilot.app.screen
        await _open_workspace_profile_modal(pilot)
        token = screen._profile_modal_token
        assert token is not None
        await pilot.click("#confirm-workspace-profile")
        await _wait_until(pilot, lambda: controller.profile_confirm_calls == 1)
        assert screen.query_one("#create-workspace-profile", Button).disabled

        screen._builtin_workspace_profile_finished(True, token)
        screen._builtin_workspace_profile_finished(True, token)
        await pilot.pause()
        assert controller.profile_confirm_calls == 1
        release.set()
        await _wait_until(
            pilot,
            lambda: not screen.query_one(
                "#create-workspace-profile", Button
            ).disabled,
        )
        assert controller.profile_confirm_calls == 1


@pytest.mark.asyncio
async def test_runtime_profile_retry_is_enabled_only_after_owner_is_cleared() -> None:
    from src.developer_workflow.config import BUILTIN_WORKSPACE_PROFILE
    from textual.widgets import Button, Select

    controller = _RuntimeProfileController(
        [RuntimeError("first"), RuntimeError("second"), True]
    )
    async with _wizard_app(controller).run_test() as pilot:
        screen = pilot.app.screen
        for expected_calls in (1, 2):
            assert screen._profile_task is None
            assert not screen._profile_modal_open
            screen._request_builtin_workspace_profile()
            await _wait_until(
                pilot,
                lambda: (pilot.app.screen.id or "").startswith(
                    "setup-workspace-profile-confirmation-"
                )
                and len(pilot.app.screen.query("#confirm-workspace-profile")) == 1,
            )
            await pilot.click("#confirm-workspace-profile")
            await _wait_until(
                pilot,
                lambda: controller.profile_confirm_calls == expected_calls
                and not screen.query_one(
                    "#create-workspace-profile", Button
                ).disabled,
            )
            assert screen._profile_task is None
            assert screen.query_one(
                "#create-workspace-profile", Button
            ).label.plain == "Retry safe workspace profile"

        screen._request_builtin_workspace_profile()
        await _wait_until(
            pilot,
            lambda: (pilot.app.screen.id or "").startswith(
                "setup-workspace-profile-confirmation-"
            )
            and len(pilot.app.screen.query("#confirm-workspace-profile")) == 1,
        )
        await pilot.click("#confirm-workspace-profile")
        await _wait_until(
            pilot,
            lambda: screen.query_one("#sandbox-profile", Select).value
            == BUILTIN_WORKSPACE_PROFILE,
        )
        assert controller.profile_confirm_calls == 3
        assert screen._profile_task is None
        assert not screen.query_one("#create-workspace-profile", Button).disabled


@pytest.mark.asyncio
async def test_escape_during_profile_probe_cancels_and_leaves_retry_available() -> None:
    import asyncio

    from textual.widgets import Button

    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingController(_RuntimeProfileController):
        accepted_secret_count = 3
        accepted_secret_presence = {
            "ones": True,
            "provider": True,
            "codex": True,
        }

        @property
        def state(self) -> object:
            state = super().state
            return SimpleNamespace(
                **{
                    **vars(state),
                    "secret_count": self.accepted_secret_count,
                }
            )

        async def confirm_builtin_workspace_profile(self) -> str:
            self.profile_confirm_calls += 1
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    controller = BlockingController()
    async with _wizard_app(controller).run_test() as pilot:
        screen = pilot.app.screen
        supervisor_closes: list[bool] = []
        original_close = screen._supervisor.close

        def record_close() -> None:
            supervisor_closes.append(True)
            original_close()

        screen._supervisor.close = record_close
        assert controller.state.secret_count == 3
        secret_presence = dict(controller.accepted_secret_presence)
        await _open_workspace_profile_modal(pilot)
        await pilot.click("#confirm-workspace-profile")
        await entered.wait()
        await pilot.press("escape")
        await cancelled.wait()
        await pilot.pause()
        create = screen.query_one("#create-workspace-profile", Button)
        assert create.disabled is False
        assert create.label.plain == "Retry safe workspace profile"
        assert controller.profile_confirm_calls == 1
        assert controller.cancel_calls == 0
        assert controller.state.secret_count == 3
        assert controller.accepted_secret_presence == secret_presence
        assert screen._profile_task is None
        assert supervisor_closes == []


@pytest.mark.asyncio
async def test_runtime_profile_modal_is_single_flight_and_clears_every_exit() -> None:
    controller = _RuntimeProfileController()
    async with _wizard_app(controller).run_test() as pilot:
        wizard = pilot.app.screen
        wizard._request_builtin_workspace_profile()
        wizard._request_builtin_workspace_profile()
        await _wait_until(
            pilot,
            lambda: (pilot.app.screen.id or "").startswith(
                "setup-workspace-profile-confirmation-"
            )
            and len(pilot.app.screen.query("#cancel-workspace-profile")) == 1,
        )
        assert wizard._profile_modal_open
        assert wizard.query_one("#create-workspace-profile").disabled
        assert wizard.query_one("#test-connection").disabled
        assert wizard.query_one("#next-step").disabled
        assert sum(
            (item.id or "").startswith("setup-workspace-profile-confirmation-")
            for item in pilot.app.screen_stack
        ) == 1

        await pilot.click("#cancel-workspace-profile")
        await _wait_until(pilot, lambda: pilot.app.screen is wizard)
        assert not wizard._profile_modal_open
        assert controller.profile_confirm_calls == 0

        await _open_workspace_profile_modal(pilot)
        await pilot.press("escape")
        await _wait_until(pilot, lambda: pilot.app.screen is wizard)
        assert not wizard._profile_modal_open
        assert controller.profile_confirm_calls == 0


@pytest.mark.asyncio
async def test_runtime_profile_modal_push_failure_and_late_callback_are_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _RuntimeProfileController()
    async with _wizard_app(controller).run_test() as pilot:
        wizard = pilot.app.screen

        def fail_push(*args: object, **kwargs: object) -> None:
            raise RuntimeError("PRIVATE-PUSH-CANARY")

        monkeypatch.setattr(pilot.app, "push_screen", fail_push)
        epoch = wizard._mount_epoch
        wizard._request_builtin_workspace_profile()
        assert not wizard._profile_modal_open
        assert wizard._mount_epoch == epoch + 1
        assert controller.profile_confirm_calls == 0
        notice = str(wizard.query_one("#setup-notice").render())
        assert "Safe workspace profile could not be verified" in notice
        assert "PRIVATE-PUSH-CANARY" not in notice


@pytest.mark.asyncio
async def test_runtime_profile_modal_callback_token_is_one_shot() -> None:
    callbacks: list[object] = []
    controller = _RuntimeProfileController([True])
    async with _wizard_app(controller).run_test() as pilot:
        wizard = pilot.app.screen
        pilot.app.push_screen = (  # type: ignore[method-assign]
            lambda modal, callback: callbacks.append(callback)
        )

        wizard._request_builtin_workspace_profile()
        first = callbacks[-1]
        first(False)
        first(True)
        await pilot.pause()
        assert controller.profile_confirm_calls == 0

        wizard._request_builtin_workspace_profile()
        second = callbacks[-1]
        second(True)
        await _wait_until(pilot, lambda: wizard._profile_task is None)
        assert controller.profile_confirm_calls == 1
        second(True)
        await pilot.pause()
        assert controller.profile_confirm_calls == 1


@pytest.mark.asyncio
async def test_profile_probe_busy_blocks_all_underlying_mutations() -> None:
    import asyncio

    from src.developer_workflow.setup_validation import SetupStep, ValidationStatus
    from textual.widgets import Button, Input, Select

    release = asyncio.Event()

    class BlockingFailureController(_RuntimeProfileController):
        async def list_managed_profiles(self) -> tuple[str, ...]:
            return ("managed-profile",)

        async def confirm_builtin_workspace_profile(self) -> str:
            self.profile_confirm_calls += 1
            await release.wait()
            raise RuntimeError("fixed failure")

    controller = BlockingFailureController()
    for step in SetupStep:
        controller._results[step] = controller._results[step].model_copy(
            update={"status": ValidationStatus.PASSED, "category": "ok"}
        )
    async with _wizard_app(controller).run_test() as pilot:
        screen = pilot.app.screen
        profile = screen.query_one("#sandbox-profile", Select)
        profile.value = "managed-profile"
        await pilot.pause()
        assert not screen.query_one("#next-step", Button).disabled

        screen._start_builtin_workspace_profile()
        await _wait_until(pilot, lambda: controller.profile_confirm_calls == 1)
        assert all(widget.disabled for widget in screen.query(Input))
        assert all(widget.disabled for widget in screen.query(Select))
        for widget_id in (
            "create-workspace-profile",
            "test-connection",
            "next-step",
            "back-step",
            "review-setup",
            "nav-ones",
        ):
            assert screen.query_one(f"#{widget_id}", Button).disabled

        original_step = screen.current_step
        original_calls = list(controller.calls)
        original_workflows = list(controller.workflow_calls)
        screen._pressed_next()
        screen._pressed_back()
        screen._pressed_review()
        screen._pressed_test()
        screen._pressed_navigation(
            SimpleNamespace(button=screen.query_one("#nav-ones", Button))
        )
        await screen.action_test_connection()
        screen._profile_changed(SimpleNamespace(value="managed-profile"))
        assert screen.current_step is original_step
        assert controller.calls == original_calls
        assert controller.workflow_calls == original_workflows

        release.set()
        await _wait_until(pilot, lambda: screen._profile_task is None)
        assert not screen.query_one("#next-step", Button).disabled
        assert not screen.query_one("#test-connection", Button).disabled


@pytest.mark.asyncio
async def test_remount_uses_fresh_supervisor_and_rejects_old_modal_callback() -> None:
    from textual.app import App

    from src.developer_workflow.tui.setup_screens import SetupWizardScreen

    class ObservableSupervisor:
        def __init__(self) -> None:
            self.closed = False
            self.calls = 0

        @property
        def busy(self) -> bool:
            return False

        async def run_readonly(self, call):
            assert not self.closed
            self.calls += 1
            return await call()

        def close(self) -> None:
            self.closed = True

    supervisors: list[ObservableSupervisor] = []

    def supervisor_factory() -> ObservableSupervisor:
        supervisor = ObservableSupervisor()
        supervisors.append(supervisor)
        return supervisor

    controller = _RuntimeProfileController([True])
    screen = SetupWizardScreen(
        controller,
        supervisor_factory=supervisor_factory,
    )

    class RemountApp(App[None]):
        def on_mount(self) -> None:
            self.push_screen(screen)

    async with RemountApp().run_test() as pilot:
        callbacks: list[object] = []
        real_push = pilot.app.push_screen
        pilot.app.push_screen = (  # type: ignore[method-assign]
            lambda modal, callback=None: callbacks.append(callback)
        )
        screen._request_builtin_workspace_profile()
        old_callback = callbacks[-1]
        pilot.app.push_screen = real_push  # type: ignore[method-assign]

        await screen.remove()
        assert supervisors[0].closed
        await pilot.app.push_screen(screen)
        await pilot.pause()
        assert len(supervisors) == 2
        assert not supervisors[1].closed

        old_callback(True)
        await pilot.pause()
        assert controller.profile_confirm_calls == 0

        callbacks.clear()
        pilot.app.push_screen = (  # type: ignore[method-assign]
            lambda modal, callback=None: callbacks.append(callback)
        )
        screen._request_builtin_workspace_profile()
        callbacks[-1](True)
        await _wait_until(pilot, lambda: screen._profile_task is None)
        assert controller.profile_confirm_calls == 1
        assert supervisors[1].calls == 1


@pytest.mark.asyncio
async def test_unmount_discards_late_profile_probe_without_render_mutation() -> None:
    import asyncio

    entered = asyncio.Event()
    release = asyncio.Event()

    class LateController(_RuntimeProfileController):
        async def confirm_builtin_workspace_profile(self) -> str:
            self.profile_confirm_calls += 1
            entered.set()
            try:
                await asyncio.shield(release.wait())
            except asyncio.CancelledError:
                await release.wait()
            return "ones-dev-workspace"

    controller = LateController()
    async with _wizard_app(controller).run_test() as pilot:
        await _open_workspace_profile_modal(pilot)
        await pilot.click("#confirm-workspace-profile")
        await entered.wait()
        screen = pilot.app.screen
        await screen.remove()
        release.set()
        await pilot.pause()
        assert controller.profile_confirm_calls == 1
        assert controller.cancel_calls == 1
        assert screen._managed_profiles == ()


@pytest.mark.asyncio
async def test_failed_runtime_profile_keeps_existing_managed_selection() -> None:
    from textual.widgets import Select

    class ManagedController(_RuntimeProfileController):
        async def list_managed_profiles(self) -> tuple[str, ...]:
            return ("managed-profile", "restricted-profile")

    controller = ManagedController([RuntimeError("hidden")])
    async with _wizard_app(controller).run_test() as pilot:
        screen = pilot.app.screen
        screen.query_one("#sandbox-profile", Select).value = "restricted-profile"
        await pilot.pause()
        await _open_workspace_profile_modal(pilot)
        await pilot.click("#confirm-workspace-profile")
        await _wait_until(pilot, lambda: controller.profile_confirm_calls == 1)
        assert screen.query_one("#sandbox-profile", Select).value == "restricted-profile"
        assert screen.query_one("#codex-profile", Select).value == "restricted-profile"


@pytest.mark.asyncio
async def test_detached_profile_loader_cancellation_propagates_without_render() -> None:
    import asyncio

    from src.developer_workflow.tui.setup_screens import SetupWizardScreen

    class CancelledController(_WizardSetupController):
        async def list_managed_profiles(self) -> tuple[str, ...]:
            raise asyncio.CancelledError

    screen = SetupWizardScreen(CancelledController())
    populated: list[bool] = []
    rendered: list[bool] = []
    screen._populate_public_draft = lambda: populated.append(True)  # type: ignore[method-assign]
    screen._render_state = lambda: rendered.append(True)  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await screen.on_mount()

    assert populated == []
    assert rendered == []


@pytest.mark.asyncio
async def test_setup_cancel_keeps_attached_wizard_reusable() -> None:
    from src.developer_workflow.setup_validation import SetupStep, ValidationStatus

    controller = _WizardSetupController()
    controller.current_step = SetupStep.ONES
    controller._results[SetupStep.PROFILE] = controller._results[
        SetupStep.PROFILE
    ].model_copy(update={"status": ValidationStatus.PASSED, "category": "ok"})
    async with _wizard_app(controller).run_test() as pilot:
        screen = pilot.app.screen
        await pilot.press("escape")
        for widget_id, value in {
            "#ones-team-id": "team-1",
            "#ones-project-id": "project-1",
            "#ones-status-id": "status-1",
            "#ones-item-id": "item-1",
        }.items():
            screen.query_one(widget_id).value = value
        await screen.action_test_connection()
        assert len(controller.calls) == 1
        assert controller.cancel_calls == 1


@pytest.mark.asyncio
async def test_normal_navigation_preserves_controller_accepted_credentials() -> None:
    from src.developer_workflow.setup_validation import SetupStep, ValidationStatus

    controller = _WizardSetupController()
    controller.current_step = SetupStep.ONES
    for step in (SetupStep.PROFILE, SetupStep.ONES):
        controller._results[step] = controller._results[step].model_copy(
            update={"status": ValidationStatus.PASSED, "category": "ok"}
        )
    async with _wizard_app(controller).run_test() as pilot:
        screen = pilot.app.screen
        screen.query_one("#ones-password").value = "UNCOMMITTED-LOCAL"
        await pilot.click("#next-step")
        assert screen.current_step is SetupStep.REPOSITORIES
        assert screen.query_one("#ones-password").value == ""
        assert controller.cancel_calls == 0


@pytest.mark.asyncio
async def test_test_button_handler_returns_before_blocking_builder_and_escape_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from threading import Event

    from src.developer_workflow.setup_validation import SetupStep, ValidationStatus

    started = Event()
    release = Event()
    commits: list[object] = []

    class BlockingController(_WizardSetupController):
        revision = 0

        @property
        def draft(self) -> object:
            from src.developer_workflow.setup_models import SetupDraft

            return SetupDraft()

        def apply_step_transaction(self, *args: object, **kwargs: object) -> None:
            commits.append((args, kwargs))

    def blocking_build(**fields: object) -> object:
        from src.developer_workflow.contracts import RepositoryMapping

        started.set()
        release.wait(2)
        return RepositoryMapping(
            key=fields["key"], project_id=fields["project_id"],
            iteration_id=fields["iteration_id"], repo_url=fields["repo_url"],
            repo_name=fields["repo_name"], base_branch=fields["base_branch"],
        )

    monkeypatch.setattr(
        "src.developer_workflow.tui.setup_screens.build_repository", blocking_build
    )
    controller = BlockingController()
    controller.current_step = SetupStep.REPOSITORIES
    for step in (SetupStep.PROFILE, SetupStep.ONES):
        controller._results[step] = controller._results[step].model_copy(
            update={"status": ValidationStatus.PASSED, "category": "ok"}
        )
    try:
        async with _wizard_app(controller).run_test() as pilot:
            screen = pilot.app.screen
            for widget_id, value in {
                "#repository-key": "primary",
                "#repository-project-id": "project-1",
                "#repository-iteration-id": "iteration-1",
                "#repository-name": "primary",
                "#repository-path": "C:/safe/repository",
                "#repository-url": "https://git.example.invalid/team/primary.git",
                "#repository-branch": "main",
                "#repository-role": "primary",
            }.items():
                screen.query_one(widget_id).value = value
            click = asyncio.create_task(pilot.click("#test-connection"))
            while not started.is_set():
                await asyncio.sleep(0)
            await asyncio.wait_for(asyncio.shield(click), timeout=0.1)
            await pilot.press("escape")
            release.set()
            await pilot.pause()
            assert controller.cancel_calls == 1
            assert commits == []
    finally:
        release.set()


@pytest.mark.asyncio
async def test_blocking_repository_builder_runs_off_ui_loop_and_cancel_discards_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from threading import Event

    from src.developer_workflow.setup_validation import SetupStep, ValidationStatus

    started = Event()
    release = Event()

    commits: list[object] = []

    class BlockingController(_WizardSetupController):
        revision = 0

        @property
        def draft(self) -> object:
            from src.developer_workflow.setup_models import SetupDraft

            return SetupDraft()

        def apply_step_transaction(self, *args: object, **kwargs: object) -> None:
            commits.append((args, kwargs))

    def blocking_build(**fields: object) -> object:
        from src.developer_workflow.contracts import RepositoryMapping

        started.set()
        release.wait(2)
        return RepositoryMapping(
            key=fields["key"], project_id=fields["project_id"],
            iteration_id=fields["iteration_id"], repo_url=fields["repo_url"],
            repo_name=fields["repo_name"], base_branch=fields["base_branch"],
        )

    monkeypatch.setattr(
        "src.developer_workflow.tui.setup_screens.build_repository",
        blocking_build,
    )

    controller = BlockingController()
    controller.current_step = SetupStep.REPOSITORIES
    for step in (SetupStep.PROFILE, SetupStep.ONES):
        controller._results[step] = controller._results[step].model_copy(
            update={"status": ValidationStatus.PASSED, "category": "ok"}
        )
    async with _wizard_app(controller).run_test() as pilot:
        screen = pilot.app.screen
        values = {
            "#repository-key": "primary",
            "#repository-project-id": "project-1",
            "#repository-iteration-id": "iteration-1",
            "#repository-name": "primary",
            "#repository-path": "C:/safe/repository",
            "#repository-url": "https://git.example.invalid/team/primary.git",
            "#repository-branch": "main",
            "#repository-role": "primary",
        }
        for widget_id, value in values.items():
            screen.query_one(widget_id).value = value
        task = asyncio.create_task(screen.action_test_connection())
        while not started.is_set():
            await asyncio.sleep(0)
        ticked = False
        await asyncio.sleep(0.01)
        ticked = True
        screen.action_cancel_edit()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert ticked
        assert controller.calls == []
        assert commits == []


@pytest.mark.asyncio
async def test_each_step_candidate_receives_only_its_exact_config_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow.setup_validation import SetupStep, ValidationStatus
    from src.developer_workflow.tui.setup_screens import SetupWizardScreen

    expected = {
        SetupStep.PROFILE: {"sandbox-profile"},
        SetupStep.ONES: {
            "ones-base-url", "ones-team-id", "ones-issue-type-id",
            "ones-project-id", "ones-status-id", "ones-item-id",
        },
        SetupStep.REPOSITORIES: {
            "repository-key", "repository-project-id", "repository-iteration-id",
            "repository-name", "repository-path", "repository-url",
            "repository-branch", "repository-role", "repository-depends-on",
            "repository-allowed-paths", "repository-lint-commands",
            "repository-build-commands", "repository-test-commands",
            "repository-group-key", "repository-primary",
            "repository-integration-commands",
        },
        SetupStep.PROVIDER: {
            "provider-host", "provider-api-url", "git-author-name",
            "git-author-email", "provider-type", "repository-branch",
        },
        SetupStep.CODEX: {
            "codex-auth-mode", "codex-profile", "codex-worktree", "codex-home",
        },
        SetupStep.PRIVATE_PATHS: {"run-root", "mirror-root", "worktree-root"},
    }
    captured: list[tuple[SetupStep, set[str]]] = []
    original = SetupWizardScreen._build_step_transaction

    def capture(
        self: SetupWizardScreen,
        fields: object,
        draft: object,
        saved_runtime_fields: object,
    ) -> object:
        captured.append((self.current_step, set(fields)))
        return original(self, fields, draft, saved_runtime_fields)

    monkeypatch.setattr(SetupWizardScreen, "_build_step_transaction", capture)
    controller = _WizardSetupController()
    controller.revision = 0
    controller.apply_step_transaction = lambda *args, **kwargs: None
    controller.draft = SimpleNamespace(workflow=__import__(
        "src.developer_workflow.setup_models", fromlist=["WorkflowDraft"]
    ).WorkflowDraft())
    for step in tuple(SetupStep)[:-1]:
        controller._results[step] = controller._results[step].model_copy(
            update={"status": ValidationStatus.PASSED, "category": "ok"}
        )
    async with _wizard_app(controller).run_test() as pilot:
        screen = pilot.app.screen
        screen.query_one("#sandbox-profile").value = "managed-profile"
        for step in tuple(SetupStep)[:-1]:
            screen.current_step = step
            screen._render_state()
            try:
                await screen.action_test_connection()
            except BaseException:
                pass
    assert dict(captured) == expected


@pytest.mark.asyncio
async def test_setup_prepare_failure_restores_button_and_retry_succeeds() -> None:
    from src.developer_workflow.setup_validation import SetupStep

    class FailsOnce(_WizardSetupController):
        failures = 1

        async def test_step(self, step: object, probe: object = None) -> object:
            if self.failures:
                self.failures -= 1
                raise RuntimeError("SENSITIVE FAILURE")
            return await super().test_step(step, probe)

    controller = FailsOnce()
    async with _wizard_app(controller).run_test() as pilot:
        screen = pilot.app.screen
        screen.query_one("#sandbox-profile").value = "managed-profile"
        await screen.action_test_connection()
        assert screen.query_one("#test-connection").disabled is False
        await screen.action_test_connection()
        assert len(controller.calls) == 1
        assert screen.query_one("#test-connection").disabled is False


@pytest.mark.asyncio
async def test_missing_git_result_is_fixed_and_repository_probe_can_retry() -> None:
    from textual.widgets import Button, Static

    from src.developer_workflow.setup_validation import (
        ConnectionTestResult,
        SetupStep,
        ValidationStatus,
    )

    class MissingGitOnce(_WizardSetupController):
        attempts = 0

        async def test_step(self, step: object, probe: object = None) -> object:
            self.attempts += 1
            self.calls.append((step, probe))
            result = ConnectionTestResult(
                step=step,
                status=(
                    ValidationStatus.FAILED
                    if self.attempts == 1
                    else ValidationStatus.PASSED
                ),
                category="git_unavailable" if self.attempts == 1 else "ok",
            )
            self._results[step] = result
            return result

    controller = MissingGitOnce()
    controller.current_step = SetupStep.REPOSITORIES
    for step in (SetupStep.PROFILE, SetupStep.ONES):
        controller._results[step] = controller._results[step].model_copy(
            update={"status": ValidationStatus.PASSED, "category": "ok"}
        )

    async with _wizard_app(controller).run_test() as pilot:
        screen = pilot.app.screen
        values = {
            "#repository-key": "primary",
            "#repository-project-id": "project-1",
            "#repository-iteration-id": "iteration-1",
            "#repository-name": "primary",
            "#repository-path": "C:/safe/repository",
            "#repository-url": "https://git.example.invalid/team/primary.git",
            "#repository-branch": "main",
        }
        for widget_id, value in values.items():
            screen.query_one(widget_id).value = value

        await screen.action_test_connection()

        assert screen.is_attached
        assert str(screen.query_one("#setup-notice", Static).renderable) == (
            "Git executable is unavailable"
        )
        assert screen.query_one("#test-connection", Button).disabled is False

        await screen.action_test_connection()

        assert screen.is_attached
        assert controller.attempts == 2
        assert str(screen.query_one("#setup-notice", Static).renderable) == (
            "Connection test passed"
        )
        assert screen.query_one("#test-connection", Button).disabled is False


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
    from src.developer_workflow.tui.setup_screens import SetupWizardScreen

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
        assert type(app.screen) is SetupWizardScreen
        assert built == []
        assert not app.query("#run-list")


@pytest.mark.asyncio
async def test_existing_configuration_opens_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
    from pathlib import Path
    from src.developer_workflow.setup_import import ImportDetection
    from src.developer_workflow.tui.setup_screens import SetupImportContext

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

    context = SetupImportContext(
        detection=ImportDetection((), (), False),
        dotenv_path=Path("must-not-open.env"),
    )
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup,
        runtime_bootstrapper=object(),
        setup_import=context,
    )
    monkeypatch.setattr(app, "push_screen", push)
    monkeypatch.setattr(app, "set_interval", lambda *args: None)
    await app.on_mount()

    assert setup.activate_calls == 1
    assert app.runtime_session is session
    assert len(pushed) == 1
    assert context.consumed is True
    assert app._setup_import is None


@pytest.mark.asyncio
async def test_close_ui_discards_import_context_without_mounting_screen() -> None:
    from pathlib import Path
    from src.developer_workflow.setup_import import ImportDetection
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
    from src.developer_workflow.tui.setup_screens import SetupImportContext

    setup = _SetupController(None)
    context = SetupImportContext(
        detection=ImportDetection((), (), False),
        dotenv_path=Path("must-not-open.env"),
    )
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup,
        runtime_bootstrapper=object(),
        setup_import=context,
    )

    await app._close_ui()

    assert context.consumed is True
    assert app._setup_import is None
    with pytest.raises(RuntimeError, match="import source is unavailable"):
        context.import_into(setup, "environment")


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
async def test_duplicate_setup_completion_same_handle_is_not_closed_by_loser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp

    closes: list[str] = []
    handle = SimpleNamespace(close=lambda: closes.append("handle"))
    session = _RuntimeSession()
    app = DeveloperWorkflowTuiApp(
        setup_controller=_SetupController(None),
        setup_controller_factory=lambda: _SetupController(None),
        runtime_bootstrapper=object(),
    )
    monkeypatch.setattr(app, "_build_runtime_session", lambda value: session)
    monkeypatch.setattr(
        app,
        "_build_dashboard",
        lambda value: SimpleNamespace(refresh_runs=_async_noop),
    )
    monkeypatch.setattr(app, "push_screen", _async_noop)
    monkeypatch.setattr(app, "set_interval", lambda *args: None)

    await asyncio.gather(app._setup_done(handle), app._setup_done(handle))

    assert app.runtime_session is session
    assert closes == []


@pytest.mark.asyncio
async def test_app_close_is_single_flight_when_first_waiter_is_cancelled() -> None:
    import asyncio

    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp

    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingSetup(_SetupController):
        close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1
            entered.set()
            await release.wait()
            self.closed = True

    setup = BlockingSetup(None)
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup,
        runtime_bootstrapper=object(),
    )

    first = asyncio.create_task(app._close_ui())
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not app._close_complete.is_set()
    release.set()
    await app._close_ui()

    assert setup.close_calls == 1
    assert setup.closed
    assert app._close_complete.is_set()


@pytest.mark.asyncio
async def test_app_internal_close_task_cancel_is_taken_over_without_double_close() -> None:
    import asyncio

    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp

    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingSetup(_SetupController):
        close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1
            entered.set()
            await release.wait()
            self.closed = True

    setup = BlockingSetup(None)
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup,
        runtime_bootstrapper=object(),
    )
    waiter = asyncio.create_task(app._close_ui())
    await entered.wait()
    assert app._close_task is not None
    app._close_task.cancel()
    await asyncio.sleep(0)
    release.set()
    await app._close_ui()
    await waiter

    assert setup.close_calls == 1
    assert setup.closed
    assert app._close_complete.is_set()


@pytest.mark.asyncio
async def test_app_close_deadline_preserves_owned_setup_until_real_drain() -> None:
    import asyncio

    from src.developer_workflow.tui.app import (
        DeveloperWorkflowTuiApp,
        TuiAppCloseError,
    )

    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingSetup(_SetupController):
        close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1
            entered.set()
            await release.wait()
            self.closed = True

    setup = BlockingSetup(None)
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup,
        runtime_bootstrapper=object(),
        close_timeout=0.05,
    )

    with pytest.raises(TuiAppCloseError) as raised:
        await app._close_ui()
    assert str(raised.value) == "TUI close timed out"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert entered.is_set()
    assert not app._close_complete.is_set()
    assert app._closing_setup_controller is setup
    assert setup.close_calls == 1

    release.set()
    await app._close_ui()

    assert setup.closed
    assert app._closing_setup_controller is None
    assert app._close_complete.is_set()


@pytest.mark.asyncio
async def test_app_close_child_cancellation_never_reports_orphans_as_closed() -> None:
    import asyncio
    from contextlib import suppress

    from src.developer_workflow.tui.app import (
        DeveloperWorkflowTuiApp,
        TuiAppCloseError,
    )

    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingSetup(_SetupController):
        async def aclose(self) -> None:
            entered.set()
            await release.wait()
            self.closed = True

    setup = BlockingSetup(None)
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup,
        runtime_bootstrapper=object(),
        close_timeout=0.05,
    )
    outer = asyncio.create_task(app._close_ui())
    await entered.wait()
    owned = next(
        task
        for task in asyncio.all_tasks()
        if task not in {asyncio.current_task(), outer, app._close_task}
        and "capture" in repr(task.get_coro())
    )

    owned.cancel()
    assert app._close_task is not None
    app._close_task.cancel()
    with suppress(asyncio.CancelledError, TuiAppCloseError):
        await outer
    await asyncio.sleep(0)

    assert not app._close_complete.is_set()
    assert app._closing_setup_controller is setup

    release.set()
    await app._close_ui()
    assert setup.closed
    assert app._close_complete.is_set()


@pytest.mark.asyncio
async def test_app_close_single_retry_replaces_stale_incomplete_task_once() -> None:
    import asyncio

    from src.developer_workflow.tui.app import (
        DeveloperWorkflowTuiApp,
        TuiAppCloseError,
    )

    class CountingSetup(_SetupController):
        close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1
            self.closed = True

    setup = CountingSetup(None)
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup,
        runtime_bootstrapper=object(),
        close_timeout=0.05,
    )
    await app._transition_lock.acquire()

    with pytest.raises(TuiAppCloseError, match="TUI close timed out"):
        await app._close_ui()
    old_task = app._close_task
    assert old_task is not None
    await asyncio.wait_for(asyncio.shield(old_task), timeout=0.5)
    assert not old_task.result().complete
    app._transition_lock.release()

    await asyncio.gather(app._close_ui(), app._close_ui())

    assert setup.close_calls == 1
    assert setup.closed
    assert app._close_complete.is_set()
    assert app._close_task is not old_task


@pytest.mark.asyncio
@pytest.mark.parametrize("fatal_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("fatal_stage", ["setup", "session"])
async def test_app_close_drains_then_propagates_same_fatal_to_all_waiters(
    fatal_type: type[BaseException],
    fatal_stage: str,
) -> None:
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp

    calls: list[str] = []
    setup = _SetupController(None)

    async def fatal_setup_close() -> None:
        calls.append("setup")
        raise fatal_type("original fatal")

    if fatal_stage == "setup":
        setup.aclose = fatal_setup_close  # type: ignore[method-assign]
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup,
        runtime_bootstrapper=object(),
    )
    if fatal_stage == "session":
        class FatalSession:
            supervisor = object()

            async def close(self) -> None:
                calls.append("session")
                raise fatal_type("original fatal")

        session = FatalSession()
        app.runtime_session = session  # type: ignore[assignment]
        app.supervisor = session.supervisor

    for _ in range(2):
        with pytest.raises(fatal_type, match="original fatal"):
            await app._close_ui()

    assert calls == [fatal_stage]
    assert app._close_complete.is_set()


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
    assert app._activation_handles == set()


@pytest.mark.asyncio
async def test_duplicate_same_handle_never_closes_active_runtime_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp

    class Handle:
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    winner = Handle()
    app = DeveloperWorkflowTuiApp(
        setup_controller=_SetupController(None), runtime_bootstrapper=object()
    )
    monkeypatch.setattr(app, "_build_runtime_session", lambda handle: _RuntimeSession())
    monkeypatch.setattr(
        app, "_build_dashboard", lambda session: SimpleNamespace(refresh_runs=_async_noop)
    )
    monkeypatch.setattr(app, "push_screen", _async_noop)
    monkeypatch.setattr(app, "set_interval", lambda *args: None)

    await asyncio.gather(app._setup_done(winner), app._setup_done(winner))

    assert winner.closed == 0
    assert app._active_handle_identity == id(winner)


@pytest.mark.asyncio
async def test_app_close_timeout_retains_runtime_owner_until_retry_completes() -> None:
    import asyncio
    from threading import Event

    from src.developer_workflow.tui.app import (
        DeveloperWorkflowTuiApp,
        TuiAppCloseError,
    )
    from src.developer_workflow.tui.runtime_session import TuiRuntimeSession

    entered, release = Event(), Event()

    def close_controller() -> None:
        entered.set()
        release.wait(2)

    session = TuiRuntimeSession(
        handle=SimpleNamespace(close=lambda: None),
        controller=SimpleNamespace(close=close_controller),
        run_index=object(),
        supervisor=SimpleNamespace(close=_async_noop),
        event_sink=lambda event: None,
        close_timeout=0.03,
    )
    app = DeveloperWorkflowTuiApp(
        setup_controller=_SetupController(None),
        runtime_bootstrapper=object(),
        close_timeout=0.08,
    )
    app.runtime_session = session
    app.supervisor = session.supervisor

    with pytest.raises(TuiAppCloseError, match="TUI close timed out"):
        await app._close_ui()
    assert entered.is_set()
    assert app.runtime_session is session
    assert not app._close_complete.is_set()

    release.set()
    await asyncio.sleep(0.05)
    await app._close_ui()
    assert app.runtime_session is None
    assert app._close_complete.is_set()
    assert session.handle is None
    assert session.controller is None


@pytest.mark.asyncio
async def test_reconfigure_pending_close_keeps_owner_and_does_not_open_setup() -> None:
    from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
    from src.developer_workflow.tui.runtime_session import TuiRuntimeCloseError

    class PendingSession:
        close_complete = False

        async def close(self) -> None:
            raise TuiRuntimeCloseError("TUI runtime close failed")

    created: list[object] = []
    session = PendingSession()
    app = DeveloperWorkflowTuiApp(
        setup_controller=_SetupController(None),
        setup_controller_factory=lambda: created.append(object()) or object(),
        runtime_bootstrapper=object(),
        close_timeout=0.03,
    )
    app.runtime_session = session  # type: ignore[assignment]
    app.controller = object()  # type: ignore[assignment]
    app.supervisor = object()

    await app._begin_reconfigure()

    assert app.runtime_session is session
    assert app.controller is not None
    assert created == []


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


@pytest.mark.asyncio
@pytest.mark.parametrize("fatal_type", [KeyboardInterrupt, SystemExit])
async def test_runtime_session_fatal_drains_all_resources_and_rethrows_consistently(
    fatal_type: type[BaseException],
) -> None:
    from src.developer_workflow.tui.runtime_session import TuiRuntimeSession

    order: list[str] = []

    class Supervisor:
        async def close(self) -> None:
            order.append("supervisor")

    def close_controller() -> None:
        order.append("controller")
        raise fatal_type("original fatal")

    session = TuiRuntimeSession(
        handle=SimpleNamespace(close=lambda: order.append("handle")),
        controller=SimpleNamespace(close=close_controller),
        run_index=object(),
        supervisor=Supervisor(),
        event_sink=lambda event: None,
    )

    for _ in range(2):
        with pytest.raises(fatal_type, match="original fatal"):
            await session.close()

    assert order == ["supervisor", "controller", "handle"]


@pytest.mark.asyncio
async def test_runtime_session_controller_and_handle_share_owned_serial_worker() -> None:
    import asyncio
    from threading import Event

    from src.developer_workflow.tui.runtime_session import (
        TuiRuntimeCloseError,
        TuiRuntimeSession,
    )

    entered, release, finished = Event(), Event(), Event()
    order: list[str] = []

    def close_controller() -> None:
        order.append("controller-start")
        entered.set()
        release.wait(2)
        order.append("controller-end")

    def close_handle() -> None:
        order.append("handle")
        finished.set()

    session = TuiRuntimeSession(
        handle=SimpleNamespace(close=close_handle),
        controller=SimpleNamespace(close=close_controller),
        run_index=object(),
        supervisor=SimpleNamespace(close=_async_noop),
        event_sink=lambda event: None,
        close_timeout=0.05,
    )

    with pytest.raises(TuiRuntimeCloseError):
        await session.close()
    assert entered.is_set()
    assert order == ["controller-start"]
    release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    assert order == ["controller-start", "controller-end", "handle"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fatal_type", [KeyboardInterrupt, SystemExit])
async def test_supervisor_fatal_is_captured_without_child_task_escape(
    fatal_type: type[BaseException],
) -> None:
    from src.developer_workflow.tui.runtime_session import TuiRuntimeSession

    order: list[str] = []

    class Supervisor:
        async def close(self) -> None:
            order.append("supervisor")
            raise fatal_type("supervisor fatal")

    session = TuiRuntimeSession(
        handle=SimpleNamespace(close=lambda: order.append("handle")),
        controller=SimpleNamespace(close=lambda: order.append("controller")),
        run_index=object(),
        supervisor=Supervisor(),
        event_sink=lambda event: None,
    )

    for _ in range(2):
        with pytest.raises(fatal_type, match="supervisor fatal"):
            await session.close()

    assert order == ["supervisor", "controller", "handle"]


@pytest.mark.asyncio
async def test_runtime_close_thread_start_failure_drains_synchronously_and_is_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.developer_workflow.tui.runtime_session as runtime_session
    from src.developer_workflow.tui.runtime_session import (
        TuiRuntimeCloseError,
        TuiRuntimeSession,
    )

    order: list[str] = []

    class BrokenThread:
        def __init__(self, *args: object, target, **kwargs: object) -> None:
            self.target = target

        def start(self) -> None:
            raise RuntimeError("sensitive thread start failure")

    monkeypatch.setattr(runtime_session, "Thread", BrokenThread)
    session = TuiRuntimeSession(
        handle=SimpleNamespace(close=lambda: order.append("handle")),
        controller=SimpleNamespace(close=lambda: order.append("controller")),
        run_index=object(),
        supervisor=SimpleNamespace(close=_async_noop),
        event_sink=lambda event: None,
    )

    for _ in range(2):
        with pytest.raises(TuiRuntimeCloseError) as raised:
            await session.close()
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None

    assert order == ["controller", "handle"]


@pytest.mark.asyncio
async def test_runtime_close_thread_start_fallback_never_blocks_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading

    import src.developer_workflow.tui.runtime_session as runtime_session
    from src.developer_workflow.tui.runtime_session import (
        TuiRuntimeCloseError,
        TuiRuntimeSession,
    )

    entered = threading.Event()
    release = threading.Event()
    handle_closed = threading.Event()

    class BrokenThread:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("sensitive thread start failure")

    def close_controller() -> None:
        entered.set()
        release.wait()

    monkeypatch.setattr(runtime_session, "Thread", BrokenThread)
    session = TuiRuntimeSession(
        handle=SimpleNamespace(close=handle_closed.set),
        controller=SimpleNamespace(close=close_controller),
        run_index=object(),
        supervisor=SimpleNamespace(close=_async_noop),
        event_sink=lambda event: None,
        close_timeout=0.05,
    )

    try:
        with pytest.raises(TuiRuntimeCloseError, match="runtime close failed"):
            await asyncio.wait_for(session.close(), timeout=0.5)
        assert entered.wait(0.5)
        assert not handle_closed.is_set()
    finally:
        release.set()

    assert handle_closed.wait(0.5)


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
