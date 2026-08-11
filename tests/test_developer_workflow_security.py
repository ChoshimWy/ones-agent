from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
from pathlib import Path

import pytest

from src.contracts import ProjectRef, RequirementRecord, StatusRef, WikiPageRef, WikiPageSnapshot
from src.developer_workflow.approval import issue_approval
from src.developer_workflow.contracts import (
    ApprovalPackage,
    CodexResult,
    CommandResult,
    PreparedWorktree,
    PublicationResult,
    RepositoryMapping,
    RepositorySnapshot,
    WorkflowRun,
    WorkflowState,
)
from src.developer_workflow.config import DeveloperWorkflowConfig, PublishingConfig, PublishingProvider
from src.developer_workflow.publisher import PublicationBlocked, Publisher
from src.developer_workflow.requirement_flow import RequirementFlow
from src.developer_workflow.state_store import FileRunStore


NOW = datetime(2026, 8, 11, tzinfo=UTC)


def _assembly_config(tmp_path: Path, mapping: RepositoryMapping) -> DeveloperWorkflowConfig:
    return DeveloperWorkflowConfig(
        run_root=(tmp_path / "runs").resolve(),
        worktree_root=(tmp_path / "worktrees").resolve(),
        mirror_root=(tmp_path / "mirrors").resolve(),
        sandbox_permission_profile="managed-test-profile",
        max_codex_attempts=1,
        repositories=(mapping,),
        publishing=PublishingConfig(provider=PublishingProvider.LOCAL_FAKE),
    )


def _assembly_requirement() -> RequirementRecord:
    return RequirementRecord(
        requirement_id="REQ-1",
        number="REQ-1",
        title="Secure requirement",
        project=ProjectRef(id="P"),
        iteration=ProjectRef(id="I"),
        status=StatusRef(id="open", name="Open", category="open"),
        wiki_refs=[WikiPageRef(
            team_id="T", space_id="S", page_id="W",
            source_url="http://ones.invalid/wiki/#/team/T/space/S/page/W",
        )],
    )


def _assembly_wiki(content: str) -> WikiPageSnapshot:
    return WikiPageSnapshot(
        team_id="T", space_id="S", page_id="W", title="Requirement", version="1",
        updated_at="2026-08-11T00:00:00Z", normalized_content=content,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        source_url="http://ones.invalid/wiki/#/team/T/space/S/page/W",
    )


@dataclass
class _AssemblyGateway:
    wiki: WikiPageSnapshot

    def get_normalized_requirement_sync(self, issue_id: str) -> RequirementRecord:
        assert issue_id == "REQ-1"
        return _assembly_requirement()

    def get_wiki_snapshot_sync(self, url: str) -> WikiPageSnapshot:
        assert url == self.wiki.source_url
        return self.wiki


@dataclass
class _AssemblyRepository:
    root: Path
    prepare_calls: int = 0
    head_changed: bool = False

    def recover(self, run_id, mapping, branch):
        return None

    def prepare(self, run_id, mapping, branch):
        self.prepare_calls += 1
        return PreparedWorktree(
            path=(self.root / "worktree").resolve(), branch=branch,
            base_commit="a" * 40, head_commit="a" * 40,
            mirror_path=(self.root / "mirror.git").resolve(),
        )

    def assert_head_unchanged(self, prepared):
        if self.head_changed:
            raise RuntimeError("Codex changed HEAD")

    def snapshot(self, prepared, mapping):
        return RepositorySnapshot(
            head_commit="a" * 40, diff_sha256="b" * 64,
            changed_files=("src/a.py",), patch="diff --git a/src/a.py b/src/a.py\n+x",
            is_clean=False,
        )


@dataclass
class _HeadChangingCodex:
    repository: _AssemblyRepository

    def preflight(self, **kwargs):
        return CodexResult(summary="source is verifiable")

    def run_stage(self, stage: str, **kwargs):
        assert stage == "implementation"
        self.repository.head_changed = True
        return CodexResult(summary="implementation", changed_files=("src/a.py",))

    def analyze_testing(self, **kwargs):
        raise AssertionError("testing must not run after HEAD drift")


class _NeverTestRunner:
    def run(self, command: str, *, cwd: Path):
        raise AssertionError("tests must not run before source and HEAD gates pass")


