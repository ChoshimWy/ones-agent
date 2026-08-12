from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from types import MappingProxyType
from typing import Any

import pytest

from src.developer_workflow.config import PublishingConfig, PublishingProvider
from src.developer_workflow.contracts import RepositoryMapping
from src.developer_workflow.credential_store import CredentialStoreError
from src.developer_workflow.setup_controller import SetupActionError, SetupController
from src.developer_workflow.setup_models import (
    ActiveSetup,
    RuntimePublicConfig,
    RuntimeSecrets,
    SecretKind,
    SetupDocument,
    SetupDraft,
    SetupValidationError,
    WorkflowDraft,
)
from src.developer_workflow.setup_store import SetupStore
from src.developer_workflow.setup_validation import (
    ConnectionTestResult,
    SetupStep,
    ValidationStatus,
)


def _runtime() -> RuntimePublicConfig:
    return RuntimePublicConfig(
        ones_base_url="https://ones.example.test",
        ones_team_id="team-1",
        ones_issue_type_id="issue-1",
        ones_comment_list_path_template="/team/{team_id}/item/{item_id}",
        provider_host="provider.example.test",
        provider_api_url="https://provider.example.test/api",
        git_author_name="ONES Agent",
        git_author_email="agent@example.test",
        codex_auth_mode="credential",
    )


def _workflow(tmp_path: Path) -> WorkflowDraft:
    repository = RepositoryMapping(
        key="sdk",
        project_id="project-1",
        iteration_id="iteration-1",
        repo_url="https://git.example.test/sdk.git",
        repo_name="sdk",
    )
    return WorkflowDraft(
        run_root=tmp_path / "runs",
        mirror_root=tmp_path / "mirrors",
        worktree_root=tmp_path / "worktrees",
        sandbox_permission_profile="managed-profile",
        repositories=(repository,),
        publishing=PublishingConfig(
            provider=PublishingProvider.GITHUB,
            default_target_branch="main",
        ),
    )


def _result(step: SetupStep, passed: bool = True) -> ConnectionTestResult:
    return ConnectionTestResult(
        step=step,
        status=ValidationStatus.PASSED if passed else ValidationStatus.FAILED,
        category="ok" if passed else "unreachable",
    )


class FakeCatalog:
    def require_selected(self, profile: str) -> str:
        if profile != "managed-profile":
            raise ValueError("SECRET profile")
        return profile


class FakeValidator:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    async def _probe(self, step: SetupStep, probe: object) -> ConnectionTestResult:
        del probe
        if self.block:
            self.started.set()
            await self.release.wait()
        return _result(step)

    async def probe_ones(self, probe: object) -> ConnectionTestResult:
        return await self._probe(SetupStep.ONES, probe)

    async def probe_repository(self, probe: object) -> ConnectionTestResult:
        return await self._probe(SetupStep.REPOSITORIES, probe)

    async def probe_provider(self, probe: object) -> ConnectionTestResult:
        return await self._probe(SetupStep.PROVIDER, probe)

    async def probe_codex(self, probe: object) -> ConnectionTestResult:
        return await self._probe(SetupStep.CODEX, probe)

    async def probe_private_paths(self, probe: object) -> ConnectionTestResult:
        return await self._probe(SetupStep.PRIVATE_PATHS, probe)


class FakeBootstrap:
    def __init__(self) -> None:
        self.catalog = FakeCatalog()
        self.validator = FakeValidator()


class Handle:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class FakeRuntimeBuilder:
    def __init__(self) -> None:
        self.calls: list[tuple[ActiveSetup, RuntimeSecrets]] = []
        self.handle = Handle()
        self.error: BaseException | None = None

    def build(self, active: ActiveSetup, secrets: RuntimeSecrets) -> Handle:
        self.calls.append((active, secrets))
        if self.error is not None:
            raise self.error
        return self.handle


