from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
import math

import pytest

from src.contracts import WikiPageSnapshot
from src.developer_workflow.approval import (
    ApprovalInvalidatedError,
    ApprovalValidationError,
    approval_fingerprint,
    issue_approval,
    validate_for_approval,
    verify_approval,
)
from src.developer_workflow.contracts import (
    ApprovalPackage,
    CommandResult,
    CommandOutcome,
    RepositoryMapping,
    RootCauseEvidence,
    RootCauseSupportingPoint,
)


def _package() -> ApprovalPackage:
    timestamp = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
    repository = RepositoryMapping(
        key="repo",
        project_id="PROJECT-1",
        iteration_id="ITERATION-1",
        repo_url="ssh://git@example.invalid/team/repo.git",
        repo_name="repo",
        base_branch="main",
        test_commands=("uv run pytest",),
        allowed_paths=("src", "tests"),
    )
    return ApprovalPackage(
        work_item_id="REQ-1",
        work_item_title="Add export",
        work_item_status="In progress",
        source_versions={"requirement_version": "7", "content_sha256": "a" * 64},
        wiki_hashes={"PAGE-1": "b" * 64},
        wiki_snapshots=(
            WikiPageSnapshot(
                team_id="TEAM-1",
                space_id="SPACE-1",
                page_id="PAGE-1",
                title="Export design",
                version="3",
                updated_at="2026-08-10T09:00:00Z",
                normalized_content="# Acceptance Criteria\n- CSV is downloadable",
                content_sha256="b" * 64,
                source_url="http://ones.local/wiki/team/TEAM-1/space/SPACE-1/page/PAGE-1",
            ),
        ),
        repository=repository,
        repo_url="ssh://git@example.invalid/team/repo.git",
        base_branch="main",
        base_commit="c" * 40,
        head_commit="d" * 40,
        diff_hash="e" * 64,
        diff_summary="2 files changed",
        branch="requirement/REQ-1-export",
        changed_files=("src/export.py", "tests/test_export.py"),
        coverage={"CSV is downloadable": "tests/test_export.py::test_download"},
        evidence=("src/export.py:20",),
        tests=(
            CommandResult(
                        command="uv run pytest tests/test_export.py",
                        argv=("uv", "run", "pytest", "tests/test_export.py"),
                exit_code=0,
                summary="1 passed",
                started_at=timestamp,
                finished_at=timestamp,
            ),
        ),
        review=("No blocking findings",),
        risks=("Large exports use memory",),
        unresolved_items=(),
        manual_checks=("Download in browser",),
        unrelated_changes_checked=True,
        commit_message="feat: export CSV",
        pr_title="feat: export CSV",
        pr_body="Implements REQ-1",
    )


def _changed(package: ApprovalPackage, field: str, value: object) -> ApprovalPackage:
    return package.model_copy(update={field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("work_item_id", "REQ-2"),
        ("work_item_title", "Changed requirement"),
        ("work_item_status", "Done"),
        ("source_versions", {"requirement_version": "8", "content_sha256": "f" * 64}),
        ("base_commit", "1" * 40),
        ("head_commit", "2" * 40),
        ("diff_hash", "3" * 64),
        ("diff_summary", "3 files changed"),
        ("changed_files", ("src/export.py",)),
        ("coverage", {"CSV is downloadable": "missing"}),
        ("evidence", ("src/export.py:21",)),
        ("review", ("Potential regression",)),
        ("risks", ("New risk",)),
        ("unresolved_items", ("Clarify format",)),
        ("commit_message", "fix: export CSV"),
        ("pr_title", "fix: export CSV"),
        ("pr_body", "Changed publication body"),
        ("repo_url", "ssh://git@example.invalid/team/other.git"),
        ("base_branch", "release"),
        ("branch", "requirement/REQ-1-export-v2"),
    ],
)
def test_each_approval_input_change_invalidates(
    field: str, value: object
) -> None:
    package = _package()
    expected = approval_fingerprint(package)

    with pytest.raises(ApprovalInvalidatedError):
        verify_approval(expected, _changed(package, field, value))


@pytest.mark.parametrize("field", ["command", "exit_code", "summary"])
def test_each_test_result_change_invalidates(field: str) -> None:
    package = _package()
    result = package.tests[0]
    values = {"command": "pytest -q", "exit_code": 1, "summary": "failed"}
    update = {field: values[field]}
    if field == "exit_code":
        update["outcome"] = CommandOutcome.COMMAND_ERROR
    changed = result.model_copy(update=update)

    with pytest.raises(ApprovalInvalidatedError):
        verify_approval(
            approval_fingerprint(package),
            package.model_copy(update={"tests": (changed,)}),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_url", "http://ones.local/wiki/other"),
        ("version", "4"),
        ("updated_at", "2026-08-10T10:00:00Z"),
        ("content_sha256", "f" * 64),
        ("normalized_content", "changed content"),
    ],
)
def test_each_wiki_source_change_invalidates(field: str, value: str) -> None:
    package = _package()
    snapshot = replace(package.wiki_snapshots[0], **{field: value})

    with pytest.raises(ApprovalInvalidatedError):
        verify_approval(
            approval_fingerprint(package),
            package.model_copy(update={"wiki_snapshots": (snapshot,)}),
        )


