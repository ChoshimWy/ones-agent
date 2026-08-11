from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from src.contracts import ProjectRef, RequirementRecord, StatusRef
from src.developer_workflow.approval import ApprovalInvalidatedError, issue_approval, verify_approval
from src.developer_workflow.approval_rebuilder import ApprovalRebuildError, WorkflowApprovalRebuilder
from src.developer_workflow.contracts import (
    AcceptanceCoverage, CodexResult, CommandOutcome, CommandResult,
    MultiRepositoryPublicationResult, PreparedWorktree,
    RepositoryApprovalEvidence, RepositoryChangeClaim, RepositoryGroupMapping,
    RepositoryMapping, RepositoryPublicationResult, RepositoryRole,
    RepositoryRunEvidence, RepositorySnapshot,
    WorkflowRun,
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


def test_group_rebuilder_checks_every_repository_and_recreates_same_fingerprint(
    tmp_path: Path,
) -> None:
    now = __import__("datetime").datetime.now(__import__("datetime").UTC)
    mappings = (
        RepositoryMapping(
            key="sdk", project_id="P", iteration_id="I",
            repo_url="https://example.invalid/sdk.git", repo_name="sdk",
            role=RepositoryRole.DEPENDENCY, test_commands=("pytest sdk",),
            allowed_paths=("src",),
        ),
        RepositoryMapping(
            key="app", project_id="P", iteration_id="I",
            repo_url="https://example.invalid/app.git", repo_name="app",
            role=RepositoryRole.PRIMARY, depends_on=("sdk",),
            test_commands=("pytest app",), allowed_paths=("src",),
        ),
    )
    group = RepositoryGroupMapping(
        key="suite", project_id="P", iteration_id="I",
        primary_repository="app", repositories=mappings,
        integration_test_commands=("pytest integration",),
    )
    results = {
        mapping.key: CommandResult(
            command=mapping.test_commands[0], argv=tuple(mapping.test_commands[0].split()),
            exit_code=0, outcome=CommandOutcome.PASSED, summary="passed",
            started_at=now, finished_at=now,
        ) for mapping in mappings
    }
    integration = CommandResult(
        command="pytest integration", argv=("pytest", "integration"), exit_code=0,
        outcome=CommandOutcome.PASSED, summary="passed", started_at=now, finished_at=now,
    )
    snapshots = {
        key: RepositorySnapshot(
            head_commit="a" * 40, diff_sha256=digest * 64,
            changed_files=(f"src/{key}.py",), patch=f"diff {key}", is_clean=False,
        ) for key, digest in (("sdk", "b"), ("app", "c"))
    }
    evidence = tuple(
        RepositoryRunEvidence(
            repository_key=mapping.key, mapping=mapping,
            prepared_worktree=PreparedWorktree(
                path=(tmp_path / mapping.key).resolve(),
                mirror_path=(tmp_path / f"{mapping.key}.git").resolve(),
                branch=f"codex/REQ-1-{mapping.key}", base_commit="a" * 40,
                head_commit="a" * 40,
            ),
            tested_snapshot=snapshots[mapping.key],
            test_results=(results[mapping.key],),
            changed_files=snapshots[mapping.key].changed_files,
        ) for mapping in mappings
    )
    repositories = tuple(
        RepositoryApprovalEvidence(
            repository_key=item.repository_key, mapping=item.mapping,
            base_commit=item.prepared_worktree.base_commit,
            head_commit=item.prepared_worktree.head_commit,
            diff_hash=item.tested_snapshot.diff_sha256,
            diff_summary=f"changed 1 file(s): src/{item.repository_key}.py",
            branch=item.prepared_worktree.branch,
            changed_files=item.changed_files, tests=item.test_results,
            tree_hash=("d" if item.repository_key == "sdk" else "e") * 40,
            commit_message=f"feat({item.repository_key}): Title",
            pr_title=f"REQ-1: Title [{item.repository_key}]",
            pr_body=f"Repository: {item.repository_key}",
        ) for item in evidence
    )
    package = _package().model_copy(update={
        "repository": None, "repo_url": "", "base_branch": "", "base_commit": "",
        "head_commit": "", "diff_hash": "", "diff_summary": "", "branch": "",
        "changed_files": (), "tests": (), "commit_message": "", "pr_title": "",
        "pr_body": "", "repository_group": group, "repositories": repositories,
        "integration_tests": (integration,),
        "coverage": {"AC-1: AC": "files=sdk:src/sdk.py; tests=pytest sdk"},
    })
    run = WorkflowRun.new("requirement", "REQ-1").validated_update(
        requirement=_requirement(), repository_group=group,
        repository_evidence=evidence, integration_test_results=(integration,),
        wiki_snapshots=package.wiki_snapshots,
        acceptance_coverage=(AcceptanceCoverage(
            criterion_id="AC-1", criterion_text="AC",
            repository_files=(RepositoryChangeClaim(
                repository_key="sdk", path="src/sdk.py"
            ),), tests=("pytest sdk",),
        ),), review=CodexResult(
            summary="reviewed", review_findings=("reviewed",),
            repository_changes=(
                RepositoryChangeClaim(repository_key="sdk", path="src/sdk.py"),
                RepositoryChangeClaim(repository_key="app", path="src/app.py"),
            ), unrelated_changes_checked=True,
        ), approval=package,
    )

    class GroupRepository(Repository):
        reject_head: bool = False

        def assert_head_unchanged(self, prepared):
            if self.reject_head:
                raise RuntimeError("publication advanced HEAD")
            super().assert_head_unchanged(prepared)

        def snapshot(self, prepared, mapping):
            return snapshots[mapping.key]

    repository = GroupRepository(snapshots["app"])
    gateway = Gateway(_requirement(), package.wiki_snapshots[0])
    rebuilt = WorkflowApprovalRebuilder(gateway, repository).rebuild(run)
    signed = issue_approval(rebuilt, approved_by="alice")
    verify_approval(
        signed.fingerprint,
        WorkflowApprovalRebuilder(gateway, repository).rebuild(
            run.validated_update(approval=rebuilt)
        ),
    )
    marker = f"<!-- ones-dev-run:{run.run_id} -->"
    publication = MultiRepositoryPublicationResult(
        order=group.topological_keys(), comment_marker=marker,
        repositories=tuple(
            RepositoryPublicationResult(
                repository_key=item.repository_key,
                approved_fingerprint=signed.fingerprint,
                repo_url=item.mapping.repo_url, provider="github",
                provider_host="example.invalid", expected_parent=item.head_commit,
                expected_tree=("d" if item.repository_key == "sdk" else "e") * 40,
                commit_message=item.commit_message, remote_branch=item.branch,
                pr_marker=f"ones-dev-run:{run.run_id}:{item.repository_key}",
                pr_base=item.mapping.base_branch, pr_head=item.branch,
                pr_title=item.pr_title, pr_body=item.pr_body, comment_marker=marker,
                commit_hash=("1" if item.repository_key == "sdk" else "2") * 40,
            ) for item in signed.repositories
        ),
    )
    repository.reject_head = True
    recovered = WorkflowApprovalRebuilder(gateway, repository).rebuild(
        run.validated_update(approval=signed, group_publication=publication)
    )
    verify_approval(signed.fingerprint, recovered)
    repository.reject_head = False
    snapshots["sdk"] = snapshots["sdk"].validated_update(diff_sha256="d" * 64)
    with pytest.raises(ApprovalRebuildError, match="tested evidence"):
        WorkflowApprovalRebuilder(gateway, repository).rebuild(run)
