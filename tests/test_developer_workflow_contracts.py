from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from dataclasses import replace
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.contracts import DefectRecord, RequirementRecord, WikiPageRef, WikiPageSnapshot
from src.developer_workflow.contracts import (
    ApprovalPackage,
    AcceptanceCoverage,
    CodexResult,
    CommandResult,
    PreparedWorktree,
    RepositoryMapping,
    RepositorySnapshot,
    WorkflowRun,
    WorkflowState,
    WorkflowType,
)
from src.developer_workflow.command_utils import display_argv, parse_command_argv


def test_workflow_state_contains_complete_persisted_vocabulary() -> None:
    assert {state.value for state in WorkflowState} == {
        "CREATED",
        "READING_ONES",
        "VALIDATING",
        "PREPARING_REPO",
        "IMPLEMENTING",
        "TESTING",
        "AI_REVIEW",
        "WAITING_APPROVAL",
        "PUBLISHING",
        "COMPLETED",
        "BLOCKED",
        "CANCELLED",
        "PARTIAL_SUCCESS",
        "FAILED",
    }
    assert {kind.value for kind in WorkflowType} == {"requirement", "defect"}


def test_ones_contracts_are_reexported_without_duplicate_models() -> None:
    from src.developer_workflow import contracts

    assert contracts.RequirementRecord is RequirementRecord
    assert contracts.DefectRecord is DefectRecord
    assert contracts.WikiPageRef is WikiPageRef
    assert contracts.WikiPageSnapshot is WikiPageSnapshot


def test_new_runs_have_isolated_mutable_defaults() -> None:
    first = WorkflowRun.new(WorkflowType.REQUIREMENT, "REQ-1")
    second = WorkflowRun.new("requirement", "REQ-2")
    defect = WorkflowRun.new_defect("project", "iteration", "owner", "DEF-1")

    first = first.validated_update(changed_files=("src/example.py",))

    assert first.run_id != second.run_id != defect.run_id
    assert first.changed_files == ("src/example.py",)
    assert second.changed_files == ()
    assert second.history == ()
    assert defect.history == ()
    assert defect.type is WorkflowType.DEFECT
    assert defect.workflow_type is WorkflowType.DEFECT
    assert defect.project_id == "project"
    assert defect.iteration_id == "iteration"
    assert defect.assignee_id == "owner"
    assert defect.candidate_id == "DEF-1"
    assert defect.work_item_id == "DEF-1"
    assert defect.state is WorkflowState.CREATED
    assert defect.version == 0
    assert re.fullmatch(r"[0-9a-f]{32}", first.run_id)
    assert re.fullmatch(r"[0-9a-f]{32}", defect.run_id)
    assert first.created_at.utcoffset() == timedelta(0)
    assert first.updated_at.utcoffset() == timedelta(0)
    assert defect.created_at.utcoffset() == timedelta(0)
    assert defect.updated_at.utcoffset() == timedelta(0)


def test_workflow_run_mutable_defaults_are_fully_isolated() -> None:
    first = WorkflowRun.new("requirement", "REQ-1")
    second = WorkflowRun.new("requirement", "REQ-2")

    first = first.validated_update(
        revisions=({"feedback": "change", "occurred_at": datetime.now(UTC)},),
        codex_results=({"summary": "done"},),
        test_results=(
            {
                "command": "pytest",
                "exit_code": 0,
                "summary": "passed",
                "started_at": datetime.now(UTC),
                "finished_at": datetime.now(UTC),
            },
        ),
    )

    assert len(first.revisions) == len(first.codex_results) == len(first.test_results) == 1
    assert second.history == ()
    assert second.revisions == ()
    assert second.codex_results == ()
    assert second.test_results == ()


def test_assignment_and_validated_copy_cannot_bypass_validation() -> None:
    run = WorkflowRun.new("requirement", "REQ-1")

    with pytest.raises(ValidationError):
        run.state = "NOT_A_STATE"
    with pytest.raises(ValidationError, match="timezone"):
        run.created_at = datetime(2026, 8, 10)
    with pytest.raises(ValidationError):
        run.model_copy(update={"state": "NOT_A_STATE"})
    with pytest.raises(ValidationError):
        run.validated_update(codex_results=({"unknown": True},))


