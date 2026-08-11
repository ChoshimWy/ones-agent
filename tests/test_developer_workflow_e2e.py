from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

import pytest

from src.contracts import (
    DefectRecord,
    IdentityRef,
    IssueTypeRef,
    PriorityRef,
    ProjectRef,
    RequirementRecord,
    StatusRef,
    WikiPageRef,
    WikiPageSnapshot,
)
from src.developer_workflow.approval_rebuilder import WorkflowApprovalRebuilder
from src.developer_workflow.command_utils import parse_command_argv
from src.developer_workflow.config import DeveloperWorkflowConfig, PublishingConfig, PublishingProvider
from src.developer_workflow.contracts import (
    AcceptanceCoverage,
    CodexResult,
    CommandOutcome,
    CommandResult,
    RepositoryMapping,
    RootCauseEvidence,
    RootCauseSupportingPoint,
    WorkflowState,
)
from src.developer_workflow.defect_flow import DefectCandidateService, DefectFlow
from src.developer_workflow.orchestrator import DeveloperWorkflowOrchestrator
from src.developer_workflow.publisher import Publisher
from src.developer_workflow.repository import WorktreeRepository
from src.developer_workflow.requirement_flow import RequirementFlow
from src.developer_workflow.state_store import FileRunStore


NOW = datetime(2026, 8, 11, tzinfo=UTC)
REMOTE_URL = "https://git.example.invalid/team/sample.git"


def _git(*args: str, cwd: Path) -> str:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True,
        text=True, encoding="utf-8",
    ).stdout.strip()


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    source, remote = tmp_path / "source", tmp_path / "remote.git"
    source.mkdir()
    _git("init", "-b", "main", cwd=source)
    _git("config", "user.name", "E2E", cwd=source)
    _git("config", "user.email", "e2e@example.invalid", cwd=source)
    (source / "src").mkdir()
    (source / "tests").mkdir()
    (source / "src" / "__init__.py").write_text("", encoding="utf-8")
    (source / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (source / "src" / "report.py").write_text("def export(rows):\n    return rows[0]\n", encoding="utf-8")
    (source / "tests" / "test_report.py").write_text(
        "from pathlib import Path\nns = {}\nexec((Path(__file__).parents[1] / 'src/report.py').read_text(), ns)\nassert ns['export'](['x']) == 'x'\n",
        encoding="utf-8",
    )
    _git("add", ".", cwd=source)
    _git("commit", "-m", "base", cwd=source)
    _git("clone", "--bare", str(source), str(remote), cwd=tmp_path)
    return remote


class _URLRemapRunner:
    def __init__(self, remote: Path) -> None:
        self.remote = str(remote.resolve())
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: Sequence[str], cwd: Path | None):
        original = list(command)
        self.commands.append(tuple(original))
        actual = [self.remote if item == REMOTE_URL else item for item in original]
        if "ls-remote" in actual or "push" in actual or "fetch" in actual:
            actual = [self.remote if item == "origin" else item for item in actual]
        if "fetch" in actual and self.remote in actual:
            actual.append("+refs/heads/*:refs/remotes/origin/*")
        completed = subprocess.run(actual, cwd=cwd, capture_output=True, check=False)
        if completed.returncode == 0 and "clone" in original and "--bare" in original:
            mirror = Path(original[-1])
            subprocess.run(
                ["git", "--git-dir", str(mirror), "remote", "set-url", "origin", REMOTE_URL],
                capture_output=True, check=True,
            )
        return completed


@dataclass
class FakeONES:
    requirement: RequirementRecord
    wiki: WikiPageSnapshot
    defect: DefectRecord
    status_updates: int = 0
    comment_writes: int = 0

    def get_normalized_requirement_sync(self, issue_id: str) -> RequirementRecord:
        assert issue_id == self.requirement.requirement_id
        return self.requirement

    def get_wiki_snapshot_sync(self, url: str) -> WikiPageSnapshot:
        assert url == self.wiki.source_url
        return self.wiki

    def get_wiki_snapshot_by_ids_sync(self, space_id: str, page_id: str, *, source_url=None):
        assert (space_id, page_id, source_url) == (self.wiki.space_id, self.wiki.page_id, self.wiki.source_url)
        return self.wiki

    def get_normalized_defect_sync(self, issue_id: str, **kwargs) -> DefectRecord:
        assert issue_id == self.defect.defect_id
        return self.defect

    async def list_open_defects(self, **kwargs) -> list[DefectRecord]:
        return [self.defect]

    def update_status(self, *args, **kwargs):
        self.status_updates += 1
        raise AssertionError("workflow must not update ONES status")

    def add_comment(self, *args, **kwargs):
        self.comment_writes += 1


