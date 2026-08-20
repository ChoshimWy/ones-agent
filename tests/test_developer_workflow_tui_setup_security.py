from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from uuid import uuid4

import pytest
from textual.widgets import Button, Input, Select, Static

from src.developer_workflow.config import (
    BUILTIN_WORKSPACE_PROFILE,
    SandboxPermissionProfileSource,
)
from src.developer_workflow.setup_models import SetupDraft
from src.developer_workflow.setup_models import SecretKind, WorkflowDraft
from src.developer_workflow.setup_import import ImportDetection
from src.developer_workflow.setup_controller import SetupController
from src.developer_workflow.setup_store import SetupStore
from src.developer_workflow.runtime_bootstrap import (
    RuntimeAdapterBundle,
    RuntimeBootstrapper,
)
from src.developer_workflow.credential_store import (
    CredentialStoreError,
    WindowsCredentialStore,
)
from src.developer_workflow.repository import WorktreeRepository
from src.developer_workflow.setup_validation import (
    BuiltinWorkspaceSandboxExecutorFactory,
    ConnectionTestResult,
    ManagedSandboxExecutorFactory,
    ManagedProfileCatalog,
    SetupStep,
    SetupValidator,
    SubprocessDoctorRunner,
    ValidationStatus,
)
from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
from src.developer_workflow.contracts import WorkflowState
from src.developer_workflow.tui.app import TuiTaskMessage
from src.developer_workflow.tui.models import RunActivity
from src.developer_workflow.tui.supervisor import TaskEvent
from src.developer_workflow.tui.screens import DashboardScreen
from src.developer_workflow.tui.setup_screens import SetupImportContext, SetupWizardScreen
from tests.test_developer_workflow_tui_integration import (
    _EffectRepository,
    _MultiRemoteRunner,
    _assert_recorded_sandbox_prefixes,
    _cold_start_codex_preparer,
    _group_ui_runtime,
    _security_facts,
)
from tests.test_developer_workflow_tui_integration import _source_facts
from tests.test_developer_workflow_tui_security import SECRETS, _audit
from tests.test_developer_workflow_setup_controller import (
    FakeBootstrap,
    FakeRuntimeBuilder,
    IntegrationCredentials,
    _runtime,
    _workflow,
)
from tests.test_developer_workflow_setup_validation import _doctor


@pytest.fixture
def real_windows_credentials():
    if os.name != "nt":
        pytest.skip("Windows Credential Manager is required")
    profile = f"tui-e2e-{uuid4().hex}"
    try:
        credentials = WindowsCredentialStore()
        assert credentials.list_generations(profile) == ()
    except CredentialStoreError:
        pytest.skip("Windows Credential Manager is unavailable")
    known_generations: set[str] = set()
    try:
        yield credentials, profile, known_generations
    finally:
        _cleanup_profile_generations(credentials, profile, known_generations)


def _cleanup_profile_generations(credentials, profile: str, known: set[str]) -> None:
    candidates = set(known)
    failures: list[str] = []
    try:
        candidates.update(credentials.list_generations(profile))
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        failures.append("list")
    for generation in sorted(candidates):
        try:
            credentials.delete_generation(profile, generation)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            failures.append("delete")
    try:
        remaining = credentials.list_generations(profile)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        failures.append("verify")
        remaining = ("unknown",)
    if failures or remaining:
        raise AssertionError(
            f"credential cleanup failed safely ({len(failures)} operations)"
        )


def test_credential_cleanup_attempts_later_generation_after_first_delete_fails() -> None:
    class FaultyCredentials:
        def __init__(self) -> None:
            self.list_calls = 0
            self.deleted: list[str] = []

        def list_generations(self, profile: str):
            self.list_calls += 1
            if self.list_calls == 1:
                raise OSError("target-must-not-escape")
            return ("a" * 32,)

        def delete_generation(self, profile: str, generation: str):
            self.deleted.append(generation)
            if generation == "a" * 32:
                raise OSError("target-must-not-escape")

    credentials = FaultyCredentials()
    with pytest.raises(AssertionError, match=r"credential cleanup failed safely \(2 operations\)") as caught:
        _cleanup_profile_generations(
            credentials, "profile-must-not-escape", {"a" * 32, "b" * 32}
        )
    assert credentials.deleted == ["a" * 32, "b" * 32]
    assert "target" not in str(caught.value)
    assert "profile-must-not-escape" not in str(caught.value)


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
        self.import_calls: list[object] = []

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

    async def list_managed_profiles(self) -> tuple[str, ...]:
        return ("managed-profile",)

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

    def import_secrets(self, **kwargs) -> None:
        self.import_calls.append(kwargs)

    def apply_workflow(self, workflow, *, changed_step) -> None:
        self.draft.workflow = workflow.model_copy(deep=True)


