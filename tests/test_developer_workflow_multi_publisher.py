from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.contracts import WikiPageSnapshot
from src.developer_workflow.approval import issue_approval
from src.developer_workflow.contracts import (
    ApprovalPackage, CommandOutcome, CommandResult, MultiRepositoryPublicationResult,
    PreparedWorktree, RepositoryPublicationResult,
    RepositoryApprovalEvidence, RepositoryGroupMapping, RepositoryMapping,
    RepositoryRole, RepositoryRunEvidence, RepositorySnapshot, WorkflowRun,
    WorkflowState,
)
from src.developer_workflow.publisher import Publisher
from src.developer_workflow.state_store import FileRunStore, InvalidRunMutationError


def _approved_group(tmp_path: Path) -> tuple[ApprovalPackage, tuple[RepositoryRunEvidence, ...]]:
    now = datetime(2026, 8, 11, tzinfo=UTC)
    mappings = (
        RepositoryMapping(
            key="sdk", project_id="P", iteration_id="I",
            repo_url="https://github.example/team/sdk.git", repo_name="sdk",
            role=RepositoryRole.DEPENDENCY, test_commands=("pytest sdk",),
            allowed_paths=("src",),
        ),
        RepositoryMapping(
            key="app", project_id="P", iteration_id="I",
            repo_url="https://github.example/team/app.git", repo_name="app",
            role=RepositoryRole.PRIMARY, depends_on=("sdk",),
            test_commands=("pytest app",), allowed_paths=("src",),
        ),
    )
    group = RepositoryGroupMapping(
        key="suite", project_id="P", iteration_id="I",
        primary_repository="app", repositories=mappings,
    )
    evidence: list[RepositoryRunEvidence] = []
    approvals: list[RepositoryApprovalEvidence] = []
    for index, mapping in enumerate(mappings):
        result = CommandResult(
            command=mapping.test_commands[0], argv=tuple(mapping.test_commands[0].split()),
            exit_code=0, outcome=CommandOutcome.PASSED, summary="passed",
            started_at=now, finished_at=now,
        )
        snapshot = RepositorySnapshot(
            head_commit="a" * 40, diff_sha256=("b" if index == 0 else "c") * 64,
            changed_files=(f"src/{mapping.key}.py",), patch=f"diff {mapping.key}",
            is_clean=False,
        )
        prepared = PreparedWorktree(
            path=(tmp_path / mapping.key).resolve(),
            mirror_path=(tmp_path / f"{mapping.key}.git").resolve(),
            branch=f"codex/REQ-1-{mapping.key}", base_commit="a" * 40,
            head_commit="a" * 40,
        )
        evidence.append(RepositoryRunEvidence(
            repository_key=mapping.key, mapping=mapping, prepared_worktree=prepared,
            tested_snapshot=snapshot, test_results=(result,),
            changed_files=snapshot.changed_files,
        ))
        approvals.append(RepositoryApprovalEvidence(
            repository_key=mapping.key, mapping=mapping, base_commit="a" * 40,
            head_commit="a" * 40, diff_hash=snapshot.diff_sha256,
            diff_summary=f"changed 1 file(s): src/{mapping.key}.py",
            branch=prepared.branch, changed_files=snapshot.changed_files,
            tests=(result,), tree_hash=("d" if mapping.key == "sdk" else "e") * 40,
            commit_message=f"feat({mapping.key}): title",
            pr_title=f"REQ-1 [{mapping.key}]", pr_body=f"Repository {mapping.key}",
        ))
    content = "AC"
    wiki = WikiPageSnapshot(
        team_id="T", space_id="S", page_id="page", title="Doc", version="1",
        updated_at="2026-08-11T00:00:00Z", normalized_content=content,
        content_sha256=__import__("hashlib").sha256(content.encode()).hexdigest(),
        source_url="http://ones/wiki",
    )
    package = ApprovalPackage(
        work_item_id="REQ-1", work_item_title="Title", work_item_status="Doing",
        source_versions={"version": "1"}, wiki_hashes={"page": wiki.content_sha256},
        wiki_snapshots=(wiki,), repository_group=group,
        repositories=tuple(approvals), coverage={"AC-1": "covered"},
        evidence=("verified",), review=("reviewed",), risks=("low",),
        unrelated_changes_checked=True,
    )
    return issue_approval(package, approved_by="alice"), tuple(evidence)


@dataclass
class GroupRepository:
    calls: list[tuple[str, str]] = field(default_factory=list)
    commits: dict[str, str] = field(default_factory=dict)
    remotes: dict[str, str] = field(default_factory=dict)
    base_checks: list[str] = field(default_factory=list)
    fail_base_for: str | None = None

    def _key(self, run: WorkflowRun) -> str:
        assert run.repository is not None
        return run.repository.key

    def prepare_commit_intent(self, run, approval):
        key = self._key(run); self.calls.append(("prepare", key)); return ("d" if key == "sdk" else "e") * 40
    def find_approved_commit(self, run):
        key = self._key(run); self.calls.append(("find", key)); return self.commits.get(key)
    def commit_approved(self, run):
        key = self._key(run); self.calls.append(("commit", key)); self.commits[key] = ("1" if key == "sdk" else "2") * 40; return self.commits[key]
    def remote_branch_oid(self, run):
        key = self._key(run); self.calls.append(("remote", key)); return self.remotes.get(key)
    def push_approved(self, run):
        key = self._key(run); self.calls.append(("push", key)); self.remotes[key] = run.publication.commit_hash
    def assert_remote_base_unchanged(self, prepared, mapping):
        self.base_checks.append(mapping.key)
        if self.fail_base_for == mapping.key:
            raise RuntimeError("remote base moved")


