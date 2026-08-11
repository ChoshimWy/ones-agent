from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Sequence

import pytest
from textual.widgets import Button, Input

from src.contracts import WikiPageSnapshot
from src.developer_workflow import cli
from src.developer_workflow.approval import approval_fingerprint
from src.developer_workflow.config import DeveloperWorkflowConfig
from src.developer_workflow.contracts import (
    ApprovalPackage,
    CommandOutcome,
    CommandResult,
    DefectCandidate,
    RepositoryApprovalEvidence,
    RepositoryGroupMapping,
    RepositoryMapping,
    RepositoryRole,
    RepositoryRunEvidence,
    WorkflowRun,
    WorkflowState,
)
from src.developer_workflow.orchestrator import DeveloperWorkflowOrchestrator
from src.developer_workflow.publisher import Publisher
from src.developer_workflow.repository import WorktreeRepository
from src.developer_workflow.repository_group import RepositoryGroupWorkspace
from src.developer_workflow.state_store import FileRunStore
from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp
from src.developer_workflow.tui.controller import TuiController
from src.developer_workflow.tui.run_index import RunIndex


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "run_root": str(tmp_path / "runs"),
                "worktree_root": str(tmp_path / "worktrees"),
                "mirror_root": str(tmp_path / "mirrors"),
                "sandbox_permission_profile": "managed-dev",
                "max_codex_attempts": 2,
                "repositories": [
                    {
                        "key": "repo",
                        "project_id": "PROJ",
                        "iteration_id": "ITER",
                        "repo_url": "ssh://git@example.invalid/team/repo.git",
                        "repo_name": "repo",
                        "base_branch": "main",
                        "test_commands": ["uv run pytest"],
                        "lint_commands": [],
                    }
                ],
                "publishing": {"provider": "github"},
            }
        ),
        encoding="utf-8",
    )
    return path


class _Store:
    def list_run_ids(self) -> tuple[str, ...]:
        return ()


class _Orchestrator:
    def __init__(self) -> None:
        self.store = _Store()


def test_tui_command_reuses_factory_orchestrator_store_and_configured_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _Orchestrator()
    seen: list[object] = []
    closed: list[object] = []
    original_close = TuiController.close

    def close(controller: TuiController) -> None:
        closed.append(controller)
        original_close(controller)

    monkeypatch.setattr(TuiController, "close", close)

    def factory(config: DeveloperWorkflowConfig) -> _Orchestrator:
        seen.append(config)
        return orchestrator

    def runner(controller: object, max_concurrency: int) -> None:
        seen.append((controller, max_concurrency))

    assert cli.main(
        ["tui", "--config", str(_config_file(tmp_path))],
        factory=factory,  # type: ignore[arg-type]
        tui_runner=runner,
    ) == 0

    config = seen[0]
    controller, limit = seen[1]  # type: ignore[misc]
    assert isinstance(config, DeveloperWorkflowConfig)
    assert limit == 3
    assert controller._orchestrator is orchestrator  # type: ignore[attr-defined]
    assert controller._run_index._store is orchestrator.store  # type: ignore[attr-defined]
    assert closed == [controller]


class _RunnerFailure(RuntimeError):
    pass


def test_execute_tui_preserves_runner_failure_when_controller_close_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DeveloperWorkflowConfig.load(_config_file(tmp_path))
    orchestrator = _Orchestrator()
    closes: list[object] = []

    def close(controller: TuiController) -> None:
        closes.append(controller)
        raise RuntimeError("close failed")

    monkeypatch.setattr(TuiController, "close", close)

    with pytest.raises(_RunnerFailure, match="runner failed"):
        cli._execute_tui(
            config,
            lambda loaded: orchestrator,  # type: ignore[arg-type]
            lambda controller, limit: (_ for _ in ()).throw(
                _RunnerFailure("runner failed")
            ),
        )
    assert len(closes) == 1


def test_tui_keyboard_interrupt_is_not_obscured_by_controller_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _Orchestrator()
    closes: list[object] = []

    def close(controller: TuiController) -> None:
        closes.append(controller)
        raise RuntimeError("close failed")

    monkeypatch.setattr(TuiController, "close", close)

    code = cli.main(
        ["tui", "--config", str(_config_file(tmp_path))],
        factory=lambda loaded: orchestrator,  # type: ignore[arg-type]
        tui_runner=lambda controller, limit: (_ for _ in ()).throw(
            KeyboardInterrupt()
        ),
    )

    assert code == 130
    assert len(closes) == 1


