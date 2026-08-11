from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

import pytest

from src.developer_workflow.approval import approval_fingerprint
from src.developer_workflow.contracts import (
    ApprovalPackage,
    CommandOutcome,
    CommandResult,
    DefectCandidate,
    MultiRepositoryPublicationResult,
    PreparedWorktree,
    PublicationResult,
    RepositoryApprovalEvidence,
    RepositoryGroupMapping,
    RepositoryMapping,
    RepositoryPublicationResult,
    RepositoryRole,
    RepositoryRunEvidence,
    RepositorySnapshot,
    StateEvent,
    WorkflowRun,
    WorkflowState,
    WorkflowType,
)
from src.developer_workflow.tui.models import (
    DangerousActionRequest,
    DefectChoice,
    RunActivity,
    RunDetail,
    RunFilter,
    RunSummary,
    TuiDisplayError,
    safe_tui_text,
)


NOW = datetime(2026, 8, 11, 4, 0, tzinfo=UTC)
EMPTY_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
SENTINEL = "LEAK-ME-9f3d7e"


@pytest.mark.parametrize(
    "value",
    [
        "secret\nforged",
        "secret\x00forged",
        "secret\u202eforged",
        "secret\u200bforged",
        "secret\u2028forged",
        "secret\ud800forged",
        "secret" + "x" * 4096,
    ],
)
def test_safe_tui_text_rejects_unsafe_unicode_without_echoing_input(value: str) -> None:
    with pytest.raises(TuiDisplayError) as raised:
        safe_tui_text(value)

    assert str(raised.value) == "display value is invalid"
    assert "secret" not in str(raised.value)


def test_safe_tui_text_accepts_normal_chinese_and_emoji_and_controls_empty_policy() -> None:
    assert safe_tui_text("需求已完成 🚀") == "需求已完成 🚀"
    assert safe_tui_text("", allow_empty=True) == ""
    with pytest.raises(TuiDisplayError, match="^display value is invalid$"):
        safe_tui_text(123)  # type: ignore[arg-type]
    with pytest.raises(TuiDisplayError, match="^display value is invalid$"):
        safe_tui_text("")


def _mapping(
    key: str,
    *,
    role: RepositoryRole = RepositoryRole.PRIMARY,
    depends_on: tuple[str, ...] = (),
) -> RepositoryMapping:
    return RepositoryMapping(
        key=key,
        project_id="P",
        iteration_id="I",
        repo_url=f"https://git.example.invalid/team/{key}.git",
        repo_name=key,
        role=role,
        depends_on=depends_on,
        test_commands=("uv run pytest",),
        allowed_paths=("src", "tests"),
    )


def _worktree(tmp_path: Path, key: str, base: str, head: str) -> PreparedWorktree:
    return PreparedWorktree(
        path=(tmp_path / "worktrees" / key).resolve(),
        mirror_path=(tmp_path / "mirrors" / key).resolve(),
        branch=f"codex/{key}-change",
        base_commit=base,
        head_commit=head,
    )


def _test(command: str = "uv run pytest") -> CommandResult:
    return CommandResult(
        command=command,
        argv=tuple(command.split()),
        exit_code=0,
        summary=f"{SENTINEL} raw command output summary",
        started_at=NOW,
        finished_at=NOW,
        outcome=CommandOutcome.PASSED,
    )


def _dirty_snapshot(head: str, path: str) -> RepositorySnapshot:
    return RepositorySnapshot(
        head_commit=head,
        diff_sha256="d" * 64,
        changed_files=(path,),
        patch=f"{SENTINEL} private patch body",
        is_clean=False,
    )


def _publication(
    *,
    fingerprint: str,
    parent: str,
    tree: str,
    commit: str,
    pr_url: str,
    repository_key: str | None = None,
) -> PublicationResult | RepositoryPublicationResult:
    values = dict(
        approved_fingerprint=fingerprint,
        repo_url="https://git.example.invalid/team/repo.git",
        provider="github",
        provider_host="git.example.invalid",
        expected_parent=parent,
        expected_tree=tree,
        commit_message="feat: safe change",
        commit_hash=commit,
        remote_branch="codex/safe-change",
        push_completed_at=NOW,
        pr_marker="marker",
        pr_base="main",
        pr_head="codex/safe-change",
        pr_title="Safe change",
        pr_body="Safe body",
        pr_url=pr_url,
        comment_marker="comment-marker",
        error=f"{SENTINEL} provider exception",
    )
    if repository_key is None:
        return PublicationResult(**values)
    return RepositoryPublicationResult(repository_key=repository_key, **values)