@dataclass
class GroupPR:
    fail_once: str | None = "app"
    urls: dict[str, str] = field(default_factory=dict)
    created: list[str] = field(default_factory=list)

    @staticmethod
    def _key(repo_url: str) -> str:
        return repo_url.rsplit("/", 1)[-1].removesuffix(".git")
    def find(self, *, repo_url, **kwargs): return self.urls.get(self._key(repo_url))
    def create(self, *, repo_url, **kwargs):
        key = self._key(repo_url)
        if self.fail_once == key:
            self.fail_once = None
            raise RuntimeError("uncertain")
        self.created.append(key)
        self.urls[key] = f"https://github.example/team/{key}/pull/1"
        return self.urls[key]


@dataclass
class GroupCommenter:
    calls: int = 0
    def ensure_comment(self, run): self.calls += 1; return "comment-1"


def test_group_publication_resumes_only_unfinished_repository(tmp_path: Path) -> None:
    approval, evidence = _approved_group(tmp_path)
    store = FileRunStore(tmp_path / "runs")
    run = store.create(WorkflowRun.new("requirement", "REQ-1"))
    for state in (
        WorkflowState.READING_ONES, WorkflowState.VALIDATING,
        WorkflowState.PREPARING_REPO, WorkflowState.IMPLEMENTING,
        WorkflowState.TESTING, WorkflowState.AI_REVIEW,
    ):
        run = store.transition(run.run_id, run.version, state, state.value)
    run = store.save(run.validated_update(
        repository_model_version=2, repository_group=approval.repository_group,
        repository_evidence=evidence, approval=approval,
    ), run.version)
    run = store.transition(run.run_id, run.version, WorkflowState.WAITING_APPROVAL, "wait")
    repository, pr, commenter = GroupRepository(), GroupPR(), GroupCommenter()
    publisher = Publisher(
        store=store, repository=repository,
        approval_rebuilder=lambda current: approval.model_copy(
            update={"fingerprint": "", "approved_by": None, "approved_at": None}
        ),
        pr_client=pr, commenter=commenter,
        provider="github", provider_host="github.example",
    )

    partial = publisher.publish(run)
    assert partial.state is WorkflowState.PARTIAL_SUCCESS
    assert pr.created == ["sdk"]
    assert commenter.calls == 0
    assert partial.group_publication is not None
    partial_by_key = {
        item.repository_key: item for item in partial.group_publication.repositories
    }
    assert partial_by_key["app"].error == "PR creation outcome is uncertain"
    assert partial.group_publication.error == "PR creation outcome is uncertain"

    completed = publisher.publish(partial)
    assert completed.state is WorkflowState.COMPLETED
    assert [key for operation, key in repository.calls if operation == "commit"] == ["sdk", "app"]
    assert [key for operation, key in repository.calls if operation == "push"] == ["sdk", "app"]
    assert pr.created == ["sdk", "app"]
    assert commenter.calls == 1
    assert repository.base_checks == ["sdk", "app", "sdk", "app"]
    assert completed.group_publication is not None
    assert not next(
        item for item in completed.group_publication.repositories
        if item.repository_key == "app"
    ).error


def _waiting_group(
    tmp_path: Path,
) -> tuple[FileRunStore, WorkflowRun, ApprovalPackage, tuple[RepositoryRunEvidence, ...]]:
    approval, evidence = _approved_group(tmp_path)
    store = FileRunStore(tmp_path / "runs")
    run = store.create(WorkflowRun.new("requirement", "REQ-1"))
    for state in (
        WorkflowState.READING_ONES, WorkflowState.VALIDATING,
        WorkflowState.PREPARING_REPO, WorkflowState.IMPLEMENTING,
        WorkflowState.TESTING, WorkflowState.AI_REVIEW,
    ):
        run = store.transition(run.run_id, run.version, state, state.value)
    run = store.save(run.validated_update(
        repository_model_version=2, repository_group=approval.repository_group,
        repository_evidence=evidence, approval=approval,
    ), run.version)
    return (
        store,
        store.transition(run.run_id, run.version, WorkflowState.WAITING_APPROVAL, "wait"),
        approval,
        evidence,
    )


