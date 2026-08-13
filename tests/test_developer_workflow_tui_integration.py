from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Sequence

import pytest
from textual.widgets import Button, Input

from src.contracts import (
    ProjectRef,
    RequirementRecord,
    StatusRef,
    WikiPageRef,
    WikiPageSnapshot,
)
from src.developer_workflow import cli
from src.developer_workflow.approval_rebuilder import WorkflowApprovalRebuilder
from src.developer_workflow.config import DeveloperWorkflowConfig
from src.developer_workflow.contracts import (
    AcceptanceCoverage,
    CodexResult,
    DefectCandidate,
    RepositoryChangeClaim,
    RepositoryGroupMapping,
    RepositoryMapping,
    RepositoryRole,
    WorkflowRun,
    WorkflowState,
)
from src.developer_workflow.orchestrator import DeveloperWorkflowOrchestrator
from src.developer_workflow.publisher import Publisher
from src.developer_workflow.repository import WorktreeRepository
from src.developer_workflow.repository_group import RepositoryGroupWorkspace
from src.developer_workflow.requirement_flow import (
    RequirementFlow,
    SandboxCommandExecutor,
    SandboxStatePolicy,
    SubprocessConfiguredTestRunner,
)
from src.developer_workflow.state_store import FileRunStore
from src.developer_workflow.tui.app import DeveloperWorkflowTuiApp, TuiTaskMessage
from src.developer_workflow.tui.controller import TuiController
from src.developer_workflow.tui.models import RunActivity
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


def test_tui_command_uses_bootstrap_host_and_ignores_legacy_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[object] = []
    host = (lambda: object(), object())

    assert cli.main(
        ["tui", "--config", str(_config_file(tmp_path))],
        factory=lambda config: (_ for _ in ()).throw(
            AssertionError("legacy factory called")
        ),
        tui_host_factory=lambda path: host,
        tui_runner=lambda first, second: seen.append((first, second)),
    ) == 0

    assert seen == [host]


class _RunnerFailure(RuntimeError):
    pass


def test_tui_bootstrap_runner_failure_fails_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = io.StringIO()
    code = cli.main(
        ["tui", "--config", str(_config_file(tmp_path))],
        tui_host_factory=lambda path: (lambda: object(), object()),
        tui_runner=lambda first, second: (_ for _ in ()).throw(
            _RunnerFailure("runner failed")
        ),
        stderr=error,
    )
    assert code == 1
    assert error.getvalue() == "error: command failed safely\n"


def test_tui_keyboard_interrupt_from_bootstrap_runner_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = cli.main(
        ["tui", "--config", str(_config_file(tmp_path))],
        tui_host_factory=lambda path: (lambda: object(), object()),
        tui_runner=lambda first, second: (_ for _ in ()).throw(
            KeyboardInterrupt()
        ),
    )

    assert code == 130


def test_tui_runner_success_does_not_construct_setup_eagerly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_calls: list[object] = []
    factory = lambda: setup_calls.append(object())

    code = cli.main(
        ["tui", "--config", str(_config_file(tmp_path))],
        tui_host_factory=lambda path: (factory, object()),
        tui_runner=lambda first, second: None,
    )

    assert code == 0
    assert setup_calls == []