def _single_run(tmp_path: Path) -> WorkflowRun:
    mapping = _mapping("repo")
    base, head, tree = "1" * 40, "2" * 40, "3" * 40
    approval = ApprovalPackage(
        work_item_id="REQ-中文-🚀",
        repository=mapping,
        repo_url=mapping.repo_url,
        base_branch="main",
        base_commit=base,
        head_commit=head,
        diff_hash="d" * 64,
        diff_summary="1 file changed",
        branch="codex/repo-change",
        changed_files=("src/app.py",),
        tests=(_test(),),
        review=(f"{SENTINEL} raw AI review",),
        risks=(f"{SENTINEL} risk detail",),
        unresolved_items=(f"{SENTINEL} unresolved detail",),
        unrelated_changes_checked=True,
        commit_message="feat: safe change",
        pr_title="Safe change",
        pr_body="Safe body",
    )
    fingerprint = approval_fingerprint(approval)
    approval = approval.validated_update(
        fingerprint=fingerprint, approved_by="alice", approved_at=NOW
    )
    return WorkflowRun.new("requirement", "REQ-中文-🚀").validated_update(
        state=WorkflowState.BLOCKED,
        version=7,
        updated_at=NOW,
        repository=mapping,
        prepared_worktree=_worktree(tmp_path, "repo", base, head),
        tested_snapshot=_dirty_snapshot(head, "src/app.py"),
        base_commit=base,
        head_commit=head,
        branch="codex/repo-change",
        changed_files=("src/app.py",),
        test_results=(_test(),),
        review={"summary": f"{SENTINEL} Codex review summary"},
        approval=approval,
        publication=_publication(
            fingerprint=fingerprint,
            parent=head,
            tree=tree,
            commit="4" * 40,
            pr_url="https://git.example.invalid/team/repo/pull/7?token=SECRET#fragment",
        ),
        blocked_reason=f"{SENTINEL} raw exception",
        error=f"{SENTINEL} environment body",
        history=(
            StateEvent(
                source=WorkflowState.CREATED,
                target=WorkflowState.BLOCKED,
                reason=f"{SENTINEL} private history reason",
                occurred_at=NOW,
            ),
        ),
    )


def test_run_summary_is_frozen_slotted_and_contains_only_whitelisted_fields(
    tmp_path: Path,
) -> None:
    run = _single_run(tmp_path)
    summary = RunSummary.from_run(run, activity=RunActivity.QUEUED)

    assert summary.run_id == run.run_id
    assert summary.workflow_type is WorkflowType.REQUIREMENT
    assert summary.work_item_id == "REQ-中文-🚀"
    assert summary.state is WorkflowState.BLOCKED
    assert summary.version == 7
    assert summary.updated_at == NOW
    assert summary.activity is RunActivity.QUEUED
    assert not summary.corrupted
    assert not hasattr(summary, "__dict__")
    for forbidden in ("requirement", "defect", "wiki", "codex", "patch", "approval"):
        assert not hasattr(summary, forbidden)
    with pytest.raises(FrozenInstanceError):
        summary.version = 8  # type: ignore[misc]


def test_corrupted_summary_is_fixed_and_contains_no_storage_content() -> None:
    summary = RunSummary.corrupted_entry("a" * 32)

    assert summary.corrupted
    assert summary.state is WorkflowState.BLOCKED
    assert summary.work_item_id == "storage-corrupted"
    assert summary.version == 0
    assert SENTINEL not in _all_strings(summary)