class FakeStore:
    def __init__(self) -> None:
        self.document = SetupDocument(profile_id="managed-profile")
        self.commits = 0
        self.restores = 0
        self.finalizes = 0
        self.finalize_error: BaseException | None = None

    def load_or_empty(self, *, profile_id: str) -> SetupDocument:
        assert profile_id == "managed-profile"
        return self.document

    def commit(
        self, profile_id: str, candidate: ActiveSetup, secrets: RuntimeSecrets
    ) -> SetupDocument:
        assert profile_id == "managed-profile"
        assert set(candidate.credential_kinds) == set(secrets.values)
        self.commits += 1
        self.document = self.document.validated_update(
            active=candidate, previous=self.document.active
        )
        return self.document

    def read_active_secrets(self, document: SetupDocument) -> RuntimeSecrets:
        assert document.active is not None
        return RuntimeSecrets(
            MappingProxyType(
                {kind: "persisted-value" for kind in document.active.credential_kinds}
            )
        )

    def restore_previous(self, profile_id: str) -> SetupDocument:
        assert profile_id == "managed-profile"
        self.restores += 1
        self.document = self.document.validated_update(
            active=self.document.previous, previous=None
        )
        return self.document

    def finalize_activation(self, profile_id: str) -> SetupDocument:
        assert profile_id == "managed-profile"
        self.finalizes += 1
        if self.finalize_error is not None:
            raise self.finalize_error
        self.document = self.document.validated_update(previous=None)
        return self.document


class IntegrationCredentials:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], RuntimeSecrets] = {}

    def write_fresh_generation(
        self, profile_id: str, generation: str, secrets: RuntimeSecrets
    ) -> bool:
        key = (profile_id, generation)
        if key in self.data:
            raise CredentialStoreError("credential operation failed")
        self.data[key] = secrets
        return True

    def read_generation(
        self, profile_id: str, generation: str, kinds: tuple[SecretKind, ...]
    ) -> RuntimeSecrets:
        try:
            values = self.data[(profile_id, generation)]
            return RuntimeSecrets({kind: values.require(kind) for kind in kinds})
        except (KeyError, SetupValidationError):
            raise CredentialStoreError("credential operation failed") from None

    def delete_generation(self, profile_id: str, generation: str) -> None:
        self.data.pop((profile_id, generation), None)

    def list_generations(self, profile_id: str) -> tuple[str, ...]:
        return tuple(sorted(generation for profile, generation in self.data if profile == profile_id))

def _controller(
    tmp_path: Path,
    *,
    store: FakeStore | None = None,
    builder: FakeRuntimeBuilder | None = None,
) -> tuple[SetupController, FakeStore, FakeRuntimeBuilder, FakeBootstrap]:
    actual_store = store or FakeStore()
    actual_builder = builder or FakeRuntimeBuilder()
    bootstrap = FakeBootstrap()
    controller = SetupController(
        profile_id="managed-profile",
        store=actual_store,
        runtime_bootstrap=bootstrap,
        runtime_builder=actual_builder,
        activation_timeout=1.0,
    )
    controller.apply_runtime(_runtime())
    controller.apply_workflow(_workflow(tmp_path))
    for kind in (SecretKind.ONES_PASSWORD, SecretKind.PROVIDER_TOKEN):
        controller.set_secret(kind, "TOKEN-SECRET")
    return controller, actual_store, actual_builder, bootstrap


async def _pass_all(controller: SetupController) -> None:
    await controller.test_step(SetupStep.PROFILE)
    for step in SetupController.STEPS[1:-1]:
        await controller.test_step(step, object())


@pytest.mark.asyncio
async def test_incomplete_setup_never_builds_runtime(tmp_path: Path) -> None:
    controller, _, builder, _ = _controller(tmp_path)
    with pytest.raises(SetupActionError, match="configuration is incomplete"):
        await controller.save_and_activate(confirmed=True)
    assert builder.calls == []


def test_cancel_clears_transient_secrets(tmp_path: Path) -> None:
    controller, _, _, _ = _controller(tmp_path)
    assert "TOKEN-SECRET" not in repr(controller)
    controller.cancel_edit()
    assert controller.secret_presence(SecretKind.ONES_PASSWORD) is False


@pytest.mark.asyncio
async def test_steps_cannot_be_skipped_and_edit_invalidates_downstream(
    tmp_path: Path,
) -> None:
    controller, _, _, _ = _controller(tmp_path)
    with pytest.raises(SetupActionError, match="step is unavailable"):
        await controller.test_step(SetupStep.PROVIDER, object())
    await _pass_all(controller)
    assert controller.current_step is SetupStep.REVIEW
    controller.apply_runtime(_runtime(), changed_step=SetupStep.ONES)
    assert controller.result_for(SetupStep.PROFILE).status is ValidationStatus.PASSED
    assert controller.result_for(SetupStep.ONES).status is ValidationStatus.NOT_CONFIGURED
    assert controller.current_step is SetupStep.ONES


