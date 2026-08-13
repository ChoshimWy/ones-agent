from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest
from textual.widgets import Button, Input, Static

from src.developer_workflow.setup_models import SetupDraft
from src.developer_workflow.setup_controller import SetupController
from src.developer_workflow.setup_store import SetupStore
from src.developer_workflow.runtime_bootstrap import RuntimeBootstrapper
from src.developer_workflow.setup_validation import (
    ConnectionTestResult,
    SetupStep,
    ValidationStatus,
)
from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
from src.developer_workflow.contracts import WorkflowState
from src.developer_workflow.tui.app import TuiTaskMessage
from src.developer_workflow.tui.models import RunActivity
from src.developer_workflow.tui.supervisor import TaskEvent
from src.developer_workflow.tui.screens import DashboardScreen
from src.developer_workflow.tui.setup_screens import SetupWizardScreen
from tests.test_developer_workflow_tui_integration import _group_ui_runtime
from tests.test_developer_workflow_tui_integration import _source_facts
from tests.test_developer_workflow_tui_security import SECRETS, _audit
from tests.test_developer_workflow_setup_controller import (
    FakeBootstrap,
    IntegrationCredentials,
)


class _SetupHarness:
    """Exercise the real wizard transaction boundary before handing off a real graph."""

    def __init__(self, handle: object) -> None:
        self.handle = handle
        self.current_step = SetupStep.PROFILE
        self.draft = SetupDraft()
        self._runtime_fields: dict[str, str] = {}
        self._results: dict[SetupStep, ConnectionTestResult] = {}
        self._revision = 0
        self.review_confirmed = False
        self.activation_calls = 0
        self.closed = False
        self.probe_effects: list[str] = []
        self.probe_failure: BaseException | None = None

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def runtime_public_fields(self):
        return MappingProxyType(dict(self._runtime_fields))

    @property
    def state(self):
        return SimpleNamespace(
            current_step=self.current_step,
            results=tuple(self._results.values()),
            repository_count=len(self.draft.workflow.repositories),
            repository_group_count=len(self.draft.workflow.repository_groups),
            secret_count=0,
            review_confirmed=self.review_confirmed,
            closed=self.closed,
            error_category=None,
        )

    @property
    def recovery_state(self):
        return SimpleNamespace(owner_generation=None)

    async def activate_existing(self) -> None:
        return None

    def apply_step_transaction(
        self, step, transaction, *, expected_revision, secrets
    ) -> None:
        assert expected_revision == self._revision
        if transaction.runtime_fields is not None:
            self._runtime_fields.update(transaction.runtime_fields)
        if transaction.runtime is not None:
            self.draft.runtime = transaction.runtime.model_copy(deep=True)
        if transaction.workflow is not None:
            self.draft.workflow = transaction.workflow.model_copy(deep=True)
        workflow = self.draft.workflow
        if transaction.repository is not None:
            repository = transaction.repository.model_copy(deep=True)
            workflow.repositories = tuple(
                repository if item.key == repository.key else item
                for item in workflow.repositories
            )
            if not any(item.key == repository.key for item in workflow.repositories):
                workflow.repositories = (*workflow.repositories, repository)
        if transaction.repository_group is not None:
            group = transaction.repository_group.model_copy(deep=True)
            members = {item.key for item in group.repositories}
            workflow.repositories = tuple(
                item for item in workflow.repositories if item.key not in members
            )
            workflow.repository_groups = tuple(
                group if item.key == group.key else item
                for item in workflow.repository_groups
            )
            if not any(item.key == group.key for item in workflow.repository_groups):
                workflow.repository_groups = (*workflow.repository_groups, group)
        # The UI owns transient strings only until this call returns.
        assert all(value for value in secrets.values())
        self._revision += 1
        self.review_confirmed = False
        self._results.pop(step, None)

    async def test_step(self, step: SetupStep, probe: object):
        del probe
        if self.probe_failure is not None:
            raise self.probe_failure
        result = ConnectionTestResult(
            step=step, status=ValidationStatus.PASSED, category="ok"
        )
        self._results[step] = result
        return result

    def confirm_review(self) -> None:
        assert all(
            self._results.get(step, object()).status is ValidationStatus.PASSED
            for step in tuple(SetupStep)[:-1]
        )
        self.review_confirmed = True
        self._results[SetupStep.REVIEW] = ConnectionTestResult(
            step=SetupStep.REVIEW,
            status=ValidationStatus.PASSED,
            category="ok",
        )

    async def save_and_activate(self) -> object:
        assert self.review_confirmed
        self.activation_calls += 1
        return self.handle

    def cancel_edit(self) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


async def _set_inputs(app: DeveloperWorkflowTuiApp, values: dict[str, str]) -> None:
    for widget_id, value in values.items():
        app.screen.query_one(f"#{widget_id}", Input).value = value