def test_single_repository_detail_maps_only_safe_facts(tmp_path: Path) -> None:
    run = _single_run(tmp_path)
    detail = RunDetail.from_run(run)

    assert len(detail.repositories) == 1
    repository = detail.repositories[0]
    assert (repository.key, repository.role) == ("repo", "primary")
    assert repository.base_commit == "1" * 40
    assert repository.head_commit == "2" * 40
    assert repository.tree_hash == "3" * 40
    assert repository.changed_files == ("src/app.py",)
    assert repository.changed_file_count == 1
    assert repository.commit_hash == "4" * 40
    assert repository.pushed
    assert repository.pr_url == "https://git.example.invalid/team/repo/pull/7"
    assert repository.error == "publication failed safely"
    assert [(item.command, item.outcome, item.exit_code) for item in detail.tests] == [
        ("test command", "passed", 0)
    ]
    assert detail.review == ("review recorded",)
    assert detail.publication.error == "publication failed safely"
    assert detail.history[0].source == "CREATED"
    assert detail.history[0].target == "BLOCKED"
    assert detail.history[0].occurred_at == NOW
    assert detail.blocked_reason == "workflow blocked safely"
    assert run.approval is not None
    assert detail.fingerprint == run.approval.fingerprint
    assert detail.risk_count == 1
    assert detail.unresolved_count == 1
    assert SENTINEL not in _all_strings(detail)
    for forbidden in ("requirement", "defect", "wiki", "codex", "patch"):
        assert not hasattr(detail, forbidden)


def _multi_run(tmp_path: Path) -> WorkflowRun:
    sdk = _mapping("sdk", role=RepositoryRole.DEPENDENCY)
    app = _mapping("app", depends_on=("sdk",))
    group = RepositoryGroupMapping(
        key="suite",
        project_id="P",
        iteration_id="I",
        primary_repository="app",
        repositories=(sdk, app),
        integration_test_commands=("uv run pytest tests/integration",),
    )
    bases = {"sdk": "1" * 40, "app": "2" * 40}
    heads = {"sdk": "3" * 40, "app": "4" * 40}
    paths = {"sdk": "src/sdk.py", "app": "src/app.py"}
    evidence = tuple(
        RepositoryRunEvidence(
            repository_key=item.key,
            mapping=item,
            prepared_worktree=_worktree(tmp_path, item.key, bases[item.key], heads[item.key]),
            tested_snapshot=_dirty_snapshot(heads[item.key], paths[item.key]),
            test_results=(_test(f"uv run pytest tests/{item.key}"),),
            changed_files=(paths[item.key],),
        )
        for item in group.repositories
    )
    approvals = tuple(
        RepositoryApprovalEvidence(
            repository_key=item.key,
            mapping=item,
            base_commit=bases[item.key],
            head_commit=heads[item.key],
            diff_hash="d" * 64,
            diff_summary="1 file changed",
            branch=f"codex/{item.key}-change",
            changed_files=(paths[item.key],),
            tests=(_test(f"uv run pytest tests/{item.key}"),),
            tree_hash=("6" if item.key == "sdk" else "7") * 40,
            commit_message="feat: safe change",
            pr_title="Safe change",
            pr_body="Safe body",
        )
        for item in group.repositories
    )
    approval = ApprovalPackage(
        work_item_id="REQ-2",
        repository_group=group,
        repositories=approvals,
        integration_tests=(_test("uv run pytest tests/integration"),),
        review=("safe result",),
        risks=("one", "two"),
        unresolved_items=(),
    )
    fingerprint = approval_fingerprint(approval)
    approval = approval.validated_update(
        fingerprint=fingerprint, approved_by="alice", approved_at=NOW
    )
    publications = tuple(
        _publication(
            repository_key=key,
            fingerprint=fingerprint,
            parent=heads[key],
            tree=("6" if key == "sdk" else "7") * 40,
            commit=("9" if key == "sdk" else "a") * 40,
            pr_url=f"https://git.example.invalid/team/{key}/pull/{1 if key == 'sdk' else 2}",
        )
        for key in group.topological_keys()
    )
    return WorkflowRun.new("requirement", "REQ-2").validated_update(
        repository_group=group,
        repository_evidence=evidence,
        integration_test_results=(_test("uv run pytest tests/integration"),),
        approval=approval,
        group_publication=MultiRepositoryPublicationResult(
            order=group.topological_keys(),
            repositories=publications,
            comment_marker="comment",
            comment_id="remote-comment-secret-id",
            error=f"{SENTINEL} aggregate provider error",
        ),
        history=(
            StateEvent(
                source=WorkflowState.TESTING,
                target=WorkflowState.WAITING_APPROVAL,
                reason=f"{SENTINEL} hidden reason",
                occurred_at=NOW,
            ),
        ),
        state=WorkflowState.WAITING_APPROVAL,
        updated_at=NOW,
    )