def test_model_copy_keeps_deep_signature_and_validates_nested_elements() -> None:
    run = WorkflowRun.new("requirement", "REQ-1").validated_update(
        codex_results=({"summary": "ok", "changed_files": ("a.py",)},)
    )

    copied = run.model_copy(deep=True, update={"state": WorkflowState.READING_ONES})

    assert copied.state is WorkflowState.READING_ONES
    assert copied.codex_results == run.codex_results
    assert copied.codex_results is not run.codex_results


def test_revision_blocks_run_sets_resume_point_and_clears_approval() -> None:
    run = WorkflowRun.new("requirement", "REQ-1").model_copy(
        update={
            "state": WorkflowState.WAITING_APPROVAL,
            "approval": ApprovalPackage(
                work_item_id="REQ-1",
                fingerprint="old-fingerprint",
                approved_by="reviewer",
                approved_at=datetime.now(UTC),
            ),
        }
    )

    revised = run.for_revision("Please cover the error path")

    assert revised.state is WorkflowState.BLOCKED
    assert revised.resume_state is WorkflowState.IMPLEMENTING
    assert revised.approval is not None
    assert revised.approval.work_item_id == "REQ-1"
    assert revised.approval.approved_by is None
    assert revised.approval.approved_at is None
    assert revised.approval.fingerprint == ""
    assert revised.revisions[-1].feedback == "Please cover the error path"
    assert revised.revisions[-1].occurred_at.tzinfo is not None
    with pytest.raises(ValueError, match="feedback"):
        revised.for_revision("  ")


def test_approval_is_only_recorded_at_waiting_approval() -> None:
    package = ApprovalPackage(work_item_id="REQ-1", fingerprint="fingerprint")
    created = WorkflowRun.new("requirement", "REQ-1").model_copy(
        update={"approval": package}
    )

    with pytest.raises(ValueError, match="WAITING_APPROVAL"):
        created.with_approval("reviewer")

    waiting = created.model_copy(update={"state": WorkflowState.WAITING_APPROVAL})
    approved = waiting.with_approval("reviewer")

    assert approved.approval is not None
    assert approved.approval.approved_by == "reviewer"
    assert approved.approval.approved_at is not None
    assert approved.approval.approved_at.utcoffset() == timedelta(0)
    assert approved.publication.commit_hash == ""
    with pytest.raises(ValueError, match="approved_by"):
        waiting.with_approval(" ")


def test_approval_rejects_naive_timestamp() -> None:
    waiting = WorkflowRun.new("requirement", "REQ-1").model_copy(
        update={
            "state": WorkflowState.WAITING_APPROVAL,
            "approval": ApprovalPackage(work_item_id="REQ-1"),
        }
    )

    with pytest.raises(ValueError, match="timezone"):
        waiting.with_approval("reviewer", approved_at=datetime(2026, 8, 10))


def test_approval_normalizes_aware_timestamp_to_utc() -> None:
    waiting = WorkflowRun.new("requirement", "REQ-1").model_copy(
        update={
            "state": WorkflowState.WAITING_APPROVAL,
            "approval": ApprovalPackage(work_item_id="REQ-1"),
        }
    )

    approved = waiting.with_approval(
        "reviewer",
        approved_at=datetime(2026, 8, 10, 18, 30, tzinfo=timezone(timedelta(hours=8))),
    )

    assert approved.approval is not None
    assert approved.approval.approved_at == datetime(2026, 8, 10, 10, 30, tzinfo=UTC)
    assert approved.approval.approved_at.tzinfo is UTC


def test_approval_package_records_repository_and_fingerprint_inputs() -> None:
    repository = RepositoryMapping(
        key="repo",
        project_id="project",
        iteration_id="*",
        repo_url="https://example.invalid/repo.git",
        repo_name="repo",
    )

    package = ApprovalPackage(
        work_item_id="REQ-1",
        source_versions={"requirement": "7"},
        wiki_hashes={"PAGE-1": "abc123"},
        work_item_title="Feature",
        work_item_status="Open",
        repository=repository,
        base_commit="base-sha",
        head_commit="head-sha",
        diff_hash="diff-sha",
        branch="requirement/REQ-1-feature",
        manual_checks=["Review migration"],
        unrelated_changes_checked=True,
    )

    assert package.repository == repository
    assert package.base_commit == "base-sha"
    assert package.head_commit == "head-sha"
    assert package.diff_hash == "diff-sha"
    assert package.branch == "requirement/REQ-1-feature"
    assert package.manual_checks == ("Review migration",)
    assert package.unrelated_changes_checked is True