async def _test_current_step(
    pilot, app: DeveloperWorkflowTuiApp, step: SetupStep
) -> None:
    before_revision = app.screen.controller.revision
    await app.screen.action_test_connection()
    for _ in range(100):
        await pilot.pause(0.01)
        state = app.screen.controller.state
        if (
            app.screen.controller.revision > before_revision
            and app.screen._test_task is None
            and any(
            result.step is step and result.status is ValidationStatus.PASSED
            for result in state.results
            )
        ):
            return
    notice = app.screen.query_one("#setup-notice").render()
    raise AssertionError(
        f"setup step did not pass: {step.value}; current={app.screen.current_step}; "
        f"revision={app.screen.controller.revision}; notice={notice!s}"
    )


async def _next_step(pilot, app: DeveloperWorkflowTuiApp) -> None:
    app.screen._pressed_next()
    await pilot.pause()


@pytest.mark.asyncio
async def test_empty_setup_uses_all_seven_steps_then_activates_dashboard_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        unused_app,
        original_controller,
        store,
        effects,
        sources,
        remotes,
        commenter,
    ) = _group_ui_runtime(tmp_path)
    handle = SimpleNamespace(
        orchestrator=original_controller._orchestrator,
        close=lambda: None,
    )
    credentials = IntegrationCredentials()
    setup_store = SetupStore(
        credentials,
        config_path=tmp_path / "private-config" / "config.json",
    )

    class ProductionRuntimeBuilder:
        def __init__(self) -> None:
            self.calls = 0
            self.bootstrapper = RuntimeBootstrapper(
                sandbox_profile_validator=lambda profile, env: None,
                ambient_environment=lambda: {},
            )

        def build(self, active, secrets):
            # Exercise the real production graph constructor.  The E2E then swaps
            # to the established fully-instrumented local graph so its allowed
            # fake ONES/Codex/PR/comment transports remain observable.
            built = self.bootstrapper.build(active, secrets)
            built.close()
            self.calls += 1
            return handle

    runtime_builder = ProductionRuntimeBuilder()
    bootstrap = FakeBootstrap()
    setup = SetupController(
        profile_id="managed-profile",
        store=setup_store,
        runtime_builder=runtime_builder,
        runtime_bootstrap=bootstrap,
        activation_timeout=10,
    )
    # Keep the real repository/group builders and all local snapshot checks;
    # replace only their allowed read-only remote transport.
    from src.developer_workflow.setup_validation import ReadOnlyRepositoryInspector

    class LocalReadOnlyInspector(ReadOnlyRepositoryInspector):
        def _run(self, argv, **kwargs):
            if "ls-remote" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return super()._run(argv, **kwargs)

        def ls_remote(self, path, remote_url, *, timeout):
            del path, remote_url, timeout
            return None

    monkeypatch.setattr(
        "src.developer_workflow.setup_repository.ReadOnlyRepositoryInspector",
        LocalReadOnlyInspector,
    )
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup,
        setup_controller_factory=lambda: SetupController(
            profile_id="managed-profile",
            store=setup_store,
            runtime_builder=runtime_builder,
            runtime_bootstrap=FakeBootstrap(),
            activation_timeout=10,
        ),
        runtime_bootstrapper=object(),
        poll_interval=10,
    )
    source_before = {key: _source_facts(path) for key, path in sources.items()}
    run_id: str | None = None
    confirmation_finished = asyncio.Event()
    approval_finished = asyncio.Event()

    def observe_ui_message(message: object) -> None:
        if not isinstance(message, TuiTaskMessage) or run_id is None:
            return
        event = message.event
        if event.run_id != run_id or event.activity is not RunActivity.IDLE:
            return
        if event.action == "confirm-repository":
            confirmation_finished.set()
        elif event.action == "approve":
            approval_finished.set()

    async with app.run_test(
        size=(140, 40), message_hook=observe_ui_message
    ) as pilot:
        assert isinstance(app.screen, SetupWizardScreen)
        assert store.list_run_ids() == ()
        assert effects == []

        await _set_inputs(app, {"sandbox-profile": "managed-profile"})
        await _test_current_step(pilot, app, SetupStep.PROFILE)
        await _next_step(pilot, app)

        await _set_inputs(
            app,
            {
                "ones-base-url": "https://ones.example.invalid",
                "ones-team-id": "TEAM",
                "ones-issue-type-id": "DEFECT",
                "ones-project-id": "P",
                "ones-status-id": "doing",
                "ones-item-id": "REQ-UI",
                "ones-email": "setup@example.invalid",
                "ones-password": "Setup-Ones-Password-92731",
            },
        )
        await _test_current_step(pilot, app, SetupStep.ONES)
        await _next_step(pilot, app)

        await _set_inputs(
            app,
            {
                "repository-key": "dependency",
                "repository-project-id": "P",
                "repository-iteration-id": "I",
                "repository-name": "dependency",
                "repository-path": str(sources["dependency"].resolve()),
                "repository-url": "https://git.example.invalid/team/dependency.git",
                "repository-branch": "main",
                "repository-group-key": "",
                "repository-primary": "",
            },
        )
        await _test_current_step(pilot, app, SetupStep.REPOSITORIES)
        # Re-test the same real step to build the second member and final group.
        await _set_inputs(
            app,
            {
                "repository-key": "primary",
                "repository-project-id": "P",
                "repository-iteration-id": "I",
                "repository-name": "primary",
                "repository-path": str(sources["primary"].resolve()),
                "repository-url": "https://git.example.invalid/team/primary.git",
                "repository-branch": "main",
                "repository-group-key": "suite",
                "repository-primary": "primary",
            },
        )
        await _test_current_step(pilot, app, SetupStep.REPOSITORIES)
        assert tuple(
            item.key
            for item in setup.draft.workflow.repository_groups[0].repositories
        ) == ("dependency", "primary")
        await _next_step(pilot, app)

        await _set_inputs(
            app,
            {
                "provider-host": "git.example.invalid",
                "provider-api-url": "https://git.example.invalid/api/v3",
                "git-author-name": "ONES Dev",
                "git-author-email": "ones-dev@example.invalid",
                "provider-type": "github",
                "repository-branch": "main",
                "provider-token": "Setup-Provider-Token-81620",
            },
        )
        await _test_current_step(pilot, app, SetupStep.PROVIDER)
        await _next_step(pilot, app)

        await _set_inputs(
            app,
            {
                "codex-auth-mode": "credential",
                "codex-profile": "managed-profile",
                "codex-worktree": str(sources["primary"].resolve()),
                "codex-home": "",
                "codex-api-key": "Setup-Codex-Key-74813",
                "codex-auth-token": "",
            },
        )
        await _test_current_step(pilot, app, SetupStep.CODEX)
        await _next_step(pilot, app)

        await _set_inputs(
            app,
            {
                "run-root": str((tmp_path / "configured-runs").resolve()),
                "mirror-root": str((tmp_path / "configured-mirrors").resolve()),
                "worktree-root": str((tmp_path / "configured-worktrees").resolve()),
            },
        )
        await _test_current_step(pilot, app, SetupStep.PRIVATE_PATHS)
        await _next_step(pilot, app)
        assert app.screen.current_step is SetupStep.REVIEW
        assert effects == [] and store.list_run_ids() == ()
        assert credentials.data == {}
        assert runtime_builder.calls == 0
        for private_root in (
            tmp_path / "configured-runs",
            tmp_path / "configured-mirrors",
            tmp_path / "configured-worktrees",
        ):
            assert not private_root.exists()
        await pilot.click("#confirm-review")
        await pilot.click("#activate-runtime")
        assert app.screen.id == "setup-activation-confirmation"
        await app.screen._confirm()
        for _ in range(100):
            await pilot.pause(0.02)
            if isinstance(app.screen, DashboardScreen):
                break
        assert isinstance(app.screen, DashboardScreen)
        assert runtime_builder.calls == 1
        assert effects == [] and store.list_run_ids() == ()
        persisted = setup_store.load()
        assert persisted.active is not None
        assert persisted.activation_owner_generation is None
        generation = persisted.active.generation
        assert ("managed-profile", generation) in credentials.data

        # Continue from the activated Dashboard through the existing real
        # requirement flow.  Only ONES/Codex/PR/comment/sandbox are fakes; the
        # store, orchestrator, repository group, worktrees and publisher are real.
        await pilot.press("n")
        await pilot.click("#workflow-requirement")
        app.screen.query_one("#requirement-id", Input).value = "REQ-UI"
        app.screen.query_one("#start-requirement", Button).focus()
        await pilot.press("enter")
        run_id = store.list_run_ids()[0]
        assert store.load(run_id, read_only=True).state is WorkflowState.VALIDATING
        assert effects == []
        app.screen.query_one("#mapping-0", Button).focus()
        await pilot.press("enter")
        assert app.screen.query_one("#confirm-start")
        await pilot.click("#confirm-start")
        await asyncio.wait_for(confirmation_finished.wait(), 180)
        await pilot.pause()
        waiting = store.load(run_id, read_only=True)
        assert waiting.state is WorkflowState.WAITING_APPROVAL
        assert effects == []
        for _ in range(100):
            await pilot.pause(0.01)
            if isinstance(app.screen, DashboardScreen):
                break
        await app.screen.refresh_runs()
        await pilot.press("a")
        assert effects == []
        app.screen.query_one("#actor", Input).value = "operator"
        await pilot.click("#confirm-approve")
        await asyncio.wait_for(approval_finished.wait(), 180)
        await pilot.pause()

    completed = store.load(run_id, read_only=True)
    assert completed.state is WorkflowState.COMPLETED
    assert effects == [
        "commit:dependency",
        "commit:primary",
        "push:dependency",
        "pr:dependency",
        "push:primary",
        "pr:primary",
        "comment",
    ]
    assert commenter.status_updates == 0
    assert commenter.mutation_requests == [("comment", "REQ-UI")]

    assert {key: _source_facts(path) for key, path in sources.items()} == source_before