def test_mapping_insertion_order_does_not_change_fingerprint() -> None:
    package = _package()
    reordered = package.model_copy(
        update={
            "source_versions": dict(reversed(tuple(package.source_versions.items()))),
            "coverage": dict(reversed(tuple(package.coverage.items()))),
        }
    )

    assert approval_fingerprint(reordered) == approval_fingerprint(package)


def test_semantically_ordered_sequences_keep_their_order() -> None:
    package = _package().model_copy(update={"risks": ("risk-a", "risk-b")})
    reordered = package.model_copy(update={"risks": ("risk-b", "risk-a")})

    assert approval_fingerprint(reordered) != approval_fingerprint(package)


def test_approval_metadata_is_not_part_of_fingerprint() -> None:
    package = _package()
    changed = package.model_copy(
        update={
            "fingerprint": "0" * 64,
            "approved_by": "reviewer",
            "approved_at": datetime(2026, 8, 10, 11, 0, tzinfo=UTC),
        }
    )

    assert approval_fingerprint(changed) == approval_fingerprint(package)


def test_issue_approval_recomputes_fingerprint_and_records_identity() -> None:
    package = _package().model_copy(update={"fingerprint": "0" * 64})
    approved_at = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)

    signed = issue_approval(package, approved_by="alice", approved_at=approved_at)

    assert signed.fingerprint == approval_fingerprint(package)
    assert signed.fingerprint != package.fingerprint
    assert signed.approved_by == "alice"
    assert signed.approved_at == approved_at


def test_verify_approval_uses_external_expected_not_package_fingerprint() -> None:
    package = _package()
    expected = approval_fingerprint(package)
    changed = package.model_copy(
        update={"work_item_title": "tampered", "fingerprint": expected}
    )

    with pytest.raises(ApprovalInvalidatedError):
        verify_approval(expected, changed)


@pytest.mark.parametrize("expected", ["", "A" * 64, "a" * 63, "a" * 65, "g" * 64])
def test_expected_fingerprint_requires_exact_canonical_sha256(expected: str) -> None:
    with pytest.raises(ApprovalInvalidatedError, match="invalid"):
        verify_approval(expected, _package())


def test_verify_approval_uses_constant_time_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    package = _package()
    expected = approval_fingerprint(package)
    calls: list[tuple[str, str]] = []

    def fake_compare(left: str, right: str) -> bool:
        calls.append((left, right))
        return True

    monkeypatch.setattr("src.developer_workflow.approval.hmac.compare_digest", fake_compare)

    verify_approval(expected, package)

    assert calls == [(expected, expected)]


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_deep_mutation_is_rejected_without_leaking_value(bad: float) -> None:
    package = _package()
    package.coverage["secret-field"] = bad  # type: ignore[assignment]

    with pytest.raises(ApprovalValidationError) as error:
        approval_fingerprint(package)

    assert "secret-field" not in str(error.value)
    assert repr(bad) not in str(error.value)


def test_lone_surrogate_is_rejected_with_sanitized_error() -> None:
    package = _package()
    package.coverage["secret-field"] = "\ud800"

    with pytest.raises(ApprovalValidationError) as error:
        approval_fingerprint(package)

    assert "secret-field" not in str(error.value)
    assert "surrogate" not in str(error.value).casefold()


def test_invalid_deep_type_mutation_is_revalidated_and_sanitized() -> None:
    package = _package()
    package.coverage["secret-field"] = {"password": "do-not-leak"}  # type: ignore[assignment]

    with pytest.raises(ApprovalValidationError) as error:
        approval_fingerprint(package)

    assert "secret-field" not in str(error.value)
    assert "do-not-leak" not in str(error.value)


def test_model_construct_cannot_bypass_validation() -> None:
    package = ApprovalPackage.model_construct(
        work_item_id="REQ-1",
        source_versions={"version": {"not": "a string"}},
    )

    with pytest.raises(ApprovalValidationError):
        approval_fingerprint(package)


