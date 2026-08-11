from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from src.contracts import ProjectRef, RequirementRecord, StatusRef
from src.developer_workflow.approval import ApprovalInvalidatedError, issue_approval, verify_approval
from src.developer_workflow.approval_rebuilder import ApprovalRebuildError, WorkflowApprovalRebuilder
from src.developer_workflow.contracts import (
    AcceptanceCoverage, PreparedWorktree, RepositorySnapshot, WorkflowRun,
)
from tests.test_developer_workflow_publisher import _package


def _requirement(title: str = "Title") -> RequirementRecord:
    return RequirementRecord(
        requirement_id="REQ-1", number="REQ-1", title=title,
        project=ProjectRef(id="P", name="Project"),
        iteration=ProjectRef(id="I", name="Iteration"),
        status=StatusRef(id="doing", name="Doing", category="in_progress"),
    )


@dataclass
class Gateway:
    requirement: RequirementRecord
    wiki: object

    def get_normalized_requirement_sync(self, issue_id):
        return self.requirement

    def get_wiki_snapshot_by_ids_sync(self, space_id, page_id, *, source_url=None):
        return self.wiki


@dataclass
class Repository:
    current: RepositorySnapshot
    assertions: int = 0

    def assert_remote_base_unchanged(self, prepared, mapping):
        self.assertions += 1

    def assert_head_unchanged(self, prepared):
        self.assertions += 1

    def snapshot(self, prepared, mapping):
        return self.current


def _run(tmp_path: Path) -> tuple[WorkflowRun, Gateway, Repository]:
    package = _package()
    prepared = PreparedWorktree(
        path=(tmp_path / "tree").resolve(), mirror_path=(tmp_path / "mirror").resolve(),
        branch=package.branch, base_commit=package.base_commit, head_commit=package.head_commit,
    )
    snapshot = RepositorySnapshot(
        head_commit=package.head_commit, diff_sha256=package.diff_hash,
        changed_files=package.changed_files, patch="diff --git a/src/a.py b/src/a.py\n",
        is_clean=False,
    )
    run = WorkflowRun.new("requirement", "REQ-1").validated_update(
        requirement=_requirement(), repository=package.repository,
        prepared_worktree=prepared, tested_snapshot=snapshot,
        wiki_snapshots=package.wiki_snapshots,
        acceptance_coverage=(AcceptanceCoverage(
            criterion_id="AC-1", criterion_text="AC",
            files=("src/a.py",), tests=("pytest",),
        ),),
        test_results=package.tests, approval=package,
    )
    return run, Gateway(_requirement(), package.wiki_snapshots[0]), Repository(snapshot)


def test_production_rebuilder_reads_live_gateway_wiki_repo_and_persisted_tests(tmp_path) -> None:
    run, gateway, repository = _run(tmp_path)
    rebuilt = WorkflowApprovalRebuilder(gateway, repository).rebuild(run)
    assert rebuilt.tests == run.test_results
    assert rebuilt.wiki_snapshots == (gateway.wiki,)
    assert rebuilt.diff_hash == repository.current.diff_sha256
    assert repository.assertions == 3


def test_requirement_rebuilder_selects_only_successful_final_retry_round(tmp_path) -> None:
    run, gateway, repository = _run(tmp_path)
    failed = run.test_results[0].validated_update(
        exit_code=1, outcome="test_failed", summary="1 failed"
    )
    run = run.validated_update(test_results=(failed, *run.test_results))
    rebuilder = WorkflowApprovalRebuilder(gateway, repository)
    first = rebuilder.rebuild(run)
    signed = issue_approval(first, approved_by="alice")
    second = rebuilder.rebuild(run.validated_update(approval=signed))
    assert second.tests == (run.test_results[-1],)
    verify_approval(signed.fingerprint, second)


@pytest.mark.parametrize("changed", ["ones", "wiki", "diff", "tests"])
def test_live_evidence_change_invalidates_signed_fingerprint(tmp_path, changed) -> None:
    run, gateway, repository = _run(tmp_path)
    rebuilder = WorkflowApprovalRebuilder(gateway, repository)
    signed = issue_approval(rebuilder.rebuild(run), approved_by="alice")
    run = run.validated_update(approval=signed)
    if changed == "ones":
        gateway.requirement = replace(gateway.requirement, title="Changed")
    elif changed == "wiki":
        gateway.wiki = replace(
            gateway.wiki, normalized_content="changed",
            content_sha256=hashlib.sha256(b"changed").hexdigest(),
        )
    elif changed == "diff":
        repository.current = repository.current.validated_update(diff_sha256="e" * 64)
    else:
        changed_result = run.test_results[0].validated_update(summary="2 passed")
        run = run.validated_update(test_results=(changed_result,))
    expected = ApprovalRebuildError if changed == "diff" else ApprovalInvalidatedError
    with pytest.raises(expected):
        verify_approval(signed.fingerprint, rebuilder.rebuild(run))