def test_tui_controller_close_failure_after_normal_runner_fails_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _Orchestrator()
    monkeypatch.setattr(
        TuiController,
        "close",
        lambda controller: (_ for _ in ()).throw(RuntimeError("close failed")),
    )
    error = io.StringIO()

    code = cli.main(
        ["tui", "--config", str(_config_file(tmp_path))],
        factory=lambda loaded: orchestrator,  # type: ignore[arg-type]
        tui_runner=lambda controller, limit: None,
        stderr=error,
    )

    assert code == 1
    assert error.getvalue() == "error: command failed safely\n"


def test_incomplete_production_runtime_fails_before_tui_runner_and_root_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "ONES_EMAIL",
        "ONES_PASSWORD",
        "ONES_API_TOKEN",
        "ONES_TEAM_ID",
        "ONES_ISSUE_TYPE_ID",
        "ONES_DEV_PROVIDER_TOKEN",
        "ONES_DEV_PROVIDER_HOST",
        "ONES_DEV_PROVIDER_API_URL",
        "ONES_DEV_GIT_AUTHOR_NAME",
        "ONES_DEV_GIT_AUTHOR_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    config_path = _config_file(tmp_path)
    calls: list[object] = []

    code = cli.main(
        ["tui", "--config", str(config_path)],
        factory=cli.build_production_orchestrator,
        tui_runner=lambda controller, limit: calls.append((controller, limit)),
    )

    config = DeveloperWorkflowConfig.load(config_path)
    assert code == 1
    assert calls == []
    assert not config.run_root.exists()
    assert not config.mirror_root.exists()
    assert not config.worktree_root.exists()


def test_run_tui_constructs_and_runs_the_production_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.developer_workflow.tui as tui

    calls: list[object] = []

    class App:
        def __init__(self, controller: object, max_concurrency: int) -> None:
            calls.append((controller, max_concurrency))

        def run(self) -> None:
            calls.append("run")

    controller = object()
    monkeypatch.setattr(tui, "DeveloperWorkflowTuiApp", App)

    tui.run_tui(controller, 4)  # type: ignore[arg-type]

    assert calls == [(controller, 4), "run"]


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        },
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _source_remote(
    root: Path, key: str, authoritative_url: str
) -> tuple[Path, Path]:
    source = root / f"source-{key}"
    remote = root / f"remote-{key}.git"
    source.mkdir()
    _git("init", "-b", "main", cwd=source)
    _git("config", "user.name", "TUI E2E", cwd=source)
    _git("config", "user.email", "tui-e2e@example.invalid", cwd=source)
    (source / "src").mkdir()
    (source / "src" / "value.py").write_text(
        f"VALUE = {key!r}\n", encoding="utf-8"
    )
    _git("add", "src/value.py", cwd=source)
    _git("commit", "-m", "base", cwd=source)
    _git("clone", "--bare", str(source), str(remote), cwd=root)
    _git("remote", "add", "origin", authoritative_url, cwd=source)
    return source, remote


class _MultiRemoteRunner:
    def __init__(self, remotes: dict[str, Path]) -> None:
        self._remotes = {
            url: str(path.resolve()) for url, path in remotes.items()
        }

    def __call__(self, command: Sequence[str], cwd: Path | None):
        original = list(command)
        actual = [self._remotes.get(item, item) for item in original]
        if "ls-remote" in actual or "push" in actual or "fetch" in actual:
            if "origin" in actual and cwd is not None:
                origin = subprocess.run(
                    ["git", "remote", "get-url", "origin"],
                    cwd=cwd,
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                ).stdout.strip()
                actual = [
                    self._remotes.get(origin, item)
                    if item == "origin"
                    else item
                    for item in actual
                ]
        completed = subprocess.run(actual, cwd=cwd, capture_output=True, check=False)
        if completed.returncode == 0 and "clone" in original and "--bare" in original:
            mirror = Path(original[-1])
            subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(mirror),
                    "remote",
                    "set-url",
                    "origin",
                    original[-2],
                ],
                check=True,
                capture_output=True,
            )
        return completed


@dataclass
class _EffectRepository:
    delegate: WorktreeRepository
    effects: list[str]

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)

    @staticmethod
    def _key(run: WorkflowRun) -> str:
        assert run.repository is not None
        return run.repository.key

    def commit_approved(self, run: WorkflowRun) -> str:
        key = self._key(run)
        result = self.delegate.commit_approved(run)
        self.effects.append(f"commit:{key}")
        return result

    def push_approved(self, run: WorkflowRun) -> None:
        key = self._key(run)
        self.delegate.push_approved(run)
        self.effects.append(f"push:{key}")