def test_multi_repository_detail_correlates_signed_and_publication_facts_by_key(
    tmp_path: Path,
) -> None:
    run = _multi_run(tmp_path)
    assert run.group_publication is not None
    positionally_mismatched = run.group_publication.model_construct(
        **{
            **run.group_publication.__dict__,
            "repositories": tuple(reversed(run.group_publication.repositories)),
        }
    )
    run = run.model_construct(
        **{**run.__dict__, "group_publication": positionally_mismatched}
    )
    detail = RunDetail.from_run(run)

    by_key = {item.key: item for item in detail.repositories}
    assert tuple(by_key) == ("sdk", "app")
    assert by_key["sdk"].role == "dependency"
    assert by_key["app"].role == "primary"
    assert by_key["sdk"].tree_hash == "6" * 40
    assert by_key["app"].tree_hash == "7" * 40
    assert by_key["sdk"].commit_hash == "9" * 40
    assert by_key["app"].commit_hash == "a" * 40
    assert by_key["sdk"].pr_url.endswith("/sdk/pull/1")
    assert by_key["app"].pr_url.endswith("/app/pull/2")
    assert len(detail.tests) == 3
    assert detail.tests[-1].command == "test command"
    assert detail.publication.comment_id == "delivered"
    assert detail.publication.error == "publication failed safely"
    assert detail.risk_count == 2
    assert SENTINEL not in _all_strings(detail)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://user:secret@git.example.invalid/team/repo/pull/1",
        "https://git.example.invalid/team/repo/pull/1\nforged",
    ],
)
def test_pr_url_rejects_unsafe_values_with_fixed_error(tmp_path: Path, url: str) -> None:
    run = _single_run(tmp_path)
    unsafe = run.publication.model_construct(**{**run.publication.__dict__, "pr_url": url})
    poisoned = run.model_construct(**{**run.__dict__, "publication": unsafe})

    with pytest.raises(TuiDisplayError) as raised:
        RunDetail.from_run(poisoned)

    assert str(raised.value) == "PR URL is invalid"
    assert "secret" not in str(raised.value)


def test_single_repository_uses_validated_snapshot_paths_not_unvalidated_run_paths(
    tmp_path: Path,
) -> None:
    run = _single_run(tmp_path).validated_update(
        changed_files=(f"C:/Users/alice/{SENTINEL}/source.py",)
    )

    with pytest.raises(TuiDisplayError) as raised:
        RunDetail.from_run(run)

    assert str(raised.value) == "workflow display facts are invalid"
    assert SENTINEL not in str(raised.value)


def test_test_view_never_displays_environment_or_credential_bearing_command(
    tmp_path: Path,
) -> None:
    result = _test(f"API_TOKEN={SENTINEL} uv run pytest tests/private.py")
    run = _single_run(tmp_path)
    assert run.approval is not None
    approval = run.approval.validated_update(
        tests=(result,), fingerprint="", approved_by=None, approved_at=None
    )
    approval = approval.validated_update(
        fingerprint=approval_fingerprint(approval), approved_by="alice", approved_at=NOW
    )
    publication = run.publication.validated_update(
        approved_fingerprint=approval.fingerprint
    )
    run = run.validated_update(
        test_results=(result,), approval=approval, publication=publication
    )

    detail = RunDetail.from_run(run)

    assert detail.tests[0].command == "test command"
    assert SENTINEL not in _all_strings(detail)


def test_unsigned_group_approval_cannot_authorize_publication_facts(
    tmp_path: Path,
) -> None:
    run = _multi_run(tmp_path)
    assert run.approval is not None
    unsigned = run.approval.validated_update(approved_by=None, approved_at=None)
    run = run.validated_update(approval=unsigned)

    detail = RunDetail.from_run(run)

    assert detail.fingerprint == unsigned.fingerprint
    assert all(repository.tree_hash == "" for repository in detail.repositories)
    assert all(repository.commit_hash == "" for repository in detail.repositories)
    assert all(not repository.pushed for repository in detail.repositories)
    assert all(repository.pr_url == "" for repository in detail.repositories)
    assert detail.publication.comment_id == ""
    assert detail.publication.error == ""


