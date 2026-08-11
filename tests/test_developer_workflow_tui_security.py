from __future__ import annotations

from datetime import UTC, datetime
import io

import pytest
from rich.console import Console
from rich.text import Text
from textual.widgets import Button, Input

from src.developer_workflow.contracts import WorkflowState, WorkflowType
from src.developer_workflow.tui.app import (
    DeveloperWorkflowTuiApp,
    TuiTaskMessage,
)
from src.developer_workflow.tui.controller import (
    CandidateSessionView,
    TuiControllerError,
)
from src.developer_workflow.tui.models import (
    DangerousActionRequest,
    DefectChoice,
    MappingCandidateView,
    PublicationView,
    RepositoryCandidateView,
    RepositoryView,
    RunActivity,
    RunDetail,
    RunFilter,
    RunSummary,
)
from src.developer_workflow.tui.supervisor import TaskEvent


NOW = datetime(2026, 8, 11, tzinfo=UTC)
SECRETS = {
    "codex": "CODEX-SECRET-[bold]do-not-render[/bold]",
    "ones": "ONES-PASSWORD-SECRET",
    "git": "GIT-TOKEN-SECRET",
    "provider": "PROVIDER-TOKEN-SECRET",
    "email": "private-person-secret@example.invalid",
    "path": "E:/PRIVATE-GIT-PATH-SECRET/[italic]askpass[/italic].exe",
    "control": "CONTROL-SECRET-\x1b[31m",
    "publication": "PUBLICATION-ERROR-SECRET",
    "exception": "CONTROLLER-EXCEPTION-SECRET",
    "candidate": "PRIVATE-CANDIDATE-TOKEN",
}


def _mapping() -> MappingCandidateView:
    return MappingCandidateView(
        key="suite",
        kind="repository-group",
        primary_repository="primary",
        repositories=(
            RepositoryCandidateView(
                key="dependency",
                role="dependency",
                source="local read-only source",
                depends_on=(),
                lint_summary="0 configured lint commands",
                build_summary="0 configured build commands",
                test_summary="1 configured test command",
                allowed_paths=("src",),
                side_effects="changes use an isolated managed worktree",
            ),
            RepositoryCandidateView(
                key="primary",
                role="primary",
                source="remote mirror",
                depends_on=("dependency",),
                lint_summary="0 configured lint commands",
                build_summary="0 configured build commands",
                test_summary="1 configured test command",
                allowed_paths=("src",),
                side_effects="changes use an isolated managed worktree",
            ),
        ),
        integration_test_summary="1 configured integration test command",
    )


def _detail(state: WorkflowState, *, version: int = 7) -> RunDetail:
    summary = RunSummary(
        run_id=f"security-{state.value.casefold()}",
        workflow_type=WorkflowType.DEFECT,
        work_item_id="BUG-7",
        state=state,
        version=version,
        updated_at=NOW,
        activity=RunActivity.IDLE,
    )
    repository = RepositoryView(
        key="primary",
        role="primary",
        base_commit="a" * 40,
        head_commit="b" * 40,
        tree_hash="c" * 40,
        changed_files=("src/value.py",),
        changed_file_count=1,
        commit_hash="",
        pushed=False,
        pr_url="",
        error="",
    )
    return RunDetail(
        summary=summary,
        repositories=(repository,),
        tests=(),
        review=("reviewed safely",),
        publication=PublicationView(
            repositories=(repository,),
            # A provider identifier is present in the ViewModel but the UI is
            # only allowed to render the fixed delivery fact, never the ID.
            comment_id=SECRETS["provider"],
            error="publication failed safely"
            if state is WorkflowState.PARTIAL_SUCCESS
            else "",
        ),
        history=(),
        blocked_reason="",
        fingerprint="f" * 64,
        risk_count=0,
        unresolved_count=0,
        mapping_candidates=(_mapping(),)
        if state is WorkflowState.VALIDATING
        else (),
    )


