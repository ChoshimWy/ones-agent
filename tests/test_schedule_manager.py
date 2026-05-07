"""定时任务管理器 + 通知服务 测试"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from config.settings import Settings
from src.core.engine import Engine
from src.core.schedule_manager import ScheduleManager, _should_fire, _should_fire_interval, _build_report
from src.integrations.notification import NotifyTarget, _md_to_html


class TestCronParser:
    def test_standard_cron_match(self):
        dt = datetime(2026, 4, 29, 9, 0, tzinfo=timezone.utc)
        assert _should_fire("0 9 * * *", dt) is True

    def test_standard_cron_no_match(self):
        dt = datetime(2026, 4, 29, 9, 30, tzinfo=timezone.utc)
        assert _should_fire("0 9 * * *", dt) is False

    def test_step_cron(self):
        dt = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
        assert _should_fire("*/30 * * * *", dt) is True

    def test_step_cron_no_match(self):
        dt = datetime(2026, 4, 29, 10, 15, tzinfo=timezone.utc)
        assert _should_fire("*/30 * * * *", dt) is False

    def test_specific_hour_and_minute(self):
        dt = datetime(2026, 4, 29, 14, 30, tzinfo=timezone.utc)
        assert _should_fire("30 14 * * *", dt) is True

    def test_wildcard_minute(self):
        dt = datetime(2026, 4, 29, 9, 45, tzinfo=timezone.utc)
        assert _should_fire("* 9 * * *", dt) is True

    def test_wrong_hour(self):
        dt = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
        assert _should_fire("0 9 * * *", dt) is False


class TestIntervalParser:
    def test_30m_on_mark(self):
        dt = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
        assert _should_fire_interval("30m", dt) is True

    def test_30m_off_mark(self):
        dt = datetime(2026, 4, 29, 10, 15, tzinfo=timezone.utc)
        assert _should_fire_interval("30m", dt) is False

    def test_1h_on_mark(self):
        dt = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
        assert _should_fire_interval("1h", dt) is True

    def test_1h_off_mark(self):
        dt = datetime(2026, 4, 29, 10, 30, tzinfo=timezone.utc)
        assert _should_fire_interval("1h", dt) is False

    def test_2h_on_mark(self):
        dt = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
        assert _should_fire_interval("2h", dt) is True

    def test_invalid_interval(self):
        dt = datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc)
        assert _should_fire_interval("abc", dt) is False


class TestBuildReport:
    def test_plan_report(self):
        items = [{"name": "Bug A", "status": {"name": "open"}}]
        results = [{"name": "Bug A", "summary": "Fix X", "steps": ["step1"], "risk_level": "low", "branch": "fix/bug-a"}]
        report = _build_report("Test Task", items, results, "plan")
        assert "Bug A" in report
        assert "Fix X" in report
        assert "step1" in report

    def test_analyze_report(self):
        items = [{"name": "Bug B"}]
        results = [{"name": "Bug B", "analysis": "Root cause: ..."}]
        report = _build_report("Test", items, results, "analyze")
        assert "Bug B" in report
        assert "Root cause" in report

    def test_no_results(self):
        items = [{"name": "Bug C", "status": {"name": "open"}, "priority": {"name": "P1"}, "assign": {"name": "dev"}}]
        report = _build_report("Test", items, [], "plan")
        assert "Bug C" in report


class TestScheduleManagerRepoMapping:
    def test_build_codebase_uses_project_repo_mapping(self, tmp_path, monkeypatch):
        settings = Settings(env_file=str(tmp_path / "missing.env"))
        engine = Engine(db_path=str(tmp_path / "agent.db"))
        engine.add_project_repo("proj-1", "Project 1", "https://example.com/repo.git", "release")
        created = {}

        class FakeCodebase:
            def __init__(self, repo_url: str, branch: str):
                created["repo_url"] = repo_url
                created["branch"] = branch

        monkeypatch.setattr("src.core.schedule_manager.Codebase", FakeCodebase)

        manager = ScheduleManager(settings, engine)
        codebase = manager._build_codebase("proj-1")

        assert isinstance(codebase, FakeCodebase)
        assert created == {"repo_url": "https://example.com/repo.git", "branch": "release"}

    def test_build_codebase_falls_back_to_config_project_id(self, tmp_path, monkeypatch):
        settings = Settings(env_file=str(tmp_path / "missing.env"))
        settings.ones.project_id = "default-proj"
        engine = Engine(db_path=str(tmp_path / "agent.db"))
        engine.add_project_repo("default-proj", "Default Project", "https://example.com/default.git", "main")
        created = {}

        class FakeCodebase:
            def __init__(self, repo_url: str, branch: str):
                created["repo_url"] = repo_url
                created["branch"] = branch

        monkeypatch.setattr("src.core.schedule_manager.Codebase", FakeCodebase)

        manager = ScheduleManager(settings, engine)
        codebase = manager._build_codebase("")

        assert isinstance(codebase, FakeCodebase)
        assert created == {"repo_url": "https://example.com/default.git", "branch": "main"}

    def test_build_codebase_returns_none_without_mapping(self, tmp_path):
        settings = Settings(env_file=str(tmp_path / "missing.env"))
        engine = Engine(db_path=str(tmp_path / "agent.db"))

        manager = ScheduleManager(settings, engine)

        assert manager._build_codebase("missing-proj") is None

    async def test_ai_process_passes_codebase_to_analyzer(self, tmp_path, monkeypatch):
        settings = Settings(env_file=str(tmp_path / "missing.env"))
        settings.llm.api_key = "test-key"
        engine = Engine(db_path=str(tmp_path / "agent.db"))
        calls = []

        class FakeAnalyzer:
            def __init__(self, base_url: str, api_key: str, model: str):
                self.base_url = base_url
                self.api_key = api_key
                self.model = model

            def analyze(self, defect: dict, codebase=None) -> str:
                calls.append({"defect": defect, "codebase": codebase})
                return "Root cause with code context"

        monkeypatch.setattr("src.core.schedule_manager.Analyzer", FakeAnalyzer)

        manager = ScheduleManager(settings, engine)
        codebase = object()
        results = await manager._ai_process(
            [{"uuid": "bug-1", "name": "Bug 1"}],
            "analyze",
            codebase=codebase,
        )

        assert calls == [{"defect": {"uuid": "bug-1", "name": "Bug 1"}, "codebase": codebase}]
        assert len(results) == 1
        assert results[0]["name"] == "Bug 1"
        assert results[0]["analysis"] == "Root cause with code context"
        assert results[0]["analysis_markdown"] == "Root cause with code context"
        assert results[0]["with_codebase"] is True
        assert results[0]["item_uuid"] == "bug-1"

    @pytest.mark.asyncio
    async def test_fetch_items_uses_current_user_scan(self, tmp_path, monkeypatch):
        settings = Settings(env_file=str(tmp_path / "missing.env"))
        settings.ones.email = "test@example.com"
        settings.ones.password = "secret"
        engine = Engine(db_path=str(tmp_path / "agent.db"))
        engine.add_project_repo("proj-1", "Project 1", "https://example.com/repo.git", "main", "sprint-11", "Sprint 11", "SPR-11")
        expected_items = [
            {"uuid": "bug-1", "issueType": {"name": "缺陷"}},
            {"uuid": "req-1", "issueType": {"name": "需求"}},
        ]

        class FakeClient:
            def __init__(self, _settings):
                self.fetch_my_defects = AsyncMock(return_value=expected_items)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        created = {}

        def make_client(conf):
            client = FakeClient(conf)
            created["client"] = client
            return client

        monkeypatch.setattr("src.core.schedule_manager.OnesAsyncClient", make_client)

        manager = ScheduleManager(settings, engine)
        items = await manager._fetch_items("proj-1", "defect")

        created["client"].fetch_my_defects.assert_awaited_once_with(project_id="proj-1", sprint_id="sprint-11")
        assert items == [{"uuid": "bug-1", "issueType": {"name": "缺陷"}}]

    @pytest.mark.asyncio
    async def test_fetch_items_uses_explicit_assignee_filter(self, tmp_path, monkeypatch):
        settings = Settings(env_file=str(tmp_path / "missing.env"))
        settings.ones.email = "test@example.com"
        settings.ones.password = "secret"
        engine = Engine(db_path=str(tmp_path / "test.db"))
        engine.add_project_repo("proj-1", "Project 1", "https://example.com/repo.git", "main", "sprint-11", "Sprint 11", "SPR-11")
        expected_items = [{"uuid": "bug-1", "issueType": {"name": "缺陷"}}]

        class FakeClient:
            def __init__(self, _settings):
                self.fetch_defects = AsyncMock(return_value=expected_items)
                self.fetch_my_defects = AsyncMock(return_value=[])

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        created = {}

        def make_client(conf):
            client = FakeClient(conf)
            created["client"] = client
            return client

        monkeypatch.setattr("src.core.schedule_manager.OnesAsyncClient", make_client)

        manager = ScheduleManager(settings, engine)
        items = await manager._fetch_items("proj-1", "defect", assignee_id="user-1")

        created["client"].fetch_defects.assert_awaited_once_with(project_id="proj-1", sprint_id="sprint-11", assign="user-1")
        created["client"].fetch_my_defects.assert_not_called()
        assert items == expected_items

    @pytest.mark.asyncio
    async def test_execute_task_persists_run_details(self, tmp_path, monkeypatch):
        settings = Settings(env_file=str(tmp_path / "missing.env"))
        settings.ones.email = "test@example.com"
        settings.ones.password = "secret"
        settings.llm.api_key = "llm-key"
        engine = Engine(db_path=str(tmp_path / "agent.db"))
        task = {
            "id": "task-run-1",
            "name": "Daily Scan",
            "projectId": "proj-1",
            "itemType": "defect",
            "action": "plan",
            "notifyEmails": "",
            "notifyWechat": False,
        }
        defect = {
            "uuid": "bug-1",
            "name": "Bug 1",
            "issueType": {"name": "缺陷"},
            "project": {"uuid": "proj-1", "name": "Project One"},
            "assign": {"name": "Alice"},
            "status": {"name": "处理中"},
            "priority": {"name": "高"},
        }

        class FakeClient:
            def __init__(self, _settings):
                self.fetch_my_defects = AsyncMock(return_value=[defect])

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class FakePlanner:
            def __init__(self, _settings):
                pass

            async def plan(self, _item):
                return type("Plan", (), {
                    "summary": "Fix the event binding",
                    "steps": ["Locate handler", "Add test"],
                    "risk_level": "medium",
                    "branch_name": "fix/bug-1",
                    "requires_human_approval": True,
                })()

        monkeypatch.setattr("src.core.schedule_manager.OnesAsyncClient", FakeClient)
        monkeypatch.setattr("src.core.schedule_manager.Planner", FakePlanner)

        manager = ScheduleManager(settings, engine)
        await manager._execute_task(task)

        runs = engine.list_scheduled_task_runs("task-run-1")
        assert len(runs) == 1
        assert runs[0]["status"] == "success"
        assert runs[0]["itemCount"] == 1

        run_items = engine.list_scheduled_task_run_items(runs[0]["id"])
        assert len(run_items) == 1
        assert run_items[0]["itemUuid"] == "bug-1"
        assert run_items[0]["planSummary"] == "Fix the event binding"
        assert run_items[0]["planSteps"] == ["Locate handler", "Add test"]
        assert run_items[0]["itemSnapshot"]["name"] == "Bug 1"


class TestNotifyTarget:
    def test_target_creation(self):
        t = NotifyTarget(emails=["a@b.com", "c@d.com"], wechat=True)
        assert t.emails == ["a@b.com", "c@d.com"]
        assert t.wechat is True

    def test_target_defaults(self):
        t = NotifyTarget()
        assert t.emails == []
        assert t.wechat is False


class TestMdToHtml:
    def test_headers(self):
        html = _md_to_html("# Title\n## Sub\n### SubSub")
        assert "<h1>Title</h1>" in html
        assert "<h2>Sub</h2>" in html
        assert "<h3>SubSub</h3>" in html

    def test_bold(self):
        html = _md_to_html("**bold**")
        assert "<strong>bold</strong>" in html

    def test_code(self):
        html = _md_to_html("`code`")
        assert "<code>code</code>" in html