def test_noncanonical_approval_fingerprint_is_rejected_without_echo(
    tmp_path: Path,
) -> None:
    run = _single_run(tmp_path)
    assert run.approval is not None
    poisoned = run.validated_update(
        approval=run.approval.validated_update(
            fingerprint=f"credential-{SENTINEL}",
            approved_by=None,
            approved_at=None,
        )
    )

    with pytest.raises(TuiDisplayError) as raised:
        RunDetail.from_run(poisoned)

    assert str(raised.value) == "display value is invalid"
    assert SENTINEL not in str(raised.value)


def test_forged_group_approval_fingerprint_cannot_authorize_publication_facts(
    tmp_path: Path,
) -> None:
    run = _multi_run(tmp_path)
    assert run.approval is not None
    assert run.group_publication is not None
    forged_fingerprint = "f" * 64
    forged_approval = run.approval.validated_update(fingerprint=forged_fingerprint)
    forged_publications = tuple(
        item.validated_update(approved_fingerprint=forged_fingerprint)
        for item in run.group_publication.repositories
    )
    run = run.validated_update(
        approval=forged_approval,
        group_publication=run.group_publication.validated_update(
            repositories=forged_publications
        ),
    )

    with pytest.raises(TuiDisplayError, match="^workflow display facts are invalid$"):
        RunDetail.from_run(run)


@pytest.mark.parametrize("fact", ["mapping", "base", "head", "files", "tests"])
def test_signed_group_approval_rejects_mismatched_repository_evidence(
    tmp_path: Path, fact: str
) -> None:
    run = _multi_run(tmp_path)
    evidence = run.repository_evidence[0]
    updates: dict[str, object] = {}
    if fact == "mapping":
        updates["mapping"] = evidence.mapping.validated_update(
            repo_url="https://git.example.invalid/other/sdk.git"
        )
    elif fact == "base":
        updates["prepared_worktree"] = evidence.prepared_worktree.validated_update(
            base_commit="e" * 40
        )
    elif fact == "head":
        updates["prepared_worktree"] = evidence.prepared_worktree.validated_update(
            head_commit="e" * 40
        )
        assert evidence.tested_snapshot is not None
        updates["tested_snapshot"] = evidence.tested_snapshot.validated_update(
            head_commit="e" * 40
        )
    elif fact == "files":
        updates["changed_files"] = ("src/other.py",)
        assert evidence.tested_snapshot is not None
        updates["tested_snapshot"] = evidence.tested_snapshot.validated_update(
            changed_files=("src/other.py",)
        )
    else:
        updates["test_results"] = (
            evidence.test_results[0].validated_update(
                exit_code=1, outcome=CommandOutcome.TEST_FAILED
            ),
        )
    changed = evidence.validated_update(**updates)
    if fact == "mapping":
        run = run.model_construct(
            **{**run.__dict__, "repository_evidence": (changed, *run.repository_evidence[1:])}
        )
    else:
        run = run.validated_update(
            repository_evidence=(changed, *run.repository_evidence[1:])
        )

    with pytest.raises(TuiDisplayError, match="^workflow display facts are invalid$"):
        RunDetail.from_run(run)
    with pytest.raises(TuiDisplayError, match="^workflow display facts are invalid$"):
        DangerousActionRequest.from_run(run, action="resume-publication")