@pytest.mark.asyncio
async def test_all_seven_setup_surfaces_hide_complete_secrets_and_fragments() -> None:
    # Reuse the established unfiltered Rich/widget auditor with every sensitive
    # boundary category injected into the real seven-step screen.
    setup = _SetupHarness(SimpleNamespace(orchestrator=object(), close=lambda: None))
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup,
        setup_controller_factory=lambda: setup,
        runtime_bootstrapper=object(),
        poll_interval=10,
    )
    async with app.run_test(size=(140, 40)) as pilot:
        for step in tuple(SetupStep)[:-1]:
            setup._results[step] = ConnectionTestResult(
                step=step, status=ValidationStatus.PASSED, category="ok"
            )
        for step in SetupStep:
            app.screen.current_step = step
            app.screen._render_state()
            # Inject credentials into the real password widgets for their owned
            # production steps.  Raw widget/repr/Rich output must still be masked.
            injected = {
                SetupStep.ONES: (("ones-email", SECRETS["email"]), ("ones-password", SECRETS["ones"])),
                SetupStep.PROVIDER: (("provider-token", SECRETS["provider"]),),
                SetupStep.CODEX: (("codex-api-key", SECRETS["codex"]),),
            }.get(step, ())
            for widget_id, secret in injected:
                app.screen.query_one(f"#{widget_id}", Input).value = secret
            _audit(app, f"setup-{step.value}")
            for widget_id, _ in injected:
                app.screen.query_one(f"#{widget_id}", Input).value = ""
        app.notify("Configuration action failed safely")
        await pilot.pause()
        _audit(app, "setup-notification")
        app.post_message(
            TuiTaskMessage(TaskEvent.failed("setup-probe", "validate"))
        )
        await pilot.pause()
        _audit(app, "setup-task-event")

        # Inject path/control/Rich-looking values through an actual failing
        # validator boundary.  The screen must retain only its fixed category.
        app.screen.current_step = SetupStep.PROFILE
        app.screen._render_state()
        app.screen.query_one("#sandbox-profile", Input).value = "managed-profile"
        setup.probe_failure = RuntimeError(
            f"{SECRETS['path']} {SECRETS['control']} {SECRETS['codex']}"
        )
        await app.screen.action_test_connection()
        _audit(app, "setup-validator-fixed-error")


