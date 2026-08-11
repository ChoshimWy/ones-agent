"""Textual screens for the read-only workflow dashboard."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import re
from typing import Literal
import unicodedata

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
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
from .controller import StaleTuiActionError, TuiController, TuiControllerError
from .models import (
    DangerousActionRequest,
    DefectChoice,
    MappingCandidateView,
    RepositoryView,
    RunActivity,
    RunDetail,
    RunFilter,
    RunSummary,
)
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
_MAPPING_REQUIRED = "repository mapping selection is invalid"
_NO_MAPPINGS = "no authorized repository mappings available"
_INPUT_REQUIRED = "required workflow fields are missing"
_NO_CANDIDATES = "no defect candidates available"
_ACTION_UNAVAILABLE = "workflow action is unavailable"
_ACTION_FAILED = "workflow action failed safely"
_ACTION_STALE = "workflow changed; review again"
_ACTION_INPUT_REQUIRED = "required action fields are missing"
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
                    name=item.run_id,
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
                    "resume state: "
                    + (
                        detail.resume_state.value
                        if detail.resume_state is not None
                        else "not available"
                    ),
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


def _mapping_candidate_text(candidate: MappingCandidateView) -> str:
    topology = " -> ".join(item.key for item in candidate.repositories)
    lines = [
        f"{candidate.key}  {candidate.kind}",
        f"primary: {candidate.primary_repository}",
        f"topology: {topology}",
    ]
    for repository in candidate.repositories:
        dependencies = ", ".join(repository.depends_on) or "none"
        allowed_paths = ", ".join(repository.allowed_paths) or "none configured"
        lines.extend(
            (
                f"repository: {repository.key}  role: {repository.role}",
                f"source: {repository.source}",
                f"depends on: {dependencies}",
                repository.lint_summary,
                repository.build_summary,
                repository.test_summary,
                f"allowed paths: {allowed_paths}",
                f"side effects: {repository.side_effects}",
            )
        )
    lines.append(candidate.integration_test_summary)
    return "\n".join(lines)


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
        self._mapping_candidates: tuple[MappingCandidateView, ...] = ()
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
        self._mapping_candidates = preview.mapping_candidates
        self._step = self.STEP_MAPPING
        body = self.query_one("#wizard-body", VerticalScroll)
        await body.remove_children()
        if not self._mapping_candidates:
            self._show_notice(_NO_MAPPINGS)
            await body.mount(Label("No authorized repository mappings"))
            return
        self._show_notice("")
        await body.mount(Label("Select an authorized repository mapping"))
        for index, candidate in enumerate(self._mapping_candidates):
            await body.mount(
                Button(
                    f"Select {candidate.key}",
                    id=f"mapping-{index}",
                    variant="primary",
                ),
                Static(
                    _mapping_candidate_text(candidate),
                    id=f"mapping-candidate-{index}",
                    markup=True,
                ),
            )

    async def _show_confirmation(self, index: int) -> None:
        preview = self._preview
        if (
            preview is None
            or self._step != self.STEP_MAPPING
            or not 0 <= index < len(self._mapping_candidates)
        ):
            self._show_notice(_MAPPING_REQUIRED)
            return
        candidate = self._mapping_candidates[index]
        self._mapping_key = candidate.key
        self._step = self.STEP_CONFIRM
        self._show_notice("")
        body = self.query_one("#wizard-body", VerticalScroll)
        await body.remove_children()
        await body.mount(
            Label("Confirm workflow"),
            Button("Confirm", id="confirm-start", variant="success"),
            Static(
                "\n".join(
                    (
                        f"work item: {preview.summary.work_item_id}",
                        _mapping_candidate_text(candidate),
                        f"state: {preview.summary.state.value}",
                    )
                ),
                id="workflow-summary",
                markup=True,
            ),
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
        elif button_id is not None and button_id.startswith("mapping-"):
            try:
                index = int(button_id.removeprefix("mapping-"))
            except ValueError:
                self._show_notice(_MAPPING_REQUIRED)
                return
            await self._show_confirmation(index)
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


@dataclass(frozen=True, slots=True)
class ApprovalSubmission:
    request: DangerousActionRequest
    actor: str


@dataclass(frozen=True, slots=True)
class RevisionSubmission:
    request: DangerousActionRequest
    feedback: str
    scope: Literal["implementation", "repair"]


@dataclass(frozen=True, slots=True)
class CancelSubmission:
    request: DangerousActionRequest
    actor: str


class _DangerousActionModal(ModalScreen[object | None]):
    """Explicit-confirmation shell; plain Enter is deliberately inert."""

    BINDINGS = [
        Binding("escape", "back", "Back", priority=True),
        Binding("enter", "ignore_enter", "", show=False, priority=True),
        Binding("ctrl+enter", "confirm", "Confirm", priority=True),
    ]

    def __init__(self, request: DangerousActionRequest, *, screen_id: str) -> None:
        super().__init__(id=screen_id)
        self.request = request

    def action_back(self) -> None:
        self.dismiss(None)

    def action_ignore_enter(self) -> None:
        return

    def action_confirm(self) -> None:
        self._confirm()

    def _confirm(self) -> None:
        raise NotImplementedError

    def _notice(self, message: str) -> None:
        self.query_one("#modal-notice", Static).update(message)


def _valid_action_text(value: str, *, maximum: int) -> str | None:
    value = value.strip()
    if not value or len(value) > maximum:
        return None
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        return None
    if any(
        ord(character) < 32
        or 127 <= ord(character) <= 159
        or unicodedata.category(character) in {"Cf", "Cs", "Zl", "Zp"}
        for character in value
    ):
        return None
    return value


def _repository_action_facts(repository: RepositoryView) -> tuple[Widget, ...]:
    return (
        Label(f"{repository.key}  {repository.role}"),
        Static(f"base: {repository.base_commit or 'not available'}"),
        Static(f"head: {repository.head_commit or 'not available'}"),
        Static(repository.tree_hash or "not available", classes="tree-hash"),
        Static(repository.test_summary),
        Static(f"PR target: {repository.pr_target or 'not available'}"),
        Static(f"commit: {repository.commit_hash or 'not created'}"),
        Static(f"push: {'completed' if repository.pushed else 'not completed'}"),
        Static(f"PR: {repository.pr_url or 'not created'}"),
        Static(f"error: {repository.error or 'none'}"),
    )


class ApprovalModal(_DangerousActionModal):
    """Review signed approval facts before allowing an explicit approval."""

    def __init__(self, request: DangerousActionRequest) -> None:
        super().__init__(request, screen_id="approval-modal")

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("Approve workflow")
            yield Static(self.request.fingerprint, id="fingerprint")
            yield Static(
                "\n".join(
                    (
                        f"work item: {self.request.work_item_id}",
                        f"repositories: {len(self.request.repositories)}",
                        f"changed files: {self.request.changed_file_count}",
                        f"tests: {self.request.test_count}",
                        f"risks: {self.request.risk_count}",
                        f"unresolved: {self.request.unresolved_count}",
                    )
                )
            )
            for repository in self.request.repositories:
                yield from _repository_action_facts(repository)
            yield Static(f"comment: {self.request.comment_status}")
            yield Static(
                f"publication error: {self.request.publication_error or 'none'}"
            )
            yield Input(placeholder="Approver actor", id="actor")
        yield Static("", id="modal-notice", markup=False)
        yield Button("Approve", id="confirm-approve", variant="warning")
        yield Button("Back", id="cancel-action")

    def _confirm(self) -> None:
        actor = _valid_action_text(
            self.query_one("#actor", Input).value, maximum=128
        )
        if actor is None:
            self._notice(_ACTION_INPUT_REQUIRED)
            return
        self.dismiss(ApprovalSubmission(self.request, actor))

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-approve":
            self._confirm()
        elif event.button.id == "cancel-action":
            self.action_back()


class RevisionModal(_DangerousActionModal):
    """Collect bounded revision feedback and an explicit workflow scope."""

    def __init__(self, request: DangerousActionRequest) -> None:
        super().__init__(request, screen_id="revision-modal")

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("Revise workflow")
            yield Static(f"work item: {self.request.work_item_id}")
            yield Input(placeholder="Revision feedback", id="feedback")
            yield Input(placeholder="implementation or repair", id="scope")
        yield Static("", id="modal-notice", markup=False)
        yield Button("Revise", id="confirm-revise", variant="warning")
        yield Button("Back", id="cancel-action")

    def _confirm(self) -> None:
        feedback = _valid_action_text(
            self.query_one("#feedback", Input).value, maximum=4096
        )
        scope = self.query_one("#scope", Input).value.strip()
        if feedback is None or scope not in {"implementation", "repair"}:
            self._notice(_ACTION_INPUT_REQUIRED)
            return
        self.dismiss(RevisionSubmission(self.request, feedback, scope))

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-revise":
            self._confirm()
        elif event.button.id == "cancel-action":
            self.action_back()


class CancelModal(_DangerousActionModal):
    """Require an actor before cancelling an authoritative run version."""

    def __init__(self, request: DangerousActionRequest) -> None:
        super().__init__(request, screen_id="cancel-modal")

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("Cancel workflow")
            yield Static(f"work item: {self.request.work_item_id}")
            yield Input(placeholder="Cancellation actor", id="actor")
        yield Static("", id="modal-notice", markup=False)
        yield Button("Cancel workflow", id="confirm-cancel", variant="error")
        yield Button("Back", id="cancel-action")

    def _confirm(self) -> None:
        actor = _valid_action_text(
            self.query_one("#actor", Input).value, maximum=128
        )
        if actor is None:
            self._notice(_ACTION_INPUT_REQUIRED)
            return
        self.dismiss(CancelSubmission(self.request, actor))

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-cancel":
            self._confirm()
        elif event.button.id == "cancel-action":
            self.action_back()


class PublicationResumeModal(_DangerousActionModal):
    """Show persisted per-repository publication checkpoints before retry."""

    def __init__(self, request: DangerousActionRequest) -> None:
        super().__init__(request, screen_id="publication-resume-modal")

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("Resume publication")
            yield Static(self.request.fingerprint, id="fingerprint")
            for repository in self.request.repositories:
                yield from _repository_action_facts(repository)
            yield Static(f"comment: {self.request.comment_status}")
            yield Static(
                f"publication error: {self.request.publication_error or 'none'}"
            )
        yield Static("", id="modal-notice", markup=False)
        yield Button(
            "Resume publication",
            id="confirm-resume-publication",
            variant="warning",
        )
        yield Button("Back", id="cancel-action")

    def _confirm(self) -> None:
        self.dismiss(self.request)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-resume-publication":
            self._confirm()
        elif event.button.id == "cancel-action":
            self.action_back()


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
        Binding("r", "resume", "Resume", show=False, priority=True),
        Binding("v", "revise", "Revise", show=False, priority=True),
        Binding("a", "approve", "Approve", show=False, priority=True),
        Binding("x", "cancel_run", "Cancel", show=False, priority=True),
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
        self._refreshing = False
        self._refresh_requested = False
        self._refresh_activities: Mapping[str, RunActivity] | None = None

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
        with Horizontal(id="action-bar"):
            yield Button("Resume", id="action-resume")
            yield Button("Revise", id="action-revise")
            yield Button("Approve", id="action-approve")
            yield Button("Cancel", id="action-cancel")
        yield Static("", id="notice", markup=False)

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

    async def refresh_runs(
        self,
        activities: Mapping[str, RunActivity] | None = None,
    ) -> None:
        self._refresh_activities = activities
        if self._refreshing:
            self._refresh_requested = True
            return
        self._refreshing = True
        try:
            while True:
                self._refresh_requested = False
                current_activities = self._refresh_activities
                await self._refresh_runs(current_activities)
                if not self._refresh_requested:
                    break
        finally:
            self._refreshing = False

    async def _refresh_runs(
        self,
        activities: Mapping[str, RunActivity] | None,
    ) -> None:
        selected_index = self._selected_index()
        selected_run_id = (
            self._runs[selected_index].run_id
            if selected_index is not None
            and 0 <= selected_index < len(self._runs)
            else None
        )
        try:
            runs = await asyncio.to_thread(
                self._controller.list_runs,
                RunFilter(),
                activities,
            )
        except TuiControllerError:
            runs = ()
            self._runs = runs
            await self.query_one(RunListPane).replace_runs(runs)
            self._detail_error = _LIST_UNAVAILABLE
            self.query_one(RunDetailPane).show_error(self._detail_error)
            return
        self._runs = runs
        await self.query_one(RunListPane).replace_runs(runs)
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

    def _selected_summary(self) -> RunSummary | None:
        index = self._selected_index()
        if index is None or not 0 <= index < len(self._runs):
            return None
        summary = self._runs[index]
        return None if summary.corrupted else summary

    def _show_action_notice(self, message: str) -> None:
        self.query_one("#notice", Static).update(message)

    async def _prepare_action(
        self, action: str
    ) -> DangerousActionRequest | None:
        summary = self._selected_summary()
        if summary is None:
            self._show_action_notice(_ACTION_UNAVAILABLE)
            return None
        try:
            return await self._supervisor.run_readonly(
                f"prepare-{action}",
                self._controller.prepare_action,
                summary.run_id,
                action,
            )
        except Exception:
            self._show_action_notice(_ACTION_FAILED)
            return None

    @staticmethod
    def _terminal(summary: RunSummary) -> bool:
        return summary.state in {
            WorkflowState.COMPLETED,
            WorkflowState.CANCELLED,
            WorkflowState.FAILED,
        }

    async def action_approve(self) -> None:
        summary = self._selected_summary()
        if summary is None or summary.state is not WorkflowState.WAITING_APPROVAL:
            self._show_action_notice(_ACTION_UNAVAILABLE)
            return
        request = await self._prepare_action("approve")
        if request is not None and request.state is WorkflowState.WAITING_APPROVAL:
            self.app.push_screen(ApprovalModal(request), callback=self._approval_done)
        elif request is not None:
            self._show_action_notice(_ACTION_UNAVAILABLE)

    async def action_revise(self) -> None:
        summary = self._selected_summary()
        if summary is None or summary.state not in {
            WorkflowState.WAITING_APPROVAL,
            WorkflowState.BLOCKED,
        }:
            self._show_action_notice(_ACTION_UNAVAILABLE)
            return
        request = await self._prepare_action("revise")
        allowed = request is not None and (
            request.state is WorkflowState.WAITING_APPROVAL
            or (
                request.state is WorkflowState.BLOCKED
                and request.resume_state
                in {
                    WorkflowState.IMPLEMENTING,
                    WorkflowState.TESTING,
                    WorkflowState.AI_REVIEW,
                    WorkflowState.WAITING_APPROVAL,
                }
            )
        )
        if allowed:
            assert request is not None
            self.app.push_screen(RevisionModal(request), callback=self._revision_done)
        elif request is not None:
            self._show_action_notice(_ACTION_UNAVAILABLE)

    async def action_cancel_run(self) -> None:
        summary = self._selected_summary()
        if summary is None or self._terminal(summary):
            self._show_action_notice(_ACTION_UNAVAILABLE)
            return
        request = await self._prepare_action("cancel")
        if request is not None and request.state not in {
            WorkflowState.COMPLETED,
            WorkflowState.CANCELLED,
            WorkflowState.FAILED,
        }:
            self.app.push_screen(CancelModal(request), callback=self._cancel_done)
        elif request is not None:
            self._show_action_notice(_ACTION_UNAVAILABLE)

    async def action_resume(self) -> None:
        summary = self._selected_summary()
        if (
            summary is None
            or self._terminal(summary)
            or summary.state is WorkflowState.WAITING_APPROVAL
        ):
            self._show_action_notice(_ACTION_UNAVAILABLE)
            return
        try:
            detail = await self._supervisor.run_readonly(
                "review-resume", self._controller.show, summary.run_id
            )
        except Exception:
            self._show_action_notice(_ACTION_FAILED)
            return
        publication_resume = (
            detail.summary.state
            in {WorkflowState.PARTIAL_SUCCESS, WorkflowState.PUBLISHING}
            or (
                detail.summary.state is WorkflowState.BLOCKED
                and detail.resume_state is WorkflowState.PUBLISHING
            )
        )
        if publication_resume:
            request = await self._prepare_action("resume-publication")
            valid_publication_request = request is not None and (
                request.state
                in {WorkflowState.PARTIAL_SUCCESS, WorkflowState.PUBLISHING}
                or (
                    request.state is WorkflowState.BLOCKED
                    and request.resume_state is WorkflowState.PUBLISHING
                )
            )
            if valid_publication_request:
                assert request is not None
                self.app.push_screen(
                    PublicationResumeModal(request),
                    callback=self._publication_resume_done,
                )
            elif request is not None:
                self._show_action_notice(_ACTION_UNAVAILABLE)
            return
        try:
            await self._supervisor.run_mutation(
                detail.summary.run_id,
                "resume",
                self._controller.resume,
                detail.summary.run_id,
                detail.summary.version,
            )
        except StaleTuiActionError:
            self._show_action_notice(_ACTION_STALE)
        except Exception:
            self._show_action_notice(_ACTION_FAILED)
        await self.refresh_runs()

    async def _approval_done(self, submission: object | None) -> None:
        if not isinstance(submission, ApprovalSubmission):
            return
        await self._run_dangerous(
            submission.request,
            "approve",
            self._controller.approve,
            submission.request,
            submission.actor,
        )

    async def _revision_done(self, submission: object | None) -> None:
        if not isinstance(submission, RevisionSubmission):
            return
        await self._run_dangerous(
            submission.request,
            "revise",
            self._controller.revise,
            submission.request,
            submission.feedback,
            submission.scope,
        )

    async def _cancel_done(self, submission: object | None) -> None:
        if not isinstance(submission, CancelSubmission):
            return
        await self._run_dangerous(
            submission.request,
            "cancel",
            self._controller.cancel,
            submission.request,
            submission.actor,
        )

    async def _publication_resume_done(self, submission: object | None) -> None:
        if not isinstance(submission, DangerousActionRequest):
            return
        await self._run_dangerous(
            submission,
            "resume-publication",
            self._controller.resume_publication,
            submission,
        )

    async def _run_dangerous(
        self,
        request: DangerousActionRequest,
        action: str,
        call,
        *args: object,
    ) -> None:
        try:
            await self._supervisor.run_mutation(
                request.run_id, action, call, *args
            )
        except StaleTuiActionError:
            self._show_action_notice(_ACTION_STALE)
        except Exception:
            self._show_action_notice(_ACTION_FAILED)
        await self.refresh_runs()

    @on(ListView.Selected, "#run-list")
    def select_run(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if (
            index is not None
            and 0 <= index < len(self._runs)
            and event.item.name == self._runs[index].run_id
        ):
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

    @on(Button.Pressed, "#action-resume")
    async def resume_button(self) -> None:
        await self.action_resume()

    @on(Button.Pressed, "#action-revise")
    async def revise_button(self) -> None:
        await self.action_revise()

    @on(Button.Pressed, "#action-approve")
    async def approve_button(self) -> None:
        await self.action_approve()

    @on(Button.Pressed, "#action-cancel")
    async def cancel_button(self) -> None:
        await self.action_cancel_run()


__all__ = [
    "ApprovalModal",
    "CancelModal",
    "DashboardScreen",
    "DefectWizardScreen",
    "NavigationPane",
    "PublicationResumeModal",
    "RunDetailPane",
    "RunDetailScreen",
    "RunListPane",
    "RequirementWizardScreen",
    "RevisionModal",
    "SettingsView",
    "WorkflowTypeScreen",
]