def test_signed_single_approval_rejects_mismatched_snapshot_and_tests(
    tmp_path: Path,
) -> None:
    run = _single_run(tmp_path)
    assert run.tested_snapshot is not None
    changed_snapshot = run.tested_snapshot.validated_update(
        changed_files=("src/other.py",)
    )
    changed_test = run.test_results[0].validated_update(
        exit_code=1, outcome=CommandOutcome.TEST_FAILED
    )
    run = run.validated_update(
        tested_snapshot=changed_snapshot,
        changed_files=changed_snapshot.changed_files,
        test_results=(changed_test,),
    )

    with pytest.raises(TuiDisplayError, match="^workflow display facts are invalid$"):
        RunDetail.from_run(run)
    with pytest.raises(TuiDisplayError, match="^workflow display facts are invalid$"):
        DangerousActionRequest.from_run(run, action="approve")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repo_url", "https://git.example.invalid/other/repo.git"),
        ("base_branch", "release"),
    ],
)
def test_signed_single_approval_binds_top_level_repository_identity(
    tmp_path: Path, field: str, value: str
) -> None:
    run = _single_run(tmp_path)
    assert run.approval is not None
    approval = run.approval.validated_update(
        **{
            field: value,
            "fingerprint": "",
            "approved_by": None,
            "approved_at": None,
        }
    )
    approval = approval.validated_update(
        fingerprint=approval_fingerprint(approval), approved_by="alice", approved_at=NOW
    )
    publication = run.publication.validated_update(
        approved_fingerprint=approval.fingerprint
    )
    run = run.validated_update(approval=approval, publication=publication)

    with pytest.raises(TuiDisplayError, match="^workflow display facts are invalid$"):
        RunDetail.from_run(run)


def test_group_publication_tree_must_match_signed_repository_tree(tmp_path: Path) -> None:
    run = _multi_run(tmp_path)
    assert run.group_publication is not None
    publications = list(run.group_publication.repositories)
    publications[0] = publications[0].validated_update(expected_tree="f" * 40)
    run = run.validated_update(
        group_publication=run.group_publication.validated_update(
            repositories=tuple(publications)
        )
    )

    with pytest.raises(TuiDisplayError, match="^workflow display facts are invalid$"):
        RunDetail.from_run(run)


@pytest.mark.parametrize("shape", ["empty", "partial", "wrong-key"])
def test_group_publication_requires_complete_changed_repository_key_set(
    tmp_path: Path, shape: str
) -> None:
    run = _multi_run(tmp_path)
    assert run.group_publication is not None
    publications = run.group_publication.repositories
    if shape == "empty":
        group_publication = run.group_publication.validated_update(repositories=())
        run = run.validated_update(group_publication=group_publication)
    elif shape == "partial":
        group_publication = run.group_publication.validated_update(
            repositories=publications[:1]
        )
        run = run.validated_update(group_publication=group_publication)
    else:
        wrong = publications[0].model_construct(
            **{**publications[0].__dict__, "repository_key": "wrong"}
        )
        group_publication = run.group_publication.model_construct(
            **{**run.group_publication.__dict__, "repositories": (wrong, publications[1])}
        )
        run = run.model_construct(
            **{**run.__dict__, "group_publication": group_publication}
        )

    with pytest.raises(TuiDisplayError, match="^workflow display facts are invalid$"):
        RunDetail.from_run(run)


def test_defect_choice_omits_snapshot_token() -> None:
    candidate = DefectCandidate(
        uuid="1" * 32,
        key="BUG-7",
        number="7",
        title="崩溃 💥",
        priority="P1",
        status="Open",
        status_id="open",
        updated_at="2026-08-11T04:00:00Z",
        snapshot_token=f"{SENTINEL}-snapshot-token",
    )

    choice = DefectChoice.from_candidate(candidate)

    assert choice.candidate_id == "1" * 32
    assert choice.title == "崩溃 💥"
    assert choice.status_id == "open"
    assert choice.priority == "P1"
    assert not hasattr(choice, "snapshot_token")
    assert SENTINEL not in _all_strings(choice)


def test_run_filter_matches_state_type_and_case_insensitive_query(tmp_path: Path) -> None:
    summary = RunSummary.from_run(_single_run(tmp_path), activity=RunActivity.IDLE)

    assert RunFilter().matches(summary)
    assert RunFilter(states=(WorkflowState.BLOCKED,)).matches(summary)
    assert not RunFilter(states=(WorkflowState.COMPLETED,)).matches(summary)
    assert RunFilter(workflow_types=(WorkflowType.REQUIREMENT,)).matches(summary)
    assert not RunFilter(workflow_types=(WorkflowType.DEFECT,)).matches(summary)
    assert RunFilter(query="req-中文").matches(summary)
    assert RunFilter(query=summary.run_id.upper()).matches(summary)
    assert not RunFilter(query="missing").matches(summary)


