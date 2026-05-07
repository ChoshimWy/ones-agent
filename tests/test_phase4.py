"""Phase 4 测试 - LLM 规划器 + 工作流引擎"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.llm.planner import Planner, DevPlan
from src.core.engine import Engine, State


# ── Planner 测试 ──────────────────────────────────────────


class TestDevPlan:
    def test_defaults(self):
        plan = DevPlan()
        assert plan.branch_name == ""
        assert plan.steps == []
        assert plan.risk_level == "low"
        assert plan.requires_human_approval is False

    def test_from_dict(self):
        plan = DevPlan(
            branch_name="feat/ONES-REQ-1-add-login",
            steps=["修改 auth.py"],
            risk_level="medium",
            requires_human_approval=True,
            summary="添加登录功能",
        )
        assert plan.branch_name.startswith("feat/")
        assert len(plan.steps) == 1


class TestPlannerParse:
    def test_parse_valid_json(self):
        planner = Planner()
        content = json.dumps({
            "branch_name": "feat/ONES-REQ-1-add-login",
            "steps": ["修改 auth.py", "新增测试"],
            "risk_level": "low",
            "requires_human_approval": False,
            "summary": "添加登录",
        })
        plan = planner._parse(content)
        assert plan.branch_name == "feat/ONES-REQ-1-add-login"
        assert len(plan.steps) == 2

    def test_parse_invalid_json(self):
        planner = Planner()
        plan = planner._parse("not json at all")
        assert plan.summary  # should contain raw content

    def test_parse_partial_json(self):
        planner = Planner()
        plan = planner._parse(json.dumps({"branch_name": "fix/bug"}))
        assert plan.branch_name == "fix/bug"
        assert plan.steps == []


class TestPlannerFallback:
    def test_fallback_for_requirement(self):
        planner = Planner()
        item = {"uuid": "abc12345", "name": "Add login page", "issueType": {"name": "需求"}}
        plan = planner._fallback_plan(item, "timeout")
        assert plan.branch_name.startswith("feat/")
        assert plan.requires_human_approval is True

    def test_fallback_for_defect(self):
        planner = Planner()
        item = {"uuid": "def67890", "name": "Fix null pointer", "issueType": {"name": "缺陷"}}
        plan = planner._fallback_plan(item, "timeout")
        assert plan.branch_name.startswith("fix/")


class TestPlannerBuildPrompt:
    def test_build_prompt_includes_key_fields(self):
        planner = Planner()
        item = {
            "uuid": "item-1",
            "name": "Add login",
            "issueType": {"name": "需求"},
            "status": {"name": "待处理"},
            "priority": {"value": "高"},
            "assign": {"name": "张三"},
            "project": {"name": "P1"},
            "description": "需要OAuth2登录",
        }
        prompt = planner._build_prompt(item)
        assert "Add login" in prompt
        assert "需求" in prompt
        assert "OAuth2" in prompt

    def test_build_prompt_minimal_item(self):
        planner = Planner()
        prompt = planner._build_prompt({"name": "Bug fix"})
        assert "Bug fix" in prompt


class TestPlannerAsync:
    @pytest.mark.asyncio
    async def test_plan_calls_litellm(self):
        planner = Planner()
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = json.dumps({
            "branch_name": "feat/ONES-REQ-1-test",
            "steps": ["step1"],
            "risk_level": "low",
            "requires_human_approval": False,
            "summary": "test",
        })

        with patch("src.llm.planner.litellm") as mock_litellm:
            mock_litellm.acompletion = AsyncMock(return_value=mock_resp)
            plan = await planner.plan({"uuid": "1", "name": "Test"})

        assert plan.branch_name == "feat/ONES-REQ-1-test"

    @pytest.mark.asyncio
    async def test_plan_fallback_on_error(self):
        planner = Planner()

        with patch("src.llm.planner.litellm") as mock_litellm:
            mock_litellm.acompletion = AsyncMock(side_effect=Exception("API down"))
            plan = await planner.plan({"uuid": "1", "name": "Test", "issueType": {"name": "需求"}})

        assert plan.requires_human_approval is True
        assert "feat/" in plan.branch_name


# ── Engine 测试 ──────────────────────────────────────────


class TestEngine:
    @pytest.fixture
    def engine(self, tmp_path):
        return Engine(db_path=str(tmp_path / "test.db"))

    def test_start_work(self, engine):
        engine.start_work("item-1")
        record = engine.get("item-1")
        assert record is not None
        assert record.state == "pending"

    def test_start_work_idempotent(self, engine):
        engine.start_work("item-1", State.PARSING)
        engine.start_work("item-1", State.PENDING)
        record = engine.get("item-1")
        assert record.state == "parsing"

    def test_transition_valid(self, engine):
        engine.start_work("item-1")
        engine.transition("item-1", State.PARSING)
        assert engine.get("item-1").state == "parsing"

    def test_transition_invalid(self, engine):
        engine.start_work("item-1")
        with pytest.raises(ValueError, match="Invalid transition"):
            engine.transition("item-1", State.SUCCESS)

    def test_full_lifecycle(self, engine):
        engine.start_work("item-1")
        for state in [State.PARSING, State.PLANNING, State.CODING, State.TESTING, State.PUSHING, State.REPORTING, State.SUCCESS]:
            engine.transition("item-1", state)
        assert engine.get("item-1").state == "success"

    def test_failure_path(self, engine):
        engine.start_work("item-1")
        engine.transition("item-1", State.PARSING)
        engine.transition("item-1", State.FAILED)
        assert engine.get("item-1").state == "failed"

    def test_retry_from_failed(self, engine):
        engine.start_work("item-1")
        engine.transition("item-1", State.PARSING)
        engine.transition("item-1", State.FAILED)
        engine.transition("item-1", State.PENDING)
        assert engine.get("item-1").state == "pending"

    def test_waiting_approval(self, engine):
        engine.start_work("item-1")
        engine.transition("item-1", State.PARSING)
        engine.transition("item-1", State.PLANNING)
        engine.transition("item-1", State.WAITING_APPROVAL)
        assert engine.get("item-1").state == "waiting_approval"

    def test_approval_to_coding(self, engine):
        engine.start_work("item-1")
        engine.transition("item-1", State.PARSING)
        engine.transition("item-1", State.PLANNING)
        engine.transition("item-1", State.WAITING_APPROVAL)
        engine.transition("item-1", State.CODING)
        assert engine.get("item-1").state == "coding"

    @pytest.mark.parametrize("state", [
        State.PENDING,
        State.PARSING,
        State.PLANNING,
        State.CODING,
        State.TESTING,
        State.PUSHING,
        State.REPORTING,
    ])
    def test_pause_from_non_terminal_workflow_states(self, engine, state):
        engine.start_work("item-1", state)
        engine.transition("item-1", State.WAITING_APPROVAL)
        assert engine.get("item-1").state == "waiting_approval"

    @pytest.mark.parametrize("state", [
        State.PENDING,
        State.PARSING,
        State.PLANNING,
        State.WAITING_APPROVAL,
        State.CODING,
        State.TESTING,
        State.PUSHING,
        State.REPORTING,
    ])
    def test_cancel_from_non_terminal_workflow_states(self, engine, state):
        engine.start_work("item-1", state)
        engine.transition("item-1", State.FAILED)
        assert engine.get("item-1").state == "failed"

    def test_update_branch_and_commit(self, engine):
        engine.start_work("item-1")
        engine.transition("item-1", State.PARSING)
        engine.transition("item-1", State.PLANNING, branch="feat/ONES-1-test")
        engine.transition("item-1", State.CODING, commit_hash="abc123")
        record = engine.get("item-1")
        assert record.branch == "feat/ONES-1-test"
        assert record.commit_hash == "abc123"

    def test_update_plan_json(self, engine):
        plan = {"steps": ["step1", "step2"]}
        engine.start_work("item-1")
        engine.transition("item-1", State.PARSING)
        engine.transition("item-1", State.PLANNING, plan_json=json.dumps(plan))
        record = engine.get("item-1")
        assert record.plan["steps"] == ["step1", "step2"]

    def test_can_proceed(self, engine):
        engine.start_work("item-1")
        assert engine.can_proceed("item-1") is True
        engine.transition("item-1", State.PARSING)
        engine.transition("item-1", State.PLANNING)
        engine.transition("item-1", State.CODING)
        engine.transition("item-1", State.TESTING)
        engine.transition("item-1", State.PUSHING)
        engine.transition("item-1", State.REPORTING)
        engine.transition("item-1", State.SUCCESS)
        assert engine.can_proceed("item-1") is False

    def test_is_terminal(self, engine):
        engine.start_work("item-1")
        assert engine.is_terminal("item-1") is False
        engine.transition("item-1", State.PARSING)
        engine.transition("item-1", State.PLANNING)
        engine.transition("item-1", State.CODING)
        engine.transition("item-1", State.TESTING)
        engine.transition("item-1", State.PUSHING)
        engine.transition("item-1", State.REPORTING)
        engine.transition("item-1", State.SUCCESS)
        assert engine.is_terminal("item-1") is True

    def test_get_by_state(self, engine):
        engine.start_work("item-1")
        engine.start_work("item-2")
        engine.transition("item-1", State.PARSING)
        pending = engine.get_by_state(State.PENDING)
        parsing = engine.get_by_state(State.PARSING)
        assert len(pending) == 1
        assert len(parsing) == 1

    def test_delete(self, engine):
        engine.start_work("item-1")
        engine.delete("item-1")
        assert engine.get("item-1") is None

    def test_nonexistent(self, engine):
        assert engine.get("not-exist") is None

    def test_persistence(self, tmp_path):
        db = str(tmp_path / "persist.db")
        engine1 = Engine(db_path=db)
        engine1.start_work("item-1")
        engine1.transition("item-1", State.PARSING)

        engine2 = Engine(db_path=db)
        record = engine2.get("item-1")
        assert record.state == "parsing"