def test_approval_package_persists_full_wiki_snapshots_as_fingerprint_input() -> None:
    snapshot = WikiPageSnapshot(
        team_id="team",
        space_id="space",
        page_id="page",
        title="Design",
        version="7",
        updated_at="2026-08-10T10:00:00Z",
        normalized_content="# Design\n\nContent",
        content_sha256="abc123",
        source_url="https://ones.example/wiki/team/space/page",
    )
    package = ApprovalPackage(work_item_id="REQ-1", wiki_snapshots=[snapshot])

    restored = ApprovalPackage.model_validate_json(package.model_dump_json())

    assert restored == package
    assert isinstance(restored.wiki_snapshots[0], WikiPageSnapshot)

    baseline = package.model_dump(mode="json")
    for field, changed in (
        ("source_url", "https://ones.example/wiki/team/space/other"),
        ("updated_at", "2026-08-10T11:00:00Z"),
        ("version", "8"),
        ("content_sha256", "changed-hash"),
    ):
        changed_snapshot = replace(snapshot, **{field: changed})
        changed_package = package.model_copy(update={"wiki_snapshots": [changed_snapshot]})
        assert changed_package.model_dump(mode="json") != baseline


def test_approval_package_wiki_snapshot_defaults_are_isolated() -> None:
    first = ApprovalPackage(work_item_id="REQ-1")
    second = ApprovalPackage(work_item_id="REQ-2")

    first = first.validated_update(wiki_snapshots=(WikiPageSnapshot(page_id="page"),))

    assert first.wiki_snapshots == (WikiPageSnapshot(page_id="page"),)
    assert second.wiki_snapshots == ()


def test_workflow_run_json_round_trip_preserves_nested_snapshots() -> None:
    run = WorkflowRun.new("requirement", "REQ-1").model_copy(
        update={
            "requirement": RequirementRecord(requirement_id="REQ-1", title="Feature"),
            "base_commit": "base-sha",
            "head_commit": "head-sha",
            "wiki_snapshots": [
                WikiPageSnapshot(page_id="PAGE-1", content_sha256="abc123")
            ],
            "test_results": [
                CommandResult(
                    command="pytest",
                    exit_code=0,
                    summary="passed",
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                )
            ],
        }
    )

    restored = WorkflowRun.model_validate_json(run.model_dump_json())

    assert restored == run
    assert isinstance(restored.requirement, RequirementRecord)
    assert restored.defect is None
    assert isinstance(restored.wiki_snapshots[0], WikiPageSnapshot)
    assert restored.base_commit == "base-sha"
    assert restored.head_commit == "head-sha"
    assert restored.base_commit != restored.head_commit
    assert isinstance(restored.wiki_snapshots, tuple)
    assert isinstance(restored.test_results, tuple)

    defect_run = WorkflowRun.new_defect(
        "project", "sprint", "alice", "DEF-1"
    ).validated_update(defect=DefectRecord(defect_id="DEF-1", title="Bug"))
    restored_defect = WorkflowRun.model_validate_json(defect_run.model_dump_json())
    assert isinstance(restored_defect.defect, DefectRecord)
    assert restored_defect.requirement is None


def test_workflow_run_round_trips_repository_candidates_and_prepared_worktree(
    tmp_path: Path,
) -> None:
    mapping = RepositoryMapping(
        key="repo",
        project_id="project",
        iteration_id="sprint",
        repo_url=str((tmp_path / "remote.git").resolve()),
        repo_name="repo",
    )
    prepared = PreparedWorktree(
        path=(tmp_path / "tree").resolve(),
        branch="requirement/REQ-1-feature",
        base_commit="a" * 40,
        head_commit="a" * 40,
        mirror_path=(tmp_path / "mirror.git").resolve(),
    )
    run = WorkflowRun.new("requirement", "REQ-1").validated_update(
        repository_candidates=(mapping,),
        prepared_worktree=prepared,
    )

    restored = WorkflowRun.model_validate_json(run.model_dump_json())

    assert restored.repository_candidates == (mapping,)
    assert restored.prepared_worktree == prepared
    with pytest.raises(ValidationError):
        WorkflowRun.model_validate(
            {**run.model_dump(), "repository_candidates": [{"key": "bad"}]}
        )