def _package() -> ApprovalPackage:
    mapping = RepositoryMapping(
        key="repo", project_id="P", iteration_id="I",
        repo_url="https://git.example.invalid/team/repo.git", repo_name="repo",
        base_branch="main", test_commands=("pytest",), allowed_paths=("src",),
    )
    return ApprovalPackage(
        work_item_id="REQ-1", work_item_title="Title", work_item_status="Open",
        source_versions={"requirement_sha256": "1" * 64}, wiki_hashes={"W": "2" * 64},
        wiki_snapshots=(WikiPageSnapshot(
            team_id="T", space_id="S", page_id="W", title="Doc", version="1",
            updated_at="2026-08-11T00:00:00Z", normalized_content="AC",
            content_sha256="2" * 64, source_url="http://ones.invalid/wiki",
        ),),
        repository=mapping, repo_url=mapping.repo_url, base_branch="main",
        base_commit="a" * 40, head_commit="a" * 40, diff_hash="d" * 64,
        diff_summary="1 file", branch="requirement/REQ-1-title",
        changed_files=("src/a.py",), coverage={"AC-1": "src/a.py; pytest"},
        evidence=("src/a.py:1",), tests=(CommandResult(
            command="pytest", argv=("pytest",), exit_code=0, summary="1 passed",
            started_at=NOW, finished_at=NOW,
        ),), review=("reviewed",), risks=("low",), unrelated_changes_checked=True,
        commit_message="feat: title", pr_title="feat: title", pr_body="Run REQ-1",
    )


def _waiting(tmp_path, *, signed: bool = True):
    store = FileRunStore(tmp_path / "runs")
    run = store.create(WorkflowRun.new("requirement", "REQ-1"))
    for state in (
        WorkflowState.READING_ONES, WorkflowState.VALIDATING, WorkflowState.PREPARING_REPO,
        WorkflowState.IMPLEMENTING, WorkflowState.TESTING, WorkflowState.AI_REVIEW,
    ):
        run = store.transition(run.run_id, run.version, state, state.value)
    package = issue_approval(_package(), approved_by="reviewer") if signed else _package()
    run = store.save(run.validated_update(approval=package), run.version)
    run = store.transition(run.run_id, run.version, WorkflowState.WAITING_APPROVAL, "wait")
    return store, run


@dataclass
class _Repository:
    calls: list[str] = field(default_factory=list)

    def prepare_commit_intent(self, run, approval): self.calls.append("prepare"); return "f" * 40
    def find_approved_commit(self, run): self.calls.append("find_commit"); return None
    def commit_approved(self, run): self.calls.append("commit"); return "c" * 40
    def remote_branch_oid(self, run):
        self.calls.append("remote")
        return run.publication.commit_hash if "push" in self.calls else None
    def push_approved(self, run): self.calls.append("push")


@dataclass
class _PR:
    fail: bool = False
    creates: int = 0
    def find(self, **kwargs): return None
    def create(self, **kwargs):
        self.creates += 1
        if self.fail: raise RuntimeError("private-provider-token")
        return "https://git.example.invalid/team/repo/-/merge_requests/1"


@dataclass
class _Comment:
    fail_once: bool = False
    calls: int = 0
    def ensure_comment(self, run):
        self.calls += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("private-ones-password")
        return "comment-1"


def _publisher(store, rebuilt, repo=None, pr=None, comment=None):
    return Publisher(
        store, repo or _Repository(), lambda run: rebuilt, pr or _PR(), comment or _Comment(),
        provider="gitlab", provider_host="git.example.invalid",
    )


def test_unapproved_run_has_zero_git_remote_pr_and_comment_side_effects(tmp_path) -> None:
    store, run = _waiting(tmp_path, signed=False)
    repo, pr, comment = _Repository(), _PR(), _Comment()
    with pytest.raises(PublicationBlocked):
        _publisher(store, _package(), repo, pr, comment).publish(run)
    assert repo.calls == [] and pr.creates == 0 and comment.calls == 0