@dataclass
class _EffectPR:
    effects: list[str]
    urls: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def _key(repo_url: str) -> str:
        return repo_url.rsplit("/", 1)[-1].removesuffix(".git")

    def find(self, *, repo_url: str, **kwargs):
        del kwargs
        return self.urls.get(self._key(repo_url))

    def create(self, *, repo_url: str, **kwargs) -> str:
        del kwargs
        key = self._key(repo_url)
        self.effects.append(f"pr:{key}")
        url = f"https://git.example.invalid/team/{key}/pull/1"
        self.urls[key] = url
        return url


@dataclass
class _EffectCommenter:
    effects: list[str]
    status_updates: int = 0

    def ensure_comment(self, run: WorkflowRun) -> str:
        assert run.group_publication is not None
        assert all(item.pr_url for item in run.group_publication.repositories)
        self.effects.append("comment")
        return "comment-1"


def _command(command: str) -> CommandResult:
    now = datetime(2026, 8, 11, tzinfo=UTC)
    return CommandResult(
        command=command,
        argv=tuple(command.split()),
        exit_code=0,
        outcome=CommandOutcome.PASSED,
        summary="passed",
        started_at=now,
        finished_at=now,
    )


def _group_ui_runtime(tmp_path: Path):
    urls = {
        "dependency": "https://git.example.invalid/team/dependency.git",
        "primary": "https://git.example.invalid/team/primary.git",
    }
    sources: dict[str, Path] = {}
    remotes: dict[str, Path] = {}
    for key, url in urls.items():
        sources[key], remotes[key] = _source_remote(tmp_path, key, url)
    mappings = (
        RepositoryMapping(
            key="dependency",
            project_id="P",
            iteration_id="I",
            repo_url=urls["dependency"],
            repo_name="dependency",
            source_path=sources["dependency"].resolve(),
            role=RepositoryRole.DEPENDENCY,
            test_commands=("python -m compileall src/dependency",),
            allowed_paths=("src",),
        ),
        RepositoryMapping(
            key="primary",
            project_id="P",
            iteration_id="I",
            repo_url=urls["primary"],
            repo_name="primary",
            source_path=sources["primary"].resolve(),
            role=RepositoryRole.PRIMARY,
            depends_on=("dependency",),
            test_commands=("python -m compileall src/primary",),
            allowed_paths=("src",),
        ),
    )
    group = RepositoryGroupMapping(
        key="suite",
        project_id="P",
        iteration_id="I",
        primary_repository="primary",
        repositories=mappings,
        integration_test_commands=("python -m compileall integration",),
    )
    raw_repository = WorktreeRepository(
        tmp_path / "mirrors",
        tmp_path / "worktrees",
        command_runner=_MultiRemoteRunner(
            {urls[key]: remotes[key] for key in urls}
        ),
        identity_env_provider=lambda: {
            "GIT_AUTHOR_NAME": "TUI Publisher",
            "GIT_AUTHOR_EMAIL": "tui@example.invalid",
            "GIT_COMMITTER_NAME": "TUI Publisher",
            "GIT_COMMITTER_EMAIL": "tui@example.invalid",
        },
    )
    workspace = RepositoryGroupWorkspace(raw_repository)
    run_id = "a" * 32
    prepared = workspace.prepare_group(
        run_id, group, "requirement", "REQ-UI", "UI group publication"
    )
    for item in prepared:
        (item.prepared.path / "src" / "value.py").write_text(
            f"VALUE = {item.repository_key!r}\nCHANGED = True\n",
            encoding="utf-8",
        )
    snapshots = workspace.snapshots(prepared)
    messages = {
        item.repository_key: f"fix({item.repository_key}): approved UI change"
        for item in prepared
    }
    trees = workspace.approval_trees(prepared, snapshots, messages)
    evidence: list[RepositoryRunEvidence] = []
    approval_evidence: list[RepositoryApprovalEvidence] = []
    for item in prepared:
        snapshot = snapshots[item.repository_key]
        result = _command(item.mapping.test_commands[0])
        evidence.append(
            RepositoryRunEvidence(
                repository_key=item.repository_key,
                mapping=item.mapping,
                prepared_worktree=item.prepared,
                tested_snapshot=snapshot,
                test_results=(result,),
                changed_files=snapshot.changed_files,
            )
        )
        approval_evidence.append(
            RepositoryApprovalEvidence(
                repository_key=item.repository_key,
                mapping=item.mapping,
                base_commit=item.prepared.base_commit,
                head_commit=snapshot.head_commit,
                diff_hash=snapshot.diff_sha256,
                diff_summary=(
                    f"changed {len(snapshot.changed_files)} file(s): "
                    f"{', '.join(snapshot.changed_files)}"
                ),
                branch=item.prepared.branch,
                changed_files=snapshot.changed_files,
                tests=(result,),
                tree_hash=trees[item.repository_key],
                commit_message=messages[item.repository_key],
                pr_title=f"REQ-UI [{item.repository_key}]",
                pr_body=f"Approved {item.repository_key} change",
            )
        )
    integration_result = _command(group.integration_test_commands[0])
    wiki_content = "# Acceptance\nAC-1: publish both repositories safely"
    wiki = WikiPageSnapshot(
        team_id="T",
        space_id="S",
        page_id="W",
        title="UI group publication",
        version="1",
        updated_at="2026-08-11T00:00:00Z",
        normalized_content=wiki_content,
        content_sha256=hashlib.sha256(wiki_content.encode()).hexdigest(),
        source_url="https://ones.invalid/wiki/W",
    )
    package = ApprovalPackage(
        work_item_id="REQ-UI",
        work_item_title="UI group publication",
        work_item_status="Doing",
        source_versions={"work_item": "1"},
        wiki_hashes={wiki.page_id: wiki.content_sha256},
        wiki_snapshots=(wiki,),
        repository_group=group,
        repositories=tuple(approval_evidence),
        integration_tests=(integration_result,),
        coverage={"AC-1": "covered in both repositories"},
        evidence=("verified from repository",),
        review=("reviewed",),
        risks=("low",),
        unrelated_changes_checked=True,
    )
    package = package.model_copy(
        update={"fingerprint": approval_fingerprint(package)}
    )
    store = FileRunStore(tmp_path / "runs")
    run = store.create(
        WorkflowRun.new("requirement", "REQ-UI").validated_update(run_id=run_id)
    )
    for state in (
        WorkflowState.READING_ONES,
        WorkflowState.VALIDATING,
        WorkflowState.PREPARING_REPO,
        WorkflowState.IMPLEMENTING,
        WorkflowState.TESTING,
        WorkflowState.AI_REVIEW,
    ):
        run = store.transition(run.run_id, run.version, state, state.value)
    run = store.save(
        run.validated_update(
            repository_model_version=2,
            repository_group=group,
            repository_evidence=tuple(evidence),
            integration_test_results=(integration_result,),
            approval=package,
        ),
        run.version,
    )
    run = store.transition(
        run.run_id,
        run.version,
        WorkflowState.WAITING_APPROVAL,
        "await approval",
    )
    effects: list[str] = []
    repository = _EffectRepository(raw_repository, effects)
    commenter = _EffectCommenter(effects)
    publisher = Publisher(
        store,
        repository,
        lambda current: package,
        _EffectPR(effects),
        commenter,
        provider="github",
        provider_host="git.example.invalid",
    )
    orchestrator = DeveloperWorkflowOrchestrator(
        store=store,
        requirement_flow=None,  # type: ignore[arg-type]
        defect_flow=None,  # type: ignore[arg-type]
        publisher=publisher,
        config=None,  # type: ignore[arg-type]
        defect_candidates=None,  # type: ignore[arg-type]
    )
    controller = TuiController(orchestrator, RunIndex(store))
    app = DeveloperWorkflowTuiApp(controller, 3, poll_interval=10)
    return app, controller, store, run, effects, sources, remotes, commenter


