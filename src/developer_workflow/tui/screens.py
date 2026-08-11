"""Textual screens for the read-only workflow dashboard."""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Label,
    ListItem,
    ListView,
    Static,
    TabbedContent,
    TabPane,
)

from .controller import TuiController, TuiControllerError
from .models import RepositoryView, RunDetail, RunFilter, RunSummary
from .supervisor import RunTaskSupervisor


_DETAIL_TABS = (
    "overview",
    "repositories",
    "tests",
    "review",
    "publication",
    "history",
)
_STORAGE_CORRUPTED = "workflow storage is corrupted safely"
_LIST_UNAVAILABLE = "workflow list is unavailable safely"
_DISPLAY_UNAVAILABLE = "workflow display is unavailable safely"


@dataclass(frozen=True, slots=True)
class SettingsView:
    """A deliberately small, credential-free settings projection."""

    max_concurrency: int
    provider_type: str
    sandbox_configured: bool
    root_labels: tuple[str, str, str] = field(
        default=(
            "private run root",
            "private mirror root",
            "managed worktree root",
        ),
        init=False,
    )

    def __post_init__(self) -> None:
        if type(self.max_concurrency) is not int or not 1 <= self.max_concurrency <= 8:
            raise ValueError("max_concurrency must be between 1 and 8")
        if self.provider_type not in {"github", "gitlab", "local_fake", "configured"}:
            raise ValueError("provider type is invalid")
        if type(self.sandbox_configured) is not bool:
            raise ValueError("sandbox configuration state is invalid")

    def display_text(self) -> str:
        roots = ", ".join(self.root_labels)
        sandbox = "configured" if self.sandbox_configured else "not configured"
        return (
            f"max concurrency: {self.max_concurrency}\n"
            f"storage labels: {roots}\n"
            f"provider: {self.provider_type}\n"
            f"sandbox profile: {sandbox}"
        )


class NavigationPane(Vertical):
    """Mouse and keyboard reachable top-level destinations."""

    def compose(self) -> ComposeResult:
        yield Button("Runs", id="nav-runs", variant="primary")
        yield Button("Defects", id="nav-defects")
        yield Button("New Run", id="nav-new-run")
        yield Button("Settings", id="nav-settings")


class RunListPane(Vertical):
    """Selectable workflow summaries."""

    def compose(self) -> ComposeResult:
        yield Label("Runs", classes="pane-title")
        yield ListView(id="run-list")

    async def replace_runs(self, runs: tuple[RunSummary, ...]) -> None:
        run_list = self.query_one("#run-list", ListView)
        await run_list.clear()
        await run_list.extend(
            [
                ListItem(
                    Label(
                        "  ".join(
                            (
                                item.state.value,
                                item.work_item_id,
                                item.activity.value,
                            )
                        ),
                        markup=True,
                    ),
                    id=f"run-item-{index}",
                )
                for index, item in enumerate(runs)
            ]
        )
        if runs:
            run_list.index = 0