@pytest.mark.asyncio
async def test_review_requires_all_six_steps_and_explicit_confirmation(
    tmp_path: Path,
) -> None:
    controller, _, _, _ = _controller(tmp_path)
    await _pass_all(controller)
    with pytest.raises(SetupActionError, match="review confirmation is required"):
        await controller.save_and_activate()


@pytest.mark.asyncio
async def test_concurrent_save_commits_and_builds_once(tmp_path: Path) -> None:
    controller, store, builder, _ = _controller(tmp_path)
    await _pass_all(controller)
    results = await asyncio.gather(
        controller.save_and_activate(confirmed=True),
        controller.save_and_activate(confirmed=True),
        return_exceptions=True,
    )
    assert sum(isinstance(value, Handle) for value in results) == 1
    assert store.commits == 1
    assert len(builder.calls) == 1


@pytest.mark.asyncio
async def test_draft_change_discards_stale_connection_result(tmp_path: Path) -> None:
    controller, _, _, bootstrap = _controller(tmp_path)
    await controller.test_step(SetupStep.PROFILE)
    bootstrap.validator.block = True
    task = asyncio.create_task(controller.test_step(SetupStep.ONES, object()))
    await bootstrap.validator.started.wait()
    controller.apply_runtime(_runtime(), changed_step=SetupStep.ONES)
    bootstrap.validator.release.set()
    with pytest.raises(SetupActionError, match="configuration changed"):
        await task
    assert controller.result_for(SetupStep.ONES).status is ValidationStatus.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_cancel_during_test_cancels_probe_and_clears_secrets(tmp_path: Path) -> None:
    controller, _, _, bootstrap = _controller(tmp_path)
    await controller.test_step(SetupStep.PROFILE)
    bootstrap.validator.block = True
    task = asyncio.create_task(controller.test_step(SetupStep.ONES, object()))
    await bootstrap.validator.started.wait()
    controller.cancel_edit()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert controller.secret_presence(SecretKind.PROVIDER_TOKEN) is False


@pytest.mark.asyncio
async def test_generation_is_fresh_and_credential_kinds_are_sorted(
    tmp_path: Path,
) -> None:
    controller, _, builder, _ = _controller(tmp_path)
    await _pass_all(controller)
    await controller.save_and_activate(confirmed=True)
    active, _ = builder.calls[0]
    assert len(active.generation) == 32
    assert active.generation != "0" * 32
    assert active.credential_kinds == tuple(
        sorted(active.credential_kinds, key=lambda kind: kind.value)
    )
    assert controller.closed is True
    assert controller.secret_presence(SecretKind.ONES_PASSWORD) is False


@pytest.mark.asyncio
async def test_build_failure_closes_partial_handle_rolls_back_and_sanitizes(
    tmp_path: Path,
) -> None:
    old_controller, store, _, _ = _controller(tmp_path)
    await _pass_all(old_controller)
    old_handle = await old_controller.save_and_activate(confirmed=True)
    assert isinstance(old_handle, Handle)
    controller, _, builder, _ = _controller(tmp_path, store=store)
    await _pass_all(controller)
    builder.error = RuntimeError("TOKEN-SECRET")
    with pytest.raises(SetupActionError, match="runtime validation failed") as error:
        await controller.save_and_activate(confirmed=True)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert store.restores == 1
    assert controller.current_step is SetupStep.REVIEW
    assert controller.secret_presence(SecretKind.ONES_PASSWORD) is False


@pytest.mark.asyncio
async def test_finalize_failure_closes_handle_and_rolls_back(tmp_path: Path) -> None:
    old_controller, store, _, _ = _controller(tmp_path)
    await _pass_all(old_controller)
    await old_controller.save_and_activate(confirmed=True)
    store.finalize_error = RuntimeError("TOKEN-SECRET")
    controller, _, builder, _ = _controller(tmp_path, store=store)
    await _pass_all(controller)
    with pytest.raises(SetupActionError, match="runtime validation failed"):
        await controller.save_and_activate(confirmed=True)
    assert builder.handle.closed == 1
    assert store.restores == 1