def _source_facts(path: Path) -> tuple[str, str, str]:
    return (
        _git("rev-parse", "HEAD", cwd=path),
        _git("status", "--porcelain=v1", "--untracked-files=all", cwd=path),
        (path / "src" / "value.py").read_text(encoding="utf-8"),
    )


@pytest.mark.asyncio
async def test_real_group_ui_approval_is_first_remote_effect_and_publishes_once(
    tmp_path: Path,
) -> None:
    app, controller, store, waiting, effects, sources, remotes, commenter = (
        _group_ui_runtime(tmp_path)
    )
    source_before = {key: _source_facts(path) for key, path in sources.items()}
    diagnostic: object = None
    try:
        async with app.run_test(size=(120, 32)) as pilot:
            assert (
                store.load(waiting.run_id, read_only=True).state
                is WorkflowState.WAITING_APPROVAL
            )
            assert effects == []
            await pilot.press("a")
            assert effects == []
            app.screen.query_one("#actor", Input).value = "operator"
            await pilot.click("#confirm-approve")
            for _ in range(400):
                await asyncio.sleep(0.05)
                if store.load(waiting.run_id, read_only=True).state in {
                    WorkflowState.COMPLETED,
                    WorkflowState.PARTIAL_SUCCESS,
                }:
                    break
            diagnostic = (
                app.screen.id,
                app.screen.query_one("#notice").render(),
                tuple(effects),
            )
    finally:
        controller.close()

    completed = store.load(waiting.run_id, read_only=True)
    assert completed.state is WorkflowState.COMPLETED, diagnostic
    assert effects == [
        "commit:dependency",
        "commit:primary",
        "push:dependency",
        "pr:dependency",
        "push:primary",
        "pr:primary",
        "comment",
    ]
    assert commenter.status_updates == 0
    assert {key: _source_facts(path) for key, path in sources.items()} == source_before
    assert _git(
        "show-ref",
        completed.group_publication.repositories[0].remote_branch,
        cwd=remotes["dependency"],
    )
    assert _git(
        "show-ref",
        completed.group_publication.repositories[1].remote_branch,
        cwd=remotes["primary"],
    )


