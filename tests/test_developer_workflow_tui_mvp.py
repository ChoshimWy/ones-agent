from __future__ import annotations

from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest


def _mvp_draft(root: Path):
    from src.developer_workflow.config import (
        BUILTIN_WORKSPACE_PROFILE,
        PublishingConfig,
        PublishingProvider,
        SandboxPermissionProfileSource,
    )
    from src.developer_workflow.contracts import RepositoryMapping
    from src.developer_workflow.setup_models import (
        RuntimePublicConfig,
        SetupDraft,
        WorkflowDraft,
    )

    return SetupDraft(
        runtime=RuntimePublicConfig(
            ones_base_url="http://localhost",
            ones_team_id="pending-team",
            ones_issue_type_id="pending-type",
            ones_comment_list_path_template=(
                "/project/api/project/team/{team_id}/task/{item_id}/comments"
            ),
            provider_host="github.com",
            provider_api_url="https://github.com/api/v3",
            git_author_name="ONES Dev Agent",
            git_author_email="ones-dev@localhost",
            codex_auth_mode="file",
        ),
        workflow=WorkflowDraft(
            run_root=root / "runs",
            mirror_root=root / "mirrors",
            worktree_root=root / "worktrees",
            sandbox_permission_profile=BUILTIN_WORKSPACE_PROFILE,
            sandbox_permission_profile_source=(
                SandboxPermissionProfileSource.BUILTIN_WORKSPACE
            ),
            repositories=(
                RepositoryMapping(
                    key="workspace",
                    project_id="pending-project",
                    iteration_id="*",
                    repo_url=str(root),
                    repo_name="workspace",
                ),
            ),
            publishing=PublishingConfig(
                provider=PublishingProvider.GITHUB,
            ),
        ),
    )