@pytest.mark.asyncio
async def test_cancelled_save_rolls_back_committed_candidate(tmp_path: Path) -> None:
    old_controller, store, _, _ = _controller(tmp_path)
    await _pass_all(old_controller)
    await old_controller.save_and_activate(confirmed=True)

    class BlockingBuilder(FakeRuntimeBuilder):
        def build(self, active: ActiveSetup, secrets: RuntimeSecrets) -> Handle:
            import time

            self.calls.append((active, secrets))
            time.sleep(0.2)
            return self.handle

    builder = BlockingBuilder()
    controller, _, _, _ = _controller(tmp_path, store=store, builder=builder)
    await _pass_all(controller)
    task = asyncio.create_task(controller.save_and_activate(confirmed=True))
    while not builder.calls:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.restores == 1
    assert controller.secret_presence(SecretKind.ONES_PASSWORD) is False
    await asyncio.sleep(0.25)
    assert builder.handle.closed == 1


@pytest.mark.asyncio
async def test_activate_existing_failure_is_fixed_and_does_not_mutate_active(
    tmp_path: Path,
) -> None:
    controller, store, _, _ = _controller(tmp_path)
    await _pass_all(controller)
    await controller.save_and_activate(confirmed=True)
    active = store.document.active
    failing = FakeRuntimeBuilder()
    failing.error = RuntimeError("TOKEN-SECRET")
    loader, _, _, _ = _controller(tmp_path, store=store, builder=failing)
    assert await loader.activate_existing() is None
    assert store.document.active == active
    assert loader.activation_error == "runtime validation failed"


def test_close_is_idempotent_and_public_state_has_only_summaries(tmp_path: Path) -> None:
    controller, _, _, _ = _controller(tmp_path)
    state = controller.state
    assert state.repository_count == 1
    assert state.repository_group_count == 0
    assert not hasattr(state, "run_root")
    assert not hasattr(state, "secrets")
    controller.close()
    controller.close()
    assert controller.closed is True
    with pytest.raises(SetupActionError, match="setup is closed"):
        controller.set_secret(SecretKind.ONES_PASSWORD, "TOKEN-SECRET")


@pytest.mark.asyncio
async def test_real_setup_store_commit_reload_and_finalize(tmp_path: Path) -> None:
    credentials = IntegrationCredentials()
    store = SetupStore(
        credentials,  # type: ignore[arg-type]
        config_path=tmp_path / "private" / "config.json",
    )
    builder = FakeRuntimeBuilder()
    controller, _, _, _ = _controller(
        tmp_path,
        store=store,  # type: ignore[arg-type]
        builder=builder,
    )
    await _pass_all(controller)
    handle = await controller.save_and_activate(confirmed=True)
    loaded = store.load()
    assert handle is builder.handle
    assert loaded.active is not None
    assert loaded.previous is None
    assert credentials.read_generation(
        loaded.profile_id,
        loaded.active.generation,
        loaded.active.credential_kinds,
    ).require(SecretKind.ONES_PASSWORD) == "TOKEN-SECRET"


@pytest.mark.asyncio
async def test_first_activation_failure_with_real_store_restores_empty_document(
    tmp_path: Path,
) -> None:
    credentials = IntegrationCredentials()
    store = SetupStore(
        credentials,  # type: ignore[arg-type]
        config_path=tmp_path / "private-failure" / "config.json",
    )
    builder = FakeRuntimeBuilder()
    builder.error = RuntimeError("TOKEN-SECRET")
    controller, _, _, _ = _controller(
        tmp_path,
        store=store,  # type: ignore[arg-type]
        builder=builder,
    )
    await _pass_all(controller)

    with pytest.raises(SetupActionError, match="^runtime validation failed$"):
        await controller.save_and_activate(confirmed=True)

    loaded = store.load()
    assert loaded.active is None
    assert loaded.previous is None
    assert store.orphan_generations() == ()


