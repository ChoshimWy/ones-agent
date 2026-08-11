from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace

import pytest
from textual.widgets import Button, Input, Static, TabbedContent

from src.developer_workflow import tui
from src.developer_workflow.contracts import WorkflowRun, WorkflowState, WorkflowType
from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
from src.developer_workflow.tui.controller import (
    CandidateSessionView,
    StaleTuiActionError,
    TuiControllerError,
)
from src.developer_workflow.tui.models import (
    DangerousActionRequest,
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
from src.developer_workflow.tui.screens import (
    ApprovalModal,
    CancelModal,
    DashboardScreen,
    DefectWizardScreen,
    HelpScreen,
    PublicationResumeModal,
    RunFilterScreen,
    RevisionModal,
    SettingsView,
)


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
        self.filters: list[RunFilter] = []
        self.raw_work_items = {
            item.run_id: item.work_item_id for item in self.runs
        }

    def list_runs(self, filters: RunFilter, activities=None):
        del activities
        self.filters.append(filters)
        return tuple(
            item
            for item in self.runs
            if filters.matches_facts(
                state=item.state,
                workflow_type=item.workflow_type,
                run_id=item.run_id,
                work_item_id=self.raw_work_items.get(
                    item.run_id, item.work_item_id
                ),
                updated_at=item.updated_at,
            )
        )

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
async def test_app_close_is_idempotent() -> None:
    app = app_factory()
    closes: list[object] = []

    class Supervisor:
        async def close(self) -> None:
            closes.append(self)

    app.supervisor = Supervisor()  # type: ignore[assignment]

    await app._close_ui()
    await app._close_ui()

    assert len(closes) == 1


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
        controller = app.controller
        async with asyncio.timeout(2):
            while not (
                controller.shown  # type: ignore[attr-defined]
                and controller.shown[-1] == "run-2"  # type: ignore[attr-defined]
                and "BUG-2" in _plain(app.screen.query_one("#overview-content"))
            ):
                await asyncio.sleep(0)
        await pilot.click("#nav-settings")
        assert app.screen.query_one("#settings").display


@pytest.mark.asyncio
async def test_help_is_dashboard_only_and_escape_returns() -> None:
    app = app_factory()
    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.press("?")
        assert isinstance(app.screen, HelpScreen)
        assert len(app.query("#help-screen")) == 1
        help_text = _plain(app.screen.query_one("#help-content"))
        for key in ("n", "r", "v", "a", "x", "q", "/", "f"):
            assert key in help_text
        assert "read-only" in help_text
        await pilot.press("escape")
        assert isinstance(app.screen, DashboardScreen)

        await pilot.press("n")
        wizard = app.screen
        await pilot.press("?")
        assert app.screen is wizard


@pytest.mark.asyncio
async def test_search_and_filter_apply_clear_and_escape_without_mutation() -> None:
    app = app_factory()
    controller = app.controller
    async with app.run_test(size=(60, 32)) as pilot:
        await pilot.press("/")
        assert isinstance(app.screen, RunFilterScreen)
        app.screen.query_one("#work-item-query", Input).value = "BUG-2"
        await pilot.click("#apply-run-filter")
        assert isinstance(app.screen, DashboardScreen)
        assert controller.filters[-1].query == "BUG-2"  # type: ignore[attr-defined]
        assert len(app.screen._runs) == 1

        await pilot.press("f")
        assert isinstance(app.screen, RunFilterScreen)
        app.screen.query_one("#filter-states", Input).value = "WAITING_APPROVAL"
        app.screen.query_one("#filter-types", Input).value = "defect"
        app.screen.query_one("#updated-after", Input).value = (
            "2026-08-11T08:00:00+00:00"
        )
        app.screen.query_one("#updated-before", Input).value = (
            "2026-08-11T10:00:00+00:00"
        )
        await pilot.click("#apply-run-filter")
        applied = controller.filters[-1]  # type: ignore[attr-defined]
        assert applied.states == (WorkflowState.WAITING_APPROVAL,)
        assert applied.workflow_types == (WorkflowType.DEFECT,)
        assert applied.query == "BUG-2"

        await pilot.press("f")
        app.screen.query_one("#filter-states", Input).value = "BROKEN"
        await pilot.press("escape")
        assert controller.filters[-1] == applied  # type: ignore[attr-defined]
        await pilot.press("f")
        await pilot.click("#clear-run-filter")
        async with asyncio.timeout(2):
            while not (
                controller.filters[-1] == RunFilter()  # type: ignore[attr-defined]
                and len(app.screen._runs) == 2
            ):
                await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_search_preserves_literal_rich_brackets_and_backslashes() -> None:
    app = app_factory()
    controller = app.controller
    raw_work_item = r"BUG-[bold]\\literal"
    summary = replace(
        controller.runs[1],  # type: ignore[attr-defined]
        work_item_id=safe_tui_text(raw_work_item),
    )
    controller.runs = (summary,)  # type: ignore[attr-defined]
    controller.raw_work_items = {  # type: ignore[attr-defined]
        summary.run_id: raw_work_item
    }
    literal_query = r"[bold]\\literal"

    async with app.run_test(size=(80, 32)) as pilot:
        await pilot.press("/")
        app.screen.query_one("#work-item-query", Input).value = literal_query
        await pilot.click("#apply-run-filter")
        assert controller.filters[-1].query == literal_query  # type: ignore[attr-defined]
        assert len(app.screen._runs) == 1
        assert raw_work_item in _plain(app.screen.query_one("#run-item-0 Label"))

        await pilot.press("/")
        assert app.screen.query_one("#work-item-query", Input).value == literal_query
        await pilot.press("escape", "/")
        app.screen.query_one("#work-item-query", Input).value = "bad\x1bvalue"
        await pilot.click("#apply-run-filter")
        assert isinstance(app.screen, RunFilterScreen)
        assert _plain(app.screen.query_one("#run-filter-notice")) == (
            "run filter is invalid"
        )


@pytest.mark.asyncio
async def test_navigation_defects_opens_defect_wizard_without_starting_work() -> None:
    app = app_factory()
    async with app.run_test(size=(120, 32)) as pilot:
        assert app.supervisor.task_count == 0
        await pilot.click("#nav-defects")
        assert isinstance(app.screen, DefectWizardScreen)
        assert app.supervisor.task_count == 0


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
async def test_blocked_poll_never_overwrites_concurrent_mouse_selection() -> None:
    app = app_factory()
    controller = app.controller  # type: ignore[attr-defined]
    original_list = controller.list_runs
    gates: list[tuple[Event, Event]] = []

    def blocked_list(filters: RunFilter, activities=None):
        if gates:
            started, release = gates.pop(0)
            started.set()
            assert release.wait(2)
        return original_list(filters, activities)

    controller.list_runs = blocked_list  # type: ignore[method-assign]
    async with app.run_test(size=(120, 32)) as pilot:
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        for _ in range(20):
            screen.action_cursor_up()
            await pilot.pause()
            assert screen.query_one("#run-list").index == 0
            started, release = Event(), Event()
            gates.append((started, release))
            refresh = asyncio.create_task(screen.refresh_runs())
            assert await asyncio.to_thread(started.wait, 2)

            current_item = screen.query_one("#run-item-1")
            await screen.click_run(  # type: ignore[arg-type]
                SimpleNamespace(widget=current_item)
            )
            shown_after_click = len(controller.shown)
            release.set()
            await refresh

            assert screen.query_one("#run-list").index == 1
            assert controller.shown[-1] == "run-2"
            assert "run-1" not in controller.shown[shown_after_click:]


@pytest.mark.asyncio
async def test_stale_mouse_item_never_selects_new_run_at_reused_index() -> None:
    app = app_factory()
    async with app.run_test(size=(120, 32)):
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        stale_item = screen.query_one("#run-item-1")
        assert stale_item.name == "run-2"

        app.controller.runs = (_summary(3), _summary(4))  # type: ignore[attr-defined]
        await screen.refresh_runs()
        shown_before = tuple(app.controller.shown)  # type: ignore[attr-defined]

        await screen.click_run(SimpleNamespace(widget=stale_item))  # type: ignore[arg-type]

        assert tuple(app.controller.shown) == shown_before  # type: ignore[attr-defined]
        assert screen.query_one("#run-list").index == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [None, "run-3"])