def test_untrusted_model_copy_update_cannot_bypass_validation() -> None:
    package = object.__new__(ApprovalPackage)
    object.__setattr__(package, "__dict__", {**_package().__dict__, "tests": ({"exit_code": object()},)})
    object.__setattr__(package, "__pydantic_fields_set__", set(package.__dict__))
    object.__setattr__(package, "__pydantic_extra__", None)
    object.__setattr__(package, "__pydantic_private__", None)

    with pytest.raises(ApprovalValidationError):
        approval_fingerprint(package)


def test_issue_approval_rejects_blank_reviewer_and_naive_time() -> None:
    with pytest.raises(ApprovalValidationError):
        issue_approval(_package(), approved_by=" ")
    with pytest.raises(ApprovalValidationError):
        issue_approval(
            _package(), approved_by="alice", approved_at=datetime(2026, 8, 10)
        )


@pytest.mark.parametrize(
    "approved_by",
    ["reviewer\ud800", "reviewer\x1f", "reviewer\x7f", "reviewer\x85"],
)
def test_issue_approval_rejects_non_utf8_or_control_actor_without_echo(
    approved_by: str,
) -> None:
    with pytest.raises(ApprovalValidationError) as error:
        issue_approval(_package(), approved_by=approved_by)

    assert approved_by not in str(error.value)


def test_issue_approval_accepts_normal_unicode_actor() -> None:
    signed = issue_approval(_package(), approved_by="张三")

    assert signed.approved_by == "张三"
    assert signed.model_dump_json()


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("work_item_id", True),
        ("work_item_id", 1),
        ("unrelated_changes_checked", 1),
        ("changed_files", ["src/export.py"]),
        ("source_versions", {"requirement_version": 1}),
        ("source_versions", {"requirement_version": True}),
    ],
)
def test_fingerprint_rejects_coercible_runtime_type_collisions(
    field: str, bad: object
) -> None:
    values = dict(_package().__dict__)
    values[field] = bad
    package = ApprovalPackage.model_construct(**values)

    with pytest.raises(ApprovalValidationError):
        approval_fingerprint(package)


def test_fingerprint_rejects_bool_command_exit_code() -> None:
    package = _package()
    result = CommandResult.model_construct(
        **{**package.tests[0].__dict__, "exit_code": True}
    )
    malicious = ApprovalPackage.model_construct(
        **{**package.__dict__, "tests": (result,)}
    )

    with pytest.raises(ApprovalValidationError):
        approval_fingerprint(malicious)


def test_valid_json_round_trip_remains_canonical_and_approvable() -> None:
    package = _package()
    restored = ApprovalPackage.model_validate_json(package.model_dump_json())

    assert approval_fingerprint(restored) == approval_fingerprint(package)
    assert validate_for_approval(restored) == restored


@pytest.mark.parametrize(
    "package",
    [
        ApprovalPackage(work_item_id="REQ-1"),
        _package().model_copy(update={"work_item_title": ""}),
        _package().model_copy(update={"source_versions": {}}),
        _package().model_copy(update={"repository": None}),
        _package().model_copy(update={"changed_files": ()}),
        _package().model_copy(update={"diff_summary": ""}),
        _package().model_copy(update={"unrelated_changes_checked": False}),
        _package().model_copy(update={"coverage": {}, "evidence": ()}),
        _package().model_copy(update={"tests": ()}),
        _package().model_copy(update={"unresolved_items": ("open question",)}),
        _package().model_copy(update={"pr_body": ""}),
    ],
)
def test_validate_for_approval_rejects_incomplete_package(
    package: ApprovalPackage,
) -> None:
    with pytest.raises(ApprovalValidationError):
        validate_for_approval(package)
    with pytest.raises(ApprovalValidationError):
        issue_approval(package, approved_by="alice")


def test_validate_for_approval_rejects_failed_or_incomplete_test_result() -> None:
    package = _package()
    for update in (
        {"exit_code": 1, "outcome": CommandOutcome.COMMAND_ERROR},
        {"command": ""},
        {"summary": ""},
    ):
        result = package.tests[0].model_copy(update=update)
        with pytest.raises(ApprovalValidationError):
            validate_for_approval(package.model_copy(update={"tests": (result,)}))


def test_validate_for_approval_rejects_repository_duplicate_mismatch() -> None:
    package = _package()
    for update in (
        {"repo_url": "ssh://git@example.invalid/team/other.git"},
        {"base_branch": "release"},
    ):
        with pytest.raises(ApprovalValidationError):
            validate_for_approval(package.model_copy(update=update))


def test_validate_for_approval_rejects_wiki_hash_mismatch() -> None:
    package = _package().model_copy(update={"wiki_hashes": {"PAGE-1": "f" * 64}})

    with pytest.raises(ApprovalValidationError):
        validate_for_approval(package)


