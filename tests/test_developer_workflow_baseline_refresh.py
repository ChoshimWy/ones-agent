from __future__ import annotations

import shutil

import pytest

from src.developer_workflow.baseline_refresh import BaselineMigrationError, transfer
from src.developer_workflow.contracts import RepositoryRunEvidence, WorkflowState
from src.developer_workflow.repository import RemoteBaseChangedError
from test_developer_workflow_repository import remote_repository, _repository, _mapping, _git
from test_developer_workflow_missing_baseline_handoff import missing_run


@pytest.mark.parametrize("conflict", [False, True])
def test_real_git_migration_preserves_source_and_untracked_binary(tmp_path, remote_repository, conflict):
    upstream, remote = remote_repository
    repo, mapping = _repository(tmp_path), _mapping(remote)
    old = repo.prepare("old", mapping, "bugfix/old")
    (old.path / "README.md").write_text("accepted repair\n", encoding="utf-8")
    (old.path / "regression.bin").write_bytes(b"\0frozen\xff")
    expected = repo.snapshot(old, mapping)
    index_before = _git("diff", "--cached", cwd=old.path)
    if conflict:
        (upstream / "README.md").write_text("upstream change\n", encoding="utf-8")
    else:
        (upstream / "upstream.txt").write_text("keep upstream\n", encoding="utf-8")
    _git("add", "-A", cwd=upstream)
    _git("commit", "-m", "upstream advanced", cwd=upstream)
    _git("push", str(remote), "main", cwd=upstream)
    new = repo.prepare("new", mapping, "bugfix/new")
    source = RepositoryRunEvidence(repository_key=mapping.key, mapping=mapping,
                                    prepared_worktree=old, tested_snapshot=expected, changed_files=expected.changed_files)
    migrated, conflicts = transfer(repo, source, new)
    assert new.base_commit != old.base_commit
    assert repo.snapshot(old, mapping) == expected
    assert _git("diff", "--cached", cwd=old.path) == index_before
    assert (new.path / "regression.bin").read_bytes() == b"\0frozen\xff"
    assert _git("diff", "--cached", cwd=new.path) == ""
    assert migrated.head_commit == new.head_commit
    if conflict:
        assert conflicts == ("README.md",)
        assert "<<<<<<<" in (new.path / "README.md").read_text()
    else:
        assert conflicts == ()
        assert (new.path / "upstream.txt").read_text() == "keep upstream\n"
        assert (new.path / "README.md").read_text() == "accepted repair\n"


def fake_migration(monkeypatch, tmp_path, repo, run):
    old = run.prepared_worktree

    def prepare(workspace_id, mapping, branch, **kwargs):
        path = tmp_path / workspace_id
        shutil.copytree(old.path, path)
        return old.validated_update(path=path, branch=branch)

    monkeypatch.setattr(repo, "prepare", prepare)
    monkeypatch.setattr("src.developer_workflow.baseline_refresh.transfer", lambda *_: (run.tested_snapshot, ()))


def test_same_task_refresh_invalidates_evidence_and_returns_to_testing(tmp_path, monkeypatch):
    flow, store, repo, codex, run = missing_run(tmp_path)
    fake_migration(monkeypatch, tmp_path, repo, run)
    run = store.transition(run.run_id, run.version, WorkflowState.AI_REVIEW, "resume")
    before = tuple(codex.stages)
    result = flow._refresh_baseline(run)
    assert result.run_id == run.run_id
    assert result.state is WorkflowState.BLOCKED and result.resume_state is WorkflowState.TESTING
    assert result.blocked_reason == "baseline migrated; revalidate repair"
    assert result.prepared_worktree.path != run.prepared_worktree.path
    assert result.review is None and result.approval is None
    assert result.test_results == result.pre_fix_test_results == result.verification_records == ()
    assert result.tested_snapshot is None
    assert result.baseline_refreshes[-1].source_review == run.review
    assert result.baseline_refreshes[-1].source_repositories[0].prepared_worktree == run.prepared_worktree
    assert result.reproduction_test_sha256 == run.reproduction_test_sha256
    assert tuple(codex.stages) == before


