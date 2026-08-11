from __future__ import annotations

from datetime import UTC, datetime

import pytest
from textual.widgets import Button, Input, Static, TabbedContent

from src.developer_workflow import tui
from src.developer_workflow.contracts import WorkflowRun, WorkflowState, WorkflowType
from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
from src.developer_workflow.tui.controller import (
    CandidateSessionView,
    TuiControllerError,
)
from src.developer_workflow.tui.models import (
    DefectChoice,
    HistoryView,
    MappingCandidateView,
    PublicationView,
    RepositoryView,
    RepositoryCandidateView,
    RunActivity,
    RunDetail,
    RunFilter,
    RunSummary,
    TestView as TuiTestView,
    safe_tui_text,
)
from src.developer_workflow.tui.screens import DashboardScreen, SettingsView


NOW = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)


def _plain(widget) -> str:
    rendered = widget.render()
    renderable = getattr(rendered, "_renderable", rendered)
    return renderable.plain if hasattr(renderable, "plain") else str(renderable)


def test_tui_package_exports_application_entry() -> None:
    assert tui.DeveloperWorkflowTuiApp is DeveloperWorkflowTuiApp


def _summary(number: int) -> RunSummary:
    return RunSummary(
        run_id=f"run-{number}",
        workflow_type=WorkflowType.DEFECT,
        work_item_id=f"BUG-{number}",
        state=WorkflowState.WAITING_APPROVAL,
        version=number,
        updated_at=NOW,
        activity=RunActivity.IDLE,
    )


def _detail(summary: RunSummary) -> RunDetail:
    return RunDetail(
        summary=summary,
        repositories=(),
        tests=(),
        review=(),
        publication=PublicationView(repositories=(), comment_id="", error=""),
        history=(),
        blocked_reason="",
        fingerprint="",
        risk_count=0,
        unresolved_count=0,
    )


class FakeController:
    def __init__(self) -> None:
        self.runs = (_summary(1), _summary(2))
        self.shown: list[str] = []

    def list_runs(self, filters: RunFilter, activities=None):
        del filters, activities
        return self.runs

    def show(self, run_id: str) -> RunDetail:
        self.shown.append(run_id)
        return _detail(next(item for item in self.runs if item.run_id == run_id))


def app_factory() -> DeveloperWorkflowTuiApp:
    return DeveloperWorkflowTuiApp(
        FakeController(),  # type: ignore[arg-type]
        3,
        provider_type="github",
        sandbox_configured=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("width", "expected_mode"), [(120, "three"), (80, "two"), (60, "one")]
)
async def test_dashboard_responsive_modes(width: int, expected_mode: str) -> None:
    async with app_factory().run_test(size=(width, 32)) as pilot:
        dashboard = pilot.app.screen.query_one("#dashboard")
        assert dashboard.has_class(expected_mode)
        assert sum(dashboard.has_class(mode) for mode in ("three", "two", "one")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("width", "expected_mode"), [(70, "two"), (99, "two"), (100, "three")]
)
async def test_dashboard_responsive_boundaries(width: int, expected_mode: str) -> None:
    async with app_factory().run_test(size=(width, 32)) as pilot:
        assert pilot.app.screen.query_one("#dashboard").has_class(expected_mode)


@pytest.mark.asyncio
async def test_keyboard_opens_run_and_switches_all_six_tabs() -> None:
    app = app_factory()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.press("j", "enter")
        assert app.screen.query_one("#run-detail").display
        assert app.controller.shown[-1] == "run-2"  # type: ignore[attr-defined]
        tabs = app.screen.query_one("#detail-tabs", TabbedContent)
        assert tabs.active == "overview"
        for expected in (
            "repositories",
            "tests",
            "review",
            "publication",
            "history",
        ):
            await pilot.press("tab")
            assert tabs.active == expected


@pytest.mark.asyncio
async def test_mouse_selects_run_and_opens_settings() -> None:
    app = app_factory()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.click("#run-item-1")
        assert app.controller.shown[-1] == "run-2"  # type: ignore[attr-defined]
        await pilot.click("#nav-settings")
        assert app.screen.query_one("#settings").display