def test_defect_evidence_can_replace_wiki_and_requirement_coverage() -> None:
    timestamp = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
    package = _package().model_copy(
        update={
            "wiki_hashes": {},
            "wiki_snapshots": (),
            "coverage": {},
            "evidence": ("Root cause: src/export.py:20",),
            "root_cause_evidence": (
                RootCauseEvidence(
                    file_path="src/export.py",
                    location="line 20, export",
                    start_line=20,
                    end_line=20,
                    symbol="export",
                    mechanism="Empty input is dereferenced before validation.",
                    code_excerpt="rows[0]",
                    reproduction_test="tests/test_export.py",
                    test_selector="tests/test_export.py",
                    reproduction_command="uv run pytest",
                    confidence=0.9,
                    insufficient_evidence=False,
                    impacted_files=("src/export.py",),
                    fix_steps=("Guard empty input before indexing.",),
                    supporting_points=(
                        RootCauseSupportingPoint(
                            kind="defect",
                            description="The ONES defect reports empty export failure.",
                            source="ones",
                            snippet="empty export",
                        ),
                    ),
                ),
            ),
            "behavior_before": "Empty input raises an index error.",
            "behavior_after": "Empty input returns an empty export.",
            "impact_scope": ("src/export.py", "tests/test_export.py"),
            "risk_level": "medium",
            "pre_fix_tests": (
                CommandResult(
                        command="uv run pytest tests/test_export.py",
                        argv=("uv", "run", "pytest", "tests/test_export.py"),
                    exit_code=1,
                    summary="regression reproduced",
                    started_at=timestamp,
                    finished_at=timestamp,
                    outcome=CommandOutcome.TEST_FAILED,
                    output_sha256="1" * 64,
                ),
            ),
            "reproduction_command": "uv run pytest tests/test_export.py",
            "reproduction_test_sha256": "2" * 64,
            "tests": (
                CommandResult(
                    command="uv run pytest tests/test_export.py",
                    argv=("uv", "run", "pytest", "tests/test_export.py"),
                    exit_code=0,
                    summary="focused passed",
                    started_at=timestamp,
                    finished_at=timestamp,
                    outcome=CommandOutcome.PASSED,
                    output_sha256="3" * 64,
                ),
                CommandResult(
                        command="uv run pytest",
                        argv=("uv", "run", "pytest"),
                    exit_code=0,
                    summary="suite passed",
                    started_at=timestamp,
                    finished_at=timestamp,
                    outcome=CommandOutcome.PASSED,
                    output_sha256="4" * 64,
                ),
            ),
        }
    )

    assert validate_for_approval(package) == package


class _ExplodingTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta:
        raise RuntimeError("do-not-leak-timezone-secret")

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)


def test_malicious_timezone_error_is_sanitized() -> None:
    package = _package()
    bad_time = datetime(2026, 8, 10, tzinfo=_ExplodingTimezone())
    result = CommandResult.model_construct(
        **{
            **package.tests[0].__dict__,
            "started_at": bad_time,
            "finished_at": bad_time,
        }
    )
    malicious = ApprovalPackage.model_construct(
        **{**package.__dict__, "tests": (result,)}
    )

    with pytest.raises(ApprovalValidationError) as error:
        approval_fingerprint(malicious)

    assert "do-not-leak" not in str(error.value)


class _ExplodingActor(str):
    def strip(self, chars: str | None = None) -> str:
        raise RuntimeError("do-not-leak-actor-secret")


def test_malicious_actor_error_is_sanitized() -> None:
    with pytest.raises(ApprovalValidationError) as error:
        issue_approval(_package(), approved_by=_ExplodingActor("alice"))

    assert "do-not-leak" not in str(error.value)


class _ForgedApprovalErrorTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta:
        raise ApprovalValidationError("TOKEN-SECRET")

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)


class _GeneratorExitTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta:
        raise GeneratorExit("control-flow")

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)


def _package_with_timezone(zone: tzinfo) -> ApprovalPackage:
    package = _package()
    bad_time = datetime(2026, 8, 10, tzinfo=zone)
    result = CommandResult.model_construct(
        **{
            **package.tests[0].__dict__,
            "started_at": bad_time,
            "finished_at": bad_time,
        }
    )
    return ApprovalPackage.model_construct(
        **{**package.__dict__, "tests": (result,)}
    )


def test_untrusted_public_approval_error_is_still_sanitized() -> None:
    package = _package_with_timezone(_ForgedApprovalErrorTimezone())

    with pytest.raises(ApprovalValidationError) as error:
        approval_fingerprint(package)

    assert "TOKEN-SECRET" not in str(error.value)


def test_generator_exit_from_untrusted_operation_is_control_flow() -> None:
    package = _package_with_timezone(_GeneratorExitTimezone())

    with pytest.raises(GeneratorExit, match="control-flow"):
        approval_fingerprint(package)
