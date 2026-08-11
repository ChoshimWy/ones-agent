from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import threading
import time

import pytest

from src.contracts import WikiPageSnapshot
from src.developer_workflow.approval import issue_approval
from src.developer_workflow.contracts import (
    ApprovalPackage,
    CommandResult,
    RepositoryMapping,
    WorkflowRun,
    WorkflowState,
)
from src.developer_workflow.publisher import PublicationBlocked, Publisher
from src.developer_workflow.state_store import FileRunStore


def _package() -> ApprovalPackage:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    mapping = RepositoryMapping(
        key="repo", project_id="P", iteration_id="I",
        repo_url="https://github.example/team/repo.git", repo_name="repo",
        base_branch="main", test_commands=("pytest",), allowed_paths=("src",),
    )
    return ApprovalPackage(
        work_item_id="REQ-1", work_item_title="Title", work_item_status="Doing",
        source_versions={"version":"1"}, wiki_hashes={"page":"b"*64},
        wiki_snapshots=(WikiPageSnapshot(team_id="T",space_id="S",page_id="page",title="Doc",version="1",updated_at="2026-08-10T00:00:00Z",normalized_content="AC",content_sha256="b"*64,source_url="http://ones/wiki"),),
        repository=mapping, repo_url=mapping.repo_url, base_branch="main",
        base_commit="a"*40, head_commit="a"*40, diff_hash="d"*64,
        diff_summary="1 file", branch="requirement/REQ-1-title",
        changed_files=("src/a.py",), coverage={"AC":"test"}, evidence=("src/a.py:1",),
        tests=(CommandResult(command="pytest",argv=("pytest",),exit_code=0,summary="1 passed",started_at=now,finished_at=now),),
        review=("ok",), risks=("low",), unrelated_changes_checked=True,
        commit_message="feat: title", pr_title="feat: title", pr_body="Run REQ-1",
    )


def _stored_waiting(tmp_path, *, signed: bool = True):
    store = FileRunStore(tmp_path / "runs")
    run = store.create(WorkflowRun.new("requirement", "REQ-1"))
    for target in (
        WorkflowState.READING_ONES, WorkflowState.VALIDATING,
        WorkflowState.PREPARING_REPO, WorkflowState.IMPLEMENTING,
        WorkflowState.TESTING, WorkflowState.AI_REVIEW,
    ):
        run = store.transition(run.run_id, run.version, target, target.value)
    package = issue_approval(_package(), approved_by="alice") if signed else _package()
    run = store.save(run.validated_update(approval=package), run.version)
    run = store.transition(run.run_id, run.version, WorkflowState.WAITING_APPROVAL, "wait")
    return store, run


@dataclass
class FakeRepository:
    calls: list[str] = field(default_factory=list)
    recovered_commit: str | None = None
    remote_oid: str | None = None
    fail_push: bool = False

    def prepare_commit_intent(self, run, approval):
        self.calls.append("prepare")
        return "f" * 40

    def find_approved_commit(self, run):
        self.calls.append("find_commit")
        return self.recovered_commit

    def commit_approved(self, run):
        self.calls.append("commit")
        self.recovered_commit = "c" * 40
        return self.recovered_commit

    def remote_branch_oid(self, run):
        self.calls.append("remote")
        return self.remote_oid

    def push_approved(self, run):
        self.calls.append("push")
        if self.fail_push:
            raise RuntimeError("token-secret")
        self.remote_oid = run.publication.commit_hash


@dataclass
class FakePR:
    existing: str | None = None
    fail: bool = False
    calls: list[str] = field(default_factory=list)

    def find(self, *, repo_url, head, base, marker):
        self.calls.append("find")
        return self.existing

    def create(self, *, repo_url, head, base, title, body, marker):
        self.calls.append("create")
        if self.fail:
            raise RuntimeError("provider-token")
        self.existing = "https://github.example/team/repo/pull/1"
        return self.existing