@pytest.mark.asyncio
async def test_single_column_opens_independent_detail_and_returns() -> None:
    app = app_factory()
    async with app.run_test(size=(60, 32)) as pilot:
        await pilot.press("enter")
        assert app.screen.id == "run-detail-screen"
        await pilot.press("escape")
        assert app.screen.id == "dashboard-screen"


@pytest.mark.asyncio
async def test_settings_are_read_only_and_secret_free() -> None:
    app = app_factory()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.click("#nav-settings")
        settings = app.screen.query_one("#settings")
        text = settings.renderable.plain
        assert "max concurrency: 3" in text
        assert "private run root" in text
        assert "provider: github" in text
        assert "sandbox profile: configured" in text
        assert "E:\\" not in text
        assert "ONES_PASSWORD" not in text
        assert "email" not in text.casefold()
        assert not settings.query(Input)


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [60, 80])
async def test_narrow_layout_keeps_settings_keyboard_accessible(width: int) -> None:
    app = app_factory()
    async with app.run_test(size=(width, 32)) as pilot:
        await pilot.press("s")
        assert app.screen.query_one("#settings").display
        await pilot.press("g")
        assert app.screen.query_one("#workspace").display


@pytest.mark.parametrize(
    "provider_type", ["github@example.test", "ONES_PASSWORD", "E:\\credentials"]
)
def test_settings_reject_unrecognized_provider_text(provider_type: str) -> None:
    with pytest.raises(ValueError, match="provider type is invalid"):
        SettingsView(
            max_concurrency=3,
            provider_type=provider_type,
            sandbox_configured=True,
        )


@pytest.mark.asyncio
async def test_detail_tabs_render_complete_safe_evidence() -> None:
    app = app_factory()
    repository = RepositoryView(
        key="primary",
        role="primary",
        base_commit="a" * 40,
        head_commit="b" * 40,
        tree_hash="c" * 64,
        changed_files=("src/fix.py",),
        changed_file_count=1,
        commit_hash="d" * 40,
        pushed=True,
        pr_url="https://github.example/team/repo/pull/7",
        error="",
    )
    detail = RunDetail(
        summary=_summary(1),
        repositories=(repository,),
        tests=(TuiTestView(command="test command", outcome="passed", exit_code=0),),
        review=("review recorded",),
        publication=PublicationView(
            repositories=(repository,), comment_id="delivered", error=""
        ),
        history=(HistoryView("TESTING", "AI_REVIEW", NOW),),
        blocked_reason="workflow blocked safely",
        fingerprint="e" * 64,
        risk_count=2,
        unresolved_count=1,
    )
    app.controller.show = (  # type: ignore[method-assign, attr-defined]
        lambda run_id: detail
    )

    async with app.run_test(size=(120, 32)):
        screen = app.screen
        overview = str(screen.query_one("#overview-content").renderable)
        repositories = str(screen.query_one("#repositories-content").renderable)
        tests = str(screen.query_one("#tests-content").renderable)
        publication = str(screen.query_one("#publication-content").renderable)
        history = str(screen.query_one("#history-content").renderable)
        assert "workflow blocked safely" in overview
        assert "e" * 64 in overview
        assert "risks: 2" in overview
        assert "unresolved: 1" in overview
        assert "a" * 40 in repositories
        assert "b" * 40 in repositories
        assert "c" * 64 in repositories
        assert "src/fix.py" in repositories
        assert "d" * 40 in repositories
        assert repository.pr_url in repositories
        assert "test command  passed  exit: 0" in tests
        assert "primary" in publication
        assert "comment delivered" in publication
        assert "TESTING -> AI_REVIEW" in history