async def test_mouse_item_requires_one_unique_current_run_identity(
    identity: str | None,
) -> None:
    app = app_factory()
    async with app.run_test(size=(120, 32)):
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        app.controller.runs = (_summary(3), _summary(3))  # type: ignore[attr-defined]
        await screen.refresh_runs()
        shown_before = tuple(app.controller.shown)  # type: ignore[attr-defined]
        event = SimpleNamespace(
            widget=SimpleNamespace(name=identity, id="run-item-0")
        )

        await screen.click_run(event)  # type: ignore[arg-type]

        assert tuple(app.controller.shown) == shown_before  # type: ignore[attr-defined]


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


def _action_repository(key: str, role: str, suffix: str) -> RepositoryView:
    return RepositoryView(
        key=key,
        role=role,
        base_commit=suffix * 40,
        head_commit=suffix.upper() * 40,
        tree_hash=suffix * 64,
        changed_files=(f"src/{key}.py",),
        changed_file_count=1,
        commit_hash="",
        pushed=False,
        pr_url="",
        error="",
        test_summary="1 verified test fact",
        pr_target="main",
    )


class ActionController(FakeController):
    def __init__(
        self,
        state: WorkflowState = WorkflowState.WAITING_APPROVAL,
        *,
        resume_state: WorkflowState | None = None,
    ) -> None:
        super().__init__()
        self.state = state
        self.resume_state = resume_state
        self.version = 7
        self.action_calls: list[tuple[object, ...]] = []
        self.remote_effects: list[str] = []
        self.repositories = (
            _action_repository("primary", "primary", "a"),
            _action_repository("dependency", "dependency", "b"),
        )

    def _summary(self) -> RunSummary:
        return RunSummary(
            run_id="run-action",
            workflow_type=WorkflowType.DEFECT,
            work_item_id="BUG-42",
            state=self.state,
            version=self.version,
            updated_at=NOW,
            activity=RunActivity.IDLE,
        )

    def _detail(self) -> RunDetail:
        return RunDetail(
            summary=self._summary(),
            repositories=self.repositories,
            tests=(TuiTestView("test command", "passed", 0),) * 2,
            review=("review recorded",),
            publication=PublicationView(
                repositories=self.repositories,
                comment_id="",
                error=(
                    "publication failed safely"
                    if self.state is WorkflowState.PARTIAL_SUCCESS
                    else ""
                ),
            ),
            history=(),
            blocked_reason=(
                "workflow blocked safely"
                if self.state is WorkflowState.BLOCKED
                else ""
            ),
            fingerprint="f" * 64,
            risk_count=2,
            unresolved_count=1,
            resume_state=self.resume_state,
        )

    def list_runs(self, filters: RunFilter, activities=None):
        del filters, activities
        return (self._summary(),)

    def show(self, run_id: str) -> RunDetail:
        assert run_id == "run-action"
        return self._detail()

    def prepare_action(self, run_id: str, action: str) -> DangerousActionRequest:
        assert run_id == "run-action"
        detail = self._detail()
        return DangerousActionRequest(
            run_id=run_id,
            version=detail.summary.version,
            action=action,
            fingerprint=detail.fingerprint,
            work_item_id=detail.summary.work_item_id,
            repositories=detail.repositories,
            changed_file_count=2,
            test_count=2,
            risk_count=detail.risk_count,
            unresolved_count=detail.unresolved_count,
            comment_status="not delivered",
            publication_error=detail.publication.error,
            state=detail.summary.state,
            resume_state=detail.resume_state,
        )

    def _current(self, version: int) -> None:
        if version != self.version:
            raise StaleTuiActionError("LEAK-STALE-BODY")

    def approve(self, request: DangerousActionRequest, actor: str) -> RunDetail:
        self._current(request.version)
        self.action_calls.append(("approve", request.version, actor))
        self.remote_effects.extend(("commit", "push", "pr", "comment"))
        return self._detail()

    def revise(
        self, request: DangerousActionRequest, feedback: str, scope: str | None
    ) -> RunDetail:
        self._current(request.version)
        self.action_calls.append(("revise", request.version, feedback, scope))
        return self._detail()

    def cancel(self, request: DangerousActionRequest, actor: str) -> RunDetail:
        self._current(request.version)
        self.action_calls.append(("cancel", request.version, actor))
        return self._detail()

    def resume_publication(self, request: DangerousActionRequest) -> RunDetail:
        self._current(request.version)
        self.action_calls.append(("resume-publication", request.version))
        return self._detail()

    def resume(self, run_id: str, expected_version: int) -> RunDetail:
        self._current(expected_version)
        self.action_calls.append(("resume", run_id, expected_version))
        return self._detail()

    def advance_authoritative_version(self) -> None:
        self.version += 1