def test_mvp_ones_transaction_configures_only_global_runtime(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.setup_controller import (
        SetupController,
        SetupStepTransaction,
    )
    from src.developer_workflow.setup_models import SecretKind
    from src.developer_workflow.setup_validation import SetupStep

    controller = SetupController(
        profile_id="mvp",
        store=object(),  # type: ignore[arg-type]
        runtime_builder=object(),  # type: ignore[arg-type]
        validator=object(),  # type: ignore[arg-type]
        profile_catalog=object(),
        draft=_mvp_draft(tmp_path),
        steps=(SetupStep.ONES, SetupStep.REVIEW),
    )

    controller.apply_step_transaction(
        SetupStep.ONES,
        SetupStepTransaction(
            runtime_fields=MappingProxyType(
                {
                    "ones_base_url": "https://ones.example.test",
                    "ones_team_id": "team-1",
                    "ones_issue_type_id": "defect-type",
                }
            ),
        ),
        secrets={
            SecretKind.ONES_EMAIL: "agent@example.test",
            SecretKind.ONES_PASSWORD: "password-value",
        },
        expected_revision=0,
    )

    assert controller.STEPS == (SetupStep.ONES, SetupStep.REVIEW)
    assert controller.draft.runtime is not None
    assert controller.draft.runtime.ones_base_url == "https://ones.example.test"
    assert controller.draft.runtime.ones_team_id == "team-1"
    assert controller.draft.workflow.repositories[0].project_id == "pending-project"
    assert controller.draft.workflow.repositories[0].iteration_id == "*"


@pytest.mark.asyncio
async def test_mvp_setup_mounts_only_ones_and_review_without_profile_discovery(
    tmp_path: Path,
) -> None:
    from textual.app import App

    from src.developer_workflow.setup_validation import SetupStep
    from src.developer_workflow.tui.setup_screens import SetupWizardScreen

    class Controller:
        STEPS = (SetupStep.ONES, SetupStep.REVIEW)
        current_step = SetupStep.ONES
        draft = _mvp_draft(tmp_path)
        runtime_public_fields = MappingProxyType({})

        @property
        def state(self) -> object:
            return SimpleNamespace(
                results=(),
                repository_count=1,
                repository_group_count=0,
                review_confirmed=False,
            )

        async def list_managed_profiles(self) -> tuple[str, ...]:
            raise AssertionError("MVP setup must not discover profiles before ONES")

    class MvpApp(App[None]):
        CSS_PATH = "../src/developer_workflow/tui/tui.tcss"

        def on_mount(self) -> None:
            self.push_screen(SetupWizardScreen(Controller()))

    async with MvpApp().run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert screen.current_step is SetupStep.ONES
        assert screen.query_one("#nav-ones") is not None
        assert not screen.query_one("#nav-review").display
        assert screen.query_one("#save-mvp-runtime").display
        assert not screen.query_one("#test-connection").display
        assert not screen.query_one("#next-step").display
        assert not screen.query_one("#review-setup").display
        assert screen.query_one("#ones-base-url").value == ""
        assert screen.query_one("#ones-team-id").value == ""
        assert screen.query_one("#ones-issue-type-id").value == ""
        assert not screen.query("#workspace-project-id")
        assert not screen.query("#ones-project-id")
        assert not screen.query("#ones-status-id")
        assert not screen.query("#ones-item-id")
        assert not screen.query("#nav-profile")
        assert not screen.query("#nav-repositories")
        assert not screen.query("#nav-provider")
        assert not screen.query("#nav-private-paths")


@pytest.mark.asyncio
async def test_mvp_save_button_confirms_activates_and_hands_runtime_to_host(
    tmp_path: Path,
) -> None:
    from textual.app import App

    from src.developer_workflow.setup_validation import (
        SetupStep,
        ValidationStatus,
    )
    from src.developer_workflow.tui.setup_screens import SetupWizardScreen

    handle = object()

    class Controller:
        STEPS = (SetupStep.ONES, SetupStep.REVIEW)
        current_step = SetupStep.ONES
        draft = _mvp_draft(tmp_path)
        runtime_public_fields = MappingProxyType({})
        activation_error = None
        tested = False
        confirmed = False

        @property
        def state(self) -> object:
            results = (
                {
                    SetupStep.ONES: SimpleNamespace(
                        status=ValidationStatus.PASSED,
                        category="ok",
                    )
                }
                if self.tested
                else {}
            )
            return SimpleNamespace(
                results=results,
                repository_count=1,
                repository_group_count=0,
                review_confirmed=self.confirmed,
            )

        def result_for(self, step: SetupStep) -> object | None:
            if step is SetupStep.ONES and self.tested:
                return SimpleNamespace(
                    status=ValidationStatus.PASSED,
                    category="ok",
                )
            return None

        def confirm_review(self) -> None:
            self.confirmed = True

    controller = Controller()
    returned: list[object | None] = []

    class MvpScreen(SetupWizardScreen):
        async def action_test_connection(self) -> None:
            controller.tested = True

    async def activate() -> object:
        assert controller.confirmed
        return handle

    class MvpApp(App[None]):
        CSS_PATH = "../src/developer_workflow/tui/tui.tcss"

        def on_mount(self) -> None:
            self.push_screen(
                MvpScreen(controller, activation_callback=activate),
                returned.append,
            )

    async with MvpApp().run_test() as pilot:
        await pilot.pause()
        assert await pilot.click("#save-mvp-runtime")
        for _ in range(20):
            await pilot.pause()
            if returned:
                break

    assert returned == [handle]
    assert controller.tested
    assert controller.confirmed


def test_mvp_runtime_accepts_local_workspace_without_provider_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.developer_workflow.runtime_bootstrap as runtime_module
    from src.developer_workflow.config import (
        BUILTIN_WORKSPACE_PROFILE,
        DeveloperWorkflowConfig,
        PublishingConfig,
        SandboxPermissionProfileSource,
    )
    from src.developer_workflow.contracts import RepositoryMapping
    from src.developer_workflow.runtime_bootstrap import (
        RuntimeAdapterBundle,
        RuntimeBootstrapper,
    )
    from src.developer_workflow.setup_models import (
        ActiveSetup,
        RuntimePublicConfig,
        RuntimeSecrets,
        SecretKind,
    )

    class LocalPr:
        def close(self) -> None:
            return None

    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    workflow = DeveloperWorkflowConfig(
        run_root=(tmp_path / "runs").resolve(),
        mirror_root=(tmp_path / "mirrors").resolve(),
        worktree_root=(tmp_path / "worktrees").resolve(),
        sandbox_permission_profile=BUILTIN_WORKSPACE_PROFILE,
        sandbox_permission_profile_source=(
            SandboxPermissionProfileSource.BUILTIN_WORKSPACE
        ),
        max_codex_attempts=3,
        repositories=(
            RepositoryMapping(
                key="workspace",
                project_id="project-1",
                iteration_id="*",
                repo_url=str(workspace),
                repo_name="workspace",
            ),
        ),
        publishing=PublishingConfig(provider="github"),
    )
    active = ActiveSetup(
        generation="a" * 32,
        runtime=RuntimePublicConfig(
            ones_base_url="https://ones.example.test",
            ones_team_id="team-1",
            ones_issue_type_id="defect-type",
            ones_comment_list_path_template=(
                "/project/api/project/team/{team_id}/task/{item_id}/comments"
            ),
            provider_host="github.com",
            provider_api_url="https://github.com/api/v3",
            git_author_name="ONES Dev Agent",
            git_author_email="ones-dev@localhost",
            codex_auth_mode="file",
        ),
        workflow=workflow,
        credential_kinds=(SecretKind.ONES_EMAIL, SecretKind.ONES_PASSWORD),
    )
    secrets = RuntimeSecrets(
        {
            SecretKind.ONES_EMAIL: "agent@example.test",
            SecretKind.ONES_PASSWORD: "password-value",
        }
    )
    codex_home = (tmp_path / "codex-home").resolve()
    monkeypatch.setattr(
        runtime_module,
        "validate_codex_auth_source",
        lambda environment: codex_home,
    )
    handle = RuntimeBootstrapper(
        private_root_preparer=lambda roots: tuple(Path(root) for root in roots),
        sandbox_profile_validator=lambda profile, source, environment: None,
        adapters=RuntimeAdapterBundle(pr_factory=lambda **kwargs: LocalPr()),
        ambient_environment=lambda: {},
    ).build(active, secrets)
    try:
        assert handle.orchestrator is not None
        assert handle.orchestrator.config.repositories[0].repo_url == str(workspace)
    finally:
        handle.close()


def test_production_mvp_uses_unsandboxed_codex_and_direct_test_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.developer_workflow.cli import build_production_tui_host
    from src.developer_workflow.requirement_flow import DirectConfiguredTestRunner

    monkeypatch.chdir(tmp_path)
    _factory, runtime = build_production_tui_host(tmp_path / "missing.json")
    codex_factory = runtime.adapters.codex_factory
    test_factory = runtime.adapters.sandbox_factory

    assert callable(codex_factory)
    assert callable(test_factory)
    codex = codex_factory(tmp_path.resolve(), object(), lambda: {})
    runner = test_factory("ones-dev-workspace", object())

    assert codex.sandbox_mode_override == "danger-full-access"
    assert type(runner) is DirectConfiguredTestRunner