class RunDetailPane(Vertical):
    """Six fixed evidence tabs backed only by safe view-model fields."""

    def compose(self) -> ComposeResult:
        yield Label("Run detail", classes="pane-title")
        with TabbedContent(initial="overview", id="detail-tabs"):
            with TabPane("Overview", id="overview"):
                yield Static("No run selected", id="overview-content", markup=True)
            with TabPane("Repositories", id="repositories"):
                yield Static(
                    "No repository evidence",
                    id="repositories-content",
                    markup=True,
                )
            with TabPane("Tests", id="tests"):
                yield Static("No test evidence", id="tests-content", markup=True)
            with TabPane("Review", id="review"):
                yield Static("No review evidence", id="review-content", markup=True)
            with TabPane("Publication", id="publication"):
                yield Static(
                    "No publication evidence",
                    id="publication-content",
                    markup=True,
                )
            with TabPane("History", id="history"):
                yield Static("No history", id="history-content", markup=True)

    def set_detail(self, detail: RunDetail) -> None:
        summary = detail.summary
        self.query_one("#overview-content", Static).update(
            "\n".join(
                (
                    summary.work_item_id,
                    summary.state.value,
                    f"version: {summary.version}",
                    f"fingerprint: {detail.fingerprint or 'not available'}",
                    f"risks: {detail.risk_count}",
                    f"unresolved: {detail.unresolved_count}",
                    f"blocked: {detail.blocked_reason or 'no'}",
                )
            )
        )
        repository_lines = tuple(
            _repository_text(item)
            for item in detail.repositories
        )
        test_lines = tuple(
            f"{item.command}  {item.outcome}  exit: {item.exit_code}"
            for item in detail.tests
        )
        self.query_one("#repositories-content", Static).update(
            "\n".join(repository_lines) or "No repository evidence"
        )
        self.query_one("#tests-content", Static).update(
            "\n".join(test_lines) or "No test evidence"
        )
        self.query_one("#review-content", Static).update(
            "\n".join(detail.review) or "No review evidence"
        )
        publication = detail.publication
        publication_text = "\n\n".join(
            _repository_text(item) for item in publication.repositories
        )
        publication_status = publication.error or (
            "comment delivered" if publication.comment_id else "Not published"
        )
        self.query_one("#publication-content", Static).update(
            "\n\n".join(item for item in (publication_text, publication_status) if item)
        )
        self.query_one("#history-content", Static).update(
            "\n".join(
                f"{item.source} -> {item.target}  {item.occurred_at.isoformat()}"
                for item in detail.history
            )
            or "No history"
        )

    def clear_detail(self) -> None:
        self.query_one("#overview-content", Static).update("No run selected")
        self.query_one("#repositories-content", Static).update(
            "No repository evidence"
        )
        self.query_one("#tests-content", Static).update("No test evidence")
        self.query_one("#review-content", Static).update("No review evidence")
        self.query_one("#publication-content", Static).update(
            "No publication evidence"
        )
        self.query_one("#history-content", Static).update("No history")

    def show_error(self, message: str) -> None:
        self.clear_detail()
        self.query_one("#overview-content", Static).update(message)

    def next_tab(self) -> None:
        tabs = self.query_one("#detail-tabs", TabbedContent)
        index = _DETAIL_TABS.index(tabs.active)
        tabs.active = _DETAIL_TABS[(index + 1) % len(_DETAIL_TABS)]

    def previous_tab(self) -> None:
        tabs = self.query_one("#detail-tabs", TabbedContent)
        index = _DETAIL_TABS.index(tabs.active)
        tabs.active = _DETAIL_TABS[(index - 1) % len(_DETAIL_TABS)]


def _repository_text(item: RepositoryView) -> str:
    changed_files = ", ".join(item.changed_files) or "none"
    return "\n".join(
        (
            f"{item.key}  {item.role}",
            f"base: {item.base_commit or 'not available'}",
            f"head: {item.head_commit or 'not available'}",
            f"tree: {item.tree_hash or 'not available'}",
            f"files ({item.changed_file_count}): {changed_files}",
            f"commit: {item.commit_hash or 'not available'}",
            f"pushed: {'yes' if item.pushed else 'no'}",
            f"PR: {item.pr_url or 'not available'}",
            f"error: {item.error or 'none'}",
        )
    )