def action_app_factory(controller: ActionController) -> DeveloperWorkflowTuiApp:
    return DeveloperWorkflowTuiApp(controller, 3)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_approval_modal_renders_complete_signed_multi_repository_facts() -> None:
    controller = ActionController()
    async with action_app_factory(controller).run_test(size=(120, 32)) as pilot:
        await pilot.press("a")
        assert isinstance(pilot.app.screen, ApprovalModal)
        assert _plain(pilot.app.screen.query_one("#fingerprint")) == "f" * 64
        rendered = "\n".join(_plain(item) for item in pilot.app.screen.query(Static))
        assert "work item: BUG-42" in rendered
        assert "repositories: 2" in rendered
        assert "changed files: 2" in rendered
        assert "tests: 2" in rendered
        assert "risks: 2" in rendered
        assert "unresolved: 1" in rendered
        assert [
            _plain(item) for item in pilot.app.screen.query(".tree-hash")
        ] == ["a" * 64, "b" * 64]
        assert "base: " + "a" * 40 in rendered
        assert "head: " + "A" * 40 in rendered
        assert "1 verified test fact" in rendered
        assert "PR target: main" in rendered
        assert "commit: not created" in rendered
        assert "push: not completed" in rendered
        assert "PR: not created" in rendered
        assert "comment: not delivered" in rendered
        assert controller.remote_effects == []


