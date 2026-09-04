from __future__ import annotations

from unittest.mock import patch

import pytest

from src.developer_workflow.contracts import WorkflowState
from src.developer_workflow.defect_flow import _safe_unexpected_block
from src.developer_workflow.repository import RemoteBaseChangedError, RepositoryCommandError, WorktreeRepository
from src.developer_workflow.tui.detail_rendering import next_step
from src.developer_workflow.tui.models import RunDetail
from test_developer_workflow_missing_baseline_handoff import missing_run


@pytest.mark.parametrize("error,reason", [
    (RemoteBaseChangedError("private repository details"), "remote target branch changed since baseline"),
    (RepositoryCommandError("private credential details"), "repository command failed"),
])
def test_repository_failure_is_not_presented_as_manual_verification(tmp_path, error, reason):
    _, _, _, _, run = missing_run(tmp_path)
    blocked = _safe_unexpected_block(error, WorkflowState.AI_REVIEW)
    assert blocked.reason == reason
    detail = RunDetail.from_run(run.validated_update(blocked_reason=reason))
    assert "不是人工验证门禁" in detail.status_message
    assert "private" not in detail.status_message
    assert "完成相应环境验证" not in next_step(detail)


@pytest.mark.parametrize("reason", [
    "remote target branch changed since baseline", "repository command failed",
    "defect workflow safety validation failed",
])
def test_retry_reuses_review_but_rechecks_approval_conditions(tmp_path, reason):
    flow, store, _, codex, run = missing_run(tmp_path)
    run = run.validated_update(blocked_reason=reason)
    store.run = run
    stages = tuple(codex.stages)
    result = flow.execute(run)
    assert result.state is WorkflowState.WAITING_APPROVAL
    assert result.approval.draft_pr
    assert tuple(codex.stages) == stages


def test_moved_remote_baseline_raises_typed_error(tmp_path):
    _, _, _, _, run = missing_run(tmp_path)
    with patch.object(WorktreeRepository, "remote_base_oid", return_value="f" * 40):
        with pytest.raises(RemoteBaseChangedError):
            WorktreeRepository.assert_remote_base_unchanged(object.__new__(WorktreeRepository), run.prepared_worktree, run.repository)


def test_baseline_change_cannot_be_deferred_as_manual_check(tmp_path):
    flow, store, _, codex, run = missing_run(tmp_path)
    flow.config = flow.config.validated_update(max_baseline_refreshes=0)
    run = run.validated_update(blocked_reason="defect workflow safety validation failed")
    store.run = run
    stages = tuple(codex.stages)
    with patch.object(type(flow), "_approval_package", side_effect=RemoteBaseChangedError("remote moved")):
        result = flow.execute(run)
    assert result.state is WorkflowState.BLOCKED
    assert result.blocked_reason == "remote target branch changed since baseline"
    assert result.approval is None
    assert tuple(codex.stages) == stages