def test_codex_acceptance_coverage_is_strict_and_old_results_keep_safe_defaults() -> None:
    coverage = AcceptanceCoverage(
        criterion_id="AC-1",
        criterion_text="export works",
        files=("src/export.py",),
        tests=("pytest -q",),
    )
    result = CodexResult(summary="done", acceptance_coverage=(coverage,))

    restored = CodexResult.model_validate_json(result.model_dump_json())

    assert restored.acceptance_coverage == (coverage,)
    assert restored.unrelated_changes_checked is False
    assert CodexResult.model_validate({"summary": "legacy"}).acceptance_coverage == ()
    with pytest.raises(ValidationError):
        AcceptanceCoverage(
            criterion_id="1",
            criterion_text="export works",
            files=("../escape.py",),
            tests=(),
        )


@pytest.mark.parametrize(
    "path",
    ("..\\tests\\test_export.py", "src\\export.py", "src/mixed\\export.py"),
)
def test_all_repository_relative_contracts_reject_backslash_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        RepositorySnapshot(
            head_commit="a" * 40,
            diff_sha256="b" * 64,
            changed_files=(path,),
            patch="diff",
            is_clean=False,
        )
    with pytest.raises(ValidationError):
        AcceptanceCoverage(
            criterion_id="AC-1",
            criterion_text="export works",
            files=(path,),
            tests=("uv run pytest",),
        )


def test_repository_relative_contracts_keep_legal_posix_paths() -> None:
    snapshot = RepositorySnapshot(
        head_commit="a" * 40,
        diff_sha256="b" * 64,
        changed_files=("tests/test_export.py",),
        patch="diff",
        is_clean=False,
    )
    assert snapshot.changed_files == ("tests/test_export.py",)


def test_repository_mapping_rejects_duplicate_commands_by_canonical_argv() -> None:
    with pytest.raises(ValidationError):
        RepositoryMapping(
            key="repo",
            project_id="project",
            iteration_id="*",
            repo_url="https://example.invalid/repo.git",
            repo_name="repo",
            lint_commands=('uv run pytest "tests/test one.py"',),
            test_commands=('uv run pytest "tests/test one.py"',),
        )


def test_shared_command_parser_preserves_quoted_argument_as_one_argv_token() -> None:
    argv = parse_command_argv('uv run pytest "tests/test quoted.py::test_case"')
    assert argv == ("uv", "run", "pytest", "tests/test quoted.py::test_case")
    assert parse_command_argv(display_argv(argv)) == argv


def test_workflow_run_round_trips_tested_snapshot_and_authoritative_coverage() -> None:
    snapshot = RepositorySnapshot(
        head_commit="a" * 40,
        diff_sha256="b" * 64,
        changed_files=("src/export.py",),
        patch="diff",
        is_clean=False,
    )
    coverage = AcceptanceCoverage(
        criterion_id="AC-1",
        criterion_text="export works",
        files=("src/export.py",),
        tests=("pytest -q",),
    )
    run = WorkflowRun.new("requirement", "REQ-1").validated_update(
        tested_snapshot=snapshot,
        acceptance_coverage=(coverage,),
    )

    restored = WorkflowRun.model_validate_json(run.model_dump_json())

    assert restored.tested_snapshot == snapshot
    assert restored.acceptance_coverage == (coverage,)


@pytest.mark.parametrize(
    "path",
    ["../outside", "src/../outside", "/absolute", "C:\\repo", "src\\nested"],
)
def test_repository_mapping_rejects_unsafe_allowed_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        RepositoryMapping(
            key="repo",
            project_id="project",
            iteration_id="*",
            repo_url="https://example.invalid/repo.git",
            repo_name="repo",
            allowed_paths=(path,),
        )


@pytest.mark.parametrize("command", ["", "   ", "pytest\x00--quiet"])
def test_repository_mapping_rejects_invalid_commands(command: str) -> None:
    with pytest.raises(ValidationError):
        RepositoryMapping(
            key="repo",
            project_id="project",
            iteration_id="*",
            repo_url="https://example.invalid/repo.git",
            repo_name="repo",
            test_commands=(command,),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("key", "repo\x00bad"),
        ("project_id", "project\x00bad"),
        ("iteration_id", "iteration\x00bad"),
        ("repo_url", "https://example.invalid/repo.git\x00bad"),
        ("repo_name", "repo\x00bad"),
        ("base_branch", "main\x00bad"),
    ],
)
def test_repository_mapping_rejects_nul_in_scalar_fields(field: str, value: str) -> None:
    values = {
        "key": "repo",
        "project_id": "project",
        "iteration_id": "*",
        "repo_url": "https://example.invalid/repo.git",
        "repo_name": "repo",
        "base_branch": "main",
    }
    values[field] = value
    with pytest.raises(ValidationError):
        RepositoryMapping(**values)