class _SecurityController:
    """Fake external boundaries; only safe view projections cross into the UI."""

    def __init__(
        self,
        state: WorkflowState = WorkflowState.WAITING_APPROVAL,
        *,
        fail_query: bool = False,
        fail_requirement: bool = False,
    ) -> None:
        self.detail = _detail(state)
        self.fail_query = fail_query
        self.fail_requirement = fail_requirement
        # Secrets enter through distinct real boundary-shaped facts. They must
        # remain outside every returned display projection.
        self.fake_ones_snapshot_token = SECRETS["candidate"]
        self.fake_ones_raw = {"password": SECRETS["ones"]}
        self.fake_codex_result = {"raw_output": SECRETS["codex"]}
        self.fake_provider_error = RuntimeError(SECRETS["provider"])
        self.fake_git_environment = {
            "GIT_ASKPASS": SECRETS["path"],
            "TOKEN": SECRETS["git"],
        }
        self.raw_publication_error = SECRETS["publication"]

    def list_runs(self, filters: RunFilter, activities=None):
        del filters, activities
        return (self.detail.summary,)

    def show(self, run_id: str) -> RunDetail:
        assert run_id == self.detail.summary.run_id
        return self.detail

    def query_defects(self, project, iteration, assignee, status_ids):
        del project, iteration, assignee, status_ids
        if self.fail_query:
            raise TuiControllerError(SECRETS["exception"])
        return CandidateSessionView(
            session_id=self.fake_ones_snapshot_token,
            items=(
                DefectChoice(
                    candidate_id="d" * 32,
                    title="Qt lifecycle defect",
                    status_id="todo-id",
                    priority="normal",
                ),
            ),
        )

    def start_defect(self, session_id: str, candidate_id: str) -> RunDetail:
        assert session_id == self.fake_ones_snapshot_token
        assert candidate_id == "d" * 32
        return _detail(WorkflowState.VALIDATING, version=1)

    def start_requirement(self, requirement_id: str) -> RunDetail:
        if self.fail_requirement:
            raise TuiControllerError(
                f"{SECRETS['exception']} {SECRETS['codex']}"
            )
        assert requirement_id == "REQ-UI"
        return _detail(WorkflowState.VALIDATING, version=1)

    def confirm_repository(
        self, run_id: str, mapping_key: str, expected_version: int
    ) -> RunDetail:
        assert run_id and mapping_key == "suite" and expected_version == 1
        return _detail(WorkflowState.WAITING_APPROVAL)

    def prepare_action(self, run_id: str, action: str) -> DangerousActionRequest:
        assert run_id == self.detail.summary.run_id
        return DangerousActionRequest(
            run_id=run_id,
            version=self.detail.summary.version,
            action=action,  # type: ignore[arg-type]
            fingerprint=self.detail.fingerprint,
            work_item_id=self.detail.summary.work_item_id,
            repositories=self.detail.repositories,
            changed_file_count=1,
            test_count=0,
            risk_count=0,
            unresolved_count=0,
            publication_error=self.detail.publication.error,
            state=self.detail.summary.state,
        )


def _raw_surface(value: object) -> tuple[str, ...]:
    captured = [str(value), repr(value)]
    if isinstance(value, Text):
        captured.extend((value.plain, repr(value.spans), repr(value.style)))
    nested = getattr(value, "_renderable", None)
    if nested is not None and nested is not value:
        captured.extend(_raw_surface(nested))
    return tuple(captured)


def _audit(app: DeveloperWorkflowTuiApp, surface: str) -> str:
    """Collect raw widget/Rich representations without redacting output."""

    captured = [f"SURFACE:{surface}"]
    for widget in app.query("*"):
        rendered = widget.render()
        captured.extend(_raw_surface(widget))
        captured.extend(_raw_surface(rendered))
        for attribute in ("label", "renderable"):
            value = getattr(widget, attribute, None)
            if value is not None:
                captured.extend(_raw_surface(value))
        output = io.StringIO()
        console = Console(
            file=output,
            width=160,
            color_system=None,
            force_terminal=False,
        )
        console.print(rendered)
        captured.append(output.getvalue())
    captured.extend(_raw_surface(getattr(app, "_notifications", ())))
    result = "\n".join(captured)
    for secret in SECRETS.values():
        assert secret not in result, surface
    for fragment in (
        "CODEX-SECRET",
        "ONES-PASSWORD",
        "GIT-TOKEN",
        "PROVIDER-TOKEN",
        "private-person-secret@",
        "PRIVATE-GIT-PATH",
        "CONTROL-SECRET",
        "PUBLICATION-ERROR-SECRET",
        "CONTROLLER-EXCEPTION-SECRET",
        "PRIVATE-CANDIDATE-TOKEN",
    ):
        assert fragment not in result, surface
    return result