def test_dangerous_action_request_captures_confirmation_facts_and_rejects_stale(
    tmp_path: Path,
) -> None:
    run = _single_run(tmp_path)
    request = DangerousActionRequest.from_run(
        run, action="approve", expected_version=run.version
    )

    assert request.run_id == run.run_id
    assert request.version == 7
    assert request.action == "approve"
    assert request.fingerprint == run.approval.fingerprint
    assert request.work_item_id == "REQ-中文-🚀"
    assert request.repositories == RunDetail.from_run(run).repositories
    assert request.changed_file_count == 1
    assert request.test_count == 1
    assert request.risk_count == 1
    assert request.unresolved_count == 1

    with pytest.raises(TuiDisplayError, match="^workflow action is stale$"):
        DangerousActionRequest.from_run(
            run, action="approve", expected_version=run.version - 1
        )
    request.assert_current(run)
    with pytest.raises(TuiDisplayError, match="^workflow action is stale$"):
        request.assert_current(run.validated_update(version=run.version + 1))


def test_dangerous_action_request_captures_unsigned_package_fingerprint(
    tmp_path: Path,
) -> None:
    run = _single_run(tmp_path)
    assert run.approval is not None
    run = run.validated_update(
        approval=run.approval.validated_update(approved_by=None, approved_at=None)
    )

    request = DangerousActionRequest.from_run(run, action="approve")

    assert request.fingerprint == run.approval.fingerprint


def test_unsigned_package_allows_approve_but_rejects_resume_publication(
    tmp_path: Path,
) -> None:
    run = _single_run(tmp_path)
    assert run.approval is not None
    unsigned = run.approval.validated_update(
        fingerprint="", approved_by=None, approved_at=None
    )
    run = run.validated_update(approval=unsigned, publication=PublicationResult())

    request = DangerousActionRequest.from_run(run, action="approve")

    assert request.action == "approve"
    assert request.fingerprint == ""
    with pytest.raises(TuiDisplayError, match="^workflow action is unavailable$"):
        DangerousActionRequest.from_run(run, action="resume-publication")


def test_canonical_but_forged_fingerprint_rejects_request_and_stale_check(
    tmp_path: Path,
) -> None:
    run = _single_run(tmp_path)
    original = DangerousActionRequest.from_run(run, action="approve")
    assert run.approval is not None
    forged = run.validated_update(
        approval=run.approval.validated_update(fingerprint="f" * 64)
    )

    with pytest.raises(TuiDisplayError, match="^workflow display facts are invalid$"):
        DangerousActionRequest.from_run(forged, action="approve")
    with pytest.raises(TuiDisplayError, match="^workflow display facts are invalid$"):
        original.assert_current(forged)


@pytest.mark.parametrize("action", ["delete", "approve\nforged", "", "APPROVE"])
def test_dangerous_action_request_rejects_unknown_actions(
    tmp_path: Path, action: str
) -> None:
    with pytest.raises(TuiDisplayError, match="^workflow action is invalid$"):
        DangerousActionRequest.from_run(_single_run(tmp_path), action=action)


def test_dangerous_action_request_cannot_be_directly_constructed_with_unknown_action(
    tmp_path: Path,
) -> None:
    valid = DangerousActionRequest.from_run(_single_run(tmp_path), action="cancel")

    with pytest.raises(TuiDisplayError, match="^workflow action is invalid$"):
        DangerousActionRequest(
            run_id=valid.run_id,
            version=valid.version,
            action="delete",  # type: ignore[arg-type]
            fingerprint=valid.fingerprint,
            work_item_id=valid.work_item_id,
            repositories=valid.repositories,
            changed_file_count=valid.changed_file_count,
            test_count=valid.test_count,
            risk_count=valid.risk_count,
            unresolved_count=valid.unresolved_count,
        )


def _all_strings(value: object) -> tuple[str, ...]:
    found: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, Enum):
            visit(item.value)
        elif is_dataclass(item):
            for field in fields(item):
                visit(getattr(item, field.name))
        elif isinstance(item, (tuple, list)):
            for child in item:
                visit(child)

    visit(value)
    return tuple(found)