@pytest.mark.parametrize(
    "repo_url",
    [
        "http://example.invalid/repo.git",
        "https://example.invalid/repo.git",
        "ssh://git@example.invalid/team/repo.git",
        "git@example.invalid:team/repo.git",
        "C:\\repos\\repo",
        "/var/lib/repos/repo",
    ],
)
def test_repository_mapping_accepts_supported_repo_urls(repo_url: str) -> None:
    mapping = RepositoryMapping(
        key="repo",
        project_id="project",
        iteration_id="*",
        repo_url=repo_url,
        repo_name="repo",
    )

    assert mapping.repo_url == repo_url


@pytest.mark.parametrize(
    "repo_url",
    [
        "relative/repo",
        "ftp://example.invalid/repo.git",
        "https://user@example.invalid/repo.git",
        "https://user:password@example.invalid/repo.git",
        "ssh://git:password@example.invalid/repo.git",
    ],
)
def test_repository_mapping_rejects_unsupported_or_credential_urls(repo_url: str) -> None:
    with pytest.raises(ValidationError):
        RepositoryMapping(
            key="repo",
            project_id="project",
            iteration_id="*",
            repo_url=repo_url,
            repo_name="repo",
        )


@pytest.mark.parametrize(
    "field",
    ["key", "project_id", "iteration_id", "repo_url", "repo_name", "base_branch"],
)
@pytest.mark.parametrize("value", ["", "   "])
def test_repository_mapping_rejects_empty_core_scalars(field: str, value: str) -> None:
    values = {
        "key": "repo",
        "project_id": "project",
        "iteration_id": "*",
        "repo_url": "https://example.invalid/repo.git",
        "repo_name": "repo",
        "base_branch": "main",
    }
    values[field] = value
    with pytest.raises(ValidationError):
        RepositoryMapping(**values)


@pytest.mark.parametrize("command_field", ["test_commands", "lint_commands", "build_commands"])
@pytest.mark.parametrize("command", ["", "  ", "command\x00argument"])
def test_all_command_groups_reject_blank_or_nul(
    command_field: str, command: str
) -> None:
    values = {
        "key": "repo",
        "project_id": "project",
        "iteration_id": "*",
        "repo_url": "https://example.invalid/repo.git",
        "repo_name": "repo",
        command_field: (command,),
    }
    with pytest.raises(ValidationError):
        RepositoryMapping(**values)


@pytest.mark.parametrize(
    ("factory", "extra"),
    [
        (
            RepositoryMapping,
            {
                "key": "repo",
                "project_id": "project",
                "iteration_id": "*",
                "repo_url": "https://example.invalid/repo.git",
                "repo_name": "repo",
                "unknown": True,
            },
        ),
        (
            WorkflowRun,
            {
                "run_id": "0" * 32,
                "type": "requirement",
                "unknown": True,
            },
        ),
    ],
)
def test_contract_models_forbid_extra_fields(factory: object, extra: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        factory(**extra)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("key", "."),
        ("key", ".."),
        ("key", "team/repo"),
        ("key", "team\\repo"),
        ("key", "C:repo"),
        ("repo_name", "/absolute"),
        ("repo_name", "team/repo"),
        ("repo_name", "repo.lock"),
        ("repo_name", "repo\nname"),
    ],
)
def test_mapping_key_and_repo_name_are_safe_single_segments(
    field: str, value: str
) -> None:
    values = {
        "key": "repo-key",
        "project_id": "project",
        "iteration_id": "*",
        "repo_url": "https://example.invalid/repo.git",
        "repo_name": "repo.name",
    }
    values[field] = value
    with pytest.raises(ValidationError):
        RepositoryMapping(**values)


@pytest.mark.parametrize("path", [".", "./", "src/./code", "src:name", "src\nname"])
def test_allowed_paths_reject_dot_control_and_ads(path: str) -> None:
    with pytest.raises(ValidationError):
        RepositoryMapping(
            key="repo",
            project_id="project",
            iteration_id="*",
            repo_url="https://example.invalid/repo.git",
            repo_name="repo",
            allowed_paths=(path,),
        )