@dataclass
class FakePR:
    creates: int = 0
    url: str | None = None

    def find(self, **kwargs):
        return self.url

    def create(self, **kwargs):
        self.creates += 1
        self.url = "https://git.example.invalid/team/sample/-/merge_requests/1"
        return self.url


@dataclass
class FakeComment:
    ones: FakeONES
    calls: int = 0

    def ensure_comment(self, run):
        assert run.publication.pr_url
        self.calls += 1
        self.ones.add_comment(run.work_item_id, run.publication.pr_url)
        return f"comment-{run.run_id}"


@dataclass
class RecordingRepository:
    delegate: WorktreeRepository
    prepare_commit_calls: int = 0
    commit_calls: int = 0
    push_calls: int = 0

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)

    def prepare_commit_intent(self, run, approval):
        self.prepare_commit_calls += 1
        return self.delegate.prepare_commit_intent(run, approval)

    def commit_approved(self, run):
        self.commit_calls += 1
        return self.delegate.commit_approved(run)

    def push_approved(self, run):
        self.push_calls += 1
        return self.delegate.push_approved(run)


@dataclass
class _RealTestRunner:
    project_root: Path
    outputs: list[str] = field(default_factory=list)

    def run(self, command: str, *, cwd: Path) -> CommandResult:
        return self.run_argv(parse_command_argv(command), display_command=command, cwd=cwd)

    def run_argv(self, argv: tuple[str, ...], *, display_command: str, cwd: Path) -> CommandResult:
        started = datetime.now(UTC)
        actual_argv = (sys.executable, *argv[1:]) if argv[0] == "python" else argv
        completed = subprocess.run(
            actual_argv, cwd=cwd, capture_output=True, check=False,
            env={**os.environ, "UV_PROJECT": str(self.project_root)},
        )
        finished = datetime.now(UTC)
        output = completed.stdout + completed.stderr
        self.outputs.append(output.decode("utf-8", "replace"))
        return CommandResult(
            command=display_command, argv=argv, exit_code=completed.returncode,
            summary="passed" if completed.returncode == 0 else "failed as expected",
            started_at=started, finished_at=finished,
            outcome=(CommandOutcome.PASSED if completed.returncode == 0 else
                     CommandOutcome.TEST_FAILED if completed.returncode == 1 else
                     CommandOutcome.COMMAND_ERROR),
            output_sha256=hashlib.sha256(output).hexdigest(),
        )


class FakeRequirementCodex:
    command: str = ""
    def preflight(self, **kwargs):
        return CodexResult(summary="Requirement and acceptance criterion are consistent.")

    def run_stage(self, stage: str, **kwargs):
        prepared = kwargs["prepared"]
        if stage == "implementation":
            (prepared.path / "src" / "report.py").write_text(
                "def export(rows):\n    return [] if not rows else rows\n", encoding="utf-8"
            )
            (prepared.path / "tests" / "test_report.py").write_text(
                "from pathlib import Path\nns = {}\nexec((Path(__file__).parents[1] / 'src/report.py').read_text(), ns)\nassert ns['export']([]) == []\n",
                encoding="utf-8",
            )
            return CodexResult(
                summary="Implemented empty export.",
                changed_files=("src/report.py", "tests/test_report.py"),
                evidence=("AC-1 -> src/report.py and tests/test_report.py",),
                acceptance_coverage=(AcceptanceCoverage(
                    criterion_id="AC-1", criterion_text="空数据可以安全导出",
                    files=("src/report.py", "tests/test_report.py"),
                    tests=(self.command,),
                ),),
            )
        assert stage == "review"
        return CodexResult(
            summary="Implementation and test cover the acceptance criterion.",
            changed_files=("src/report.py", "tests/test_report.py"),
            review_findings=("No unrelated changes.",), unrelated_changes_checked=True,
        )

    def analyze_testing(self, **kwargs):
        return CodexResult(summary="Configured test passed with real exit evidence.")


