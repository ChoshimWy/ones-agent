from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest
from textual.widgets import ListView

from src.developer_workflow.contracts import (
    PublicationResult,
    WorkflowRun,
    WorkflowState,
)
from src.developer_workflow.state_store import FileRunStore
from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp, TuiTaskMessage
from src.developer_workflow.orchestrator import DeveloperWorkflowOrchestrator
from src.developer_workflow.tui.controller import TuiController
from src.developer_workflow.tui.models import (
    DangerousActionRequest,
    RunActivity,
    RunDetail,
    RunFilter,
    RunSummary,
)
from src.developer_workflow.tui.run_index import RunIndex
from src.developer_workflow.tui.supervisor import TaskEvent


NOW = datetime(2026, 8, 11, 9, 0, tzinfo=UTC)


def _plain(widget: object) -> str:
    rendered = widget.render()  # type: ignore[attr-defined]
    renderable = getattr(rendered, "_renderable", rendered)
    return renderable.plain if hasattr(renderable, "plain") else str(renderable)


def _run(state: WorkflowState, number: int = 1) -> WorkflowRun:
    return WorkflowRun.new("requirement", f"REQ-{number}").validated_update(
        run_id=f"{number:032x}",
        state=state,
        version=0 if state is WorkflowState.CREATED else number,
        updated_at=NOW,
        blocked_reason="workflow blocked safely"
        if state is WorkflowState.BLOCKED
        else "",
        resume_state=WorkflowState.PUBLISHING
        if state is WorkflowState.BLOCKED
        else None,
    )


def _persist(store: FileRunStore, state: WorkflowState) -> WorkflowRun:
    current = store.create(_run(WorkflowState.CREATED))
    if state is WorkflowState.CREATED:
        return current
    for target in (
        WorkflowState.READING_ONES,
        WorkflowState.VALIDATING,
        WorkflowState.PREPARING_REPO,
        WorkflowState.IMPLEMENTING,
        WorkflowState.TESTING,
        WorkflowState.AI_REVIEW,
        WorkflowState.WAITING_APPROVAL,
        WorkflowState.PUBLISHING,
    ):
        current = store.transition(
            current.run_id, current.version, target, "advance safely"
        )
    if state is WorkflowState.PARTIAL_SUCCESS:
        current = store.save(
            current.validated_update(
                publication=PublicationResult(
                    approved_fingerprint="f" * 64,
                    repo_url="git@github.example:Team/Repo.git",
                    provider="github",
                    provider_host="github.example",
                    expected_parent="a" * 40,
                    expected_tree="b" * 40,
                    commit_message="feat: approved",
                    commit_hash="c" * 40,
                    remote_branch="feature/run",
                    push_completed_at=NOW,
                    pr_marker="ones-dev-run:abc",
                    pr_base="main",
                    pr_head="feature/run",
                    pr_title="Approved title",
                    pr_body="Approved body",
                    pr_url="https://github.example/Team/Repo/pull/1",
                    comment_marker="<!-- ones-dev-run:abc -->",
                    error="comment failed",
                )
            ),
            expected_version=current.version,
        )
        current = store.transition(
            current.run_id,
            current.version,
            WorkflowState.PARTIAL_SUCCESS,
            "publication incomplete",
        )
    elif state is WorkflowState.BLOCKED:
        current = store.transition(
            current.run_id,
            current.version,
            WorkflowState.BLOCKED,
            "blocked safely",
            WorkflowState.PUBLISHING,
        )
    return current


class StoreController:
    def __init__(self, store: FileRunStore) -> None:
        self.store = store
        self.index = RunIndex(store)
        self.cancel_calls: list[object] = []
        self.list_calls = 0

    def list_runs(self, filters: RunFilter, activities=None):
        self.list_calls += 1
        return self.index.list(filters, activities)

    def show(self, run_id: str) -> RunDetail:
        return RunDetail.from_run(self.store.load(run_id, read_only=True))

    def prepare_action(self, run_id: str, action: str) -> DangerousActionRequest:
        detail = self.show(run_id)
        return DangerousActionRequest(
            run_id=run_id,
            version=detail.summary.version,
            action=action,  # type: ignore[arg-type]
            fingerprint="f" * 64,
            work_item_id=detail.summary.work_item_id,
            repositories=detail.repositories,
            changed_file_count=0,
            test_count=0,
            risk_count=detail.risk_count,
            unresolved_count=detail.unresolved_count,
            publication_error=detail.publication.error,
            state=detail.summary.state,
            resume_state=detail.resume_state,
        )


