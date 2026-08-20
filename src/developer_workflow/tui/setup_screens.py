"""Restricted bootstrap screens used before a workflow runtime exists."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import inspect
from pathlib import Path
from types import MappingProxyType

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, Label, Select, Static

from ..config import (
    BUILTIN_WORKSPACE_PROFILE,
    PublishingConfig,
    PublishingProvider,
    SandboxPermissionProfileSource,
)
from ..contracts import RepositoryRole
from ..setup_models import (
    DEFAULT_ONES_COMMENT_LIST_PATH_TEMPLATE,
    RuntimePublicConfig,
    SecretKind,
    WorkflowDraft,
)
from ..setup_controller import SetupStepTransaction
from ..setup_import import (
    ImportDetection,
    parse_dotenv,
    selected_environment_values,
)
from ..setup_repository import RepositoryGroupDraftBuilder, build_repository
from ..setup_validation import SetupStep, ValidationStatus
from .setup_models import build_setup_step_view, setup_step_label


_RESULT_TEXT = {
    "ok": "Connection test passed",
    "git_unavailable": "Git executable is unavailable",
    "authentication": "Authentication failed",
    "unreachable": "Host is unreachable",
    "tls": "TLS validation failed",
    "timeout": "Connection test timed out",
    "incompatible": "Response is incompatible",
    "unsafe_path": "Private path validation failed",
    "sandbox": "Sandbox validation failed",
    "invalid_field": "Configuration fields are incomplete",
}


@dataclass(slots=True, repr=False)
class SetupImportContext:
    """Host-owned import material; only metadata is ever rendered."""

    detection: ImportDetection
    dotenv_path: Path | None
    template_workflow: WorkflowDraft | None = None
    _consumed: bool = False

    def __repr__(self) -> str:
        return "SetupImportContext(<redacted>)"

    @property
    def consumed(self) -> bool:
        return self._consumed

    def discard(self) -> None:
        self._consumed = True
        self.dotenv_path = None
        self.template_workflow = None

    def import_into(self, controller: object, source: str) -> None:
        if self._consumed:
            raise RuntimeError("import source is unavailable")
        self._consumed = True
        environment: dict[str, str] = {}
        dotenv: dict[str, str] = {}
        try:
            if source == "template":
                if self.template_workflow is None:
                    raise RuntimeError("import source is unavailable")
                controller.apply_workflow(
                    self.template_workflow.model_copy(deep=True),
                    changed_step=SetupStep.PROFILE,
                )
                return
            kinds = (
                self.detection.environment
                if source == "environment"
                else self.detection.dotenv if source == "dotenv" else ()
            )
            if not kinds:
                raise RuntimeError("import source is unavailable")
            if source == "environment":
                environment = selected_environment_values(kinds)
            else:
                if self.dotenv_path is None:
                    raise RuntimeError("import source is unavailable")
                dotenv = parse_dotenv(self.dotenv_path)
            controller.import_secrets(
                detection=self.detection,
                environment=environment,
                dotenv_values=dotenv,
                selected=kinds,
                source_choice={kind: source for kind in kinds},
            )
        finally:
            for values in (environment, dotenv):
                for name in tuple(values):
                    values[name] = ""
                values.clear()
            self.dotenv_path = None
            self.template_workflow = None


class SetupImportScreen(ModalScreen[str | None]):
    """Require an explicit source choice followed by a separate confirmation."""

    BINDINGS = [Binding("escape", "cancel_import", "Cancel import")]

    def __init__(self, context: SetupImportContext) -> None:
        super().__init__(id="setup-import")
        self._import_context = context
        self._selected: str | None = None

    def compose(self) -> ComposeResult:
        detection = self._import_context.detection
        conflicts = set(detection.environment) & set(detection.dotenv)
        summary = (
            f"environment: {len(detection.environment)} kinds; "
            f"dotenv: {len(detection.dotenv)} kinds; "
            f"conflicts: {len(conflicts)}; "
            f"template: {'available' if detection.template_available else 'missing'}"
        )
        with Vertical(id="import-dialog"):
            yield Static(summary, id="import-summary")
            yield Button("Select environment", id="import-environment")
            yield Button("Select dotenv", id="import-dotenv")
            yield Button("Select public template", id="import-template")
            yield Button("Confirm import", id="confirm-import", disabled=True)
            yield Button("Cancel", id="cancel-import")

    @on(Button.Pressed, "#import-environment, #import-dotenv, #import-template")
    def _select(self, event: Button.Pressed) -> None:
        source = {
            "import-environment": "environment",
            "import-dotenv": "dotenv",
            "import-template": "template",
        }.get(event.button.id or "")
        detection = self._import_context.detection
        available = (
            bool(detection.environment) if source == "environment"
            else bool(detection.dotenv) if source == "dotenv"
            else detection.template_available and self._import_context.template_workflow is not None
        )
        if source is None or not available:
            return
        self._selected = source
        self.query_one("#confirm-import", Button).disabled = False

    @on(Button.Pressed, "#confirm-import")
    def _confirm(self) -> None:
        if self._selected is not None:
            self.dismiss(self._selected)

    @on(Button.Pressed, "#cancel-import")
    def _cancel(self) -> None:
        self.dismiss(None)

    def action_cancel_import(self) -> None:
        self.dismiss(None)

_STEP_IDS = {
    SetupStep.PROFILE: "profile-step",
    SetupStep.ONES: "ones-step",
    SetupStep.REPOSITORIES: "repositories-step",
    SetupStep.PROVIDER: "provider-step",
    SetupStep.CODEX: "codex-step",
    SetupStep.PRIVATE_PATHS: "private-paths-step",
    SetupStep.REVIEW: "review-step",
}

_SECRET_FIELDS = {
    SetupStep.ONES: (
        ("ones-email", SecretKind.ONES_EMAIL),
        ("ones-password", SecretKind.ONES_PASSWORD),
    ),
    SetupStep.PROVIDER: (("provider-token", SecretKind.PROVIDER_TOKEN),),
    SetupStep.CODEX: (
        ("codex-api-key", SecretKind.CODEX_API_KEY),
        ("codex-auth-token", SecretKind.CODEX_AUTH_TOKEN),
    ),
}
_PROBE_FIELDS = {
    SetupStep.PROFILE: (),
    SetupStep.ONES: (
        "ones-team-id",
        "ones-project-id",
        "ones-status-id",
        "ones-item-id",
        "ones-issue-type-id",
    ),
    SetupStep.REPOSITORIES: ("repository-path", "repository-url"),
    SetupStep.PROVIDER: ("provider-host", "provider-api-url"),
    SetupStep.CODEX: ("codex-profile", "codex-worktree"),
    SetupStep.PRIVATE_PATHS: ("run-root", "mirror-root", "worktree-root"),
    SetupStep.REVIEW: (),
}
_CONFIG_FIELDS = {
    SetupStep.PROFILE: ("sandbox-profile",),
    SetupStep.ONES: (
        "ones-base-url",
        "ones-team-id",
        "ones-issue-type-id",
        "ones-project-id",
        "ones-status-id",
        "ones-item-id",
    ),
    SetupStep.REPOSITORIES: (
        "repository-key",
        "repository-project-id",
        "repository-iteration-id",
        "repository-name",
        "repository-path",
        "repository-url",
        "repository-branch",
        "repository-role",
        "repository-depends-on",
        "repository-allowed-paths",
        "repository-lint-commands",
        "repository-build-commands",
        "repository-test-commands",
        "repository-group-key",
        "repository-primary",
        "repository-integration-commands",
    ),
    SetupStep.PROVIDER: (
        "provider-host",
        "provider-api-url",
        "git-author-name",
        "git-author-email",
        "provider-type",
        "repository-branch",
    ),
    SetupStep.CODEX: (
        "codex-auth-mode",
        "codex-profile",
        "codex-worktree",
        "codex-home",
    ),
    SetupStep.PRIVATE_PATHS: ("run-root", "mirror-root", "worktree-root"),
    SetupStep.REVIEW: (),
}


class _SetupReadOnlySupervisor:
    """Own setup probe tasks without emitting workflow TaskEvents."""

    def __init__(self) -> None:
        self._task: asyncio.Task[object] | None = None
        self._closed = False

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run_readonly(
        self, call: Callable[[], Awaitable[object]]
    ) -> object:
        if self._closed or self.busy:
            raise RuntimeError("setup test is unavailable")
        task = asyncio.create_task(call(), name="tui-setup-readonly")
        self._task = task
        try:
            return await task
        finally:
            if self._task is task:
                self._task = None

    def close(self) -> None:
        self._closed = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()


class SetupRootScreen(Screen[object | None]):
    """Host setup safely without constructing or inspecting workflow runtime data."""

    def __init__(self, controller: object, *, screen_id: str = "setup-root") -> None:
        super().__init__(id=screen_id)
        self.controller = controller

    def compose(self) -> ComposeResult:
        yield Static("Runtime configuration is required", id="setup-required")

    def complete(self, handle: object) -> None:
        """Hand a successfully activated runtime back to the application host."""

        if handle is None:
            return
        self.dismiss(handle)


class SetupWizardScreen(SetupRootScreen):
    """Seven-step, controller-gated setup surface with transient form fields."""

    BINDINGS = [
        Binding("escape", "cancel_edit", "Cancel edit"),
        Binding("ctrl+enter", "confirm_primary_action", "Confirm"),
    ]

    def __init__(
        self,
        controller: object,
        *,
        activation_callback: Callable[[], Awaitable[object | None]] | None = None,
        cancel_callback: Callable[[], Awaitable[None]] | None = None,
        import_context: SetupImportContext | None = None,
        import_discard_callback: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(controller, screen_id="setup-wizard")
        self.current_step = self._controller_step()
        self._activation_callback = activation_callback
        self._cancel_callback = cancel_callback
        self._import_context = import_context
        self._import_discard_callback = import_discard_callback
        self._supervisor = _SetupReadOnlySupervisor()
        self._generation = 0
        self._transients_cleared = False
        self._test_task: asyncio.Task[None] | None = None
        self._profile_task: asyncio.Task[None] | None = None
        self._managed_profiles: tuple[str, ...] = ()
        self._profile_catalog_ready = False
        self._profile_generation = 0
        self._syncing_profile = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="setup-shell"):
            with VerticalScroll(id="setup-navigation"):
                yield Static("Runtime setup", classes="setup-title")
                for step in SetupStep:
                    yield Button(
                        setup_step_label(step),
                        id=f"nav-{step.value.replace('_', '-')}",
                        classes="setup-nav-button",
                    )
            with VerticalScroll(id="setup-form"):
                yield from self._profile_fields()
                yield from self._ones_fields()
                yield from self._repository_fields()
                yield from self._provider_fields()
                yield from self._codex_fields()
                yield from self._private_path_fields()
                with Vertical(id=_STEP_IDS[SetupStep.REVIEW], classes="setup-step"):
                    yield Label("Review validated configuration")
                    yield Button("Confirm review", id="confirm-review")
                    yield Button("Save and activate", id="activate-runtime")
            with VerticalScroll(id="setup-summary"):
                yield Static("", id="setup-status")
                yield Static("", id="setup-summary-text")
                yield Static("", id="setup-notice")
        with Horizontal(id="setup-actions"):
            yield Button("Back", id="back-step")
            yield Button("Test connection", id="test-connection")
            yield Button("Next", id="next-step")
            yield Button("Review", id="review-setup")
            yield Button("Cancel", id="cancel-setup", variant="error")

    def _profile_fields(self) -> ComposeResult:
        with Vertical(id=_STEP_IDS[SetupStep.PROFILE], classes="setup-step"):
            yield Label("Workflow permission profile")
            yield Select([], prompt="Select managed profile", id="sandbox-profile", disabled=True)
            yield Button(
                "Create safe workspace profile",
                id="create-workspace-profile",
            )
            yield Button("Review import sources", id="review-imports")

    def _ones_fields(self) -> ComposeResult:
        with Vertical(id=_STEP_IDS[SetupStep.ONES], classes="setup-step"):
            yield Label("ONES connection")
            yield Input(placeholder="Base URL", id="ones-base-url")
            yield Input(placeholder="Team ID", id="ones-team-id")
            yield Input(placeholder="Defect issue type ID", id="ones-issue-type-id")
            yield Input(placeholder="Project ID for test", id="ones-project-id")
            yield Input(placeholder="Status ID for test", id="ones-status-id")
            yield Input(placeholder="Work item ID for test", id="ones-item-id")
            yield Input(placeholder="Email", id="ones-email", password=True)
            yield Input(placeholder="Password", id="ones-password", password=True)

    def _repository_fields(self) -> ComposeResult:
        with Vertical(id=_STEP_IDS[SetupStep.REPOSITORIES], classes="setup-step"):
            yield Label("Repository and group mapping")
            yield Input(placeholder="Repository key", id="repository-key")
            yield Input(placeholder="ONES project ID", id="repository-project-id")
            yield Input(placeholder="ONES iteration ID", id="repository-iteration-id")
            yield Input(placeholder="Repository name", id="repository-name")
            yield Input(placeholder="Local absolute path", id="repository-path")
            yield Input(placeholder="Credential-free remote URL", id="repository-url")
            yield Input(placeholder="Base branch", id="repository-branch")
            yield Input(placeholder="Role: primary or dependency", id="repository-role")
            yield Input(placeholder="Depends on keys (comma separated)", id="repository-depends-on")
            yield Input(placeholder="Allowed paths (comma separated)", id="repository-allowed-paths")
            yield Input(placeholder="Lint commands (separate with ;;)", id="repository-lint-commands")
            yield Input(placeholder="Build commands (separate with ;;)", id="repository-build-commands")
            yield Input(placeholder="Test commands (separate with ;;)", id="repository-test-commands")
            yield Input(placeholder="Group key", id="repository-group-key")
            yield Input(placeholder="Primary repository key", id="repository-primary")
            yield Input(placeholder="Integration commands (separate with ;;)", id="repository-integration-commands")

    def _provider_fields(self) -> ComposeResult:
        with Vertical(id=_STEP_IDS[SetupStep.PROVIDER], classes="setup-step"):
            yield Label("Git provider connection")
            yield Input(placeholder="Provider host", id="provider-host")
            yield Input(placeholder="Provider API URL", id="provider-api-url")
            yield Input(placeholder="Git author name", id="git-author-name")
            yield Input(placeholder="Git author email", id="git-author-email")
            yield Input(placeholder="Provider type: github or gitlab", id="provider-type")
            yield Input(placeholder="Provider token", id="provider-token", password=True)

    def _codex_fields(self) -> ComposeResult:
        with Vertical(id=_STEP_IDS[SetupStep.CODEX], classes="setup-step"):
            yield Label("Codex runtime")
            yield Input(placeholder="Auth mode: credential or file", id="codex-auth-mode")
            yield Select([], prompt="Select managed profile", id="codex-profile", disabled=True)
            yield Input(placeholder="Probe worktree", id="codex-worktree")
            yield Input(placeholder="Codex home", id="codex-home")
            yield Input(placeholder="Codex API key", id="codex-api-key", password=True)
            yield Input(placeholder="Codex auth token", id="codex-auth-token", password=True)

    def _private_path_fields(self) -> ComposeResult:
        with Vertical(id=_STEP_IDS[SetupStep.PRIVATE_PATHS], classes="setup-step"):
            yield Label("Private runtime paths")
            yield Input(placeholder="Run root", id="run-root")
            yield Input(placeholder="Mirror root", id="mirror-root")
            yield Input(placeholder="Worktree root", id="worktree-root")

    async def on_mount(self) -> None:
        self._apply_layout(self.size.width)
        await self._refresh_managed_profiles()
        if not self.is_attached:
            return
        self._populate_public_draft()
        self._render_state()

    async def _refresh_managed_profiles(self) -> None:
        self._profile_generation += 1
        generation = self._profile_generation
        loader = getattr(self.controller, "list_managed_profiles", None)
        notice: str | None = None
        if not callable(loader):
            self._profile_catalog_ready = True
            if self.is_attached:
                self.query_one("#setup-notice", Static).update(
                    "Managed profiles are unavailable"
                )
            return
        try:
            profiles = tuple(await loader())
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            profiles = ()
            notice = "Managed profiles are unavailable"
        if generation != self._profile_generation or not self.is_attached:
            return
        self._managed_profiles = profiles
        self._profile_catalog_ready = True
        options = tuple((profile, profile) for profile in profiles)
        for widget_id in ("sandbox-profile", "codex-profile"):
            widget = self.query_one(f"#{widget_id}", Select)
            widget.set_options(options)
            widget.disabled = not profiles
        if not profiles:
            self.query_one("#setup-notice", Static).update(
                notice or "No managed profiles are available"
            )

    @on(Select.Changed, "#sandbox-profile, #codex-profile")
    def _profile_changed(self, event: Select.Changed) -> None:
        if (
            not self.is_attached
            or self._syncing_profile
            or type(event.value) is not str
        ):
            return
        self._syncing_profile = True
        try:
            for widget_id in ("sandbox-profile", "codex-profile"):
                widget = self.query_one(f"#{widget_id}", Select)
                if widget.value != event.value:
                    widget.value = event.value
        finally:
            self._syncing_profile = False
        self._render_state()

    @on(Button.Pressed, "#create-workspace-profile")
    def _request_builtin_workspace_profile(self) -> None:
        if self._profile_task is not None and not self._profile_task.done():
            return
        self.app.push_screen(
            SetupWorkspaceProfileConfirmation(),
            self._builtin_workspace_profile_finished,
        )

    def _builtin_workspace_profile_finished(self, confirmed: bool | None) -> None:
        if confirmed is not True or not self.is_attached:
            return
        self._start_builtin_workspace_profile()

    def _start_builtin_workspace_profile(self) -> None:
        if (
            not self.is_attached
            or self._supervisor.busy
            or self._profile_task is not None
            and not self._profile_task.done()
        ):
            return
        button = self.query_one("#create-workspace-profile", Button)
        button.disabled = True
        self._transients_cleared = False
        self._generation += 1
        generation = self._generation
        task = asyncio.create_task(
            self._confirm_builtin_workspace_profile(generation),
            name="tui-setup-workspace-profile",
        )
        self._profile_task = task

        def completed(done: asyncio.Task[None]) -> None:
            if self._profile_task is done:
                self._profile_task = None
            if done.cancelled():
                return
            try:
                failure = done.exception()
            except asyncio.CancelledError:
                return
            if failure is not None and self.is_attached:
                self.app.call_later(self._raise_profile_failure, failure)

        task.add_done_callback(completed)

    @staticmethod
    def _raise_profile_failure(failure: BaseException) -> None:
        raise failure

    async def _confirm_builtin_workspace_profile(self, generation: int) -> None:
        notice = self.query_one("#setup-notice", Static)
        notice.update(
            "Verifying safe workspace profile; first setup may safely copy about "
            "299 MB of signed native Codex into a private cache; later runs reuse it"
        )
        confirm = getattr(
            self.controller, "confirm_builtin_workspace_profile", None
        )
        succeeded = False
        try:
            if not callable(confirm):
                raise RuntimeError("workspace profile confirmation is unavailable")
            await self._supervisor.run_readonly(confirm)
            if generation != self._generation or not self.is_attached:
                return
            draft = getattr(self.controller, "draft", None)
            workflow = getattr(draft, "workflow", None)
            profile = getattr(workflow, "sandbox_permission_profile", None)
            source = getattr(
                workflow, "sandbox_permission_profile_source", None
            )
            if not (
                type(profile) is str
                and profile == BUILTIN_WORKSPACE_PROFILE
                and source is SandboxPermissionProfileSource.BUILTIN_WORKSPACE
            ):
                raise RuntimeError("workspace profile binding is unavailable")
            profiles = tuple(
                dict.fromkeys((*self._managed_profiles, BUILTIN_WORKSPACE_PROFILE))
            )
            self._managed_profiles = profiles
            options = tuple((item, item) for item in profiles)
            self._syncing_profile = True
            try:
                for widget_id in ("sandbox-profile", "codex-profile"):
                    widget = self.query_one(f"#{widget_id}", Select)
                    widget.set_options(options)
                    widget.disabled = False
                    widget.value = BUILTIN_WORKSPACE_PROFILE
            finally:
                self._syncing_profile = False
            succeeded = True
            notice.update("Safe workspace profile verified")
        except asyncio.CancelledError:
            return
        except BaseException as error:
            if isinstance(
                error,
                (KeyboardInterrupt, SystemExit, MemoryError, GeneratorExit),
            ):
                raise
            if generation == self._generation and self.is_attached:
                notice.update("Safe workspace profile could not be verified")
        finally:
            if generation == self._generation and self.is_attached:
                button = self.query_one("#create-workspace-profile", Button)
                button.label = (
                    "Create safe workspace profile"
                    if succeeded
                    else "Retry safe workspace profile"
                )
                button.disabled = False
                self._render_state()

    def _populate_public_draft(self) -> None:
        """Render the detached public draft; credential inputs always stay blank."""

        draft = getattr(self.controller, "draft", None)
        runtime = getattr(draft, "runtime", None)
        workflow = getattr(draft, "workflow", None)
        values: dict[str, str] = {}
        if runtime is not None:
            values.update(
                {
                    "ones-base-url": runtime.ones_base_url,
                    "ones-team-id": runtime.ones_team_id,
                    "ones-issue-type-id": runtime.ones_issue_type_id,
                    "provider-host": runtime.provider_host,
                    "provider-api-url": runtime.provider_api_url,
                    "git-author-name": runtime.git_author_name,
                    "git-author-email": runtime.git_author_email,
                    "codex-auth-mode": runtime.codex_auth_mode,
                    "codex-home": str(runtime.codex_home or ""),
                }
            )
        if workflow is not None:
            values.update(
                {
                    "sandbox-profile": workflow.sandbox_permission_profile or "",
                    "run-root": str(workflow.run_root or ""),
                    "mirror-root": str(workflow.mirror_root or ""),
                    "worktree-root": str(workflow.worktree_root or ""),
                }
            )
            publishing = workflow.publishing
            if publishing is not None:
                values["provider-type"] = publishing.provider.value
                values["repository-branch"] = publishing.default_target_branch
            repositories = tuple(workflow.repositories)
            if repositories:
                repository = repositories[0]
                values.update(
                    {
                        "repository-key": repository.key,
                        "repository-project-id": repository.project_id,
                        "repository-iteration-id": repository.iteration_id,
                        "repository-name": repository.repo_name,
                        "repository-path": str(repository.source_path or ""),
                        "repository-url": repository.repo_url,
                        "repository-branch": repository.base_branch,
                        "repository-role": repository.role.value,
                        "repository-depends-on": ",".join(repository.depends_on),
                        "repository-allowed-paths": ",".join(repository.allowed_paths),
                        "repository-lint-commands": "\n".join(repository.lint_commands),
                        "repository-build-commands": "\n".join(repository.build_commands),
                        "repository-test-commands": "\n".join(repository.test_commands),
                    }
                )
            groups = tuple(workflow.repository_groups)
            if groups:
                values["repository-group-key"] = groups[0].key
                values["repository-primary"] = groups[0].primary_repository
        selected_profile = values.pop("sandbox-profile", "")
        if selected_profile in self._managed_profiles:
            for widget_id in ("sandbox-profile", "codex-profile"):
                self.query_one(f"#{widget_id}", Select).value = selected_profile
        for widget_id, value in values.items():
            self.query_one(f"#{widget_id}", Input).value = value

    def on_resize(self, event: Resize) -> None:
        self._apply_layout(event.size.width)

    @on(Button.Pressed, "#review-imports")
    def _review_imports(self) -> None:
        if self._import_context is None:
            self.query_one("#setup-notice", Static).update("No import sources available")
            return
        self.app.push_screen(
            SetupImportScreen(self._import_context), self._import_finished
        )

    def _import_finished(self, source: str | None) -> None:
        context = self._import_context
        if context is None:
            return
        try:
            if source is None or not self.is_attached:
                context.discard()
            else:
                context.import_into(self.controller, source)
            if source == "template" and self.is_attached:
                self._populate_public_draft()
            if self.is_attached:
                self.query_one("#setup-notice", Static).update("Import completed")
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if self.is_attached:
                self.query_one("#setup-notice", Static).update("Import failed safely")
        finally:
            self._import_context = None
            callback = self._import_discard_callback
            if callback is not None:
                callback()
        if self.is_attached:
            self._render_state()

    def _apply_layout(self, width: int) -> None:
        self.remove_class("one", "two", "three")
        self.add_class("one" if width < 80 else "two" if width < 110 else "three")

    def _controller_step(self) -> SetupStep:
        step = getattr(self.controller, "current_step", SetupStep.PROFILE)
        return step if type(step) is SetupStep else SetupStep.PROFILE

    def _state(self) -> object:
        return getattr(self.controller, "state", object())

    def _render_state(self) -> None:
        view = build_setup_step_view(self._state(), self.current_step)
        for step, container_id in _STEP_IDS.items():
            self.query_one(f"#{container_id}").display = step is self.current_step
        self.query_one("#setup-status", Static).update(
            f"{view.label}: {view.status.value.replace('_', ' ')}"
        )
        self.query_one("#setup-summary-text", Static).update(" 路 ".join(view.summary))
        self.query_one("#test-connection", Button).disabled = not view.can_test
        self.query_one("#next-step", Button).disabled = not view.can_continue
        self.query_one("#back-step", Button).disabled = self.current_step is tuple(SetupStep)[0]
        self.query_one("#review-setup", Button).disabled = not all(
            build_setup_step_view(self._state(), step).can_continue
            for step in tuple(SetupStep)[:-1]
        )
        if self.current_step in {SetupStep.PROFILE, SetupStep.CODEX} and not self._selected_profile():
            self.query_one("#test-connection", Button).disabled = True
            self.query_one("#next-step", Button).disabled = True

    def _selected_profile(self) -> str:
        if not self._profile_catalog_ready:
            return ""
        selected = {
            value
            for widget_id in ("sandbox-profile", "codex-profile")
            if type(
                value := self.query_one(f"#{widget_id}", Select).value
            ) is str
            and value in self._managed_profiles
        }
        if len(selected) != 1:
            return ""
        value = selected.pop()
        self._syncing_profile = True
        try:
            for widget_id in ("sandbox-profile", "codex-profile"):
                widget = self.query_one(f"#{widget_id}", Select)
                if type(widget.value) is not str:
                    widget.value = value
        finally:
            self._syncing_profile = False
        return value

    def _field_value(self, field_name: str) -> str:
        if field_name in {"sandbox-profile", "codex-profile"}:
            return self._selected_profile()
        return self.query_one(f"#{field_name}", Input).value

    def _snapshot_fields(self) -> Mapping[str, str]:
        values = {
            field_name: self._field_value(field_name)
            for field_name in _PROBE_FIELDS[self.current_step]
        }
        return MappingProxyType(values)

    def _snapshot_config_fields(self) -> Mapping[str, str]:
        """Copy only the public fields owned by the current setup step."""

        return MappingProxyType(
            {
                field_name: self._field_value(field_name)
                for field_name in _CONFIG_FIELDS[self.current_step]
            }
        )

    def _take_step_secrets(self) -> list[list[object]]:
        values: list[list[object]] = []
        for widget_id, kind in _SECRET_FIELDS.get(self.current_step, ()):
            widget = self.query_one(f"#{widget_id}", Input)
            if widget.value:
                values.append([kind, widget.value])
            widget.value = ""
        return values

    def _consume_step_secrets(self, values: list[list[object]]) -> None:
        set_secret = getattr(self.controller, "set_secret", None)
        for item in values:
            kind, value = item
            if not callable(set_secret):
                raise RuntimeError("credential input is unavailable")
            try:
                set_secret(kind, value)
            finally:
                item[1] = ""

    def _apply_current_public_fields(
        self,
        fields: Mapping[str, str],
        saved_runtime_fields: Mapping[str, str] = MappingProxyType({}),
    ) -> None:
        apply_runtime_fields = getattr(
            self.controller, "apply_runtime_fields", None
        )
        if self.current_step is SetupStep.ONES and callable(apply_runtime_fields):
            apply_runtime_fields(
                SetupStep.ONES,
                MappingProxyType(
                    {
                        "ones_base_url": fields["ones-base-url"],
                        "ones_team_id": fields["ones-team-id"],
                        "ones_issue_type_id": fields["ones-issue-type-id"],
                    }
                ),
            )
        elif self.current_step is SetupStep.PROVIDER and callable(
            apply_runtime_fields
        ):
            apply_runtime_fields(
                SetupStep.PROVIDER,
                MappingProxyType(
                    {
                        "provider_host": fields["provider-host"],
                        "provider_api_url": fields["provider-api-url"],
                        "git_author_name": fields["git-author-name"],
                        "git_author_email": fields["git-author-email"],
                        "provider": fields["provider-type"],
                    }
                ),
            )
        elif self.current_step is SetupStep.CODEX and callable(
            apply_runtime_fields
        ):
            apply_runtime_fields(
                SetupStep.CODEX,
                MappingProxyType(
                    {
                        "codex_auth_mode": fields["codex-auth-mode"],
                        "codex_home": fields["codex-home"],
                    }
                ),
            )
        if self.current_step is SetupStep.PROFILE:
            apply_workflow = getattr(self.controller, "apply_workflow", None)
            if not callable(apply_workflow):
                raise RuntimeError("workflow input is unavailable")
            draft = getattr(self.controller, "draft", None)
            workflow = (
                draft.workflow.model_copy(deep=True)
                if draft is not None and hasattr(draft, "workflow")
                else WorkflowDraft()
            )
            workflow.sandbox_permission_profile = fields["sandbox-profile"]
            apply_workflow(workflow, changed_step=SetupStep.PROFILE)
            return
        if self.current_step is SetupStep.REPOSITORIES:
            upsert_repository = getattr(self.controller, "upsert_repository", None)
            add_repository = getattr(self.controller, "add_repository", None)
            repository_writer = (
                upsert_repository if callable(upsert_repository) else add_repository
            )
            if not callable(repository_writer):
                raise RuntimeError("repository input is unavailable")
            key = fields["repository-key"]
            draft = getattr(self.controller, "draft", None)
            existing = (
                tuple(draft.workflow.repositories)
                if draft is not None and hasattr(draft, "workflow")
                else ()
            )
            repository = next(
                (item for item in existing if getattr(item, "key", None) == key),
                None,
            )
            if repository is None or callable(upsert_repository):
                repository = repository_writer(
                    key=key,
                    project_id=fields["repository-project-id"],
                    iteration_id=fields["repository-iteration-id"],
                    repo_url=fields["repository-url"],
                    repo_name=fields["repository-name"],
                    base_branch=fields["repository-branch"],
                    source_path=(
                        Path(fields["repository-path"])
                        if fields["repository-path"].strip()
                        else None
                    ),
                )
            group_key = fields["repository-group-key"]
            primary = fields["repository-primary"]
            if group_key or primary:
                add_group = getattr(self.controller, "add_repository_group", None)
                if not callable(add_group):
                    raise RuntimeError("repository group input is unavailable")
                groups = (
                    tuple(draft.workflow.repository_groups)
                    if draft is not None and hasattr(draft, "workflow")
                    else ()
                )
                if not any(getattr(item, "key", None) == group_key for item in groups):
                    builder = RepositoryGroupDraftBuilder(
                        key=group_key,
                        project_id=fields["repository-project-id"],
                        iteration_id=fields["repository-iteration-id"],
                    )
                    builder.add(repository)
                    add_group(builder, primary=primary)
            return
        if self.current_step is SetupStep.PROVIDER:
            apply_workflow = getattr(self.controller, "apply_workflow", None)
            if not callable(apply_workflow):
                raise RuntimeError("workflow input is unavailable")
            draft = getattr(self.controller, "draft", None)
            workflow = (
                draft.workflow.model_copy(deep=True)
                if draft is not None and hasattr(draft, "workflow")
                else WorkflowDraft()
            )
            provider = fields["provider-type"]
            workflow.publishing = PublishingConfig(
                provider=PublishingProvider(provider),
                default_target_branch=fields["repository-branch"],
            )
            apply_workflow(workflow, changed_step=SetupStep.PROVIDER)
            return
        if self.current_step is SetupStep.PRIVATE_PATHS:
            apply_workflow = getattr(self.controller, "apply_workflow", None)
            if not callable(apply_workflow):
                raise RuntimeError("workflow input is unavailable")
            draft = getattr(self.controller, "draft", None)
            workflow = (
                draft.workflow.model_copy(deep=True)
                if draft is not None and hasattr(draft, "workflow")
                else WorkflowDraft()
            )
            workflow.run_root = Path(fields["run-root"])
            workflow.mirror_root = Path(fields["mirror-root"])
            workflow.worktree_root = Path(fields["worktree-root"])
            apply_workflow(workflow, changed_step=SetupStep.PRIVATE_PATHS)
            return
        if self.current_step is not SetupStep.CODEX:
            return
        apply_runtime = getattr(self.controller, "apply_runtime", None)
        if not callable(apply_runtime):
            raise RuntimeError("runtime input is unavailable")
        codex_home = fields["codex-home"]
        runtime = RuntimePublicConfig(
            ones_base_url=saved_runtime_fields["ones_base_url"],
            ones_team_id=saved_runtime_fields["ones_team_id"],
            ones_issue_type_id=saved_runtime_fields["ones_issue_type_id"],
            ones_comment_list_path_template=(
                DEFAULT_ONES_COMMENT_LIST_PATH_TEMPLATE
            ),
            provider_host=saved_runtime_fields["provider_host"],
            provider_api_url=saved_runtime_fields["provider_api_url"],
            git_author_name=saved_runtime_fields["git_author_name"],
            git_author_email=saved_runtime_fields["git_author_email"],
            codex_auth_mode=fields["codex-auth-mode"],
            codex_home=Path(codex_home) if codex_home else None,
        )
        apply_runtime(runtime, changed_step=SetupStep.CODEX)

    def _build_step_transaction(
        self,
        fields: Mapping[str, str],
        draft: object | None,
        saved_runtime_fields: Mapping[str, str] = MappingProxyType({}),
    ) -> SetupStepTransaction:
        workflow = (
            draft.workflow.model_copy(deep=True)
            if draft is not None and hasattr(draft, "workflow")
            else WorkflowDraft()
        )
        if self.current_step is SetupStep.PROFILE:
            workflow.sandbox_permission_profile = fields["sandbox-profile"]
            return SetupStepTransaction(workflow=workflow)
        if self.current_step is SetupStep.ONES:
            return SetupStepTransaction(
                runtime_fields=MappingProxyType(
                    {
                        "ones_base_url": fields["ones-base-url"],
                        "ones_team_id": fields["ones-team-id"],
                        "ones_issue_type_id": fields["ones-issue-type-id"],
                    }
                )
            )
        if self.current_step is SetupStep.REPOSITORIES:
            repository = build_repository(
                key=fields["repository-key"],
                project_id=fields["repository-project-id"],
                iteration_id=fields["repository-iteration-id"],
                repo_url=fields["repository-url"],
                repo_name=fields["repository-name"],
                base_branch=fields["repository-branch"],
                source_path=(
                    Path(fields["repository-path"])
                    if fields["repository-path"].strip()
                    else None
                ),
                role=RepositoryRole(fields["repository-role"]),
                depends_on=self._comma_values(fields["repository-depends-on"]),
                allowed_paths=self._comma_values(fields["repository-allowed-paths"]),
                lint_commands=self._command_values(fields["repository-lint-commands"]),
                build_commands=self._command_values(fields["repository-build-commands"]),
                test_commands=self._command_values(fields["repository-test-commands"]),
            )
            group_key = fields["repository-group-key"]
            primary = fields["repository-primary"]
            if not group_key and not primary:
                return SetupStepTransaction(repository=repository)
            members = [
                item
                for item in workflow.repositories
                if item.project_id == repository.project_id
                and item.iteration_id == repository.iteration_id
                and item.key != repository.key
            ]
            existing_group = next(
                (
                    item
                    for item in workflow.repository_groups
                    if item.key == group_key
                ),
                None,
            )
            if existing_group is not None:
                members.extend(
                    item
                    for item in existing_group.repositories
                    if item.key != repository.key
                    and not any(candidate.key == item.key for candidate in members)
                )
            members.append(repository)
            builder = RepositoryGroupDraftBuilder(
                key=group_key,
                project_id=repository.project_id,
                iteration_id=repository.iteration_id,
                integration_test_commands=self._command_values(
                    fields["repository-integration-commands"]
                ),
            )
            for member in members:
                builder.add(member)
            return SetupStepTransaction(
                repository_group=builder.build(primary=primary)
            )
        if self.current_step is SetupStep.PROVIDER:
            workflow.publishing = PublishingConfig(
                provider=PublishingProvider(fields["provider-type"]),
                default_target_branch=fields["repository-branch"],
            )
            return SetupStepTransaction(
                workflow=workflow,
                runtime_fields=MappingProxyType(
                    {
                        "provider_host": fields["provider-host"],
                        "provider_api_url": fields["provider-api-url"],
                        "git_author_name": fields["git-author-name"],
                        "git_author_email": fields["git-author-email"],
                        "provider": fields["provider-type"],
                    }
                ),
            )
        if self.current_step is SetupStep.CODEX:
            codex_home = fields["codex-home"]
            return SetupStepTransaction(
                runtime=RuntimePublicConfig(
                    ones_base_url=saved_runtime_fields["ones_base_url"],
                    ones_team_id=saved_runtime_fields["ones_team_id"],
                    ones_issue_type_id=saved_runtime_fields["ones_issue_type_id"],
                    ones_comment_list_path_template=(
                        DEFAULT_ONES_COMMENT_LIST_PATH_TEMPLATE
                    ),
                    provider_host=saved_runtime_fields["provider_host"],
                    provider_api_url=saved_runtime_fields["provider_api_url"],
                    git_author_name=saved_runtime_fields["git_author_name"],
                    git_author_email=saved_runtime_fields["git_author_email"],
                    codex_auth_mode=fields["codex-auth-mode"],
                    codex_home=Path(codex_home) if codex_home else None,
                )
            )
        if self.current_step is SetupStep.PRIVATE_PATHS:
            workflow.run_root = Path(fields["run-root"])
            workflow.mirror_root = Path(fields["mirror-root"])
            workflow.worktree_root = Path(fields["worktree-root"])
            return SetupStepTransaction(workflow=workflow)
        return SetupStepTransaction()

    @staticmethod
    def _comma_values(value: str) -> tuple[str, ...]:
        if "\n" in value or "\r" in value:
            raise ValueError("repository policy is invalid")
        return tuple(item.strip() for item in value.split(",") if item.strip())

    @staticmethod
    def _command_values(value: str) -> tuple[str, ...]:
        if "\r" in value:
            raise ValueError("repository command is invalid")
        normalized = value.replace("\n", ";;")
        return tuple(item.strip() for item in normalized.split(";;") if item.strip())

    def _commit_step_candidate(
        self,
        transaction: SetupStepTransaction,
        secret_values: list[list[object]],
        expected_revision: int | None,
        fields: Mapping[str, str],
        saved_runtime_fields: Mapping[str, str],
    ) -> None:
        apply_transaction = getattr(
            self.controller, "apply_step_transaction", None
        )
        if callable(apply_transaction) and expected_revision is not None:
            secrets = MappingProxyType(
                {item[0]: item[1] for item in secret_values}
            )
            try:
                apply_transaction(
                    self.current_step,
                    transaction,
                    expected_revision=expected_revision,
                    secrets=secrets,
                )
            finally:
                for item in secret_values:
                    item[1] = ""
            return
        self._consume_step_secrets(secret_values)
        self._apply_current_public_fields(fields, saved_runtime_fields)

    def _belongs_to_current_step(self, widget: Input) -> bool:
        target = _STEP_IDS[self.current_step]
        return any(getattr(node, "id", None) == target for node in widget.ancestors)

    def _clear_inputs(self) -> None:
        """Erase only credential widgets; public draft fields remain editable."""

        for widget in self.query(Input):
            if widget.password:
                widget.value = ""

    def _clear_controller_transients_once(self) -> None:
        if self._transients_cleared:
            return
        self._transients_cleared = True
        cancel = getattr(self.controller, "cancel_edit", None)
        if callable(cancel):
            cancel()

    def _leave_step(self) -> None:
        self._clear_inputs()

    def _start_test_connection(self) -> None:
        if self._test_task is not None and not self._test_task.done():
            return
        task = asyncio.create_task(
            self.action_test_connection(), name="tui-setup-test-action"
        )
        self._test_task = task

        def completed(done: asyncio.Task[None]) -> None:
            if self._test_task is done:
                self._test_task = None
            if done.cancelled():
                return
            try:
                done.exception()
            except BaseException:
                return

        task.add_done_callback(completed)

    def action_start_test_connection(self) -> None:
        """Binding entry point: schedule work without occupying the message pump."""

        self._start_test_connection()

    def action_confirm_primary_action(self) -> None:
        if self.current_step is SetupStep.REVIEW:
            self._request_activation_confirmation()
        else:
            self._start_test_connection()

    async def action_test_connection(self) -> None:
        if self._supervisor.busy or self.current_step is SetupStep.REVIEW:
            return
        button = self.query_one("#test-connection", Button)
        profile_ready = (
            self.current_step in {SetupStep.PROFILE, SetupStep.CODEX}
            and bool(self._selected_profile())
        )
        if button.disabled and not profile_ready:
            return
        self._transients_cleared = False
        self._generation += 1
        generation = self._generation
        fields = self._snapshot_fields()
        public_fields = self._snapshot_config_fields()
        secret_values = self._take_step_secrets()
        expected_revision = getattr(self.controller, "revision", None)
        draft = getattr(self.controller, "draft", None)
        saved_runtime_fields = getattr(
            self.controller, "runtime_public_fields", MappingProxyType({})
        )
        button.disabled = True
        self.query_one("#setup-notice", Static).update("Testing connection")
        async def prepare_and_probe() -> object:
            if callable(getattr(self.controller, "apply_step_transaction", None)):
                transaction = await asyncio.to_thread(
                    self._build_step_transaction,
                    public_fields,
                    draft,
                    saved_runtime_fields,
                )
            else:
                transaction = SetupStepTransaction()
            if generation != self._generation or not self.is_attached:
                raise asyncio.CancelledError
            self._commit_step_candidate(
                transaction,
                secret_values,
                expected_revision,
                public_fields,
                saved_runtime_fields,
            )
            if generation != self._generation or not self.is_attached:
                raise asyncio.CancelledError
            return await self.controller.test_step(self.current_step, fields)
        try:
            result = await self._supervisor.run_readonly(
                prepare_and_probe
            )
        except asyncio.CancelledError:
            return
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if generation == self._generation and self.is_attached:
                self.query_one("#setup-notice", Static).update(
                    "Connection test failed safely"
                )
            return
        finally:
            self._clear_inputs()
            if generation == self._generation and self.is_attached:
                self._render_state()
        if generation != self._generation or not self.is_attached:
            return
        category = getattr(result, "category", "incompatible")
        status = getattr(result, "status", ValidationStatus.FAILED)
        fixed = _RESULT_TEXT.get(category, _RESULT_TEXT["incompatible"])
        self.query_one("#setup-notice", Static).update(fixed)
        self._render_state()
        if status is not ValidationStatus.PASSED:
            self.current_step = self._controller_step()
            self._render_state()

    def action_cancel_edit(self) -> None:
        self._generation += 1
        self._supervisor.close()
        profile_task = self._profile_task
        profile_pending = profile_task is not None and not profile_task.done()
        if profile_pending:
            profile_task.cancel()
        self._leave_step()
        self._clear_controller_transients_once()
        self._supervisor = _SetupReadOnlySupervisor()
        if self.is_attached:
            if profile_pending:
                self.query_one("#create-workspace-profile", Button).label = (
                    "Retry safe workspace profile"
                )
                self.query_one("#create-workspace-profile", Button).disabled = False
            self.query_one("#setup-notice", Static).update("Setup edit cancelled")
            self._render_state()

    @on(Button.Pressed, "#test-connection")
    def _pressed_test(self) -> None:
        self._start_test_connection()

    @on(Button.Pressed, ".setup-nav-button")
    def _pressed_navigation(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        requested = next(
            (
                step
                for step in SetupStep
                if button_id == f"nav-{step.value.replace('_', '-')}"
            ),
            None,
        )
        if requested is None or requested is self.current_step:
            return
        if requested is SetupStep.REVIEW:
            allowed = all(
                build_setup_step_view(self._state(), step).can_continue
                for step in tuple(SetupStep)[:-1]
            )
        else:
            allowed = build_setup_step_view(self._state(), requested).can_test
        if not allowed:
            self.query_one("#setup-notice", Static).update("Step is not available")
            return
        self._leave_step()
        self.current_step = requested
        self._transients_cleared = False
        self._render_state()

    @on(Button.Pressed, "#next-step")
    def _pressed_next(self) -> None:
        view = build_setup_step_view(self._state(), self.current_step)
        if not view.can_continue:
            return
        order = tuple(SetupStep)
        index = order.index(self.current_step)
        if index < len(order) - 1:
            self._leave_step()
            self.current_step = order[index + 1]
            self._transients_cleared = False
            self._render_state()

    @on(Button.Pressed, "#back-step")
    def _pressed_back(self) -> None:
        order = tuple(SetupStep)
        index = order.index(self.current_step)
        if index > 0:
            self._leave_step()
            self.current_step = order[index - 1]
            self._transients_cleared = False
            self._render_state()

    @on(Button.Pressed, "#review-setup")
    def _pressed_review(self) -> None:
        if not self.query_one("#review-setup", Button).disabled:
            self._leave_step()
            self.current_step = SetupStep.REVIEW
            self._transients_cleared = False
            self._render_state()

    @on(Button.Pressed, "#confirm-review")
    def _pressed_confirm(self) -> None:
        confirm = getattr(self.controller, "confirm_review", None)
        if callable(confirm):
            try:
                confirm()
            except BaseException:
                self.query_one("#setup-notice", Static).update(
                    "Configuration review is unavailable"
                )
                return
        self._render_state()

    @on(Button.Pressed, "#activate-runtime")
    def _pressed_activate(self) -> None:
        self._request_activation_confirmation()

    def _request_activation_confirmation(self) -> None:
        callback = self._activation_callback
        if callback is None:
            self.query_one("#setup-notice", Static).update(
                "Activation is ready for confirmation"
            )
            return
        self._clear_inputs()
        self.app.push_screen(
            SetupActivationConfirmation(callback), self._activation_finished
        )

    def _activation_finished(self, handle: object | None) -> None:
        if handle is not None and self.is_attached:
            self.complete(handle)
        elif self.is_attached:
            self.query_one("#setup-notice", Static).update(
                "Runtime activation failed safely"
            )

    @on(Button.Pressed, "#cancel-setup")
    async def _pressed_cancel(self) -> None:
        self.action_cancel_edit()
        callback = self._cancel_callback
        if callback is not None:
            await callback()

    def on_unmount(self) -> None:
        self._generation += 1
        self._profile_generation += 1
        self._supervisor.close()
        task = self._test_task
        if task is not None and not task.done():
            task.cancel()
        profile_task = self._profile_task
        if profile_task is not None and not profile_task.done():
            profile_task.cancel()
        context = self._import_context
        if context is not None:
            context.discard()
            self._import_context = None
        callback = self._import_discard_callback
        if callback is not None:
            callback()
        self._clear_inputs()
        self._clear_controller_transients_once()


class _ExplicitConfirmation(ModalScreen[object | None]):
    """Mouse/explicit-key confirmation that deliberately ignores plain Enter."""

    BINDINGS = [Binding("enter", "ignore_enter", "", show=False, priority=True)]

    def action_ignore_enter(self) -> None:
        return None


class SetupWorkspaceProfileConfirmation(_ExplicitConfirmation):
    """Require an explicit click before provisioning the fixed safe profile."""

    BINDINGS = [
        Binding("enter", "ignore_enter", "", show=False, priority=True),
        Binding("escape", "cancel_workspace_profile", "Back", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__(id="setup-workspace-profile-confirmation")

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-confirmation"):
            yield Static(
                "Enable the fixed ones-dev workspace profile only after "
                "inside-write, outside-deny, network-deny, and environment "
                "isolation checks pass. First setup may safely copy about 299 MB "
                "of signed native Codex into a private cache; later runs reuse it."
            )
            yield Button(
                "Enable safe workspace profile",
                id="confirm-workspace-profile",
                variant="warning",
            )
            yield Button("Back", id="cancel-workspace-profile")

    @on(Button.Pressed, "#confirm-workspace-profile")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel-workspace-profile")
    def _cancel(self) -> None:
        self.dismiss(False)

    def action_cancel_workspace_profile(self) -> None:
        self.dismiss(False)


class SetupActivationConfirmation(_ExplicitConfirmation):
    def __init__(self, callback: Callable[[], Awaitable[object | None]]) -> None:
        super().__init__(id="setup-activation-confirmation")
        self._callback = callback

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-confirmation"):
            yield Static("Save configuration and activate the runtime?")
            yield Button(
                "Save and activate", id="confirm-setup-activation", variant="warning"
            )
            yield Button("Back", id="cancel-setup-activation")

    @on(Button.Pressed, "#confirm-setup-activation")
    async def _confirm(self) -> None:
        try:
            handle = await self._callback()
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            handle = None
        self.dismiss(handle)

    @on(Button.Pressed, "#cancel-setup-activation")
    def _cancel(self) -> None:
        self.dismiss(None)


class SetupRecoveryScreen(SetupRootScreen):
    """Restricted pending-activation recovery without credential reads."""

    BINDINGS = [Binding("enter", "ignore_enter", "", show=False, priority=True)]

    def __init__(
        self,
        controller: object,
        *,
        owner_generation: str | None = None,
        recovery_state: object | None = None,
        recovery_callback: (
            Callable[[str, str], Awaitable[bool]] | None
        ) = None,
    ) -> None:
        super().__init__(controller, screen_id="setup-recovery")
        state = recovery_state or getattr(controller, "recovery_state", object())
        self.owner_generation = owner_generation or getattr(
            state, "owner_generation", ""
        )
        self._previous_available = bool(
            getattr(state, "previous_available", False)
        )
        self._orphan_count = int(getattr(state, "orphan_count", 0) or 0)
        self._pending_action: str | None = None
        self._recovery_callback = recovery_callback

    def compose(self) -> ComposeResult:
        yield Static("Runtime activation recovery required", id="recovery-title")
        yield Static(
            "Previous configuration: "
            + ("available" if self._previous_available else "unavailable"),
            id="recovery-previous",
        )
        yield Static(f"Orphan credentials: {self._orphan_count}", id="recovery-orphans")
        yield Static("Recovery category: activation interrupted", id="recovery-category")
        yield Static("", id="recovery-notice")
        yield Button(
            "Restore previous",
            id="restore-previous",
            disabled=not self._previous_available,
        )
        yield Button("Discard pending", id="discard-pending", variant="warning")
        yield Button(
            "Clean orphan credentials",
            id="cleanup-orphans",
            variant="error",
            disabled=self._orphan_count == 0,
        )

    def action_ignore_enter(self) -> None:
        return None

    @on(Button.Pressed, "#restore-previous")
    def _restore(self) -> None:
        self._confirm_action("restore")

    @on(Button.Pressed, "#discard-pending")
    def _discard(self) -> None:
        self._confirm_action("discard")

    @on(Button.Pressed, "#cleanup-orphans")
    def _cleanup(self) -> None:
        self._confirm_action("cleanup")

    def _confirm_action(self, action: str) -> None:
        self._pending_action = action
        self.app.push_screen(_RecoveryConfirmation(), self._confirmed)

    async def _confirmed(self, confirmed: bool | None) -> None:
        if confirmed is not True or self._pending_action is None:
            self._pending_action = None
            return
        action, self._pending_action = self._pending_action, None
        try:
            callback = self._recovery_callback
            if callback is not None:
                succeeded = await callback(action, self.owner_generation)
                if not succeeded:
                    raise RuntimeError("recovery action failed")
                outcome = None
            elif action == "cleanup":
                generations = self.controller.list_orphan_generations()
                if inspect.isawaitable(generations):
                    generations = await generations
                outcome = self.controller.cleanup_orphans(generations)
            elif action == "restore":
                outcome = self.controller.restore_pending(self.owner_generation)
            else:
                outcome = self.controller.discard_pending(self.owner_generation)
            if inspect.isawaitable(outcome):
                await outcome
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            self.query_one("#recovery-notice", Static).update(
                "Recovery action failed safely"
            )
            return
        if action == "cleanup":
            self.query_one("#recovery-notice", Static).update(
                "Orphan credentials cleaned"
            )
            return
        self.dismiss(action)


class _RecoveryConfirmation(_ExplicitConfirmation):
    def compose(self) -> ComposeResult:
        yield Static("Confirm the selected recovery action")
        yield Button("Confirm", id="confirm-recovery-action", variant="warning")
        yield Button("Back", id="cancel-recovery-action")

    @on(Button.Pressed, "#confirm-recovery-action")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel-recovery-action")
    def _cancel(self) -> None:
        self.dismiss(False)


__all__ = [
    "SetupImportContext",
    "SetupRecoveryScreen",
    "SetupRootScreen",
    "SetupWizardScreen",
]