@pytest.mark.asyncio
async def test_refresh_replaces_list_atomically_and_preserves_selected_run() -> None:
    app = app_factory()
    async with app.run_test(size=(120, 32)) as pilot:
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        await pilot.press("j", "enter")
        assert app.controller.shown[-1] == "run-2"  # type: ignore[attr-defined]

        app.controller.runs = (_summary(2), _summary(3))  # type: ignore[attr-defined]
        await screen.refresh_runs()
        assert screen.query_one("#run-list").index == 0
        assert app.controller.shown[-1] == "run-2"  # type: ignore[attr-defined]
        assert len(screen.query("#run-list ListItem")) == 2

        app.controller.runs = (_summary(3),)  # type: ignore[attr-defined]
        await screen.refresh_runs()
        assert screen.query_one("#run-list").index == 0
        assert app.controller.shown[-1] == "run-3"  # type: ignore[attr-defined]
        assert len(screen.query("#run-list ListItem")) == 1

        app.controller.runs = ()  # type: ignore[attr-defined]
        await screen.refresh_runs()
        assert screen.query_one("#run-list").index is None
        assert len(screen.query("#run-list ListItem")) == 0
        overview = screen.query_one("#overview-content")
        assert str(overview.renderable) == "No run selected"


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [60, 120])
async def test_corrupted_run_never_calls_show_and_uses_fixed_error_view(
    width: int,
) -> None:
    controller = FakeController()
    controller.runs = (RunSummary.corrupted_entry("a" * 32),)

    def forbidden_show(run_id: str) -> RunDetail:
        raise AssertionError(f"show must not be called: {run_id}")

    controller.show = forbidden_show  # type: ignore[method-assign]
    app = DeveloperWorkflowTuiApp(controller, 3)  # type: ignore[arg-type]
    async with app.run_test(size=(width, 32)) as pilot:
        assert _plain(app.screen.query_one("#overview-content")) == (
            "workflow storage is corrupted safely"
        )
        await pilot.press("enter")
        assert _plain(app.screen.query_one("#overview-content")) == (
            "workflow storage is corrupted safely"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["list", "show"])
async def test_expected_controller_errors_render_fixed_safe_views(failure: str) -> None:
    controller = FakeController()
    if failure == "list":
        controller.list_runs = (  # type: ignore[method-assign]
            lambda filters, activities=None: (_ for _ in ()).throw(
                TuiControllerError("LEAK-ME controller list body")
            )
        )
        expected = "workflow list is unavailable safely"
    else:
        controller.show = (  # type: ignore[method-assign]
            lambda run_id: (_ for _ in ()).throw(
                TuiControllerError("LEAK-ME controller show body")
            )
        )
        expected = "workflow display is unavailable safely"
    app = DeveloperWorkflowTuiApp(controller, 3)  # type: ignore[arg-type]
    async with app.run_test(size=(120, 32)):
        text = _plain(app.screen.query_one("#overview-content"))
        assert text == expected
        assert "LEAK-ME" not in text


@pytest.mark.asyncio
async def test_factory_escaped_markup_renders_as_literal_without_backslashes() -> None:
    raw = "[bold]literal[/bold]"
    run = WorkflowRun.new("requirement", raw)
    detail = RunDetail.from_run(run)
    controller = FakeController()
    controller.runs = (detail.summary,)
    controller.show = lambda run_id: detail  # type: ignore[method-assign]
    app = DeveloperWorkflowTuiApp(controller, 3)  # type: ignore[arg-type]

    async with app.run_test(size=(120, 32)):
        overview = app.screen.query_one("#overview-content")
        assert _plain(overview).splitlines()[0] == raw
        assert "\\[bold]" not in _plain(overview)