def test_failed_migration_keeps_active_worktree_and_consumes_budget(tmp_path, monkeypatch):
    flow, store, repo, _, run = missing_run(tmp_path)
    fake_migration(monkeypatch, tmp_path, repo, run)
    flow.config = flow.config.validated_update(max_baseline_refreshes=1)
    def fail(*args):
        raise BaselineMigrationError("conflict cannot be migrated safely")
    monkeypatch.setattr("src.developer_workflow.baseline_refresh.transfer", fail)
    run = store.transition(run.run_id, run.version, WorkflowState.AI_REVIEW, "resume")
    result = flow._refresh_baseline(run)
    assert result.prepared_worktree == run.prepared_worktree
    assert result.baseline_refreshes[-1].status == "failed"
    assert result.baseline_refreshes[-1].destinations
    result = store.transition(result.run_id, result.version, WorkflowState.AI_REVIEW, "resume")
    limited = flow._refresh_baseline(result)
    assert limited.blocked_reason == "automatic baseline refresh limit reached"


def test_execute_automatically_retests_and_reviews_after_remote_moves(tmp_path, monkeypatch):
    flow, store, repo, codex, run = missing_run(tmp_path)
    fake_migration(monkeypatch, tmp_path, repo, run)
    original = type(flow)._approval_package
    calls = []
    def moved_once(self, current, snapshot):
        calls.append(True)
        if len(calls) == 1:
            raise RemoteBaseChangedError("remote moved")
        return original(self, current, snapshot)
    monkeypatch.setattr(type(flow), "_approval_package", moved_once)
    flow.test_runner.exit_codes.extend([0] * 20)
    before = tuple(codex.stages)
    result = flow.execute(run)
    assert result.state is WorkflowState.WAITING_APPROVAL, result.blocked_reason
    assert result.approval.draft_pr
    assert result.test_results
    assert tuple(codex.stages) == (*before, "review")
    assert len(result.baseline_refreshes) == 1


def test_migration_conflicts_are_sent_to_same_session_implementation(tmp_path, monkeypatch):
    flow, store, repo, codex, run = missing_run(tmp_path)
    fake_migration(monkeypatch, tmp_path, repo, run)
    def conflict_transfer(repository, source, destination):
        repository.root = destination.path
        (destination.path / "src/export.py").write_text("<<<<<<< ours\nupstream\n=======\nrepair\n>>>>>>> theirs\n")
        return run.tested_snapshot, ("src/export.py",)
    monkeypatch.setattr("src.developer_workflow.baseline_refresh.transfer", conflict_transfer)
    monkeypatch.setattr(repo, "resolve_repository_path", lambda prepared, mapping, path: prepared.path / path, raising=False)
    run = store.transition(run.run_id, run.version, WorkflowState.AI_REVIEW, "resume")
    migrated = flow._refresh_baseline(run)
    assert migrated.resume_state is WorkflowState.IMPLEMENTING
    assert len(migrated.codex_results) == 2
    flow.test_runner.exit_codes.extend([0] * 20)
    stages = tuple(codex.stages)
    result = flow.execute(migrated)
    assert result.state is WorkflowState.WAITING_APPROVAL, result.blocked_reason
    assert tuple(codex.stages) == (*stages, "implementation", "review")
    assert "<<<<<<<" not in (result.prepared_worktree.path / "src/export.py").read_text()
    assert result.run_id == run.run_id