def _app(controller: _SecurityController) -> DeveloperWorkflowTuiApp:
    return DeveloperWorkflowTuiApp(
        controller,  # type: ignore[arg-type]
        3,
        provider_type="github",
        sandbox_configured=True,
        poll_interval=10,
    )


@pytest.fixture(autouse=True)
def _sensitive_parent_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "CODEX_API_KEY": SECRETS["codex"],
        "ONES_PASSWORD": SECRETS["ones"],
        "ONES_EMAIL": SECRETS["email"],
        "ONES_DEV_PROVIDER_TOKEN": SECRETS["provider"],
        "GIT_ASKPASS": SECRETS["path"],
        "GIT_SSH_COMMAND": SECRETS["control"],
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


@pytest.mark.asyncio
async def test_dashboard_settings_help_and_all_dangerous_modals_are_secret_free() -> None:
    controller = _SecurityController()
    app = _app(controller)
    async with app.run_test(size=(120, 32)) as pilot:
        _audit(app, "dashboard")
        await pilot.click("#nav-settings")
        _audit(app, "settings")
        await pilot.press("g", "?")
        _audit(app, "help")
        await pilot.press("escape", "a")
        _audit(app, "approval-modal")
        await pilot.press("escape", "v")
        _audit(app, "revision-modal")
        await pilot.press("escape", "x")
        _audit(app, "cancel-modal")

    partial = _app(_SecurityController(WorkflowState.PARTIAL_SUCCESS))
    async with partial.run_test(size=(120, 32)) as pilot:
        await pilot.press("r")
        _audit(partial, "publication-resume-modal")


@pytest.mark.asyncio
async def test_defect_wizard_all_stages_and_error_notice_are_secret_free() -> None:
    app = _app(_SecurityController())
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.press("n")
        await pilot.click("#workflow-defect")
        _audit(app, "defect-filter")
        for widget_id, value in (
            ("project", "P"),
            ("iteration", "I"),
            ("assignee", "A"),
            ("status-ids", "todo-id,fixing-id"),
        ):
            app.screen.query_one(f"#{widget_id}", Input).value = value
        app.screen.query_one("#query-defects", Button).focus()
        await pilot.press("enter")
        _audit(app, "defect-candidate")
        app.screen.query_one("#candidate-0", Button).focus()
        await pilot.press("enter")
        _audit(app, "defect-mapping")
        app.screen.query_one("#mapping-0", Button).focus()
        await pilot.press("enter")
        _audit(app, "defect-confirm")

    failed = _app(_SecurityController(fail_query=True))
    async with failed.run_test(size=(120, 32)) as pilot:
        await pilot.press("n")
        await pilot.click("#workflow-defect")
        for widget_id in ("project", "iteration", "assignee"):
            failed.screen.query_one(f"#{widget_id}", Input).value = "safe-id"
        failed.screen.query_one("#query-defects", Button).focus()
        await pilot.press("enter")
        _audit(failed, "defect-error-notice")


@pytest.mark.asyncio
async def test_requirement_wizard_all_stages_and_error_notice_are_secret_free() -> None:
    app = _app(_SecurityController())
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.press("n")
        await pilot.click("#workflow-requirement")
        _audit(app, "requirement-filter")
        app.screen.query_one("#requirement-id", Input).value = "REQ-UI"
        app.screen.query_one("#start-requirement", Button).focus()
        await pilot.press("enter")
        _audit(app, "requirement-mapping")
        app.screen.query_one("#mapping-0", Button).focus()
        await pilot.press("enter")
        _audit(app, "requirement-confirm")

    failed = _app(_SecurityController(fail_requirement=True))
    async with failed.run_test(size=(120, 32)) as pilot:
        await pilot.press("n")
        await pilot.click("#workflow-requirement")
        failed.screen.query_one("#requirement-id", Input).value = "REQ-UI"
        failed.screen.query_one("#start-requirement", Button).focus()
        await pilot.press("enter")
        _audit(failed, "requirement-error-notice")


@pytest.mark.asyncio
async def test_failed_task_event_and_notifications_are_secret_free() -> None:
    app = _app(_SecurityController())
    async with app.run_test(size=(120, 32)) as pilot:
        app.post_message(
            TuiTaskMessage(
                TaskEvent.failed(
                    "security-waiting_approval",
                    "approve",
                )
            )
        )
        await pilot.pause()
        _audit(app, "failed-task-event-notification")