@pytest.mark.asyncio
async def test_all_detail_tabs_render_escaped_values_with_safe_rich_semantics() -> None:
    raw = "[bold]literal[/bold]"
    escaped = safe_tui_text(raw)
    summary = _summary(1)
    repository = RepositoryView(
        key=escaped,
        role=escaped,
        base_commit=escaped,
        head_commit=escaped,
        tree_hash=escaped,
        changed_files=(escaped,),
        changed_file_count=1,
        commit_hash=escaped,
        pushed=False,
        pr_url=escaped,
        error=escaped,
    )
    detail = RunDetail(
        summary=summary,
        repositories=(repository,),
        tests=(TuiTestView(escaped, escaped, 0),),
        review=(escaped,),
        publication=PublicationView((repository,), "", escaped),
        history=(HistoryView(escaped, escaped, NOW),),
        blocked_reason=escaped,
        fingerprint="",
        risk_count=0,
        unresolved_count=0,
    )
    controller = FakeController()
    controller.runs = (summary,)
    controller.show = lambda run_id: detail  # type: ignore[method-assign]
    app = DeveloperWorkflowTuiApp(controller, 3)  # type: ignore[arg-type]

    async with app.run_test(size=(120, 32)):
        for widget_id in (
            "overview-content",
            "repositories-content",
            "tests-content",
            "review-content",
            "publication-content",
            "history-content",
        ):
            plain = _plain(app.screen.query_one(f"#{widget_id}"))
            assert raw in plain
            assert "\\[bold]" not in plain


def _candidate(
    key: str = "app-group", *, group: bool = False
) -> MappingCandidateView:
    primary = RepositoryCandidateView(
        key="primary" if group else key,
        role="primary",
        source="local read-only source",
        depends_on=(),
        lint_summary="1 configured lint command",
        build_summary="1 configured build command",
        test_summary="1 configured test command",
        allowed_paths=("src", "tests"),
        side_effects="changes use an isolated managed worktree",
    )
    repositories = (primary,)
    if group:
        repositories += (
            RepositoryCandidateView(
                key="dependency",
                role="dependency",
                source="remote mirror",
                depends_on=("primary",),
                lint_summary="0 configured lint commands",
                build_summary="1 configured build command",
                test_summary="1 configured test command",
                allowed_paths=("lib",),
                side_effects="changes use an isolated managed worktree",
            ),
        )
    return MappingCandidateView(
        key=key,
        kind="repository-group" if group else "repository",
        primary_repository=primary.key,
        repositories=repositories,
        integration_test_summary=(
            "1 configured integration test command"
            if group
            else "0 configured integration test commands"
        ),
    )


def _validating_detail(
    work_item_id: str,
    *,
    run_id: str,
    candidates: tuple[MappingCandidateView, ...] | None = None,
) -> RunDetail:
    summary = RunSummary(
        run_id=run_id,
        workflow_type=WorkflowType.DEFECT,
        work_item_id=work_item_id,
        state=WorkflowState.VALIDATING,
        version=1,
        updated_at=NOW,
        activity=RunActivity.IDLE,
    )
    detail = _detail(summary)
    return RunDetail(
        summary=detail.summary,
        repositories=detail.repositories,
        tests=detail.tests,
        review=detail.review,
        publication=detail.publication,
        history=detail.history,
        blocked_reason=detail.blocked_reason,
        fingerprint=detail.fingerprint,
        risk_count=detail.risk_count,
        unresolved_count=detail.unresolved_count,
        mapping_candidates=(_candidate(),) if candidates is None else candidates,
    )


class WizardController(FakeController):
    def __init__(self) -> None:
        super().__init__()
        self.last_query: tuple[str, str, str, tuple[str, ...]] | None = None
        self.mutation_calls: list[tuple[object, ...]] = []
        self.confirmed_mapping = ""
        self.fail_query = False
        self.fail_start = False
        self.fail_confirm = False
        self.mapping_candidates: tuple[MappingCandidateView, ...] = (_candidate(),)

    def query_defects(
        self,
        project: str,
        iteration: str,
        assignee: str,
        status_ids: tuple[str, ...],
    ) -> CandidateSessionView:
        self.last_query = (project, iteration, assignee, status_ids)
        if self.fail_query:
            raise TuiControllerError("LEAK-DUPLICATE-UUID-CONTEXT")
        return CandidateSessionView(
            session_id="PRIVATE-CANDIDATE-CAPABILITY",
            items=(DefectChoice("defect-1", "Qt lifecycle defect", "todo-id", "normal"),),
        )

    def start_defect(self, session_id: str, candidate_id: str) -> RunDetail:
        self.mutation_calls.append(("start_defect", session_id, candidate_id))
        if self.fail_start:
            raise TuiControllerError("LEAK-STALE-CANDIDATE-CONTEXT")
        return _validating_detail(
            "defect-1",
            run_id="run-defect-1",
            candidates=self.mapping_candidates,
        )

    def start_requirement(self, requirement_id: str) -> RunDetail:
        self.mutation_calls.append(("start_requirement", requirement_id))
        return _validating_detail(
            requirement_id,
            run_id="run-requirement-1",
            candidates=self.mapping_candidates,
        )

    def confirm_repository(
        self, run_id: str, mapping_key: str, expected_version: int
    ) -> RunDetail:
        self.mutation_calls.append(
            ("confirm_repository", run_id, mapping_key, expected_version)
        )
        if self.fail_confirm:
            raise TuiControllerError("LEAK-CONFIG-DRIFT-CONTEXT")
        self.confirmed_mapping = mapping_key
        return _validating_detail("confirmed", run_id=run_id)


