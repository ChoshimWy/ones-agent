"""Scheduler 单元测试"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.scheduler import Scheduler
from src.core.engine import Engine, State


@pytest.fixture
def engine(tmp_path):
    return Engine(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.ones = MagicMock()
    s.ones.email = "test@example.com"
    s.ones.password = "secret"
    s.ones.team_id = "team-1"
    s.ones.project_id = "proj-1"
    s.ones.base_url = "http://ones.test"
    s.ones.issue_type_id = ""
    s.llm = MagicMock()
    s.llm.api_key = ""
    s.llm.provider = "openai"
    s.llm.model = "gpt-4"
    s.llm.base_url = "http://llm.test"
    s.agent = MagicMock()
    s.agent.check_interval = 10
    return s


class TestScheduler:
    def test_status_initial(self, mock_settings, engine):
        sched = Scheduler(mock_settings, engine)
        status = sched.status()
        assert status["running"] is False
        assert status["hasCredentials"] is True
        assert status["hasProject"] is True

    def test_status_no_credentials(self, mock_settings, engine):
        mock_settings.ones.email = ""
        sched = Scheduler(mock_settings, engine)
        assert sched.status()["hasCredentials"] is False

    def test_status_no_project(self, mock_settings, engine):
        mock_settings.ones.team_id = ""
        mock_settings.ones.project_id = ""
        sched = Scheduler(mock_settings, engine)
        assert sched.status()["hasProject"] is False

    @pytest.mark.asyncio
    async def test_poll_creates_tasks(self, mock_settings, engine):
        defects = [
            {"uuid": "BUG-1", "name": "Fix login bug"},
            {"uuid": "BUG-2", "name": "Fix crash"},
        ]
        with patch("src.core.scheduler.OnesAsyncClient") as MockClient, \
             patch("src.core.scheduler.Store") as MockStore:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.fetch_my_defects = AsyncMock(return_value=defects)
            MockClient.return_value = mock_client

            mock_store = MagicMock()
            mock_store.filter_new = MagicMock(return_value=defects)
            mock_store.update_check_time = MagicMock()
            MockStore.return_value = mock_store

            sched = Scheduler(mock_settings, engine)
            count = await sched.poll_now()
            assert count == 2
            mock_client.fetch_my_defects.assert_awaited_once_with(project_id="proj-1")
            assert engine.get("BUG-1") is not None
            assert engine.get("BUG-2") is not None
            assert State(engine.get("BUG-1").state) == State.PARSING

    @pytest.mark.asyncio
    async def test_poll_skips_seen(self, mock_settings, engine, tmp_path):
        with patch("src.core.scheduler.OnesAsyncClient") as MockClient, \
             patch("src.core.scheduler.Store") as MockStore:
            mock_store = MagicMock()
            mock_store.filter_new = MagicMock(side_effect=[
                [{"uuid": "BUG-1", "name": "Fix login bug"}],
                [],
            ])
            mock_store.update_check_time = MagicMock()
            MockStore.return_value = mock_store

            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.fetch_my_defects = AsyncMock(return_value=[
                {"uuid": "BUG-1", "name": "Fix login bug"},
            ])
            MockClient.return_value = mock_client

            sched = Scheduler(mock_settings, engine)
            await sched.poll_now()
            assert sched.last_new_count == 1

            await sched.poll_now()
            assert sched.last_new_count == 0

    @pytest.mark.asyncio
    async def test_poll_no_credentials(self, mock_settings, engine):
        mock_settings.ones.email = ""
        sched = Scheduler(mock_settings, engine)
        count = await sched.poll_now()
        assert count == 0

    @pytest.mark.asyncio
    async def test_poll_with_llm_planning(self, mock_settings, engine):
        mock_settings.llm.api_key = "sk-test"
        defects = [{"uuid": "BUG-3", "name": "Fix auth"}]

        with patch("src.core.scheduler.OnesAsyncClient") as MockClient, \
             patch("src.core.scheduler.Planner") as MockPlanner, \
             patch("src.core.scheduler.Store") as MockStore:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.fetch_my_defects = AsyncMock(return_value=defects)
            MockClient.return_value = mock_client

            mock_plan = MagicMock()
            mock_plan.requires_human_approval = False
            mock_plan.branch_name = "fix/BUG-3-auth"
            mock_plan.model_dump_json = MagicMock(return_value='{"steps":["fix"]}')
            MockPlanner.return_value.plan = AsyncMock(return_value=mock_plan)

            mock_store = MagicMock()
            mock_store.filter_new = MagicMock(return_value=defects)
            mock_store.update_check_time = MagicMock()
            MockStore.return_value = mock_store

            sched = Scheduler(mock_settings, engine)
            count = await sched.poll_now()
            assert count == 1
            item = engine.get("BUG-3")
            assert State(item.state) == State.CODING

    @pytest.mark.asyncio
    async def test_poll_without_project_uses_current_user_scan(self, mock_settings, engine):
        mock_settings.ones.project_id = ""
        defects = [{"uuid": "BUG-9", "name": "Mine only"}]

        with patch("src.core.scheduler.OnesAsyncClient") as MockClient, \
             patch("src.core.scheduler.Store") as MockStore:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.fetch_my_defects = AsyncMock(return_value=defects)
            MockClient.return_value = mock_client

            mock_store = MagicMock()
            mock_store.filter_new = MagicMock(return_value=defects)
            mock_store.update_check_time = MagicMock()
            MockStore.return_value = mock_store

            sched = Scheduler(mock_settings, engine)
            count = await sched.poll_now()

            assert count == 1
            mock_client.fetch_my_defects.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_poll_uses_iteration_from_project_mapping(self, mock_settings, engine):
        defects = [{"uuid": "BUG-10", "name": "Scoped by sprint"}]
        engine.add_project_repo("proj-1", "Project 1", "https://example.com/repo.git", "main", "sprint-7", "Sprint 7", "SPR-7")

        with patch("src.core.scheduler.OnesAsyncClient") as MockClient, \
             patch("src.core.scheduler.Store") as MockStore:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.fetch_my_defects = AsyncMock(return_value=defects)
            MockClient.return_value = mock_client

            mock_store = MagicMock()
            mock_store.filter_new = MagicMock(return_value=defects)
            mock_store.update_check_time = MagicMock()
            MockStore.return_value = mock_store

            sched = Scheduler(mock_settings, engine)
            count = await sched.poll_now()

            assert count == 1
            mock_client.fetch_my_defects.assert_awaited_once_with(project_id="proj-1", sprint_id="sprint-7")