@pytest.mark.asyncio
async def test_real_group_ui_version_drift_has_zero_remote_effects(
    tmp_path: Path,
) -> None:
    app, controller, store, waiting, effects, *_ = _group_ui_runtime(tmp_path)
    try:
        async with app.run_test(size=(120, 32)) as pilot:
            await pilot.press("a")
            drifted = store.save(
                store.load(waiting.run_id).validated_update(
                    updated_at=datetime.now(UTC)
                ),
                waiting.version,
            )
            assert drifted.version == waiting.version + 1
            app.screen.query_one("#actor", Input).value = "operator"
            await pilot.click("#confirm-approve")
            await pilot.pause(0.1)
    finally:
        controller.close()
    assert effects == []
    assert store.load(waiting.run_id, read_only=True).state is WorkflowState.WAITING_APPROVAL


@pytest.mark.asyncio
async def test_real_candidate_query_creates_no_run_or_worktree(
    tmp_path: Path,
) -> None:
    class Candidates:
        async def list_candidates(
            self, project, iteration, assignee, *, status_ids=None
        ):
            assert (project, iteration, assignee, status_ids) == (
                "P",
                "I",
                "A",
                ("todo-id", "fixing-id"),
            )
            return (
                DefectCandidate(
                    uuid="d" * 32,
                    key="BUG-7",
                    number="7",
                    title="Qt lifecycle defect",
                    priority="normal",
                    status="todo",
                    status_id="todo-id",
                    updated_at="2026-08-11T00:00:00Z",
                    snapshot_token="PRIVATE-CANDIDATE-TOKEN",
                ),
            )

    store = FileRunStore(tmp_path / "runs")
    worktree_root = tmp_path / "worktrees"
    orchestrator = DeveloperWorkflowOrchestrator(
        store=store,
        requirement_flow=None,  # type: ignore[arg-type]
        defect_flow=None,  # type: ignore[arg-type]
        publisher=None,  # type: ignore[arg-type]
        config=None,  # type: ignore[arg-type]
        defect_candidates=Candidates(),  # type: ignore[arg-type]
    )
    controller = TuiController(orchestrator, RunIndex(store))
    app = DeveloperWorkflowTuiApp(controller, 3, poll_interval=10)
    try:
        async with app.run_test(size=(120, 32)) as pilot:
            await pilot.press("n")
            await pilot.click("#workflow-defect")
            app.screen.query_one("#project", Input).value = "P"
            app.screen.query_one("#iteration", Input).value = "I"
            app.screen.query_one("#assignee", Input).value = "A"
            app.screen.query_one("#status-ids", Input).value = "todo-id,fixing-id"
            app.screen.query_one("#query-defects", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen.query_one("#candidate-0")
            assert "PRIVATE-CANDIDATE-TOKEN" not in "\n".join(
                str(widget.render()) for widget in app.query("*")
            )
            assert store.list_run_ids() == ()
            assert not worktree_root.exists()
    finally:
        controller.close()