async def _set_inputs(app: DeveloperWorkflowTuiApp, values: dict[str, str]) -> None:
    for widget_id, value in values.items():
        widget = app.screen.query_one(f"#{widget_id}")
        if isinstance(widget, (Input, Select)):
            widget.value = value


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


async def _wait_until(pilot: object, predicate, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause(0.01)
    assert predicate()


@pytest.mark.asyncio
async def test_tui_creates_builtin_profile_without_preconfiguration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Visible cold-start actions cross the real controller/store transaction."""

    from textual.widgets import Button, Select

    codex_config = tmp_path / "codex-home" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_bytes(b"[permissions]\n")
    config_before = _security_facts(codex_config)
    npm_source = tmp_path / "npm" / "native" / "codex.exe"
    npm_source.parent.mkdir(parents=True)
    npm_source.write_bytes(b"tiny-signed-native-codex")
    source_before = _security_facts(npm_source)
    preparer, backend, digest = _cold_start_codex_preparer(
        tmp_path / "cold-start", npm_source
    )
    private_executable = (
        tmp_path
        / "cold-start"
        / "private-runtime"
        / "codex-runtime"
        / digest
        / "codex.exe"
    )

    def builtin_factory(*, codex_preparer):
        assert codex_preparer is preparer
        return BuiltinWorkspaceSandboxExecutorFactory(
            backend_executor=backend,
            codex_preparer=codex_preparer,
        )

    monkeypatch.setattr(
        "src.developer_workflow.setup_validation.BuiltinWorkspaceSandboxExecutorFactory",
        builtin_factory,
    )
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    doctor_backend_calls: list[tuple[str, ...]] = []
    doctor_result = _doctor(codex_config)

    def doctor_backend(command, *, cwd, env, timeout, max_output_bytes):
        doctor_backend_calls.append(tuple(command))
        return doctor_result(
            command[-2:],
            cwd=cwd,
            env=env,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            shell=False,
        )

    catalog = ManagedProfileCatalog.production(probe_parent=tmp_path)
    catalog.codex_doctor = SubprocessDoctorRunner(
        codex_preparer=preparer,
        backend_runner=doctor_backend,
    )
    catalog.executor_factory = ManagedSandboxExecutorFactory(
        codex_preparer=preparer
    )
    catalog.file_security = (
        lambda path, private: path == codex_config and not private
    )
    catalog.cold_config_path = codex_config
    catalog.codex_runtime_preparer = preparer
    bootstrap = SimpleNamespace(
        catalog=catalog,
        validator=SetupValidator._testing(profile_catalog=catalog),
        codex_runtime_preparer=preparer,
    )
    credentials = IntegrationCredentials()
    setup_path = tmp_path / "setup-private" / "config.json"
    store = SetupStore(credentials, config_path=setup_path)  # type: ignore[arg-type]
    workflow_data = _workflow(tmp_path).model_dump(mode="python", round_trip=True)
    workflow_data.update(
        sandbox_permission_profile=None,
        sandbox_permission_profile_source=SandboxPermissionProfileSource.MANAGED,
    )
    draft = SetupDraft(
        runtime=_runtime(),
        workflow=WorkflowDraft.model_validate(workflow_data),
    )
    builder = FakeRuntimeBuilder()
    controller = SetupController(
        profile_id="cold-start",
        store=store,
        runtime_builder=builder,
        runtime_bootstrap=bootstrap,
        draft=draft,
    )
    for kind, value in (
        (SecretKind.ONES_PASSWORD, "ones-password-for-store"),
        (SecretKind.PROVIDER_TOKEN, "provider-token-for-store"),
    ):
        controller.set_secret(kind, value)
    app = DeveloperWorkflowTuiApp(
        setup_controller=controller,
        setup_controller_factory=lambda: controller,
        runtime_bootstrapper=builder,
        poll_interval=10,
    )

    async with app.run_test(size=(140, 40)) as pilot:
        assert isinstance(app.screen, SetupWizardScreen)
        wizard = app.screen
        assert backend.calls == []
        assert doctor_backend_calls == []
        assert not private_executable.exists()
        assert wizard.query_one("#sandbox-profile", Select).disabled
        create = wizard.query_one("#create-workspace-profile", Button)
        assert create.disabled is False
        await pilot.click("#create-workspace-profile")

        def confirmation_is_clickable() -> bool:
            matches = app.screen.query("#confirm-workspace-profile")
            if not matches:
                return False
            button = matches.first(Button)
            return (
                button.display
                and button.visible
                and not button.disabled
                and button.region.width > 0
                and button.region.height > 0
            )

        await _wait_until(
            pilot,
            confirmation_is_clickable,
        )
        assert backend.calls == []
        assert doctor_backend_calls == []
        assert not private_executable.exists()
        await pilot.click("#confirm-workspace-profile")
        await _wait_until(pilot, lambda: wizard._profile_task is not None)
        profile_task = wizard._profile_task
        assert profile_task is not None
        assert await asyncio.wait_for(asyncio.shield(profile_task), 30) == "verified"
        await pilot.pause()
        assert app.screen is wizard
        assert (
            wizard.query_one("#sandbox-profile", Select).value
            == BUILTIN_WORKSPACE_PROFILE
        )
        assert len(backend.calls) == 3
        await pilot.click("#test-connection")
        await _wait_until(pilot, lambda: wizard._test_task is not None)
        test_task = wizard._test_task
        assert test_task is not None
        await asyncio.wait_for(asyncio.shield(test_task), 30)
        await pilot.pause()
        assert any(
            result.step is SetupStep.PROFILE
            and result.status is ValidationStatus.PASSED
            for result in controller.state.results
        )
        assert len(backend.calls) == 6
        await _wait_until(
            pilot,
            lambda: wizard.query_one("#next-step", Button).disabled is False,
        )
        await pilot.click("#next-step")
        await _wait_until(
            pilot, lambda: wizard.current_step is SetupStep.ONES
        )

        # The acceptance target is the Profile transaction; the remaining remote
        # probes stay at their existing fake transport boundary before real save.
        controller._results.update(
            {
                step: ConnectionTestResult(
                    step=step,
                    status=ValidationStatus.PASSED,
                    category="ok",
                )
                for step in SetupController.STEPS[1:-1]
            }
        )
        controller.confirm_review()
        handle = await controller.save_and_activate()
        assert handle is builder.handle

    loaded = SetupStore(
        credentials, config_path=setup_path  # type: ignore[arg-type]
    ).load()
    assert loaded.active is not None
    assert loaded.active.workflow.sandbox_permission_profile == BUILTIN_WORKSPACE_PROFILE
    assert (
        loaded.active.workflow.sandbox_permission_profile_source
        is SandboxPermissionProfileSource.BUILTIN_WORKSPACE
    )
    assert [
        (
            active.workflow.sandbox_permission_profile,
            active.workflow.sandbox_permission_profile_source,
        )
        for active, _secrets in builder.calls
    ] == [
        (BUILTIN_WORKSPACE_PROFILE, SandboxPermissionProfileSource.BUILTIN_WORKSPACE)
    ]
    restarted_builder = FakeRuntimeBuilder()
    restarted = SetupController(
        profile_id="cold-start",
        store=SetupStore(
            credentials, config_path=setup_path  # type: ignore[arg-type]
        ),
        runtime_builder=restarted_builder,
        runtime_bootstrap=bootstrap,
    )
    assert await restarted.activate_existing() is restarted_builder.handle
    assert [
        active.workflow.sandbox_permission_profile_source
        for active, _secrets in [*builder.calls, *restarted_builder.calls]
    ] == [
        SandboxPermissionProfileSource.BUILTIN_WORKSPACE,
        SandboxPermissionProfileSource.BUILTIN_WORKSPACE,
    ]
    private_executable = private_executable.resolve(strict=True)
    for offset in (0, 3):
        observed = type(backend)()
        observed.calls = backend.calls[offset : offset + 3]
        _assert_recorded_sandbox_prefixes(
            observed,
            private_executable=private_executable,
            builtin=True,
            profile=BUILTIN_WORKSPACE_PROFILE,
            final_command=[
                sys.executable,
                "-I",
                "-c",
                "print('sandbox-preflight')",
            ],
        )
    assert _security_facts(codex_config) == config_before
    assert _security_facts(npm_source) == source_before
    assert doctor_backend_calls == []


@pytest.mark.asyncio
async def test_existing_managed_profile_advances_without_forced_confirmation(
    tmp_path: Path,
) -> None:
    """Catalog-managed profiles retain the legacy selection-only UI path."""

    from textual.widgets import Button, Select

    class ManagedCatalog:
        def __init__(self) -> None:
            self.selections: list[tuple[str, SandboxPermissionProfileSource]] = []

        def list_profiles(self) -> tuple[str, ...]:
            return ("managed-profile",)

        def require_selected(
            self, profile: str, source: SandboxPermissionProfileSource
        ) -> str:
            self.selections.append((profile, source))
            if (
                profile != "managed-profile"
                or source is not SandboxPermissionProfileSource.MANAGED
            ):
                raise ValueError
            return profile

    catalog = ManagedCatalog()
    bootstrap = FakeBootstrap()
    bootstrap.catalog = catalog
    builder = FakeRuntimeBuilder()
    controller = SetupController(
        profile_id="managed-compatibility",
        store=SetupStore(
            IntegrationCredentials(),  # type: ignore[arg-type]
            config_path=tmp_path / "managed-private" / "config.json",
        ),
        runtime_builder=builder,
        runtime_bootstrap=bootstrap,
        draft=SetupDraft(runtime=_runtime(), workflow=_workflow(tmp_path)),
    )
    app = DeveloperWorkflowTuiApp(
        setup_controller=controller,
        setup_controller_factory=lambda: controller,
        runtime_bootstrapper=builder,
        poll_interval=10,
    )
    async with app.run_test(size=(140, 40)) as pilot:
        wizard = app.screen
        assert isinstance(wizard, SetupWizardScreen)
        await _wait_until(
            pilot,
            lambda: wizard.query_one("#sandbox-profile", Select).value
            == "managed-profile",
        )
        assert not app.screen.query("#confirm-workspace-profile")
        await pilot.click("#test-connection")
        await _wait_until(
            pilot,
            lambda: any(
                result.step is SetupStep.PROFILE
                and result.status is ValidationStatus.PASSED
                for result in controller.state.results
            ),
        )
        await _wait_until(
            pilot,
            lambda: wizard.query_one("#next-step", Button).disabled is False,
        )
        assert not app.screen.query("#confirm-workspace-profile")
    assert catalog.selections == [
        ("managed-profile", SandboxPermissionProfileSource.MANAGED)
    ]


@pytest.mark.asyncio
async def test_empty_setup_uses_all_seven_steps_then_activates_dashboard_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_windows_credentials,
) -> None:
    (
        unused_app,
        original_controller,
        fixture_store,
        effects,
        sources,
        remotes,
        commenter,
    ) = _group_ui_runtime(tmp_path)
    credentials, profile, known_generations = real_windows_credentials
    setup_store = SetupStore(
        credentials,
        config_path=tmp_path / "private-config" / "config.json",
    )
    original = original_controller._orchestrator
    original_flow = original.requirement_flow
    original_publisher = original.publisher
    remote_runner = _MultiRemoteRunner(
        {
            "https://git.example.invalid/team/dependency.git": remotes["dependency"],
            "https://git.example.invalid/team/primary.git": remotes["primary"],
        }
    )

    def repository_factory(
        mirror_root, worktree_root, credential_provider, identity_provider
    ):
        raw = WorktreeRepository(
            mirror_root,
            worktree_root,
            command_runner=remote_runner,
            credential_env_provider=credential_provider,
            identity_env_provider=identity_provider,
        )
        return _EffectRepository(raw, effects)

    runtime_builder = RuntimeBootstrapper(
        sandbox_profile_validator=lambda selected, source, env: None,
        gateway_close=lambda gateway: None,
        ambient_environment=lambda: {},
        adapters=RuntimeAdapterBundle(
            gateway_factory=lambda settings: original_flow.gateway,
            codex_factory=lambda run_root, repository, environment: original_flow.codex,
            repository_factory=repository_factory,
            sandbox_factory=lambda selected, source: original_flow.test_runner,
            pr_factory=lambda **kwargs: original_publisher.pr_client,
            commenter_factory=lambda gateway, runtime_store: commenter,
        ),
    )
    bootstrap = FakeBootstrap()
    bootstrap.catalog = SimpleNamespace(
        list_profiles=lambda: (profile,),
        require_selected=lambda selected, source: selected
        if selected == profile
        and source is SandboxPermissionProfileSource.MANAGED
        else (_ for _ in ()).throw(ValueError("profile unavailable"))
    )
    setup = SetupController(
        profile_id=profile,
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
            profile_id=profile,
            store=setup_store,
            runtime_builder=runtime_builder,
            runtime_bootstrap=bootstrap,
            activation_timeout=10,
        ),
        runtime_bootstrapper=runtime_builder,
        setup_import=SetupImportContext(
            detection=ImportDetection((), (), True),
            dotenv_path=None,
            template_workflow=WorkflowDraft.model_validate(
                original.config.model_dump(mode="python", round_trip=True)
            ),
        ),
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
        assert fixture_store.list_run_ids() == ()
        assert effects == []

        await pilot.click("#review-imports")
        app.screen.query_one("#import-template", Button).focus()
        await pilot.press("enter")
        app.screen.query_one("#confirm-import", Button).focus()
        await pilot.press("enter")
        assert isinstance(app.screen, SetupWizardScreen)
        assert app._setup_import is None
        assert app.screen._import_context is None
        assert setup.draft.workflow.repository_groups == original.config.repository_groups

        await _set_inputs(app, {"sandbox-profile": profile})
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
                "repository-role": "dependency",
                "repository-depends-on": "",
                "repository-allowed-paths": "src",
                "repository-lint-commands": "",
                "repository-build-commands": "",
                "repository-test-commands": "python -m compileall src",
                "repository-group-key": "",
                "repository-primary": "",
                "repository-integration-commands": "",
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
                "repository-role": "primary",
                "repository-depends-on": "dependency",
                "repository-allowed-paths": "src",
                "repository-lint-commands": "",
                "repository-build-commands": "",
                "repository-test-commands": "python -m compileall src/value.py",
                "repository-group-key": "suite",
                "repository-primary": "primary",
                "repository-integration-commands": "python -m compileall .",
            },
        )
        await _test_current_step(pilot, app, SetupStep.REPOSITORIES)
        assert tuple(
            item.key
            for item in setup.draft.workflow.repository_groups[0].repositories
        ) == ("dependency", "primary")
        assert setup.draft.workflow.repository_groups == original.config.repository_groups
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
                "codex-profile": profile,
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
        assert effects == [] and fixture_store.list_run_ids() == ()
        assert credentials.list_generations(profile) == ()
        assert app.runtime_session is None
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
        assert app.runtime_session is not None
        built_handle = app.runtime_session.handle
        assert built_handle is not None
        assert built_handle.orchestrator is app.controller._orchestrator
        runtime_store = built_handle.orchestrator.store
        assert built_handle.orchestrator.requirement_flow.store is runtime_store
        assert built_handle.orchestrator.publisher.store is runtime_store
        assert built_handle.orchestrator.config.repository_groups[0].key == "suite"
        assert (
            built_handle.orchestrator.config.repository_groups[0]
            == original.config.repository_groups[0]
        ), (
            built_handle.orchestrator.config.repository_groups[0].model_dump(),
            original.config.repository_groups[0].model_dump(),
        )
        assert effects == [] and runtime_store.list_run_ids() == ()
        persisted = setup_store.load()
        assert persisted.active is not None
        assert persisted.activation_owner_generation is None
        generation = persisted.active.generation
        known_generations.add(generation)
        assert credentials.list_generations(profile) == (generation,)
        persisted_secrets = credentials.read_generation(
            profile, generation, persisted.active.credential_kinds
        )
        assert set(persisted_secrets.values) == set(persisted.active.credential_kinds)

        # Continue from the activated Dashboard through the existing real
        # requirement flow.  Only ONES/Codex/PR/comment/sandbox are fakes; the
        # store, orchestrator, repository group, worktrees and publisher are real.
        await pilot.press("n")
        await pilot.click("#workflow-requirement")
        app.screen.query_one("#requirement-id", Input).value = "REQ-UI"
        app.screen.query_one("#start-requirement", Button).focus()
        await pilot.press("enter")
        run_id = runtime_store.list_run_ids()[0]
        assert runtime_store.load(run_id, read_only=True).state is WorkflowState.VALIDATING
        assert effects == []
        app.screen.query_one("#mapping-0", Button).focus()
        await pilot.press("enter")
        assert app.screen.query_one("#confirm-start")
        await pilot.click("#confirm-start")
        await asyncio.wait_for(confirmation_finished.wait(), 180)
        await pilot.pause()
        waiting = runtime_store.load(run_id, read_only=True)
        assert waiting.state is WorkflowState.WAITING_APPROVAL, (
            waiting.blocked_reason,
            tuple((item.source, item.target, item.reason) for item in waiting.history),
            type(app.screen).__name__,
        )
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

    completed = runtime_store.load(run_id, read_only=True)
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
            TuiTaskMessage(
                TaskEvent.failed(
                    "setup-probe",
                    "validate",
                    RuntimeError(" ".join(SECRETS.values())),
                )
            )
        )
        await pilot.pause()
        _audit(app, "setup-task-event")

        # Inject path/control/Rich-looking values through an actual failing
        # validator boundary.  The screen must retain only its fixed category.
        app.screen.current_step = SetupStep.PROFILE
        app.screen._render_state()
        app.screen.query_one("#sandbox-profile", Select).value = "managed-profile"
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


@pytest.mark.asyncio
async def test_setup_import_requires_visible_selection_and_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _SetupHarness(SimpleNamespace(orchestrator=object(), close=lambda: None))
    monkeypatch.setenv("ONES_EMAIL", SECRETS["email"])
    monkeypatch.setenv("ONES_PASSWORD", SECRETS["ones"])
    context = SetupImportContext(
        detection=ImportDetection(
            environment=(SecretKind.ONES_EMAIL, SecretKind.ONES_PASSWORD),
            dotenv=(SecretKind.ONES_PASSWORD, SecretKind.PROVIDER_TOKEN),
            template_available=True,
        ),
        dotenv_path=None,
        template_workflow=WorkflowDraft(sandbox_permission_profile="managed-template"),
    )
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup,
        runtime_bootstrapper=object(),
        setup_import=context,
        poll_interval=10,
    )
    async with app.run_test(size=(140, 40)) as pilot:
        assert setup.import_calls == []
        assert not hasattr(context, "environment")
        assert not hasattr(context, "dotenv_values")
        assert not any(secret in repr(context) + repr(app) for secret in SECRETS.values())
        await pilot.click("#review-imports")
        assert app.screen.id == "setup-import"
        rendered = str(app.screen.query_one("#import-summary").render())
        assert "environment" in rendered and "dotenv" in rendered and "conflict" in rendered
        assert not any(secret in rendered for secret in SECRETS.values())
        app.screen.query_one("#import-environment", Button).focus()
        await pilot.press("enter")
        assert setup.import_calls == []
        app.screen.query_one("#confirm-import", Button).focus()
        await pilot.press("enter")
        assert len(setup.import_calls) == 1
        call = setup.import_calls[0]
        assert call["selected"] == (SecretKind.ONES_EMAIL, SecretKind.ONES_PASSWORD)
        assert set(call["source_choice"].values()) == {"environment"}
        assert context.consumed is True
        assert app._setup_import is None
        assert app.screen._import_context is None
        assert call["environment"] == {}
        with pytest.raises(RuntimeError, match="import source is unavailable"):
            context.import_into(setup, "environment")


def test_repository_command_input_supports_multiple_safe_entries() -> None:
    assert SetupWizardScreen._command_values(
        "uv run ruff check .;;uv run pytest -q"
    ) == ("uv run ruff check .", "uv run pytest -q")


@pytest.mark.asyncio
async def test_setup_import_cancel_discards_descriptor_without_reading_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"ONES_PASSWORD={SECRETS['ones']}\n", encoding="utf-8")
    context = SetupImportContext(
        detection=ImportDetection((), (SecretKind.ONES_PASSWORD,), False),
        dotenv_path=dotenv,
    )
    reads: list[Path] = []
    monkeypatch.setattr(
        "src.developer_workflow.tui.setup_screens.parse_dotenv",
        lambda path: reads.append(path) or {},
    )
    setup = _SetupHarness(SimpleNamespace(orchestrator=object(), close=lambda: None))
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup,
        runtime_bootstrapper=object(),
        setup_import=context,
        poll_interval=10,
    )
    async with app.run_test(size=(140, 40)) as pilot:
        assert SECRETS["ones"] not in repr(context) + repr(app)
        await pilot.click("#review-imports")
        await pilot.press("escape")
        assert reads == []
        assert context.consumed is True
        assert context.dotenv_path is None
        assert app._setup_import is None


@pytest.mark.asyncio
async def test_setup_wizard_unmount_discards_unconsumed_import_context() -> None:
    context = SetupImportContext(
        detection=ImportDetection((), (), False),
        dotenv_path=Path("unopened.env"),
    )
    setup = _SetupHarness(SimpleNamespace(orchestrator=object(), close=lambda: None))
    app = DeveloperWorkflowTuiApp(
        setup_controller=setup,
        runtime_bootstrapper=object(),
        setup_import=context,
        poll_interval=10,
    )
    async with app.run_test(size=(120, 32)):
        assert context.consumed is False
    assert context.consumed is True
    assert context.dotenv_path is None
    assert app._setup_import is None


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