@pytest.mark.asyncio
async def test_plain_enter_never_confirms_dangerous_actions_and_escape_returns() -> None:
    controller = ActionController()
    async with action_app_factory(controller).run_test() as pilot:
        await pilot.press("x")
        assert isinstance(pilot.app.screen, CancelModal)
        pilot.app.screen.query_one("#actor", Input).value = "operator"
        await pilot.press("enter")
        assert controller.action_calls == []
        await pilot.press("escape")
        assert pilot.app.screen.id == "dashboard-screen"


@pytest.mark.asyncio
async def test_actor_input_rejects_bidi_controls_with_a_fixed_error() -> None:
    controller = ActionController()
    async with action_app_factory(controller).run_test() as pilot:
        await pilot.press("x")
        pilot.app.screen.query_one("#actor", Input).value = "operator\u202esecret"
        await pilot.press("ctrl+enter")
        notice = _plain(pilot.app.screen.query_one("#modal-notice"))
        assert notice == "required action fields are missing"
        assert "secret" not in notice
        assert controller.action_calls == []


@pytest.mark.asyncio
async def test_approval_stale_confirmation_is_fixed_and_has_zero_effects() -> None:
    controller = ActionController()
    async with action_app_factory(controller).run_test() as pilot:
        await pilot.press("a")
        controller.advance_authoritative_version()
        pilot.app.screen.query_one("#actor", Input).value = "operator"
        await pilot.click("#confirm-approve")
        await pilot.pause(0.1)
        assert pilot.app.screen.id == "dashboard-screen"
        assert _plain(pilot.app.screen.query_one("#notice")) == (
            "workflow changed; review again"
        )
        assert controller.action_calls == []
        assert controller.remote_effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["revise", "cancel", "resume-publication"])
async def test_every_dangerous_action_fails_closed_after_authoritative_drift(
    action: str,
) -> None:
    controller = ActionController(
        WorkflowState.PARTIAL_SUCCESS
        if action == "resume-publication"
        else WorkflowState.WAITING_APPROVAL
    )
    async with action_app_factory(controller).run_test() as pilot:
        await pilot.press(
            {"revise": "v", "cancel": "x", "resume-publication": "r"}[action]
        )
        controller.advance_authoritative_version()
        if action == "revise":
            pilot.app.screen.query_one("#feedback", Input).value = "fix lifecycle"
            pilot.app.screen.query_one("#scope", Input).value = "repair"
        elif action == "cancel":
            pilot.app.screen.query_one("#actor", Input).value = "operator"
        await pilot.press("ctrl+enter")
        await pilot.pause()
        assert pilot.app.screen.id == "dashboard-screen"
        assert _plain(pilot.app.screen.query_one("#notice")) == (
            "workflow changed; review again"
        )
        assert controller.action_calls == []
        assert controller.remote_effects == []