@dataclass
class FakeCommenter:
    fail: bool = False
    calls: int = 0

    def ensure_comment(self, run):
        self.calls += 1
        if self.fail:
            raise RuntimeError("ones-password")
        return "comment-1"


def _publisher(store, run, repo=None, pr=None, commenter=None, rebuild=None):
    return Publisher(
        store=store, repository=repo or FakeRepository(),
        approval_rebuilder=rebuild or (lambda current: _package()),
        pr_client=pr or FakePR(), commenter=commenter or FakeCommenter(),
        provider="github", provider_host="github.example",
    )


def test_unsigned_approval_has_zero_side_effects(tmp_path) -> None:
    store, run = _stored_waiting(tmp_path, signed=False)
    repo, pr, commenter = FakeRepository(), FakePR(), FakeCommenter()

    with pytest.raises(PublicationBlocked):
        _publisher(store, run, repo, pr, commenter).publish(run)

    assert repo.calls == [] and pr.calls == [] and commenter.calls == 0
    assert store.load(run.run_id).state is WorkflowState.WAITING_APPROVAL


def test_rebuilt_approval_change_blocks_before_side_effect(tmp_path) -> None:
    store, run = _stored_waiting(tmp_path)
    repo = FakeRepository()
    changed = _package().model_copy(update={"pr_title":"changed"})

    with pytest.raises(PublicationBlocked):
        _publisher(store, run, repo=repo, rebuild=lambda current: changed).publish(run)

    assert repo.calls == []


def test_crash_between_publishing_transition_and_intent_save_rebuilds_gate(tmp_path) -> None:
    store, run = _stored_waiting(tmp_path)
    class FailIntentOnce(FakeRepository):
        failed = False
        def prepare_commit_intent(self, run, approval):
            if not self.failed:
                self.failed = True
                raise RuntimeError("crash")
            return super().prepare_commit_intent(run, approval)
    repo = FailIntentOnce()
    publisher = _publisher(store, run, repo=repo)

    with pytest.raises(PublicationBlocked):
        publisher.publish(run)
    poisoned = store.load(run.run_id)
    assert poisoned.state is WorkflowState.PUBLISHING
    assert poisoned.publication.approved_fingerprint == ""

    assert publisher.publish(poisoned).state is WorkflowState.COMPLETED


def test_push_failure_retry_reuses_commit(tmp_path) -> None:
    store, run = _stored_waiting(tmp_path)
    repo = FakeRepository(fail_push=True)
    publisher = _publisher(store, run, repo=repo)

    with pytest.raises(PublicationBlocked) as caught:
        publisher.publish(run)
    assert "token-secret" not in str(caught.value)
    after = store.load(run.run_id)
    assert after.state is WorkflowState.PUBLISHING
    assert after.publication.commit_hash == "c" * 40

    repo.fail_push = False
    result = publisher.publish(after)
    assert result.state is WorkflowState.COMPLETED
    assert repo.calls.count("commit") == 1


def test_existing_remote_and_pr_are_reused(tmp_path) -> None:
    store, run = _stored_waiting(tmp_path)
    repo = FakeRepository(recovered_commit="c"*40, remote_oid="c"*40)
    pr = FakePR(existing="https://github.example/team/repo/pull/9")

    result = _publisher(store, run, repo=repo, pr=pr).publish(run)

    assert result.state is WorkflowState.COMPLETED
    assert "commit" not in repo.calls and "push" not in repo.calls
    assert pr.calls == ["find"]


def test_different_remote_oid_blocks_without_push(tmp_path) -> None:
    store, run = _stored_waiting(tmp_path)
    repo = FakeRepository(recovered_commit="c"*40, remote_oid="e"*40)

    with pytest.raises(PublicationBlocked):
        _publisher(store, run, repo=repo).publish(run)

    assert "push" not in repo.calls