def wizard_app_factory(controller: WizardController | None = None) -> DeveloperWorkflowTuiApp:
    return DeveloperWorkflowTuiApp(
        controller or WizardController(),  # type: ignore[arg-type]
        3,
        provider_type="github",
        sandbox_configured=True,
    )


async def _open_defect_wizard(pilot) -> None:
    await pilot.press("n")
    await pilot.click("#workflow-defect")
    await pilot.pause()


async def _query_defects(pilot) -> None:
    pilot.app.screen.query_one("#project", Input).value = "project-id"
    pilot.app.screen.query_one("#iteration", Input).value = "iteration-id"
    pilot.app.screen.query_one("#assignee", Input).value = "assignee-id"
    pilot.app.screen.query_one("#status-ids", Input).value = "todo-id,fixing-id"
    await pilot.click("#query-defects")
    await pilot.pause()


@pytest.mark.asyncio
async def test_defect_wizard_uses_only_status_ids_and_confirms_mapping() -> None:
    controller = WizardController()
    async with wizard_app_factory(controller).run_test(size=(120, 32)) as pilot:
        await _open_defect_wizard(pilot)
        await _query_defects(pilot)
        assert controller.last_query == (
            "project-id",
            "iteration-id",
            "assignee-id",
            ("todo-id", "fixing-id"),
        )
        rendered = "\n".join(_plain(widget) for widget in pilot.app.screen.query(Static))
        assert "PRIVATE-CANDIDATE-CAPABILITY" not in rendered

        await pilot.click("#candidate-0")
        await pilot.pause()
        assert controller.mutation_calls == [
            ("start_defect", "PRIVATE-CANDIDATE-CAPABILITY", "defect-1")
        ]
        assert not pilot.app.screen.query("#mapping-key")
        await pilot.click("#mapping-0")
        await pilot.pause()
        await pilot.click("#confirm-start")
        await pilot.pause()

        assert controller.confirmed_mapping == "app-group"
        assert controller.mutation_calls[-1] == (
            "confirm_repository",
            "run-defect-1",
            "app-group",
            1,
        )


@pytest.mark.asyncio
async def test_candidate_query_has_zero_mutation_side_effects() -> None:
    controller = WizardController()
    async with wizard_app_factory(controller).run_test() as pilot:
        await _open_defect_wizard(pilot)
        await _query_defects(pilot)
        assert controller.mutation_calls == []


@pytest.mark.asyncio
async def test_ambiguous_candidate_snapshot_fails_closed_before_start() -> None:
    controller = WizardController()
    controller.fail_query = True
    async with wizard_app_factory(controller).run_test() as pilot:
        await _open_defect_wizard(pilot)
        await _query_defects(pilot)
        notice = _plain(pilot.app.screen.query_one("#wizard-notice"))
        assert notice == "workflow wizard action failed safely"
        assert "LEAK-DUPLICATE" not in notice
        assert not any(
            (button.id or "").startswith("candidate-")
            for button in pilot.app.screen.query(Button)
        )
        assert controller.mutation_calls == []