class FakeDefectCodex:
    def __init__(self) -> None:
        self.stages: list[str] = []
        self.command = ""

    def preflight(self, **kwargs):
        return CodexResult(summary="ONES defect source contains concrete reproduction clues.")

    def run_stage(self, stage: str, **kwargs):
        self.stages.append(stage)
        prepared = kwargs["prepared"]
        evidence = RootCauseEvidence(
            file_path="src/report.py", location="lines 1-2 export", start_line=1, end_line=2,
            symbol="export", mechanism="Empty rows are indexed before an empty check.",
            code_excerpt="return rows[0]", reproduction_test="tests/test_report.py",
            test_selector="tests/test_report.py::test_empty", reproduction_command=self.command,
            confidence=0.9, insufficient_evidence=False, impacted_files=("src/report.py",),
            fix_steps=("Return an empty result before indexing.",),
            supporting_points=(RootCauseSupportingPoint(
                kind="defect", description="ONES reports empty export crashes.",
                source="ones", snippet="empty export crashes", direct_root_cause=False,
            ),),
        )
        common = dict(
            root_cause_evidence=(evidence,), behavior_before="Empty export raises IndexError.",
            impact_scope=("src/report.py", "tests/test_report.py"), risk_level="medium",
            risks=("Empty-input behavior changes.",),
        )
        if stage == "root_cause":
            return CodexResult(summary="Repository evidence confirms unchecked indexing.", **common)
        if stage == "reproduction":
            (prepared.path / "tests" / "test_report.py").write_text(
                "from pathlib import Path\nns = {}\nexec((Path(__file__).parents[1] / 'src/report.py').read_text(), ns)\nassert ns['export']([]) == []\n", encoding="utf-8"
            )
            return CodexResult(
                summary="Added deterministic failing reproduction.",
                changed_files=("tests/test_report.py",), unrelated_changes_checked=True, **common,
            )
        if stage == "implementation":
            (prepared.path / "src" / "report.py").write_text(
                "def export(rows):\n    return [] if not rows else rows[0]\n", encoding="utf-8"
            )
            return CodexResult(
                summary="Guarded empty rows.", changed_files=("src/report.py", "tests/test_report.py"),
                behavior_after="Empty export returns an empty result.", unrelated_changes_checked=True,
                **common,
            )
        assert stage == "review"
        return CodexResult(
            summary="Root cause, reproduction, repair and final tests agree.",
            changed_files=("src/report.py", "tests/test_report.py"),
            behavior_after="Empty export returns an empty result.",
            review_findings=("Regression and unrelated-change checks passed.",),
            unrelated_changes_checked=True, **common,
        )


def _sources() -> tuple[RequirementRecord, WikiPageSnapshot, DefectRecord]:
    url = "http://ones.invalid/wiki/#/team/T/space/S/page/W"
    wiki_text = "# 验收标准\n1. 空数据可以安全导出"
    wiki = WikiPageSnapshot(
        team_id="T", space_id="S", page_id="W", title="Export", version="1",
        updated_at="2026-08-11T00:00:00Z", normalized_content=wiki_text,
        content_sha256=hashlib.sha256(wiki_text.encode()).hexdigest(), source_url=url,
    )
    requirement = RequirementRecord(
        requirement_id="REQ-1", number="1", title="Safe export",
        project=ProjectRef(id="P", name="Project"), iteration=ProjectRef(id="I", name="Iteration"),
        status=StatusRef(id="open", name="Open", category="open"),
        wiki_refs=[WikiPageRef(team_id="T", space_id="S", page_id="W", source_url=url)],
    )
    defect = DefectRecord(
        defect_id="d" * 32, number="7", title="Empty export crashes",
        description="empty export crashes", project=ProjectRef(id="P", name="Project"),
        issue_type=IssueTypeRef(id="BUG", name="Defect"),
        priority=PriorityRef(id="high", value="High"),
        status=StatusRef(id="open", name="Open", category="open"),
        assignee=IdentityRef(id="alice", name="Alice"),
        updated_at="2026-08-11T00:00:00Z",
        raw={"key": "BUG-7", "sprint": {"uuid": "I"}},
    )
    return requirement, wiki, defect


