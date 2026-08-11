from __future__ import annotations

from datetime import UTC, datetime

import pytest
from textual.widgets import Input, TabbedContent

from src.developer_workflow import tui
from src.developer_workflow.contracts import WorkflowState, WorkflowType
from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
from src.developer_workflow.tui.models import (
    HistoryView,
    PublicationView,
    RepositoryView,
    RunActivity,
    RunDetail,
    RunFilter,
    RunSummary,
    TestView as TuiTestView,
)
from src.developer_workflow.tui.screens import DashboardScreen, SettingsView


NOW = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)


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