def test_incomplete_production_runtime_enters_setup_before_legacy_config_load(
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

    monkeypatch.setattr(
        DeveloperWorkflowConfig,
        "load",
        lambda path: (_ for _ in ()).throw(AssertionError("legacy load called")),
    )
    host = (SimpleNamespace(close=lambda: None), object())
    code = cli.main(
        ["tui", "--config", str(config_path)],
        factory=cli.build_production_orchestrator,
        tui_host_factory=lambda path: host,
        tui_runner=lambda controller, limit: calls.append((controller, limit)),
    )

    assert code == 0
    assert calls == [host]


def test_run_tui_rejects_preconstructed_controller_bypass(
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

    with pytest.raises(TypeError):
        tui.run_tui(controller, 4)  # type: ignore[arg-type]

    assert calls == []


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
    (source / ".gitignore").write_text(
        "__pycache__/\n.ones-sandbox/\n", encoding="utf-8"
    )
    _git("add", "src/value.py", ".gitignore", cwd=source)
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
            test_commands=("python -m compileall src",),
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
            test_commands=("python -m compileall src/value.py",),
            allowed_paths=("src",),
        ),
    )
    group = RepositoryGroupMapping(
        key="suite",
        project_id="P",
        iteration_id="I",
        primary_repository="primary",
        repositories=mappings,
        integration_test_commands=("python -m compileall .",),
    )
    config = DeveloperWorkflowConfig(
        run_root=(tmp_path / "runs").resolve(),
        worktree_root=(tmp_path / "worktrees").resolve(),
        mirror_root=(tmp_path / "mirrors").resolve(),
        sandbox_permission_profile="test-profile",
        max_codex_attempts=2,
        repositories=(),
        repository_groups=(group,),
        publishing={"provider": "github"},
    )
    raw_repository = WorktreeRepository(
        config.mirror_root,
        config.worktree_root,
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
    wiki_content = (
        "# Acceptance Criteria\n"
        "1. Publish both repositories safely\n"
    )
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
    requirement = RequirementRecord(
        requirement_id="REQ-UI",
        number="REQ-UI",
        title="UI group publication",
        project=ProjectRef(id="P", name="Project"),
        iteration=ProjectRef(id="I", name="Iteration"),
        status=StatusRef(id="doing", name="Doing", category="open"),
        wiki_refs=[
            WikiPageRef(
                team_id=wiki.team_id,
                space_id=wiki.space_id,
                page_id=wiki.page_id,
                source_url=wiki.source_url,
            )
        ],
    )

    class Gateway:
        def get_normalized_requirement_sync(self, issue_id: str):
            assert issue_id == requirement.requirement_id
            return requirement

        def get_wiki_snapshot_sync(self, url: str):
            assert url == wiki.source_url
            return wiki

        def get_wiki_snapshot_by_ids_sync(
            self, space_id: str, page_id: str, *, source_url=None
        ):
            assert (space_id, page_id, source_url) == (
                wiki.space_id,
                wiki.page_id,
                wiki.source_url,
            )
            return wiki

    class Codex:
        def preflight(self, **kwargs):
            del kwargs
            return CodexResult(summary="source preflight passed")

        def run_group_stage(self, stage: str, **kwargs):
            prepared = kwargs["prepared"]
            claims = tuple(
                RepositoryChangeClaim(
                    repository_key=item.repository_key,
                    path="src/value.py",
                )
                for item in prepared
            )
            if stage == "implementation":
                for item in prepared:
                    (item.prepared.path / "src" / "value.py").write_text(
                        f"VALUE = {item.repository_key!r}\nCHANGED = True\n",
                        encoding="utf-8",
                    )
                return CodexResult(
                    summary="implemented repository group",
                    repository_changes=claims,
                    acceptance_coverage=(
                        AcceptanceCoverage(
                            criterion_id="AC-1",
                            criterion_text="Publish both repositories safely",
                            repository_files=claims,
                            tests=(
                                mappings[0].test_commands[0],
                                mappings[1].test_commands[0],
                                group.integration_test_commands[0],
                            ),
                        ),
                    ),
                )
            assert stage == "review"
            return CodexResult(
                summary="reviewed repository group",
                repository_changes=claims,
                review_findings=("all repository changes reviewed",),
                unrelated_changes_checked=True,
            )

        def analyze_testing(self, **kwargs):
            del kwargs
            return CodexResult(summary="configured tests passed")

    def sandbox_backend(
        command, *, cwd, env, timeout, max_output_bytes, stdin=None
    ):
        del timeout, max_output_bytes, stdin
        child = command[command.index("--") + 1 :]
        code = child[3] if len(child) > 3 and child[1:3] == ["-I", "-c"] else ""
        if "socket.socket" in code:
            return subprocess.CompletedProcess(command, 23, stdout="", stderr="")
        if "Path(sys.argv[1]).write_text" in code:
            target = Path(child[-2])
            if any(
                part.startswith(".ones-sandbox-probes-")
                for part in target.parts
            ):
                return subprocess.CompletedProcess(command, 23, stdout="", stderr="")
        return subprocess.run(
            child,
            cwd=cwd,
            env=env,
            input=None,
            capture_output=True,
            text=True,
            check=False,
        )

    sandbox = SandboxCommandExecutor(
        sandbox_state_provider=lambda cwd: SandboxStatePolicy(
            payload={"policy": "local-test"},
            working_directory=cwd,
            writable_roots=(cwd,),
            network_disabled=True,
        ),
        backend_executor=sandbox_backend,
        codex_binary="codex",
    )
    store = FileRunStore(config.run_root)
    gateway = Gateway()
    flow = RequirementFlow(
        store=store,
        gateway=gateway,  # type: ignore[arg-type]
        config=config,
        repository=raw_repository,
        group_workspace=workspace,
        codex=Codex(),  # type: ignore[arg-type]
        test_runner=SubprocessConfiguredTestRunner(sandbox),
    )
    effects: list[str] = []
    repository = _EffectRepository(raw_repository, effects)
    commenter = _EffectCommenter(effects)
    publisher = Publisher(
        store,
        repository,
        WorkflowApprovalRebuilder(gateway, repository),  # type: ignore[arg-type]
        _EffectPR(effects),
        commenter,
        provider="github",
        provider_host="git.example.invalid",
    )
    orchestrator = DeveloperWorkflowOrchestrator(
        store=store,
        requirement_flow=flow,
        defect_flow=None,  # type: ignore[arg-type]
        publisher=publisher,
        config=config,
        defect_candidates=None,  # type: ignore[arg-type]
    )
    controller = TuiController(orchestrator, RunIndex(store))
    app = DeveloperWorkflowTuiApp(controller, 3, poll_interval=10)
    return app, controller, store, effects, sources, remotes, commenter


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
    app, controller, store, effects, sources, remotes, commenter = (
        _group_ui_runtime(tmp_path)
    )
    source_before = {key: _source_facts(path) for key, path in sources.items()}
    diagnostic: object = None
    run_id: str | None = None
    confirmation_finished = asyncio.Event()
    approval_finished = asyncio.Event()

    def observe_ui_message(message: object) -> None:
        if not isinstance(message, TuiTaskMessage) or run_id is None:
            return
        event = message.event
        if event.run_id != run_id or event.activity is not RunActivity.IDLE:
            return
        if event.action == "confirm-repository":
            confirmation_finished.set()
        elif event.action == "approve":
            approval_finished.set()

    try:
        async with app.run_test(
            size=(120, 32), message_hook=observe_ui_message
        ) as pilot:
            assert effects == []
            assert store.list_run_ids() == ()
            await pilot.press("n")
            await pilot.click("#workflow-requirement")
            app.screen.query_one("#requirement-id", Input).value = "REQ-UI"
            app.screen.query_one("#start-requirement", Button).focus()
            await pilot.press("enter")
            assert app.screen.query_one("#mapping-0")
            run_id = store.list_run_ids()[0]
            assert store.load(run_id, read_only=True).state is WorkflowState.VALIDATING
            assert effects == []
            app.screen.query_one("#mapping-0", Button).focus()
            await pilot.press("enter")
            assert app.screen.query_one("#confirm-start")
            assert effects == []
            await pilot.click("#confirm-start")
            await asyncio.wait_for(confirmation_finished.wait(), 180)
            await pilot.pause()
            waiting = store.load(run_id, read_only=True)
            assert waiting.state is WorkflowState.WAITING_APPROVAL, (
                waiting.blocked_reason,
                waiting.resume_state,
            )
            assert waiting.approval is not None
            assert waiting.approval.approved_by is None
            targets = tuple(event.target for event in waiting.history)
            for state in (
                WorkflowState.READING_ONES,
                WorkflowState.VALIDATING,
                WorkflowState.PREPARING_REPO,
                WorkflowState.IMPLEMENTING,
                WorkflowState.TESTING,
                WorkflowState.AI_REVIEW,
                WorkflowState.WAITING_APPROVAL,
            ):
                assert state in targets
            assert effects == []
            for _ in range(100):
                await asyncio.sleep(0.01)
                if app.screen.id == "dashboard-screen":
                    break
            assert app.screen.id == "dashboard-screen"
            await app.screen.refresh_runs()
            await pilot.press("a")
            assert effects == []
            assert app.screen.id == "approval-modal", (
                app._dashboard._runs,
                app._dashboard.query_one("#notice").render(),
            )
            app.screen.query_one("#actor", Input).value = "operator"
            await pilot.click("#confirm-approve")
            await asyncio.wait_for(approval_finished.wait(), 180)
            await pilot.pause()
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
    assert app.supervisor.closed is True
    assert not any(
        path.name.startswith((".ones-sandbox", ".ones-sandbox-probes-"))
        for path in (tmp_path / "worktrees").rglob("*")
    )
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
    app, controller, store, effects, *_ = _group_ui_runtime(tmp_path)
    try:
        async with app.run_test(size=(120, 32)) as pilot:
            waiting = controller.start_requirement("REQ-UI")
            waiting = controller.confirm_repository(
                waiting.summary.run_id,
                "suite",
                waiting.summary.version,
            )
            assert waiting.summary.state is WorkflowState.WAITING_APPROVAL
            await app.screen.refresh_runs()
            await pilot.press("a")
            drifted = store.save(
                store.load(waiting.summary.run_id).validated_update(
                    updated_at=datetime.now(UTC)
                ),
                waiting.summary.version,
            )
            assert drifted.version == waiting.summary.version + 1
            app.screen.query_one("#actor", Input).value = "operator"
            await pilot.click("#confirm-approve")
            await pilot.pause(0.1)
    finally:
        controller.close()
    assert effects == []
    assert (
        store.load(waiting.summary.run_id, read_only=True).state
        is WorkflowState.WAITING_APPROVAL
    )


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