def _app(controller: StoreController, *, poll_interval: float = 0.02):
    return DeveloperWorkflowTuiApp(
        controller,  # type: ignore[arg-type]
        3,
        poll_interval=poll_interval,
    )


@pytest.mark.asyncio
async def test_external_store_update_refreshes_list_and_selected_detail(
    tmp_path,
) -> None:
    store = FileRunStore(tmp_path)
    created = store.create(_run(WorkflowState.CREATED))
    controller = StoreController(store)
    app = _app(controller)

    async with app.run_test(size=(120, 32)) as pilot:
        await app.screen.refresh_runs()
        assert WorkflowState.CREATED.value in _plain(
            app.screen.query_one("#overview-content")
        )
        updated = store.transition(
            created.run_id,
            created.version,
            WorkflowState.BLOCKED,
            "blocked safely",
            WorkflowState.CREATED,
        )
        await pilot.pause(0.08)
        overview = _plain(app.screen.query_one("#overview-content"))
        assert updated.state.value in overview
        assert f"version: {updated.version}" in overview
        assert controller.list_calls >= 2


@pytest.mark.asyncio
async def test_production_poll_and_detail_read_create_no_lock_or_mtime_write(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path)
    run = store.create(_run(WorkflowState.CREATED))
    lock_file = tmp_path / run.run_id / ".lock"
    lock_file.unlink()
    run_dir = tmp_path / run.run_id
    run_file = run_dir / "run.json"
    before = (run_dir.stat().st_mtime_ns, run_file.stat().st_mtime_ns)
    orchestrator = DeveloperWorkflowOrchestrator(
        store=store,
        requirement_flow=None,  # type: ignore[arg-type]
        defect_flow=None,  # type: ignore[arg-type]
        publisher=None,  # type: ignore[arg-type]
        config=None,  # type: ignore[arg-type]
        defect_candidates=None,  # type: ignore[arg-type]
    )
    controller = TuiController(orchestrator, RunIndex(store))
    app = DeveloperWorkflowTuiApp(controller, 3, poll_interval=0.02)

    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause(0.08)

    assert not lock_file.exists()
    assert (run_dir.stat().st_mtime_ns, run_file.stat().st_mtime_ns) == before


@pytest.mark.asyncio
async def test_slow_detail_read_does_not_block_textual_event_loop() -> None:
    started = Event()
    release = Event()

    class SlowController:
        cancel_calls: list[object] = []

        def __init__(self) -> None:
            self.slow = False

        def list_runs(self, filters: RunFilter, activities=None):
            del filters, activities
            return (
                RunSummary.from_run(
                    _run(WorkflowState.CREATED), activity=RunActivity.IDLE
                ),
            )

        def show(self, run_id: str) -> RunDetail:
            del run_id
            if self.slow:
                started.set()
                release.wait(0.5)
            return RunDetail.from_run(_run(WorkflowState.CREATED))

    controller = SlowController()
    app = DeveloperWorkflowTuiApp(
        controller,  # type: ignore[arg-type]
        3,
        poll_interval=10,
    )
    async with app.run_test(size=(120, 32)):
        ticked = False

        async def tick() -> None:
            nonlocal ticked
            await asyncio.sleep(0.01)
            ticked = True

        controller.slow = True
        ticker = asyncio.create_task(tick())
        refresh = asyncio.create_task(app.screen.refresh_runs())
        try:
            await asyncio.sleep(0.05)
            assert started.is_set()
            assert ticked is True
            assert not refresh.done()
        finally:
            release.set()
            await asyncio.gather(ticker, refresh)


