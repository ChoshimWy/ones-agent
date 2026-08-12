"""Secret-safe seven-step setup orchestration without UI dependencies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol
import unicodedata
from uuid import uuid4

from .config import DeveloperWorkflowConfig
from .contracts import RepositoryGroupMapping, RepositoryMapping
from .setup_import import ImportDetection, import_selected
from .setup_models import (
    ActiveSetup,
    RuntimePublicConfig,
    RuntimeSecrets,
    SecretKind,
    SetupDraft,
    WorkflowDraft,
)
from .setup_repository import RepositoryGroupDraftBuilder, build_repository
from .setup_store import SetupStore, SetupStoreError
from .setup_validation import (
    ConnectionTestResult,
    SetupStep,
    SetupValidator,
    ValidationStatus,
)


class SetupActionError(RuntimeError):
    """A fixed, non-sensitive setup action failure."""


class _ConfigurationChanged(RuntimeError):
    """Internal CAS failure without draft or credential details."""


class RuntimeBuilder(Protocol):
    """Synchronous production runtime construction boundary."""

    def build(self, active: ActiveSetup, secrets: RuntimeSecrets) -> object: ...


@dataclass(frozen=True, slots=True)
class SetupControllerState:
    """Safe summary for future views; paths and raw values are deliberately absent."""

    current_step: SetupStep
    results: tuple[ConnectionTestResult, ...]
    repository_count: int
    repository_group_count: int
    secret_count: int
    review_confirmed: bool
    closed: bool
    error_category: str | None


_NOT_CONFIGURED = "invalid_field"
_SECRET_STEP: dict[SecretKind, SetupStep] = {
    SecretKind.ONES_EMAIL: SetupStep.ONES,
    SecretKind.ONES_PASSWORD: SetupStep.ONES,
    SecretKind.PROVIDER_TOKEN: SetupStep.PROVIDER,
    SecretKind.CODEX_API_KEY: SetupStep.CODEX,
    SecretKind.CODEX_AUTH_TOKEN: SetupStep.CODEX,
    SecretKind.GIT_ASKPASS: SetupStep.REPOSITORIES,
    SecretKind.GIT_SSH: SetupStep.REPOSITORIES,
    SecretKind.GIT_SSH_COMMAND: SetupStep.REPOSITORIES,
    SecretKind.SSH_ASKPASS: SetupStep.REPOSITORIES,
    SecretKind.SSH_AUTH_SOCK: SetupStep.REPOSITORIES,
}


def _fixed_result(
    step: SetupStep,
    *,
    status: ValidationStatus = ValidationStatus.NOT_CONFIGURED,
    category: str = _NOT_CONFIGURED,
) -> ConnectionTestResult:
    return ConnectionTestResult(step=step, status=status, category=category)


def _close_handle(handle: object | None) -> None:
    if handle is None:
        return
    close = getattr(handle, "close", None)
    if not callable(close):
        return
    try:
        close()
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise


class SetupController:
    """Serialize setup probes, commit one generation, then activate it once."""

    STEPS = (
        SetupStep.PROFILE,
        SetupStep.ONES,
        SetupStep.REPOSITORIES,
        SetupStep.PROVIDER,
        SetupStep.CODEX,
        SetupStep.PRIVATE_PATHS,
        SetupStep.REVIEW,
    )

    def __init__(
        self,
        *,
        profile_id: str,
        store: SetupStore,
        runtime_builder: RuntimeBuilder,
        runtime_bootstrap: object | None = None,
        validator: SetupValidator | None = None,
        profile_catalog: object | None = None,
        activation_timeout: float = 30.0,
        cleanup_timeout: float = 0.25,
        draft: SetupDraft | None = None,
    ) -> None:
        if (
            type(profile_id) is not str
            or not profile_id
            or isinstance(activation_timeout, bool)
            or not isinstance(activation_timeout, (int, float))
            or activation_timeout <= 0
            or isinstance(cleanup_timeout, bool)
            or not isinstance(cleanup_timeout, (int, float))
            or cleanup_timeout <= 0
        ):
            raise SetupActionError("setup configuration is invalid")
        bootstrap_validator = getattr(runtime_bootstrap, "validator", None)
        bootstrap_catalog = getattr(runtime_bootstrap, "catalog", None)
        actual_validator = validator or bootstrap_validator
        actual_catalog = profile_catalog or bootstrap_catalog
        if actual_validator is None or actual_catalog is None:
            raise SetupActionError("setup configuration is invalid")
        self._profile_id = profile_id
        self._store = store
        self._runtime_builder = runtime_builder
        self._validator = actual_validator
        self._profile_catalog = actual_catalog
        self._activation_timeout = float(activation_timeout)
        self._cleanup_timeout = float(cleanup_timeout)
        self._draft = (draft or SetupDraft()).model_copy(deep=True)
        self._results: dict[SetupStep, ConnectionTestResult] = {}
        self._secrets: dict[SecretKind, bytearray] = {}
        self._revision = 0
        self._review_confirmed = False
        self._closed = False
        self._operation_lock = asyncio.Lock()
        self._operation_task: asyncio.Task[Any] | None = None
        self._activation_error: str | None = None
        self._finalizing = False
        self._background_close_tasks: set[asyncio.Task[None]] = set()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def activation_error(self) -> str | None:
        return self._activation_error

    @property
    def current_step(self) -> SetupStep:
        for step in self.STEPS[:-1]:
            result = self._results.get(step)
            if result is None or result.status is not ValidationStatus.PASSED:
                return step
        return SetupStep.REVIEW

    @property
    def state(self) -> SetupControllerState:
        workflow = self._draft.workflow
        return SetupControllerState(
            current_step=self.current_step,
            results=tuple(self.result_for(step) for step in self.STEPS),
            repository_count=len(workflow.repositories),
            repository_group_count=len(workflow.repository_groups),
            secret_count=len(self._secrets),
            review_confirmed=self._review_confirmed,
            closed=self._closed,
            error_category=self._activation_error,
        )

    @property
    def draft(self) -> SetupDraft:
        """Return a detached non-secret draft for form population."""

        return self._draft.model_copy(deep=True)

    def result_for(self, step: SetupStep) -> ConnectionTestResult:
        self._require_step(step)
        return self._results.get(step, _fixed_result(step))

    def apply_runtime(
        self,
        runtime: RuntimePublicConfig,
        *,
        changed_step: SetupStep = SetupStep.ONES,
    ) -> None:
        self._ensure_mutable()
        if type(runtime) is not RuntimePublicConfig:
            raise SetupActionError("public configuration is invalid")
        invalidation_step = self._runtime_invalidation_step(
            self._draft.runtime, runtime, changed_step
        )
        self._draft.runtime = runtime.model_copy(deep=True)
        self._changed(invalidation_step)

    def apply_workflow(
        self,
        workflow: WorkflowDraft,
        *,
        changed_step: SetupStep = SetupStep.REPOSITORIES,
    ) -> None:
        self._ensure_mutable()
        if type(workflow) is not WorkflowDraft:
            raise SetupActionError("workflow configuration is invalid")
        invalidation_step = self._workflow_invalidation_step(
            self._draft.workflow, workflow, changed_step
        )
        self._draft.workflow = workflow.model_copy(deep=True)
        self._changed(invalidation_step)

    def add_repository(self, **fields: object) -> RepositoryMapping:
        """Build a strict repository draft through the setup repository boundary."""

        self._ensure_mutable()
        try:
            repository = build_repository(**fields)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SetupActionError("repository configuration is invalid") from None
        workflow = self._draft.workflow.model_copy(deep=True)
        workflow.repositories = (*workflow.repositories, repository)
        self.apply_workflow(workflow, changed_step=SetupStep.REPOSITORIES)
        return repository.model_copy(deep=True)

    def add_repository_group(
        self,
        builder: RepositoryGroupDraftBuilder,
        *,
        primary: str,
    ) -> RepositoryGroupMapping:
        self._ensure_mutable()
        if not isinstance(builder, RepositoryGroupDraftBuilder):
            raise SetupActionError("repository group is invalid")
        try:
            group = builder.build(primary=primary)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SetupActionError("repository group is invalid") from None
        workflow = self._draft.workflow.model_copy(deep=True)
        workflow.repository_groups = (*workflow.repository_groups, group)
        self.apply_workflow(workflow, changed_step=SetupStep.REPOSITORIES)
        return group.model_copy(deep=True)

    def set_secret(self, kind: SecretKind, value: str) -> None:
        self._ensure_mutable()
        if type(kind) is not SecretKind or type(value) is not str or not value:
            raise SetupActionError("credential is invalid")
        try:
            encoded = bytearray(value.encode("utf-8", errors="strict"))
        except UnicodeError:
            raise SetupActionError("credential is invalid") from None
        if len(encoded) > 2560 or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
            for character in value
        ):
            self._zero(encoded)
            raise SetupActionError("credential is invalid")
        old = self._secrets.get(kind)
        if old is not None:
            self._zero(old)
        self._secrets[kind] = encoded
        self._changed(_SECRET_STEP[kind])

    def import_secrets(
        self,
        *,
        detection: ImportDetection,
        environment: Mapping[str, str],
        dotenv_values: Mapping[str, str],
        selected: tuple[SecretKind, ...],
        source_choice: Mapping[SecretKind, str] | None = None,
    ) -> None:
        """Import only an explicitly selected subset of previously detected kinds."""

        self._ensure_mutable()
        detected = set(detection.environment) | set(detection.dotenv)
        if any(kind not in detected for kind in selected):
            raise SetupActionError("credential selection is invalid")
        try:
            imported = import_selected(
                environment,
                dotenv_values,
                selected,
                source_choice=source_choice,  # type: ignore[arg-type]
            )
            for kind, value in imported.values.items():
                self.set_secret(kind, value)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SetupActionError("credential import failed") from None

    def secret_presence(self, kind: SecretKind) -> bool:
        if type(kind) is not SecretKind:
            return False
        value = self._secrets.get(kind)
        return value is not None and bool(value)

    def cancel_edit(self) -> None:
        """Cancel an active probe and forget every transient credential."""

        if self._closed:
            self._clear_transient_secrets()
            return
        self._revision += 1
        self._review_confirmed = False
        task = self._operation_task
        current = asyncio.current_task() if self._has_running_loop() else None
        if task is not None and task is not current and not task.done():
            task.cancel()
        self._clear_transient_secrets()

    def confirm_review(self) -> None:
        self._ensure_open()
        if not self._all_required_passed():
            raise SetupActionError("configuration is incomplete")
        self._review_confirmed = True
        self._results[SetupStep.REVIEW] = _fixed_result(
            SetupStep.REVIEW,
            status=ValidationStatus.PASSED,
            category="ok",
        )

    async def test_step(
        self, step: SetupStep, probe: object | None = None
    ) -> ConnectionTestResult:
        self._ensure_open()
        self._require_step(step)
        if step is SetupStep.REVIEW:
            raise SetupActionError("review requires explicit confirmation")
        if not self._step_available(step):
            raise SetupActionError("step is unavailable")
        async with self._operation_lock:
            self._ensure_open()
            if not self._step_available(step):
                raise SetupActionError("step is unavailable")
            task = asyncio.current_task()
            self._operation_task = task
            revision = self._revision
            self._results[step] = _fixed_result(
                step, status=ValidationStatus.PENDING, category="ok"
            )
            try:
                result = await self._probe(step, probe)
                if revision != self._revision:
                    self._results.pop(step, None)
                    raise SetupActionError("configuration changed")
                if result.step is not step:
                    result = _fixed_result(
                        step,
                        status=ValidationStatus.FAILED,
                        category="incompatible",
                    )
                self._results[step] = result
                if result.status is not ValidationStatus.PASSED:
                    self._invalidate_after(step, include_step=False)
                return result
            except asyncio.CancelledError:
                self._results.pop(step, None)
                raise
            except SetupActionError:
                raise
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                result = _fixed_result(
                    step,
                    status=ValidationStatus.FAILED,
                    category="incompatible",
                )
                self._results[step] = result
                self._invalidate_after(step, include_step=False)
                return result
            finally:
                if self._operation_task is task:
                    self._operation_task = None

    async def activate_existing(self) -> object | None:
        """Load without mutating the active pointer; failures enter safe recovery."""

        if self._closed:
            return None
        async with self._operation_lock:
            self._operation_task = asyncio.current_task()
            try:
                document = await asyncio.to_thread(
                    self._store.load_or_empty, profile_id=self._profile_id
                )
                if document.active is None:
                    return None
                secrets = await asyncio.to_thread(
                    self._store.read_active_secrets, document
                )
                handle = await self._bounded_build(document.active, secrets)
                self._activation_error = None
                return handle
            except asyncio.CancelledError:
                self._clear_transient_secrets()
                raise
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    self._clear_transient_secrets()
                    raise
                self._activation_error = "runtime validation failed"
                return None
            finally:
                self._operation_task = None

    async def save_and_activate(self, *, confirmed: bool = False) -> object:
        """Commit, reload, build, and finalize exactly one candidate generation."""

        self._ensure_open()
        async with self._operation_lock:
            self._ensure_open()
            self._operation_task = asyncio.current_task()
            if not self._all_required_passed():
                self._operation_task = None
                raise SetupActionError("configuration is incomplete")
            if confirmed is True:
                self.confirm_review()
            if not self._review_confirmed:
                self._operation_task = None
                raise SetupActionError("review confirmation is required")
            committed = False
            finalized = False
            handle: object | None = None
            failure_message: str | None = None
            candidate: ActiveSetup | None = None
            profile_id = self._profile_id
            try:
                candidate, secrets = self._build_candidate()
                revision = self._revision
                commit_task = asyncio.create_task(
                    asyncio.to_thread(
                        self._store.commit, profile_id, candidate, secrets
                    )
                )
                try:
                    document = await asyncio.shield(commit_task)
                except asyncio.CancelledError:
                    succeeded, harvested, _ = await self._harvest_task(commit_task)
                    if succeeded:
                        document = harvested
                        committed = True
                    elif candidate is not None:
                        detection_task = asyncio.create_task(
                            self._candidate_is_active(candidate.generation)
                        )
                        detected, active, _ = await self._harvest_task(
                            detection_task
                        )
                        committed = bool(detected and active)
                    raise
                committed = True
                self._require_revision(revision)
                if document.active is None:
                    raise SetupStoreError("active configuration is unavailable")
                persisted = await asyncio.to_thread(
                    self._store.read_active_secrets, document
                )
                self._require_revision(revision)
                handle = await self._bounded_build(document.active, persisted)
                self._require_revision(revision)
                # Once finalization starts, synchronous form mutations are rejected.
                # This closes the only gap between the last revision check and the
                # irreversible cleanup of the previous generation.
                self._finalizing = True
                self._require_revision(revision)
                finalize_task = asyncio.create_task(
                    asyncio.to_thread(self._store.finalize_activation, profile_id)
                )
                try:
                    await asyncio.shield(finalize_task)
                    finalized = True
                except asyncio.CancelledError:
                    finalized, _, _ = await self._harvest_task(finalize_task)
                    raise
                finally:
                    self._finalizing = False
                self._activation_error = None
                self._clear_transient_secrets()
                self._closed = True
                return handle
            except asyncio.CancelledError:
                cleanup_task = asyncio.create_task(
                    self._cleanup_save(
                        handle,
                        profile_id=profile_id,
                        rollback=committed and not finalized,
                    )
                )
                await self._harvest_task(cleanup_task)
                raise
            except BaseException as error:
                control_flow = isinstance(error, (KeyboardInterrupt, SystemExit))
                cleanup_cancelled = False
                if not committed and candidate is not None:
                    detection_task = asyncio.create_task(
                        self._candidate_is_active(candidate.generation)
                    )
                    detected, active, interrupted = await self._harvest_task(
                        detection_task
                    )
                    cleanup_cancelled = cleanup_cancelled or interrupted
                    committed = bool(detected and active)
                cleanup_task = asyncio.create_task(
                    self._cleanup_save(
                        handle,
                        profile_id=profile_id,
                        rollback=committed and not finalized,
                    )
                )
                _, _, interrupted = await self._harvest_task(cleanup_task)
                cleanup_cancelled = cleanup_cancelled or interrupted
                if isinstance(error, _ConfigurationChanged):
                    self._activation_error = "configuration changed"
                else:
                    self._activation_error = (
                        "runtime validation failed"
                        if committed
                        else "configuration save failed"
                    )
                self._review_confirmed = False
                self._results[SetupStep.REVIEW] = _fixed_result(
                    SetupStep.REVIEW,
                    status=ValidationStatus.FAILED,
                    category="incompatible",
                )
                if control_flow:
                    raise
                if cleanup_cancelled:
                    failure_message = None
                else:
                    failure_message = self._activation_error
            finally:
                self._finalizing = False
                self._operation_task = None
            if failure_message is not None:
                # Raise outside the raw exception handler so neither __cause__ nor
                # __context__ retains a backend exception that could carry a secret.
                raise SetupActionError(failure_message) from None
            raise asyncio.CancelledError from None

    def close(self) -> None:
        if self._closed:
            self._clear_transient_secrets()
            return
        self.cancel_edit()
        self._closed = True

    async def _probe(
        self, step: SetupStep, probe: object | None
    ) -> ConnectionTestResult:
        if step is SetupStep.PROFILE:
            selected = self._draft.workflow.sandbox_permission_profile or self._profile_id
            await asyncio.to_thread(self._profile_catalog.require_selected, selected)
            return _fixed_result(
                step, status=ValidationStatus.PASSED, category="ok"
            )
        method_name = {
            SetupStep.ONES: "probe_ones",
            SetupStep.REPOSITORIES: "probe_repository",
            SetupStep.PROVIDER: "probe_provider",
            SetupStep.CODEX: "probe_codex",
            SetupStep.PRIVATE_PATHS: "probe_private_paths",
        }[step]
        method = getattr(self._validator, method_name)
        if step is SetupStep.REPOSITORIES and isinstance(probe, (tuple, list)):
            if not probe:
                return _fixed_result(
                    step,
                    status=ValidationStatus.FAILED,
                    category="invalid_field",
                )
            for item in probe:
                result = await method(item)
                if result.status is not ValidationStatus.PASSED:
                    return result
            return _fixed_result(step, status=ValidationStatus.PASSED, category="ok")
        return await method(probe)

    def _build_candidate(self) -> tuple[ActiveSetup, RuntimeSecrets]:
        try:
            runtime = self._draft.runtime
            if runtime is None or not self._secrets:
                raise ValueError
            raw_workflow = self._draft.workflow.model_dump(
                mode="python", round_trip=True
            )
            workflow = DeveloperWorkflowConfig.model_validate(raw_workflow)
            values = {
                kind: bytes(raw).decode("utf-8", errors="strict")
                for kind, raw in self._secrets.items()
            }
            secrets = RuntimeSecrets(MappingProxyType(values))
            kinds = tuple(sorted(values, key=lambda kind: kind.value))
            candidate = ActiveSetup(
                generation=uuid4().hex,
                runtime=runtime,
                workflow=workflow,
                credential_kinds=kinds,
            )
            return candidate, secrets
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise SetupActionError("configuration is incomplete") from None

    async def _bounded_build(
        self, active: ActiveSetup, secrets: RuntimeSecrets
    ) -> object:
        task = asyncio.create_task(
            asyncio.to_thread(self._runtime_builder.build, active, secrets)
        )
        try:
            return await asyncio.wait_for(
                asyncio.shield(task), timeout=self._activation_timeout
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.add_done_callback(self._close_late_build)
            raise

    async def _harvest_task(
        self, task: asyncio.Task[Any]
    ) -> tuple[bool, Any | None, bool]:
        """Wait for one owned task to become terminal despite repeated cancellation."""

        interrupted = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted = True
                continue
            except BaseException:
                break
        try:
            return True, task.result(), interrupted
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return False, None, interrupted

    async def _cleanup_save(
        self,
        handle: object | None,
        *,
        profile_id: str,
        rollback: bool,
    ) -> None:
        """Run all failure cleanup in one child task protected from parent cancellation."""

        try:
            if rollback:
                await self._rollback(profile_id)
        finally:
            self._clear_transient_secrets()
        if handle is not None:
            close_task = asyncio.create_task(
                asyncio.to_thread(_close_handle, handle)
            )
            done, _ = await asyncio.wait(
                {close_task}, timeout=self._cleanup_timeout
            )
            if close_task in done:
                self._consume_close_task(close_task)
            else:
                self._background_close_tasks.add(close_task)
                close_task.add_done_callback(self._consume_close_task)

    def _consume_close_task(self, task: asyncio.Task[None]) -> None:
        """Consume a detached close outcome and release its lifecycle reference."""

        self._background_close_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except BaseException:
            pass

    @staticmethod
    def _close_late_build(task: asyncio.Task[object]) -> None:
        if task.cancelled():
            return
        try:
            handle = task.result()
        except BaseException:
            return
        _close_handle(handle)

    async def _rollback(self, profile_id: str) -> None:
        try:
            await asyncio.shield(
                asyncio.to_thread(self._store.restore_previous, profile_id)
            )
        except BaseException:
            # Cleanup is best-effort and must not replace the primary control-flow
            # exception; the store itself preserves whichever pointer is durable.
            pass

    async def _candidate_is_active(self, generation: str) -> bool:
        try:
            document = await asyncio.to_thread(
                self._store.load_or_empty, profile_id=self._profile_id
            )
            return (
                document.active is not None
                and document.active.generation == generation
            )
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return False

    def _require_revision(self, expected: int) -> None:
        if self._revision != expected:
            raise _ConfigurationChanged("configuration changed")

    def _runtime_invalidation_step(
        self,
        previous: RuntimePublicConfig | None,
        current: RuntimePublicConfig,
        requested: SetupStep,
    ) -> SetupStep:
        self._require_step(requested)
        detected: list[SetupStep] = []
        field_steps = {
            SetupStep.ONES: (
                "ones_base_url",
                "ones_team_id",
                "ones_issue_type_id",
                "ones_comment_list_path_template",
            ),
            SetupStep.PROVIDER: (
                "provider_host",
                "provider_api_url",
                "git_author_name",
                "git_author_email",
            ),
            SetupStep.CODEX: ("codex_auth_mode", "codex_home"),
        }
        for step, fields in field_steps.items():
            if previous is None or any(
                getattr(previous, field_name) != getattr(current, field_name)
                for field_name in fields
            ):
                detected.append(step)
        return self._earliest_step(requested, *detected)

    def _workflow_invalidation_step(
        self,
        previous: WorkflowDraft,
        current: WorkflowDraft,
        requested: SetupStep,
    ) -> SetupStep:
        self._require_step(requested)
        field_steps = {
            "sandbox_permission_profile": SetupStep.PROFILE,
            "repositories": SetupStep.REPOSITORIES,
            "repository_groups": SetupStep.REPOSITORIES,
            "publishing": SetupStep.PROVIDER,
            "max_codex_attempts": SetupStep.CODEX,
            "tui_max_concurrency": SetupStep.CODEX,
            "run_root": SetupStep.PRIVATE_PATHS,
            "mirror_root": SetupStep.PRIVATE_PATHS,
            "worktree_root": SetupStep.PRIVATE_PATHS,
        }
        detected = tuple(
            step
            for field_name, step in field_steps.items()
            if getattr(previous, field_name) != getattr(current, field_name)
        )
        return self._earliest_step(requested, *detected)

    def _earliest_step(
        self, requested: SetupStep, *detected: SetupStep
    ) -> SetupStep:
        return min(
            (requested, *detected),
            key=self.STEPS.index,
        )

    def _changed(self, step: SetupStep) -> None:
        self._require_step(step)
        self._revision += 1
        self._review_confirmed = False
        self._activation_error = None
        self._invalidate_after(step, include_step=True)

    def _invalidate_after(self, step: SetupStep, *, include_step: bool) -> None:
        start = self.STEPS.index(step) + (0 if include_step else 1)
        for candidate in self.STEPS[start:]:
            self._results.pop(candidate, None)

    def _step_available(self, step: SetupStep) -> bool:
        index = self.STEPS.index(step)
        if index == 0:
            return True
        return all(
            self._results.get(previous) is not None
            and self._results[previous].status is ValidationStatus.PASSED
            for previous in self.STEPS[:index]
        ) or (
            self._results.get(step) is not None
            and all(
                self._results.get(previous) is not None
                and self._results[previous].status is ValidationStatus.PASSED
                for previous in self.STEPS[:index]
            )
        )

    def _all_required_passed(self) -> bool:
        return all(
            self._results.get(step) is not None
            and self._results[step].status is ValidationStatus.PASSED
            for step in self.STEPS[:-1]
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise SetupActionError("setup is closed")

    def _ensure_mutable(self) -> None:
        self._ensure_open()
        if self._finalizing:
            raise SetupActionError("setup operation is in progress")

    def _require_step(self, step: SetupStep) -> None:
        if type(step) is not SetupStep or step not in self.STEPS:
            raise SetupActionError("step is invalid")

    def _clear_transient_secrets(self) -> None:
        for raw in self._secrets.values():
            self._zero(raw)
        self._secrets.clear()

    @staticmethod
    def _zero(raw: bytearray) -> None:
        for index in range(len(raw)):
            raw[index] = 0

    @staticmethod
    def _has_running_loop() -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True


__all__ = [
    "RuntimeBuilder",
    "SetupActionError",
    "SetupController",
    "SetupControllerState",
]