class RunDetailScreen(Screen[None]):
    """Independent detail page used by one-column terminals."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("left", "back", "Back", show=False),
        Binding("tab", "next_tab", "Next tab", show=False, priority=True),
        Binding("shift+tab", "previous_tab", "Previous tab", show=False, priority=True),
    ]

    def __init__(self, detail: RunDetail | None, *, error: str = "") -> None:
        super().__init__(id="run-detail-screen")
        self._detail = detail
        self._error = error

    def compose(self) -> ComposeResult:
        yield RunDetailPane(id="run-detail")

    def on_mount(self) -> None:
        pane = self.query_one(RunDetailPane)
        if self._detail is not None:
            pane.set_detail(self._detail)
        else:
            pane.show_error(self._error or _DISPLAY_UNAVAILABLE)

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_next_tab(self) -> None:
        self.query_one(RunDetailPane).next_tab()

    def action_previous_tab(self) -> None:
        self.query_one(RunDetailPane).previous_tab()

class DashboardScreen(Screen[None]):
    """Responsive three/two/one-column workflow dashboard."""

    BINDINGS = [
        Binding("j", "cursor_down", "Next run", show=False, priority=True),
        Binding("down", "cursor_down", "Next run", show=False, priority=True),
        Binding("k", "cursor_up", "Previous run", show=False, priority=True),
        Binding("up", "cursor_up", "Previous run", show=False, priority=True),
        Binding("enter", "open_run", "Open run", show=False, priority=True),
        Binding("tab", "next_tab", "Next tab", show=False, priority=True),
        Binding("shift+tab", "previous_tab", "Previous tab", show=False, priority=True),
        Binding("s", "show_settings", "Settings", show=False, priority=True),
        Binding("g", "show_runs", "Runs", show=False, priority=True),
    ]

    def __init__(
        self,
        controller: TuiController,
        supervisor: RunTaskSupervisor,
        settings: SettingsView,
    ) -> None:
        super().__init__(id="dashboard-screen")
        self._controller = controller
        self._supervisor = supervisor
        self._settings = settings
        self._runs: tuple[RunSummary, ...] = ()
        self._detail_error = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="dashboard", classes="three"):
            yield NavigationPane(id="navigation")
            with Horizontal(id="workspace"):
                yield RunListPane(id="run-list-pane")
                yield RunDetailPane(id="run-detail")
            yield Static(
                Text(self._settings.display_text()),
                id="settings",
                markup=False,
            )

    async def on_mount(self) -> None:
        self._set_mode(self.size.width)
        await self.refresh_runs()

    def on_resize(self, event: events.Resize) -> None:
        self._set_mode(event.size.width)

    def _set_mode(self, width: int) -> None:
        dashboard = self.query_one("#dashboard")
        dashboard.remove_class("three", "two", "one")
        mode = "three" if width >= 100 else "two" if width >= 70 else "one"
        dashboard.add_class(mode)

    async def refresh_runs(self) -> None:
        selected_index = self._selected_index()
        selected_run_id = (
            self._runs[selected_index].run_id
            if selected_index is not None
            and 0 <= selected_index < len(self._runs)
            else None
        )
        try:
            runs = self._controller.list_runs(RunFilter())
        except TuiControllerError:
            runs = ()
            await self.query_one(RunListPane).replace_runs(runs)
            self._runs = runs
            self._detail_error = _LIST_UNAVAILABLE
            self.query_one(RunDetailPane).show_error(self._detail_error)
            return
        await self.query_one(RunListPane).replace_runs(runs)
        self._runs = runs
        if not runs:
            self._detail_error = ""
            self.query_one(RunDetailPane).clear_detail()
            return
        target = next(
            (
                index
                for index, item in enumerate(runs)
                if item.run_id == selected_run_id
            ),
            0,
        )
        self.query_one("#run-list", ListView).index = target
        self._show_detail(target)

    def _selected_index(self) -> int | None:
        return self.query_one("#run-list", ListView).index

    def _show_detail(self, index: int) -> RunDetail | None:
        summary = self._runs[index]
        if summary.corrupted:
            self._detail_error = _STORAGE_CORRUPTED
            self.query_one(RunDetailPane).show_error(self._detail_error)
            return None
        try:
            detail = self._controller.show(summary.run_id)
        except TuiControllerError:
            self._detail_error = _DISPLAY_UNAVAILABLE
            self.query_one(RunDetailPane).show_error(self._detail_error)
            return None
        self._detail_error = ""
        self.query_one(RunDetailPane).set_detail(detail)
        return detail

    def action_cursor_down(self) -> None:
        self.query_one("#run-list", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#run-list", ListView).action_cursor_up()

    def action_open_run(self) -> None:
        index = self._selected_index()
        if index is None or not 0 <= index < len(self._runs):
            return
        detail = self._show_detail(index)
        if self.query_one("#dashboard").has_class("one"):
            self.app.push_screen(
                RunDetailScreen(detail, error=self._detail_error)
            )

    def action_next_tab(self) -> None:
        self.query_one(RunDetailPane).next_tab()

    def action_previous_tab(self) -> None:
        self.query_one(RunDetailPane).previous_tab()

    def action_show_settings(self) -> None:
        self.query_one("#workspace").display = False
        self.query_one("#settings").display = True

    def action_show_runs(self) -> None:
        self.query_one("#settings").display = False
        self.query_one("#workspace").display = True

    @on(ListView.Selected, "#run-list")
    def select_run(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is not None and 0 <= index < len(self._runs):
            self._show_detail(index)

    @on(ListView.Highlighted, "#run-list")
    def highlight_run(self, event: ListView.Highlighted) -> None:
        index = event.list_view.index
        if index is not None and 0 <= index < len(self._runs):
            self._show_detail(index)

    @on(events.Click, "#run-list ListItem")
    def click_run(self, event: events.Click) -> None:
        item_id = event.widget.id
        if item_id is None or not item_id.startswith("run-item-"):
            return
        try:
            index = int(item_id.removeprefix("run-item-"))
        except ValueError:
            return
        if 0 <= index < len(self._runs):
            self.query_one("#run-list", ListView).index = index
            self._show_detail(index)

    @on(Button.Pressed, "#nav-runs")
    def show_runs(self) -> None:
        self.action_show_runs()

    @on(Button.Pressed, "#nav-settings")
    def show_settings(self) -> None:
        self.action_show_settings()


__all__ = [
    "DashboardScreen",
    "NavigationPane",
    "RunDetailPane",
    "RunDetailScreen",
    "RunListPane",
    "SettingsView",
]
