"""agent.py 测试"""

from unittest.mock import MagicMock, patch

import pytest

from src.core.agent import DefectAgent

MOCK_DEFECTS = [
    {"uuid": "d1", "name": "Bug1", "status": {"name": "待处理"}, "priority": {"value": "高"},
     "assign": {"name": "张三"}, "issueType": {"name": "缺陷"}, "project": {"name": "P"},
     "createTime": 0, "deadline": None, "estimatedHours": 0, "subTaskCount": 0, "subTaskDoneCount": 0},
]

MOCK_RESULTS = [
    {"id": "d1", "title": "Bug1", "status": "待处理", "priority": "高",
     "assignee": "张三", "analysis": "根因: X"},
]


class TestDefectAgentCheckNew:
    def test_check_new_filters_seen(self, tmp_path):
        from src.core.store import Store
        store = Store(tmp_path / "test.json")
        mock_ones = MagicMock()
        mock_ones.fetch_my_defects.return_value = MOCK_DEFECTS

        agent = DefectAgent(ones=mock_ones, store=store)
        new = agent.check_new()
        assert len(new) == 1

        # 第二次应为空
        new2 = agent.check_new()
        assert len(new2) == 0

    def test_check_new_updates_store(self, tmp_path):
        from src.core.store import Store
        store = Store(tmp_path / "test.json")
        mock_ones = MagicMock()
        mock_ones.fetch_my_defects.return_value = MOCK_DEFECTS

        agent = DefectAgent(ones=mock_ones, store=store)
        agent.check_new()
        assert store.seen_count == 1
        assert store.last_check is not None


class TestDefectAgentRunOnce:
    def test_run_once_incremental(self, tmp_path):
        from src.core.store import Store
        store = Store(tmp_path / "test.json")
        mock_ones = MagicMock()
        mock_ones.fetch_my_defects.return_value = MOCK_DEFECTS
        mock_analyzer = MagicMock()
        mock_analyzer.batch_analyze.return_value = MOCK_RESULTS
        mock_bot = MagicMock()
        mock_bot.send_defect_report.return_value = {"errcode": 0}

        agent = DefectAgent(ones=mock_ones, analyzer=mock_analyzer, bot=mock_bot, store=store)
        results = agent.run_once(push=True)
        assert results == MOCK_RESULTS

        # 第二次应无新缺陷
        results2 = agent.run_once(push=True)
        assert results2 == []
        mock_bot.send_text.assert_called()

    def test_run_once_with_codebase(self, tmp_path):
        from src.core.store import Store
        store = Store(tmp_path / "test.json")
        mock_ones = MagicMock()
        mock_ones.fetch_my_defects.return_value = MOCK_DEFECTS
        mock_analyzer = MagicMock()
        mock_analyzer.batch_analyze.return_value = MOCK_RESULTS
        mock_bot = MagicMock()
        mock_bot.send_defect_report.return_value = {"errcode": 0}
        mock_codebase = MagicMock()

        agent = DefectAgent(
            ones=mock_ones, analyzer=mock_analyzer, bot=mock_bot,
            store=store, codebase=mock_codebase,
        )
        results = agent.run_once(push=True)
        # 分析器应传入 codebase
        mock_analyzer.batch_analyze.assert_called_once_with(MOCK_DEFECTS, codebase=mock_codebase)


class TestQuickReport:
    @patch("src.core.agent.DefectAgent.run_once")
    def test_quick_report(self, mock_run):
        mock_run.return_value = MOCK_RESULTS
        result = DefectAgent.quick_report(mine=True, push=False)
        mock_run.assert_called_once_with(mine=True, push=False)
