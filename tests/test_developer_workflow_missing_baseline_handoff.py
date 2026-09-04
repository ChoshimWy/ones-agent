from __future__ import annotations

import pytest

from src.developer_workflow import pr_handoff as h, verification as v
from src.developer_workflow.approval import validate_for_approval, ApprovalValidationError, approval_fingerprint
from src.developer_workflow.contracts import WorkflowState, RepositoryGroupMapping, RepositoryApprovalEvidence, RepositoryChangeClaim
from src.developer_workflow.tui.models import RunDetail, DangerousActionRequest
from src.developer_workflow.tui.detail_rendering import next_step
from src.developer_workflow.verification_models import MISSING_BASELINE_DESCRIPTION
from src.developer_workflow.verification_models import VerificationRecord
from test_developer_workflow_defect import _flow


def missing_run(tmp_path):
    flow, store, repo, codex, _ = _flow(tmp_path)
    run = flow.execute(store.run)
    assert run.state is WorkflowState.WAITING_APPROVAL
    run = run.validated_update(state=WorkflowState.BLOCKED, resume_state=WorkflowState.AI_REVIEW,
        blocked_reason="pre-fix reproduction evidence is missing", approval=None, pre_fix_test_results=())
    store.run = run
    return flow, store, repo, codex, run


def test_missing_baseline_resumes_to_draft_without_repeating_ai(tmp_path):
    flow, _, _, codex, run = missing_run(tmp_path)
    before = tuple(codex.stages)
    result = flow.execute(run)
    assert tuple(codex.stages) == before
    assert result.state is WorkflowState.WAITING_APPROVAL, result.blocked_reason
    package = result.approval
    assert package.baseline_evidence_missing and package.draft_pr
    assert package.pre_fix_tests == () and result.pre_fix_test_results == ()
    assert MISSING_BASELINE_DESCRIPTION in package.pr_body
    assert MISSING_BASELINE_DESCRIPTION in package.manual_checks
    assert any(task.need.description == MISSING_BASELINE_DESCRIPTION for task in package.deferred_verification)
    validate_for_approval(package)
    h.assert_bound(result)
    request = DangerousActionRequest.from_run(result, action="approve")
    assert request.draft_pr and request.baseline_evidence_missing
    assert MISSING_BASELINE_DESCRIPTION in RunDetail.from_run(result).review_report.external_validation


def test_missing_baseline_strict_policy_remains_blocked(tmp_path):
    flow, _, _, _, run = missing_run(tmp_path)
    flow.config = flow.config.validated_update(publishing=flow.config.publishing.validated_update(
        defer_external_verification_to_pr=False))
    result = flow.execute(run)
    assert result.state is WorkflowState.BLOCKED and result.approval is None


@pytest.mark.parametrize("change", ["flag", "obligation", "failed_after_test", "false_baseline", "frozen_hash"])
def test_missing_baseline_cannot_hide_gap_or_bypass_failed_tests(tmp_path, change):
    flow, _, _, _, run = missing_run(tmp_path)
    result = flow.execute(run)
    package = result.approval
    if change == "flag":
        altered = package.validated_update(baseline_evidence_missing=False)
    elif change == "obligation":
        altered = package.validated_update(deferred_verification=())
    elif change == "failed_after_test":
        altered = package.validated_update(tests=(package.tests[0].validated_update(exit_code=1, outcome="test_failed"), *package.tests[1:]))
    elif change == "frozen_hash":
        altered = package.validated_update(reproduction_test_sha256="")
    else:
        altered = package.validated_update(pre_fix_tests=(package.tests[0],))
    with pytest.raises(ApprovalValidationError):
        validate_for_approval(altered)
    assert approval_fingerprint(altered) != approval_fingerprint(package)


def test_snapshot_change_still_blocks_missing_baseline_handoff(tmp_path):
    flow, _, _, _, run = missing_run(tmp_path)
    result = flow.execute(run)
    changed = result.validated_update(tested_snapshot=result.tested_snapshot.validated_update(diff_sha256="f" * 64))
    with pytest.raises(ValueError, match="stale"):
        h.assert_bound(changed)


def test_missing_baseline_guidance_takes_priority_over_external_checks(tmp_path):
    _, _, _, _, run = missing_run(tmp_path)
    detail = RunDetail.from_run(run)
    assert "缺少修复前失败复现记录" in detail.status_message
    assert "重新检查审批条件" in next_step(detail)
    assert "无需重复 Review" in next_step(detail)


def test_group_approval_accepts_only_disclosed_missing_baseline(tmp_path):
    flow, _, _, _, run = missing_run(tmp_path)
    package = flow.execute(run).approval
    mapping = package.repository
    group = RepositoryGroupMapping(key="group", project_id=mapping.project_id, iteration_id=mapping.iteration_id,
        primary_repository=mapping.key, repositories=(mapping,))
    item = RepositoryApprovalEvidence(repository_key=mapping.key, mapping=mapping,
        base_commit=package.base_commit, head_commit=package.head_commit, diff_hash=package.diff_hash,
        diff_summary=package.diff_summary, branch=package.branch, changed_files=package.changed_files,
        tests=package.tests, tree_hash="a" * 40, commit_message=package.commit_message,
        pr_title=package.pr_title, pr_body=package.pr_body)
    evidence = tuple(e.validated_update(
        repository_file=RepositoryChangeClaim(repository_key=mapping.key, path=e.file_path),
        reproduction_file=RepositoryChangeClaim(repository_key=mapping.key, path=e.reproduction_test)) for e in package.root_cause_evidence)
    package = package.validated_update(repository=None, repository_group=group, repositories=(item,), root_cause_evidence=evidence,
        repo_url="", base_branch="", base_commit="", head_commit="", diff_hash="", diff_summary="", branch="",
        changed_files=(), tests=(), commit_message="", pr_title="", pr_body="")
    validate_for_approval(package)
    with pytest.raises(ApprovalValidationError):
        validate_for_approval(package.validated_update(baseline_evidence_missing=False))
    bad_test = item.tests[0].validated_update(exit_code=1, outcome="test_failed")
    with pytest.raises(ApprovalValidationError):
        validate_for_approval(package.validated_update(repositories=(item.validated_update(tests=(bad_test, *item.tests[1:])),)))


@pytest.mark.parametrize("status,expected", [("passed", "manual"), ("failed", "failed")])
def test_manual_record_never_fabricates_historical_baseline(tmp_path, status, expected):
    flow, _, _, _, run = missing_run(tmp_path)
    result = flow.execute(run)
    task = next(task for task in result.verification_plan if task.need.description == MISSING_BASELINE_DESCRIPTION)
    record = VerificationRecord(task_key=task.key, snapshot_digest=task.snapshot_digest, node_key="manual",
        status=status, actor="reviewer", evidence="Manual assessment", output_sha256="a" * 64, occurred_at="2026-09-04T00:00:00Z")
    changed = result.validated_update(verification_records=(record,))
    assert next(t for t in v.plan(changed, ()) if t.key == task.key).status == expected