@pytest.mark.asyncio
async def test_commit_post_replace_failure_is_detected_and_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials = IntegrationCredentials()
    store = SetupStore(
        credentials,  # type: ignore[arg-type]
        config_path=tmp_path / "private-commit-failure" / "config.json",
    )
    builder = FakeRuntimeBuilder()
    controller, _, _, _ = _controller(
        tmp_path,
        store=store,  # type: ignore[arg-type]
        builder=builder,
    )
    await _pass_all(controller)
    monkeypatch.setattr(
        "src.developer_workflow.setup_store._fsync_directory",
        lambda _path: (_ for _ in ()).throw(OSError("TOKEN-SECRET")),
    )

    with pytest.raises(SetupActionError, match="^runtime validation failed$"):
        await controller.save_and_activate(confirmed=True)

    assert builder.calls == []
    assert store.load().active is None
    assert store.orphan_generations() == ()


@pytest.mark.asyncio
async def test_cancel_during_finalize_harvests_success_without_rollback(
    tmp_path: Path,
) -> None:
    class BlockingFinalizeStore(FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.finalize_started = Event()
            self.finalize_release = Event()

        def finalize_activation(self, profile_id: str) -> SetupDocument:
            self.finalize_started.set()
            assert self.finalize_release.wait(2)
            return super().finalize_activation(profile_id)

    store = BlockingFinalizeStore()
    controller, _, builder, _ = _controller(tmp_path, store=store)
    await _pass_all(controller)
    task = asyncio.create_task(controller.save_and_activate(confirmed=True))
    assert await asyncio.to_thread(store.finalize_started.wait, 2)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    store.finalize_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.finalizes == 1
    assert store.restores == 0
    assert store.document.active is not None
    assert store.document.previous is None
    assert builder.handle.closed == 1


@pytest.mark.asyncio
async def test_runtime_diff_cannot_be_hidden_by_later_changed_step(
    tmp_path: Path,
) -> None:
    controller, _, _, _ = _controller(tmp_path)
    await _pass_all(controller)
    changed = _runtime().model_copy(
        update={"ones_team_id": "team-2"}, deep=True
    )

    controller.apply_runtime(changed, changed_step=SetupStep.PRIVATE_PATHS)

    assert controller.current_step is SetupStep.ONES
    assert controller.result_for(SetupStep.ONES).status is ValidationStatus.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_workflow_diff_cannot_be_hidden_by_later_changed_step(
    tmp_path: Path,
) -> None:
    controller, _, _, _ = _controller(tmp_path)
    await _pass_all(controller)
    changed = _workflow(tmp_path)
    changed.max_codex_attempts = 4

    controller.apply_workflow(changed, changed_step=SetupStep.PRIVATE_PATHS)

    assert controller.current_step is SetupStep.CODEX
    assert controller.result_for(SetupStep.CODEX).status is ValidationStatus.NOT_CONFIGURED


class BlockingMutationBuilder(FakeRuntimeBuilder):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def build(self, active: ActiveSetup, secrets: RuntimeSecrets) -> Handle:
        self.calls.append((active, secrets))
        self.started.set()
        assert self.release.wait(2)
        return self.handle


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ("draft", "secret"))
async def test_mutation_during_save_rejects_stale_candidate_and_stays_editable(
    tmp_path: Path, mutation: str
) -> None:
    old_controller, store, _, _ = _controller(tmp_path)
    await _pass_all(old_controller)
    await old_controller.save_and_activate(confirmed=True)
    builder = BlockingMutationBuilder()
    controller, _, _, _ = _controller(tmp_path, store=store, builder=builder)
    await _pass_all(controller)
    task = asyncio.create_task(controller.save_and_activate(confirmed=True))
    assert await asyncio.to_thread(builder.started.wait, 2)

    if mutation == "draft":
        changed = _workflow(tmp_path)
        changed.max_codex_attempts = 4
        controller.apply_workflow(changed, changed_step=SetupStep.PRIVATE_PATHS)
    else:
        controller.set_secret(SecretKind.ONES_PASSWORD, "NEW-TOKEN-SECRET")
    builder.release.set()

    with pytest.raises(SetupActionError, match="^configuration changed$"):
        await task
    assert builder.handle.closed == 1
    assert store.restores == 1
    assert store.finalizes == 1  # only the old activation was finalized
    assert controller.closed is False
    assert store.document.active is not None
    controller.set_secret(SecretKind.ONES_PASSWORD, "REENTERED-TOKEN")
    assert controller.secret_presence(SecretKind.ONES_PASSWORD) is True