def _assembly(tmp_path: Path, bare_remote: Path, codex):
    project_root = Path(__file__).resolve().parents[1]
    test_command = "python tests/test_report.py"
    codex.command = test_command
    mapping = RepositoryMapping(
        key="sample", project_id="P", iteration_id="I", repo_url=REMOTE_URL,
        repo_name="sample", base_branch="main",
        test_commands=(test_command,),
        allowed_paths=("src", "tests"),
    )
    config = DeveloperWorkflowConfig(
        run_root=(tmp_path / "runs").resolve(), worktree_root=(tmp_path / "trees").resolve(),
        mirror_root=(tmp_path / "mirrors").resolve(), sandbox_permission_profile="test-profile",
        max_codex_attempts=3, repositories=(mapping,),
        publishing=PublishingConfig(provider=PublishingProvider.GITLAB),
    )
    store = FileRunStore(config.run_root)
    remap = _URLRemapRunner(bare_remote)
    raw_repository = WorktreeRepository(
        config.mirror_root, config.worktree_root, command_runner=remap,
        identity_env_provider=lambda: {
            "GIT_AUTHOR_NAME": "E2E Publisher", "GIT_AUTHOR_EMAIL": "e2e@example.invalid",
            "GIT_COMMITTER_NAME": "E2E Publisher", "GIT_COMMITTER_EMAIL": "e2e@example.invalid",
        },
    )
    repository = RecordingRepository(raw_repository)
    # Keep the production repository boundary exercised by the workflow; do
    # not pre-create or mutate its branch here.
    requirement, wiki, defect = _sources()
    ones = FakeONES(requirement, wiki, defect)
    runner = _RealTestRunner(project_root)
    requirement_flow = RequirementFlow(store, ones, config, repository, codex, runner)
    defect_flow = DefectFlow(store, config, repository, codex, runner)
    candidates = DefectCandidateService(ones, "BUG")
    pr, comment = FakePR(), FakeComment(ones)
    publisher = Publisher(
        store, repository, WorkflowApprovalRebuilder(ones, repository), pr, comment,
        provider="gitlab", provider_host="git.example.invalid",
    )
    return DeveloperWorkflowOrchestrator(
        store, requirement_flow, defect_flow, publisher, config, candidates
    ), ones, pr, comment, bare_remote, remap, codex, runner, repository


def test_requirement_wiki_to_approved_publish_is_end_to_end(tmp_path: Path, bare_remote: Path) -> None:
    orchestrator, ones, pr, comment, remote, remap, _, runner, repository = _assembly(tmp_path, bare_remote, FakeRequirementCodex())

    validating = orchestrator.start_requirement("REQ-1")
    assert validating.state is WorkflowState.VALIDATING
    waiting = orchestrator.confirm_repository(validating.run_id, "sample")
    assert waiting.state is WorkflowState.WAITING_APPROVAL, (waiting.blocked_reason, waiting.resume_state, runner.outputs)
    assert (repository.prepare_commit_calls, repository.commit_calls, repository.push_calls) == (0, 0, 0)
    assert pr.creates == 0 and comment.calls == 0
    assert ones.comment_writes == 0 and ones.status_updates == 0
    completed = orchestrator.approve(waiting.run_id, "reviewer@example.invalid")

    assert completed.state is WorkflowState.COMPLETED
    assert completed.publication.pr_url
    assert (repository.prepare_commit_calls, repository.commit_calls, repository.push_calls) == (1, 1, 1)
    assert pr.creates == 1 and comment.calls == 1 and ones.status_updates == 0
    assert ones.comment_writes == 1
    assert _git("show-ref", completed.publication.remote_branch, cwd=remote)


@pytest.mark.asyncio
async def test_defect_snapshot_single_selection_evidence_and_publish_is_end_to_end(
    tmp_path: Path, bare_remote: Path,
) -> None:
    orchestrator, ones, pr, comment, _, remap, codex, runner, repository = _assembly(tmp_path, bare_remote, FakeDefectCodex())
    candidates = await orchestrator.defect_candidates.list_candidates("P", "I", "alice")
    assert len(candidates) == 1
    selected = candidates[0]

    waiting = orchestrator.start_defect("P", "I", "alice", selected.snapshot_token, selected.uuid)
    assert waiting.state is WorkflowState.VALIDATING
    waiting = orchestrator.confirm_repository(waiting.run_id, "sample")
    assert waiting.state is WorkflowState.WAITING_APPROVAL, (waiting.blocked_reason, waiting.resume_state, codex.stages, runner.outputs)
    assert waiting.pre_fix_test_results[0].outcome is CommandOutcome.TEST_FAILED
    assert len(waiting.reproduction_test_sha256) == 64
    assert (repository.prepare_commit_calls, repository.commit_calls, repository.push_calls) == (0, 0, 0)
    assert pr.creates == 0 and comment.calls == 0
    assert ones.comment_writes == 0 and ones.status_updates == 0
    completed = orchestrator.approve(waiting.run_id, "reviewer@example.invalid")

    assert completed.state is WorkflowState.COMPLETED
    assert (repository.prepare_commit_calls, repository.commit_calls, repository.push_calls) == (1, 1, 1)
    assert pr.creates == 1 and comment.calls == 1 and ones.status_updates == 0
    assert ones.comment_writes == 1