@pytest.mark.parametrize("drift", ["source", "base", "head", "diff", "test", "risk", "pr"])
def test_every_approval_fingerprint_drift_blocks_before_any_side_effect(tmp_path, drift: str) -> None:
    store, run = _waiting(tmp_path)
    original = _package()
    if drift == "source": changed = original.model_copy(update={"source_versions": {"requirement_sha256": "9" * 64}})
    elif drift == "base": changed = original.model_copy(update={"base_commit": "b" * 40})
    elif drift == "head": changed = original.model_copy(update={"head_commit": "b" * 40})
    elif drift == "diff": changed = original.model_copy(update={"diff_hash": "e" * 64})
    elif drift == "test":
        changed_test = original.tests[0].model_copy(update={"summary": "2 passed"})
        changed = original.model_copy(update={"tests": (changed_test,)})
    elif drift == "risk": changed = original.model_copy(update={"risks": ("high",)})
    else: changed = original.model_copy(update={"pr_body": "changed"})
    repo, pr, comment = _Repository(), _PR(), _Comment()
    with pytest.raises(PublicationBlocked):
        _publisher(store, changed, repo, pr, comment).publish(run)
    assert repo.calls == [] and pr.creates == 0 and comment.calls == 0


def test_pr_failure_never_calls_ones_comment(tmp_path) -> None:
    store, run = _waiting(tmp_path)
    comment = _Comment()
    with pytest.raises(PublicationBlocked) as caught:
        _publisher(store, _package(), pr=_PR(fail=True), comment=comment).publish(run)
    assert "token" not in str(caught.value) and comment.calls == 0


def test_partial_success_resume_only_retries_comment(tmp_path) -> None:
    store, run = _waiting(tmp_path)
    repo, pr, comment = _Repository(), _PR(), _Comment(fail_once=True)
    publisher = _publisher(store, _package(), repo, pr, comment)
    partial = publisher.publish(run)
    assert partial.state is WorkflowState.PARTIAL_SUCCESS
    before_repo, before_pr = list(repo.calls), pr.creates

    completed = publisher.retry_comment(partial)

    assert completed.state is WorkflowState.COMPLETED
    assert repo.calls == before_repo and pr.creates == before_pr and comment.calls == 2


def test_completed_state_contract_requires_pr_and_comment_facts() -> None:
    run = WorkflowRun.new("requirement", "REQ-1")
    with pytest.raises(ValueError):
        run.validated_update(
            state=WorkflowState.COMPLETED,
            publication=PublicationResult(commit_hash="c" * 40),
        )


def test_requirement_without_verifiable_acceptance_never_prepares_worktree(tmp_path: Path) -> None:
    mapping = _package().repository
    assert mapping is not None
    repository = _AssemblyRepository(tmp_path)
    flow = RequirementFlow(
        store=FileRunStore(tmp_path / "runs"),
        gateway=_AssemblyGateway(_assembly_wiki("# Background\nNo acceptance list.")),
        config=_assembly_config(tmp_path, mapping),
        repository=repository,
        codex=_HeadChangingCodex(repository),
        test_runner=_NeverTestRunner(),
    )
    run = flow.store.create(WorkflowRun.new("requirement", "REQ-1"))

    result = flow.execute(run)

    assert result.state is WorkflowState.BLOCKED
    assert result.resume_state is WorkflowState.READING_ONES
    assert repository.prepare_calls == 0


def test_codex_head_change_blocks_real_requirement_flow_before_tests_or_publication(
    tmp_path: Path,
) -> None:
    mapping = _package().repository
    assert mapping is not None
    repository = _AssemblyRepository(tmp_path)
    flow = RequirementFlow(
        store=FileRunStore(tmp_path / "runs"),
        gateway=_AssemblyGateway(_assembly_wiki("# Acceptance Criteria\n- behavior is testable")),
        config=_assembly_config(tmp_path, mapping),
        repository=repository,
        codex=_HeadChangingCodex(repository),
        test_runner=_NeverTestRunner(),
    )
    initial = WorkflowRun.new("requirement", "REQ-1").validated_update(repository=mapping)
    run = flow.store.create(initial)

    result = flow.execute(run)

    assert result.state is WorkflowState.BLOCKED
    assert result.resume_state is WorkflowState.IMPLEMENTING
    assert repository.prepare_calls == 1
    assert result.test_results == ()
    assert result.publication == PublicationResult()