def test_pr_failure_never_comments(tmp_path) -> None:
    store, run = _stored_waiting(tmp_path)
    pr, commenter = FakePR(fail=True), FakeCommenter()

    with pytest.raises(PublicationBlocked):
        _publisher(store, run, pr=pr, commenter=commenter).publish(run)

    assert commenter.calls == 0


def test_comment_failure_becomes_partial_and_retry_only_comments(tmp_path) -> None:
    store, run = _stored_waiting(tmp_path)
    repo, pr, commenter = FakeRepository(), FakePR(), FakeCommenter(fail=True)
    publisher = _publisher(store, run, repo, pr, commenter)

    partial = publisher.publish(run)
    assert partial.state is WorkflowState.PARTIAL_SUCCESS
    before = (list(repo.calls), list(pr.calls))

    commenter.fail = False
    completed = publisher.retry_comment(partial)
    assert completed.state is WorkflowState.COMPLETED
    assert (repo.calls, pr.calls) == before
    assert commenter.calls == 2


def test_resume_pr_uses_only_immutable_publication_intent(tmp_path) -> None:
    store, run = _stored_waiting(tmp_path)
    repo, pr = FakeRepository(fail_push=True), FakePR()
    publisher = _publisher(store, run, repo=repo, pr=pr)
    with pytest.raises(PublicationBlocked):
        publisher.publish(run)
    publishing = store.load(run.run_id)
    changed_approval = publishing.approval.model_copy(
        update={"repo_url":"https://evil.example/x/y.git", "pr_title":"evil"}
    )
    # Simulate old approval-package drift in storage. Resume must never consult it.
    publishing = store.save(
        publishing.validated_update(approval=changed_approval), publishing.version
    )
    repo.fail_push = False
    assert publisher.publish(publishing).state is WorkflowState.COMPLETED
    assert publishing.publication.repo_url == "https://github.example/team/repo.git"


def test_publication_intent_cannot_be_changed_after_checkpoint(tmp_path) -> None:
    store, run = _stored_waiting(tmp_path)
    repo = FakeRepository(fail_push=True)
    publisher = _publisher(store, run, repo=repo)
    with pytest.raises(PublicationBlocked):
        publisher.publish(run)
    publishing = store.load(run.run_id)
    poisoned = publishing.publication.model_copy(update={"pr_title":"changed"})
    from src.developer_workflow.state_store import InvalidRunMutationError
    with pytest.raises(InvalidRunMutationError, match="intent"):
        store.save(publishing.validated_update(publication=poisoned), publishing.version)


def test_resume_rejects_provider_configuration_drift(tmp_path) -> None:
    store, run = _stored_waiting(tmp_path)
    repo = FakeRepository(fail_push=True)
    publisher = _publisher(store, run, repo=repo)
    with pytest.raises(PublicationBlocked):
        publisher.publish(run)
    publishing = store.load(run.run_id)
    publisher.provider_host = "other.example"
    with pytest.raises(PublicationBlocked, match="differs"):
        publisher.publish(publishing)


def test_two_store_publishers_create_one_pr_and_one_comment(tmp_path) -> None:
    store1, run = _stored_waiting(tmp_path)
    store2 = FileRunStore(tmp_path / "runs")
    repo, commenter = FakeRepository(), FakeCommenter()

    @dataclass
    class SlowPR(FakePR):
        def create(self, **kwargs):
            time.sleep(0.05)
            return super().create(**kwargs)

    pr = SlowPR()
    publishers = (
        _publisher(store1, run, repo, pr, commenter),
        _publisher(store2, run, repo, pr, commenter),
    )
    barrier = threading.Barrier(2)
    results: list[WorkflowRun] = []

    def execute(publisher):
        barrier.wait()
        results.append(publisher.publish(run))

    threads = [threading.Thread(target=execute, args=(item,)) for item in publishers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert len(results) == 2
    assert all(item.state is WorkflowState.COMPLETED for item in results)
    assert pr.calls.count("create") == 1
    assert commenter.calls == 1