@pytest.mark.asyncio
async def test_setup_surface_auditor_detects_a_deliberate_raw_and_rich_leak() -> None:
    setup = _SetupHarness(SimpleNamespace(orchestrator=object(), close=lambda: None))
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup,
        setup_controller_factory=lambda: setup,
        runtime_bootstrapper=object(),
        poll_interval=10,
    )
    async with app.run_test(size=(120, 32)):
        await app.screen.mount(Static(SECRETS["control"], id="deliberate-leak"))
        with pytest.raises(AssertionError, match="setup-negative-control"):
            _audit(app, "setup-negative-control")


def test_setup_release_policy_declares_resources_and_private_exclusions() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = (root / "MANIFEST.in").read_text(encoding="utf-8")
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    documentation = (root / "docs" / "ones_dev_cli.md").read_text(encoding="utf-8")

    assert '"src.developer_workflow" = ["schemas/*.json", "tui/*.tcss"]' in project
    assert '"src.llm" = ["prompts/*.md"]' in project
    for required in (
        "recursive-include src *.py *.json *.tcss",
        "recursive-include src *.md",
        "prune tests",
        "prune data",
        "prune .agents",
        "prune .codex",
        "global-exclude .env .env.*",
    ):
        assert required in manifest
    for phrase in (
        "首次配置",
        "Windows Credential Manager",
        "托管 profile",
        "七个步骤",
        "恢复",
        "重新配置",
        "示例文件只用于导入",
        "非交互 CLI",
        "pip install --no-deps <wheel>",
        "不表示该 wheel 可在没有依赖的 Python 中独立运行",
    ):
        assert phrase in documentation
    for production_identifier in (
        "XjJ3QvWeJyNQWgwu",
        "JkYR4hqe",
        "Q6kE8A2m",
        "CKA6U955",
        "WwhszYN8",
    ):
        assert production_identifier not in documentation
