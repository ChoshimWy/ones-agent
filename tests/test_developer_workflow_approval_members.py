from __future__ import annotations

import asyncio
from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from src.developer_workflow.tui.controller import TuiController, TuiControllerError
from src.developer_workflow.tui.models import DangerousActionRequest, FilterChoice
from src.developer_workflow.contracts import WorkflowRun, WorkflowType


def controller_with_members(members):
    gateway = SimpleNamespace(list_team_members=AsyncMock(return_value=members))
    controller = object.__new__(TuiController)
    controller._orchestrator = SimpleNamespace(
        defect_candidates=SimpleNamespace(gateway=gateway), approve=Mock(),
        show=Mock(return_value=SimpleNamespace(project_id="", defect=None, requirement=None)))
    controller._async_runtime = SimpleNamespace(submit=asyncio.run)
    controller._assert_request = Mock()
    controller._dangerous = Mock()
    return controller, gateway


def request():
    return DangerousActionRequest(
        run_id="run", version=1, action="approve", fingerprint="f" * 64,
        work_item_id="BUG", repositories=(), changed_file_count=1,
        test_count=1, risk_count=0, unresolved_count=0,
        approvers=(FilterChoice("member-id", "同名成员"),))


def test_directory_uses_exact_ids_and_escapes_member_names():
    controller, gateway = controller_with_members([
        {"uuid": "b", "name": "同名成员"},
        {"uuid": "a", "name": "同名成员"},
        {"uuid": "c", "name": "[bold]管理员"},
        {"uuid": "a", "name": "同名成员"},
        {"uuid": "incomplete"},
    ])
    choices = controller.load_approval_members()
    assert {item.id for item in choices} == {"a", "b", "c"}
    assert next(item for item in choices if item.id == "c").name == r"\[bold]管理员"
    gateway.list_team_members.assert_awaited_once_with(uuids=None)


@pytest.mark.parametrize("members", [[], None, [{"uuid": "bad\nID", "name": "A"}]])
def test_invalid_directory_fails_closed(members):
    controller, _ = controller_with_members(members)
    with pytest.raises(TuiControllerError, match="ONES"):
        controller.load_approval_members()
    controller._dangerous.assert_not_called()


def test_member_id_is_revalidated_and_forwarded_not_name():
    controller, gateway = controller_with_members([{"uuid": "member-id", "name": "同名成员"}])
    bound = request()
    controller.approve(bound, "member-id")
    controller._dangerous.assert_called_once_with(
        controller._orchestrator.approve, "run", "member-id", expected_version=1)
    gateway.list_team_members.assert_awaited_once()


@pytest.mark.parametrize("actor,members", [
    ("forged-id", [{"uuid": "forged-id", "name": "伪造"}]),
    ("member-id", [{"uuid": "another-id", "name": "同名成员"}]),
])
def test_unoffered_or_removed_member_never_approves(actor, members):
    controller, _ = controller_with_members(members)
    with pytest.raises(TuiControllerError):
        controller.approve(request(), actor)
    controller._dangerous.assert_not_called()


def test_directory_failure_is_sanitized_and_never_approves():
    controller, gateway = controller_with_members([])
    gateway.list_team_members.side_effect = RuntimeError("private-token-value")
    with pytest.raises(TuiControllerError, match="^ONES 成员接口调用失败，请检查连接与账号权限后重新打开审批。$"):
        controller.approve(request(), "member-id")
    controller._dangerous.assert_not_called()


def test_requirement_only_runtime_uses_its_gateway():
    controller, gateway = controller_with_members([{"uuid": "member-id", "name": "成员"}])
    controller._orchestrator.defect_candidates = None
    controller._orchestrator.requirement_flow = SimpleNamespace(gateway=gateway)
    assert controller.load_approval_members()[0].id == "member-id"


def test_project_members_lookup_never_treats_empty_ids_as_all_users():
    controller, gateway = controller_with_members([])
    controller._orchestrator.show.return_value.project_id = "project-id"
    gateway.list_role_members = AsyncMock(return_value=[
        {"members": ["member-id", {"uuid": "member-id"}, {"id": "another"}]}])

    async def lookup(*, uuids):
        assert uuids == ["another", "member-id"]
        return [{"uuid": "member-id", "name": "成员"}, {"uuid": "outsider", "name": "其它项目"}]

    gateway.list_team_members.side_effect = lookup
    assert [item.id for item in controller.load_approval_members("run")] == ["member-id"]
    gateway.list_role_members.assert_awaited_once_with("project-id")


def test_no_project_members_has_specific_message_and_no_empty_lookup():
    controller, gateway = controller_with_members([])
    controller._orchestrator.show.return_value.project_id = "project-id"
    gateway.list_role_members = AsyncMock(return_value=[])
    with pytest.raises(TuiControllerError, match="项目未返回可选成员"):
        controller.load_approval_members("run")
    gateway.list_team_members.assert_not_awaited()


def test_live_directory_does_not_invalidate_bound_run_but_snapshot_changes_do():
    run = WorkflowRun.new(WorkflowType.REQUIREMENT, "REQ-1")
    bound = DangerousActionRequest.from_run(run, action="cancel")
    enriched = replace(bound, approvers=(FilterChoice("member-id", "成员"),))
    enriched.assert_current(run)
    with pytest.raises(ValueError):
        replace(enriched, fingerprint="changed").assert_current(run)