def _initial_group_intent(run: WorkflowRun, approval: ApprovalPackage):
    items = tuple(
        RepositoryPublicationResult(
            repository_key=item.repository_key,
            approved_fingerprint=approval.fingerprint,
            repo_url=item.mapping.repo_url,
            provider="github", provider_host="github.example",
            expected_parent=item.head_commit, expected_tree=("d" if index == 0 else "e") * 40,
            commit_message=item.commit_message, remote_branch=item.branch,
            pr_marker=f"ones-dev-run:{run.run_id}:{item.repository_key}",
            pr_base=item.mapping.base_branch, pr_head=item.branch,
            pr_title=item.pr_title, pr_body=item.pr_body,
            comment_marker=f"<!-- ones-dev-run:{run.run_id} -->",
        )
        for index, item in enumerate(approval.repositories)
    )
    return MultiRepositoryPublicationResult(
        order=approval.repository_group.topological_keys(),
        repositories=items,
        comment_marker=f"<!-- ones-dev-run:{run.run_id} -->",
    )


@pytest.mark.parametrize("mutation", ["omit", "title", "tree", "facts"])
def test_store_rejects_forged_initial_group_publication(
    tmp_path: Path, mutation: str,
) -> None:
    store, run, approval, _ = _waiting_group(tmp_path)
    run = store.transition(run.run_id, run.version, WorkflowState.PUBLISHING, "publish")
    publication = _initial_group_intent(run, approval)
    if mutation == "omit":
        publication = publication.validated_update(repositories=publication.repositories[:1])
    elif mutation == "title":
        changed = publication.repositories[0].validated_update(pr_title="unapproved")
        publication = publication.validated_update(
            repositories=(changed, *publication.repositories[1:])
        )
    elif mutation == "tree":
        changed = publication.repositories[0].validated_update(expected_tree="9" * 40)
        publication = publication.validated_update(
            repositories=(changed, *publication.repositories[1:])
        )
    else:
        changed = publication.repositories[0].validated_update(
            commit_hash="1" * 40, push_completed_at=datetime.now(UTC),
            pr_url="https://github.example/team/sdk/pull/forged",
        )
        publication = publication.validated_update(
            repositories=(changed, *publication.repositories[1:])
        )
    with pytest.raises(InvalidRunMutationError):
        store.save(run.validated_update(group_publication=publication), run.version)


def test_group_resume_revalidates_approval_and_persisted_remote_facts(tmp_path: Path) -> None:
    store, run, approval, _ = _waiting_group(tmp_path)
    repository, pr = GroupRepository(), GroupPR()
    publisher = Publisher(
        store=store, repository=repository,
        approval_rebuilder=lambda current: approval.model_copy(
            update={"fingerprint": "", "approved_by": None, "approved_at": None}
        ),
        pr_client=pr, commenter=GroupCommenter(),
        provider="github", provider_host="github.example",
    )
    partial = publisher.publish(run)
    assert partial.state is WorkflowState.PARTIAL_SUCCESS
    repository.remotes["sdk"] = "9" * 40
    resumed = publisher.publish(partial)
    assert resumed.state is WorkflowState.PARTIAL_SUCCESS
    assert pr.created == ["sdk"]


@pytest.mark.parametrize("drift", ["pr", "provider", "approval"])
def test_group_resume_rejects_persisted_state_drift(
    tmp_path: Path, drift: str,
) -> None:
    store, run, approval, _ = _waiting_group(tmp_path)
    repository, pr = GroupRepository(), GroupPR()
    calls = 0

    def rebuild(current):
        nonlocal calls
        calls += 1
        package = approval.model_copy(
            update={"fingerprint": "", "approved_by": None, "approved_at": None}
        )
        if drift == "approval" and calls > 1:
            repositories = (
                package.repositories[0].validated_update(pr_title="drifted"),
                *package.repositories[1:],
            )
            package = package.validated_update(repositories=repositories)
        return package

    publisher = Publisher(
        store=store, repository=repository,
        approval_rebuilder=rebuild,
        pr_client=pr, commenter=GroupCommenter(),
        provider="github", provider_host="github.example",
    )
    partial = publisher.publish(run)
    if drift == "pr":
        del pr.urls["sdk"]
    elif drift == "provider":
        publisher.provider_host = "gitlab.example"
    resumed = publisher.publish(partial)
    assert resumed.state is WorkflowState.PARTIAL_SUCCESS
    assert pr.created == ["sdk"]


def test_group_checks_every_remote_base_after_commits_before_push(tmp_path: Path) -> None:
    store, run, approval, _ = _waiting_group(tmp_path)
    repository = GroupRepository(fail_base_for="app")
    publisher = Publisher(
        store=store, repository=repository,
        approval_rebuilder=lambda current: approval.model_copy(
            update={"fingerprint": "", "approved_by": None, "approved_at": None}
        ),
        pr_client=GroupPR(fail_once=None), commenter=GroupCommenter(),
        provider="github", provider_host="github.example",
    )
    partial = publisher.publish(run)
    assert partial.state is WorkflowState.PARTIAL_SUCCESS
    assert [call for call in repository.calls if call[0] == "commit"] == [
        ("commit", "sdk"), ("commit", "app")
    ]
    assert not [call for call in repository.calls if call[0] == "push"]