def test_multi_repository_refresh_switches_only_after_all_migrate(tmp_path, monkeypatch):
    from src.developer_workflow.contracts import RepositoryGroupMapping
    flow, store, repo, _, run = missing_run(tmp_path)
    mapping = run.repository
    second = mapping.validated_update(key="secondary", repo_name="secondary", role="dependency")
    group = RepositoryGroupMapping(key="group", project_id=mapping.project_id, iteration_id=mapping.iteration_id,
                                   primary_repository=mapping.key, repositories=(mapping, second))
    sources = tuple(RepositoryRunEvidence(repository_key=key, mapping=mapping if key == mapping.key else second,
        prepared_worktree=run.prepared_worktree, tested_snapshot=run.tested_snapshot,
        changed_files=run.changed_files, test_results=run.test_results) for key in group.topological_keys())
    run = run.validated_update(repository=None, repository_group=group, repository_model_version=2, repository_evidence=sources)
    store.run = run
    def prepare(workspace_id, mapping, branch, *, repository_key):
        path = tmp_path / workspace_id / repository_key
        shutil.copytree(run.prepared_worktree.path, path)
        return run.prepared_worktree.validated_update(path=path, branch=branch)
    monkeypatch.setattr(repo, "prepare", prepare)
    monkeypatch.setattr("src.developer_workflow.baseline_refresh.transfer", lambda _, source, fresh: (source.tested_snapshot, ()))
    run = store.transition(run.run_id, run.version, WorkflowState.AI_REVIEW, "resume")
    result = flow._refresh_baseline(run)
    assert result.resume_state is WorkflowState.TESTING, result.blocked_reason
    assert tuple(e.repository_key for e in result.repository_evidence) == group.topological_keys()
    assert len({e.prepared_worktree.path.parent for e in result.repository_evidence}) == 1
    assert all(not e.test_results and e.tested_snapshot is None for e in result.repository_evidence)
    assert len(result.baseline_refreshes[-1].source_repositories) == 2


def test_frozen_test_change_stops_switch_and_records_reason(tmp_path, monkeypatch):
    flow, store, repo, _, run = missing_run(tmp_path)
    fake_migration(monkeypatch, tmp_path, repo, run)
    def tamper(_, source, fresh):
        (fresh.path / run.root_cause_evidence[0].reproduction_test).write_text("changed test")
        return source.tested_snapshot, ()
    monkeypatch.setattr("src.developer_workflow.baseline_refresh.transfer", tamper)
    run = store.transition(run.run_id, run.version, WorkflowState.AI_REVIEW, "resume")
    result = flow._refresh_baseline(run)
    assert result.prepared_worktree == run.prepared_worktree
    assert result.baseline_refreshes[-1].status == "failed"
    assert "冻结复现" in result.baseline_refreshes[-1].failure_reason


def test_migration_record_round_trip_and_ui(tmp_path, monkeypatch):
    from src.developer_workflow.contracts import WorkflowRun
    from src.developer_workflow.tui.models import RunDetail
    flow, store, repo, _, run = missing_run(tmp_path)
    fake_migration(monkeypatch, tmp_path, repo, run)
    run = store.transition(run.run_id, run.version, WorkflowState.AI_REVIEW, "resume")
    result = flow._refresh_baseline(run)
    loaded = WorkflowRun.model_validate_json(result.model_dump_json())
    assert loaded == result
    detail = RunDetail.from_run(loaded)
    assert "旧基线" in detail.baseline_refresh_history[0]
    assert "新基线" in detail.baseline_refresh_history[0]


def test_remote_keeps_moving_stops_at_persisted_budget(tmp_path, monkeypatch):
    flow, _, repo, codex, run = missing_run(tmp_path)
    fake_migration(monkeypatch, tmp_path, repo, run)
    flow.config = flow.config.validated_update(max_baseline_refreshes=2)
    def moved(*args):
        raise RemoteBaseChangedError("moved again")
    monkeypatch.setattr(type(flow), "_approval_package", moved)
    flow.test_runner.exit_codes.extend([0] * 40)
    initial_reviews = codex.stages.count("review")
    result = flow.execute(run)
    assert result.blocked_reason == "automatic baseline refresh limit reached"
    assert len(result.baseline_refreshes) == 2
    assert codex.stages.count("review") == initial_reviews + 2
    assert result.approval is None


def test_interrupted_migration_is_archived_not_overwritten(tmp_path):
    from src.developer_workflow.contracts import BaselineRefreshRecord
    flow, store, _, _, run = missing_run(tmp_path)
    flow.config = flow.config.validated_update(max_baseline_refreshes=1)
    interrupted = BaselineRefreshRecord(workspace_id="a" * 32, source_repositories=())
    run = run.validated_update(baseline_refreshes=(interrupted,))
    store.run = run
    run = store.transition(run.run_id, run.version, WorkflowState.AI_REVIEW, "resume")
    result = flow._refresh_baseline(run)
    assert result.baseline_refreshes[0].status == "failed"
    assert "中断" in result.baseline_refreshes[0].failure_reason
    assert result.prepared_worktree == run.prepared_worktree
    assert result.blocked_reason == "automatic baseline refresh limit reached"