def test_allowed_paths_require_canonical_posix_separators() -> None:
    with pytest.raises(ValidationError):
        RepositoryMapping(
            key="repo",
            project_id="project",
            iteration_id="*",
            repo_url="https://example.invalid/repo.git",
            repo_name="repo",
            allowed_paths=("src\\feature",),
        )


def test_all_datetime_fields_reject_naive_and_normalize_offsets() -> None:
    plus_eight = timezone(timedelta(hours=8))
    with pytest.raises(ValidationError, match="timezone"):
        CommandResult(
            command="pytest",
            exit_code=0,
            summary="ok",
            started_at=datetime(2026, 8, 10),
            finished_at=datetime(2026, 8, 10, 1, tzinfo=UTC),
        )

    result = CommandResult(
        command="pytest",
        exit_code=0,
        summary="ok",
        started_at=datetime(2026, 8, 10, 18, tzinfo=plus_eight),
        finished_at=datetime(2026, 8, 10, 19, tzinfo=plus_eight),
    )
    restored = CommandResult.model_validate_json(result.model_dump_json())

    assert result.started_at == datetime(2026, 8, 10, 10, tzinfo=UTC)
    assert result.started_at.tzinfo is UTC
    assert restored.finished_at.tzinfo is UTC


def test_command_result_rejects_reverse_time_range() -> None:
    with pytest.raises(ValidationError, match="finished_at"):
        CommandResult(
            command="pytest",
            exit_code=0,
            summary="invalid",
            started_at=datetime(2026, 8, 10, 11, tzinfo=UTC),
            finished_at=datetime(2026, 8, 10, 10, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            CommandResult,
            '{"command":"pytest","exit_code":"0","summary":"ok",'
            '"started_at":"2026-08-10T10:00:00Z",'
            '"finished_at":"2026-08-10T10:00:01Z"}',
        ),
        (CodexResult, '{"unrelated_changes_checked":"false"}'),
        (
            RepositorySnapshot,
            '{"head_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            '"diff_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
            '"changed_files":["src/app.py"],"patch":"diff","is_clean":"false"}',
        ),
        (ApprovalPackage, '{"work_item_id":"REQ-1","unrelated_changes_checked":"false"}'),
        (
            WorkflowRun,
            '{"run_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","type":"requirement",'
            '"version":"1","retry_count":"0"}',
        ),
    ],
)
def test_safety_evidence_rejects_coercible_json_strings(
    model: type[object], payload: str
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate_json(payload)  # type: ignore[attr-defined]


def test_strict_evidence_types_keep_valid_json_roundtrip() -> None:
    result = CommandResult(
        command="pytest",
        exit_code=0,
        summary="ok",
        started_at=datetime(2026, 8, 10, 10, tzinfo=UTC),
        finished_at=datetime(2026, 8, 10, 10, 1, tzinfo=UTC),
    )
    codex = CodexResult(unrelated_changes_checked=True, commands=(result,))

    restored = CodexResult.model_validate_json(codex.model_dump_json())

    assert restored.commands[0].exit_code == 0
    assert restored.unrelated_changes_checked is True


@pytest.mark.parametrize(
    "branch",
    [
        " feature",
        "-feature",
        "feature\nnext",
        "feature..next",
        "feature@{next",
        "feature//next",
        "feature/.hidden",
        "feature/next.lock",
        "feature/next.",
        "feature/",
    ],
)
def test_repository_base_branch_rejects_unsafe_git_refs(branch: str) -> None:
    with pytest.raises(ValidationError):
        RepositoryMapping(
            key="repo",
            project_id="project",
            iteration_id="*",
            repo_url="https://example.invalid/repo.git",
            repo_name="repo",
            base_branch=branch,
        )


@pytest.mark.parametrize("branch", ["main", "feature/REQ-1_safe", "release/2026.08"])
def test_repository_base_branch_accepts_safe_git_refs(branch: str) -> None:
    mapping = RepositoryMapping(
        key="repo",
        project_id="project",
        iteration_id="*",
        repo_url="https://example.invalid/repo.git",
        repo_name="repo",
        base_branch=branch,
    )

    assert mapping.base_branch == branch