@pytest.mark.asyncio
async def test_approval_requires_actor_and_mouse_confirmation() -> None:
    controller = ActionController()
    async with action_app_factory(controller).run_test() as pilot:
        await pilot.press("a")
        await pilot.click("#confirm-approve")
        assert _plain(pilot.app.screen.query_one("#modal-notice")) == (
            "required action fields are missing"
        )
        await pilot.press("escape", "a")
        pilot.app.screen.query_one("#actor", Input).value = "operator"
        await pilot.click("#confirm-approve")
        await pilot.pause(0.1)
        assert controller.action_calls == [("approve", 7, "operator")]


@pytest.mark.asyncio
async def test_revision_and_cancel_validate_inputs_and_use_supervisor_controller_path() -> None:
    controller = ActionController()
    async with action_app_factory(controller).run_test() as pilot:
        await pilot.press("v")
        assert isinstance(pilot.app.screen, RevisionModal)
        pilot.app.screen.query_one("#feedback", Input).value = "fix lifecycle"
        pilot.app.screen.query_one("#scope", Input).value = "repair"
        await pilot.click("#confirm-revise")
        await pilot.pause()
        assert controller.action_calls == [("revise", 7, "fix lifecycle", "repair")]

        await pilot.press("x")
        pilot.app.screen.query_one("#actor", Input).value = "operator"
        await pilot.press("ctrl+enter")
        await pilot.pause()
        assert controller.action_calls[-1] == ("cancel", 7, "operator")


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_state", [None, WorkflowState.PUBLISHING])
async def test_blocked_publication_or_unknown_checkpoint_cannot_be_revised(
    resume_state: WorkflowState | None,
) -> None:
    controller = ActionController(
        WorkflowState.BLOCKED, resume_state=resume_state
    )
    async with action_app_factory(controller).run_test() as pilot:
        await pilot.press("v")
        assert pilot.app.screen.id == "dashboard-screen"
        assert _plain(pilot.app.screen.query_one("#notice")) == (
            "workflow action is unavailable"
        )
        assert controller.action_calls == []


@pytest.mark.asyncio
async def test_ordinary_resume_has_no_modal_and_passes_expected_version() -> None:
    controller = ActionController(
        WorkflowState.BLOCKED, resume_state=WorkflowState.IMPLEMENTING
    )
    async with action_app_factory(controller).run_test() as pilot:
        assert "resume state: IMPLEMENTING" in _plain(
            pilot.app.screen.query_one("#overview-content")
        )
        await pilot.press("r")
        await pilot.pause()
        assert pilot.app.screen.id == "dashboard-screen"
        assert controller.action_calls == [("resume", "run-action", 7)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "resume_state"),
    [
        (WorkflowState.PARTIAL_SUCCESS, None),
        (WorkflowState.PUBLISHING, None),
        (WorkflowState.BLOCKED, WorkflowState.PUBLISHING),
    ],
)
async def test_publication_resume_uses_dedicated_modal_with_per_repository_facts(
    state: WorkflowState, resume_state: WorkflowState | None
) -> None:
    controller = ActionController(state, resume_state=resume_state)
    async with action_app_factory(controller).run_test(size=(60, 26)) as pilot:
        await pilot.click("#action-resume")
        await pilot.pause(0.1)
        assert isinstance(pilot.app.screen, PublicationResumeModal)
        rendered = "\n".join(_plain(item) for item in pilot.app.screen.query(Static))
        assert "primary" in rendered and "dependency" in rendered
        assert "commit: not created" in rendered
        assert "push: not completed" in rendered
        assert "PR: not created" in rendered
        assert "publication failed safely" in rendered or state is not WorkflowState.PARTIAL_SUCCESS
        await pilot.press("enter")
        assert controller.action_calls == []
        await pilot.press("ctrl+enter")
        await pilot.pause()
        assert controller.action_calls == [("resume-publication", 7)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [WorkflowState.COMPLETED, WorkflowState.CANCELLED, WorkflowState.FAILED],
)
async def test_terminal_states_reject_all_mutating_actions(state: WorkflowState) -> None:
    controller = ActionController(state)
    async with action_app_factory(controller).run_test() as pilot:
        for key in ("r", "v", "a", "x"):
            await pilot.press(key)
        assert pilot.app.screen.id == "dashboard-screen"
        assert _plain(pilot.app.screen.query_one("#notice")) == (
            "workflow action is unavailable"
        )
        assert controller.action_calls == []