@pytest.mark.asyncio
async def test_task_event_enters_ui_loop_and_updates_run_activity(tmp_path) -> None:
    store = FileRunStore(tmp_path)
    run = store.create(_run(WorkflowState.CREATED))
    app = _app(StoreController(store), poll_interval=10)

    async with app.run_test(size=(120, 32)) as pilot:
        app.post_message(TuiTaskMessage(TaskEvent.started(run.run_id, "resume")))
        await pilot.pause()
        assert "running" in _plain(app.screen.query_one("#run-item-0 Label"))

        app.post_message(TuiTaskMessage(TaskEvent.completed(run.run_id, "resume")))
        await pilot.pause()
        assert "idle" in _plain(app.screen.query_one("#run-item-0 Label"))


@pytest.mark.asyncio
async def test_concurrent_refreshes_are_serialized_without_stale_regression() -> None:
    first_started = Event()
    release_first = Event()
    first = RunSummary.from_run(_run(WorkflowState.CREATED, 1), activity=RunActivity.IDLE)
    latest = RunSummary.from_run(_run(WorkflowState.BLOCKED, 2), activity=RunActivity.IDLE)

    class DelayedController:
        cancel_calls: list[object] = []

        def __init__(self) -> None:
            self.calls = 0

        def list_runs(self, filters: RunFilter, activities=None):
            del filters, activities
            self.calls += 1
            if self.calls == 2:
                first_started.set()
                release_first.wait(2)
                return (first,)
            return (latest,) if self.calls >= 3 else (first,)

        def show(self, run_id: str) -> RunDetail:
            return RunDetail.from_run(
                _run(WorkflowState.BLOCKED, 2)
                if run_id == latest.run_id
                else _run(WorkflowState.CREATED, 1)
            )

    controller = DelayedController()
    app = DeveloperWorkflowTuiApp(
        controller,  # type: ignore[arg-type]
        3,
        poll_interval=10,
    )
    async with app.run_test(size=(120, 32)) as pilot:
        screen = app.screen
        older = asyncio.create_task(screen.refresh_runs())
        assert await asyncio.to_thread(first_started.wait, 1)
        newer = asyncio.create_task(screen.refresh_runs())
        release_first.set()
        await asyncio.gather(older, newer)
        await pilot.pause()
        assert screen.query_one("#run-list", ListView).index == 0
        assert latest.state.value in _plain(screen.query_one("#overview-content"))


@pytest.mark.asyncio
async def test_quit_closes_ui_supervisor_without_cancelling_workflow(tmp_path) -> None:
    store = FileRunStore(tmp_path)
    run = store.create(_run(WorkflowState.CREATED))
    controller = StoreController(store)
    app = _app(controller, poll_interval=10)
    started = Event()
    release = Event()

    async with app.run_test(size=(120, 32)) as pilot:
        task = app.supervisor.submit(
            run.run_id,
            "resume",
            lambda: (started.set(), release.wait(2)),
        )
        assert await asyncio.to_thread(started.wait, 1)
        await pilot.press("q")

    assert app.supervisor.closed is True
    assert controller.cancel_calls == []
    assert not task.done()
    release.set()
    await asyncio.sleep(0.05)
    assert task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        WorkflowState.PUBLISHING,
        WorkflowState.PARTIAL_SUCCESS,
        WorkflowState.BLOCKED,
    ],
)
async def test_restart_rebuilds_checkpoint_views_only_from_store(
    tmp_path, state: WorkflowState
) -> None:
    store = FileRunStore(tmp_path)
    persisted = _persist(store, state)

    first = _app(StoreController(store), poll_interval=10)
    async with first.run_test(size=(120, 32)) as pilot:
        await first.screen.refresh_runs()
        assert state.value in _plain(first.screen.query_one("#overview-content"))
    assert first.supervisor.closed is True

    second_controller = StoreController(FileRunStore(tmp_path))
    second = _app(second_controller, poll_interval=10)
    async with second.run_test(size=(120, 32)) as pilot:
        await second.screen.refresh_runs()
        overview = _plain(second.screen.query_one("#overview-content"))
        assert state.value in overview
        assert persisted.run_id in {
            item.run_id for item in second.screen._runs  # type: ignore[attr-defined]
        }
        await pilot.press("r")
        assert second.screen.id == "publication-resume-modal"