@pytest.mark.asyncio
async def test_stale_candidate_fails_closed_without_mapping_confirmation() -> None:
    controller = WizardController()
    controller.fail_start = True
    async with wizard_app_factory(controller).run_test() as pilot:
        await _open_defect_wizard(pilot)
        await _query_defects(pilot)
        await pilot.click("#candidate-0")
        await pilot.pause()
        notice = _plain(pilot.app.screen.query_one("#wizard-notice"))
        assert notice == "candidate selection is no longer valid"
        assert "LEAK-STALE" not in notice
        assert not pilot.app.screen.query("#mapping-key")
        assert not any(call[0] == "confirm_repository" for call in controller.mutation_calls)


@pytest.mark.asyncio
async def test_no_authorized_mapping_candidate_fails_closed() -> None:
    controller = WizardController()
    controller.mapping_candidates = ()
    async with wizard_app_factory(controller).run_test() as pilot:
        await _open_defect_wizard(pilot)
        await _query_defects(pilot)
        await pilot.click("#candidate-0")
        await pilot.pause()
        assert _plain(pilot.app.screen.query_one("#wizard-notice")) == (
            "no authorized repository mappings available"
        )
        assert not pilot.app.screen.query("#mapping-key")
        assert not any(
            (button.id or "").startswith("mapping-")
            for button in pilot.app.screen.query(Button)
        )
        assert not any(call[0] == "confirm_repository" for call in controller.mutation_calls)


@pytest.mark.asyncio
async def test_unlisted_mapping_and_authoritative_drift_fail_closed() -> None:
    controller = WizardController()
    async with wizard_app_factory(controller).run_test() as pilot:
        await _open_defect_wizard(pilot)
        await _query_defects(pilot)
        await pilot.click("#candidate-0")
        await pilot.pause()
        screen = pilot.app.screen
        await screen._show_confirmation(99)  # type: ignore[attr-defined]
        assert _plain(screen.query_one("#wizard-notice")) == (
            "repository mapping selection is invalid"
        )
        assert not any(
            call[0] == "confirm_repository" for call in controller.mutation_calls
        )

        await pilot.click("#mapping-0")
        await pilot.pause()
        controller.fail_confirm = True
        await pilot.click("#confirm-start")
        await pilot.pause()
        notice = _plain(pilot.app.screen.query_one("#wizard-notice"))
        assert notice == "workflow wizard action failed safely"
        assert "LEAK-CONFIG-DRIFT" not in notice
        assert controller.mutation_calls[-1] == (
            "confirm_repository",
            "run-defect-1",
            "app-group",
            1,
        )


@pytest.mark.asyncio
async def test_requirement_wizard_reuses_mapping_and_confirmation() -> None:
    controller = WizardController()
    controller.mapping_candidates = (_candidate("group-app", group=True),)
    async with wizard_app_factory(controller).run_test(size=(60, 24)) as pilot:
        await pilot.press("n")
        await pilot.click("#workflow-requirement")
        await pilot.pause()
        pilot.app.screen.query_one("#requirement-id", Input).value = "REQ-42"
        await pilot.click("#start-requirement")
        await pilot.pause()
        summary = _plain(pilot.app.screen.query_one("#mapping-candidate-0"))
        assert "repository-group" in summary
        assert "primary -> dependency" in summary
        assert "local read-only source" in summary
        assert "1 configured lint command" in summary
        assert "1 configured integration test command" in summary
        assert "changes use an isolated managed worktree" in summary
        await pilot.click("#mapping-0")
        await pilot.pause()
        await pilot.click("#confirm-start")
        await pilot.pause()

        assert controller.mutation_calls == [
            ("start_requirement", "REQ-42"),
            ("confirm_repository", "run-requirement-1", "group-app", 1),
        ]


@pytest.mark.asyncio
async def test_wizard_cancel_is_keyboard_reachable_and_has_no_side_effects() -> None:
    controller = WizardController()
    async with wizard_app_factory(controller).run_test(size=(60, 24)) as pilot:
        await pilot.press("n")
        assert pilot.app.screen.id == "workflow-type-screen"
        await pilot.press("escape")
        assert pilot.app.screen.id == "dashboard-screen"
        assert controller.last_query is None
        assert controller.mutation_calls == []
