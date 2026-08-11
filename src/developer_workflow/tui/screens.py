"""Textual screens for the read-only workflow dashboard."""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
    TabbedContent,
    TabPane,
)

from ..contracts import WorkflowState
from .controller import TuiController, TuiControllerError
from .models import DefectChoice, RepositoryView, RunDetail, RunFilter, RunSummary
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
_WIZARD_UNAVAILABLE = "workflow wizard action failed safely"
_CANDIDATE_STALE = "candidate selection is no longer valid"
_MAPPING_REQUIRED = "one repository mapping key is required"
_INPUT_REQUIRED = "required workflow fields are missing"
_NO_CANDIDATES = "no defect candidates available"
_SAFE_MAPPING_KEY = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


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


class _MappingWizardScreen(Screen[RunDetail | None]):
    """Shared mapping, review, and authoritative confirmation stages."""

    STEP_FILTER = 0
    STEP_CANDIDATE = 1
    STEP_MAPPING = 2
    STEP_CONFIRM = 3
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(
        self,
        controller: TuiController,
        supervisor: RunTaskSupervisor,
        *,
        screen_id: str,
    ) -> None:
        super().__init__(id=screen_id)
        self._controller = controller
        self._supervisor = supervisor
        self._preview: RunDetail | None = None
        self._mapping_key = ""
        self._step = self.STEP_FILTER

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="wizard-body"):
            yield from self._initial_widgets()
        yield Static("", id="wizard-notice", markup=False)
        yield Button("Cancel", id="cancel-wizard")

    def _initial_widgets(self) -> tuple[Widget, ...]:
        raise NotImplementedError

    async def _show_mapping(self, preview: RunDetail) -> None:
        if preview.summary.state is not WorkflowState.VALIDATING:
            self._show_notice(_WIZARD_UNAVAILABLE)
            return
        self._preview = preview
        self._step = self.STEP_MAPPING
        self._show_notice("")
        body = self.query_one("#wizard-body", VerticalScroll)
        await body.remove_children()
        await body.mount(
            Label("Repository mapping key"),
            Input(placeholder="configured mapping or group key", id="mapping-key"),
            Button("Review", id="review-mapping", variant="primary"),
        )

    async def _show_confirmation(self) -> None:
        preview = self._preview
        if preview is None:
            self._show_notice(_WIZARD_UNAVAILABLE)
            return
        mapping_key = self.query_one("#mapping-key", Input).value.strip()
        if (
            not _SAFE_MAPPING_KEY.fullmatch(mapping_key)
            or mapping_key in {".", ".."}
        ):
            self._show_notice(_MAPPING_REQUIRED)
            return
        self._mapping_key = mapping_key
        self._step = self.STEP_CONFIRM
        self._show_notice("")
        body = self.query_one("#wizard-body", VerticalScroll)
        await body.remove_children()
        await body.mount(
            Label("Confirm workflow"),
            Static(
                "\n".join(
                    (
                        f"work item: {preview.summary.work_item_id}",
                        f"repository mapping: {mapping_key}",
                        f"state: {preview.summary.state.value}",
                    )
                ),
                id="workflow-summary",
                markup=True,
            ),
            Button("Confirm", id="confirm-start", variant="success"),
        )

    async def _confirm(self) -> None:
        preview = self._preview
        if preview is None or not self._mapping_key:
            self._show_notice(_WIZARD_UNAVAILABLE)
            return
        try:
            detail = await self._supervisor.run_mutation(
                preview.summary.run_id,
                "confirm-repository",
                self._controller.confirm_repository,
                preview.summary.run_id,
                self._mapping_key,
                preview.summary.version,
            )
        except Exception:
            self._show_notice(_WIZARD_UNAVAILABLE)
            return
        self.dismiss(detail)

    def _show_notice(self, message: str) -> None:
        self.query_one("#wizard-notice", Static).update(message)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    async def _handle_mapping_button(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "cancel-wizard":
            self.action_cancel()
        elif button_id == "review-mapping" and self._step == self.STEP_MAPPING:
            await self._show_confirmation()
        elif button_id == "confirm-start" and self._step == self.STEP_CONFIRM:
            await self._confirm()


class DefectWizardScreen(_MappingWizardScreen):
    """Four-stage defect wizard with a read-only candidate query."""

    def __init__(self, controller: TuiController, supervisor: RunTaskSupervisor) -> None:
        super().__init__(
            controller,
            supervisor,
            screen_id="defect-wizard-screen",
        )
        self._candidate_session_id: str | None = None
        self._candidates: tuple[DefectChoice, ...] = ()

    def _initial_widgets(self) -> tuple[Widget, ...]:
        return (
            Label("New defect workflow"),
            Input(placeholder="ONES project ID", id="project"),
            Input(placeholder="ONES iteration ID", id="iteration"),
            Input(placeholder="ONES assignee ID", id="assignee"),
            Input(
                placeholder="ONES status IDs, comma-separated",
                id="status-ids",
            ),
            Button("Query defects", id="query-defects", variant="primary"),
        )

    async def _query(self) -> None:
        project = self.query_one("#project", Input).value.strip()
        iteration = self.query_one("#iteration", Input).value.strip()
        assignee = self.query_one("#assignee", Input).value.strip()
        status_values = self.query_one("#status-ids", Input).value
        status_ids = tuple(
            item.strip() for item in status_values.split(",") if item.strip()
        )
        if not project or not iteration or not assignee:
            self._show_notice(_INPUT_REQUIRED)
            return
        if any(not _SAFE_MAPPING_KEY.fullmatch(item) for item in status_ids):
            self._show_notice(_INPUT_REQUIRED)
            return
        try:
            session = await self._supervisor.run_readonly(
                "query-defects",
                self._controller.query_defects,
                project,
                iteration,
                assignee,
                status_ids,
            )
        except Exception:
            self._show_notice(_WIZARD_UNAVAILABLE)
            return
        if not session.items:
            self._show_notice(_NO_CANDIDATES)
            return
        self._candidate_session_id = session.session_id
        self._candidates = session.items
        self._step = self.STEP_CANDIDATE
        self._show_notice("")
        body = self.query_one("#wizard-body", VerticalScroll)
        await body.remove_children()
        await body.mount(Label("Select one defect"))
        for index, candidate in enumerate(session.items):
            await body.mount(
                Button(
                    "  ".join(
                        (
                            candidate.priority,
                            candidate.status_id or "status unavailable",
                            candidate.title,
                        )
                    ),
                    id=f"candidate-{index}",
                )
            )

    async def _select_candidate(self, index: int) -> None:
        session_id = self._candidate_session_id
        if (
            self._step != self.STEP_CANDIDATE
            or session_id is None
            or not 0 <= index < len(self._candidates)
        ):
            self._show_notice(_CANDIDATE_STALE)
            return
        candidate_id = self._candidates[index].candidate_id
        # Make the UI-side capability one-shot before yielding to background work.
        self._candidate_session_id = None
        try:
            preview = await self._supervisor.run_mutation(
                "new-defect",
                "start-defect",
                self._controller.start_defect,
                session_id,
                candidate_id,
            )
        except Exception:
            self._show_notice(_CANDIDATE_STALE)
            return
        await self._show_mapping(preview)

    @on(Button.Pressed)
    async def _handle_defect_button(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "query-defects" and self._step == self.STEP_FILTER:
            await self._query()
        elif button_id.startswith("candidate-"):
            try:
                index = int(button_id.removeprefix("candidate-"))
            except ValueError:
                self._show_notice(_CANDIDATE_STALE)
                return
            await self._select_candidate(index)


class RequirementWizardScreen(_MappingWizardScreen):
    """Requirement entry followed by the shared mapping confirmation flow."""

    def __init__(self, controller: TuiController, supervisor: RunTaskSupervisor) -> None:
        super().__init__(
            controller,
            supervisor,
            screen_id="requirement-wizard-screen",
        )

    def _initial_widgets(self) -> tuple[Widget, ...]:
        return (
            Label("New requirement workflow"),
            Input(placeholder="ONES requirement ID", id="requirement-id"),
            Button("Continue", id="start-requirement", variant="primary"),
        )

    async def _start_requirement(self) -> None:
        requirement_id = self.query_one("#requirement-id", Input).value.strip()
        if not requirement_id:
            self._show_notice(_INPUT_REQUIRED)
            return
        try:
            preview = await self._supervisor.run_mutation(
                "new-requirement",
                "start-requirement",
                self._controller.start_requirement,
                requirement_id,
            )
        except Exception:
            self._show_notice(_WIZARD_UNAVAILABLE)
            return
        await self._show_mapping(preview)

    @on(Button.Pressed, "#start-requirement")
    async def _handle_requirement_button(self) -> None:
        if self._step == self.STEP_FILTER:
            await self._start_requirement()


class WorkflowTypeScreen(Screen[RunDetail | None]):
    """Choose the only two supported workflow creation paths."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, controller: TuiController, supervisor: RunTaskSupervisor) -> None:
        super().__init__(id="workflow-type-screen")
        self._controller = controller
        self._supervisor = supervisor

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("New workflow")
            yield Button("Defect", id="workflow-defect", variant="primary")
            yield Button("Requirement", id="workflow-requirement")
            yield Button("Cancel", id="cancel-workflow-type")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _wizard_done(self, detail: RunDetail | None) -> None:
        if detail is not None:
            self.dismiss(detail)

    @on(Button.Pressed)
    def _choose(self, event: Button.Pressed) -> None:
        if event.button.id == "workflow-defect":
            self.app.push_screen(
                DefectWizardScreen(self._controller, self._supervisor),
                callback=self._wizard_done,
            )
        elif event.button.id == "workflow-requirement":
            self.app.push_screen(
                RequirementWizardScreen(self._controller, self._supervisor),
                callback=self._wizard_done,
            )
        elif event.button.id == "cancel-workflow-type":
            self.action_cancel()

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
        Binding("n", "new_run", "New run", show=False, priority=True),
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

    def action_new_run(self) -> None:
        self.app.push_screen(
            WorkflowTypeScreen(self._controller, self._supervisor),
            callback=lambda detail: None,
        )

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

    @on(Button.Pressed, "#nav-new-run")
    def new_run(self) -> None:
        self.action_new_run()


__all__ = [
    "DashboardScreen",
    "DefectWizardScreen",
    "NavigationPane",
    "RunDetailPane",
    "RunDetailScreen",
    "RunListPane",
    "RequirementWizardScreen",
    "SettingsView",
    "WorkflowTypeScreen",
]
