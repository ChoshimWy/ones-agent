"""server.py 测试 - MCP tools（纯数据提供者）"""

from unittest.mock import MagicMock

import pytest

import server
from server import (
    fetch_defects,
    fetch_my_defects,
    check_new_defects,
    get_defect_detail,
    list_projects,
    search_codebase,
    push_to_wechat,
)


MOCK_DEFECTS = [
    {"uuid": "d1", "name": "Bug1", "status": {"name": "待处理"}, "priority": {"value": "高"},
     "assign": {"name": "张三"}, "issueType": {"name": "缺陷"}, "project": {"name": "P"},
     "createTime": 0, "deadline": None, "estimatedHours": 0, "subTaskCount": 0, "subTaskDoneCount": 0},
]

MOCK_PROJECTS = [
    {"uuid": "p1", "name": "Project1", "isArchive": False},
]


@pytest.fixture(autouse=True)
def _mock_deps():
    """替换 server 模块的依赖为 mock"""
    mock_ones = MagicMock()
    mock_store = MagicMock()
    mock_codebase = MagicMock()
    mock_bot = MagicMock()

    orig = (server._ones, server._store, server._codebase, server._bot)
    server._ones, server._store, server._codebase, server._bot = mock_ones, mock_store, mock_codebase, mock_bot
    yield mock_ones, mock_store, mock_codebase, mock_bot
    server._ones, server._store, server._codebase, server._bot = orig


class TestFetchDefectsTool:
    def test_fetch_all_defects(self, _mock_deps):
        mock_ones, _, _, _ = _mock_deps
        mock_ones.fetch_defects.return_value = MOCK_DEFECTS

        result = fetch_defects(limit=10)

        mock_ones.fetch_defects.assert_called_once_with(project_id=None, limit=10)
        assert result == MOCK_DEFECTS

    def test_fetch_my_defects_flag(self, _mock_deps):
        mock_ones, _, _, _ = _mock_deps
        mock_ones.fetch_my_defects.return_value = MOCK_DEFECTS

        result = fetch_defects(mine=True)

        mock_ones.fetch_my_defects.assert_called_once_with(limit=50)
        assert result == MOCK_DEFECTS


class TestFetchMyDefectsTool:
    def test_fetch_my_defects(self, _mock_deps):
        mock_ones, _, _, _ = _mock_deps
        mock_ones.fetch_my_defects.return_value = MOCK_DEFECTS

        result = fetch_my_defects()

        mock_ones.fetch_my_defects.assert_called_once()
        assert result == MOCK_DEFECTS


class TestCheckNewDefectsTool:
    def test_check_new_filters_and_updates(self, _mock_deps):
        mock_ones, mock_store, _, _ = _mock_deps
        mock_ones.fetch_my_defects.return_value = MOCK_DEFECTS
        mock_store.filter_new.return_value = MOCK_DEFECTS

        result = check_new_defects(mine=True)

        mock_store.filter_new.assert_called_once_with(MOCK_DEFECTS)
        mock_store.update_check_time.assert_called_once()
        assert result == MOCK_DEFECTS

    def test_check_new_not_mine(self, _mock_deps):
        mock_ones, mock_store, _, _ = _mock_deps
        mock_ones.fetch_defects.return_value = MOCK_DEFECTS
        mock_store.filter_new.return_value = []

        result = check_new_defects(mine=False)

        mock_ones.fetch_defects.assert_called_once()
        assert result == []


class TestGetDefectDetailTool:
    def test_get_defect_detail(self, _mock_deps):
        mock_ones, _, _, _ = _mock_deps
        mock_ones.fetch_issue_detail.return_value = MOCK_DEFECTS[0]

        result = get_defect_detail("d1")

        mock_ones.fetch_issue_detail.assert_called_once_with("d1")
        assert result == MOCK_DEFECTS[0]


class TestListProjectsTool:
    def test_list_projects(self, _mock_deps):
        mock_ones, _, _, _ = _mock_deps
        mock_ones.fetch_projects.return_value = MOCK_PROJECTS

        result = list_projects()

        mock_ones.fetch_projects.assert_called_once_with(include_archived=False)
        assert result == MOCK_PROJECTS

    def test_list_projects_include_archived(self, _mock_deps):
        mock_ones, _, _, _ = _mock_deps
        mock_ones.fetch_projects.return_value = MOCK_PROJECTS

        list_projects(include_archived=True)

        mock_ones.fetch_projects.assert_called_once_with(include_archived=True)


class TestSearchCodebaseTool:
    def test_search_no_codebase(self, _mock_deps):
        _, _, _, _ = _mock_deps
        server._codebase = None

        result = search_codebase()

        assert "未配置" in result

    def test_search_by_query(self, _mock_deps):
        _, _, mock_cb, _ = _mock_deps
        mock_cb.search_keywords.return_value = {"src/app.py": "print('hi')"}

        result = search_codebase(query="login")

        mock_cb.search_keywords.assert_called_once_with(["login"], max_files=10)
        assert "src/app.py" in result

    def test_search_read_file(self, _mock_deps):
        _, _, mock_cb, _ = _mock_deps
        mock_cb.read_file.return_value = "print('hello')"

        result = search_codebase(read_file="src/main.py")

        mock_cb.read_file.assert_called_once_with("src/main.py")
        assert "print" in result

    def test_search_tree(self, _mock_deps):
        _, _, mock_cb, _ = _mock_deps
        mock_cb.tree.return_value = "src/\n  main.py"

        result = search_codebase()

        mock_cb.tree.assert_called_once_with(max_depth=3)


class TestPushToWechatTool:
    def test_push_sends_markdown(self, _mock_deps):
        _, _, _, mock_bot = _mock_deps

        result = push_to_wechat("## Alert")

        mock_bot.send_markdown.assert_called_once_with("## Alert")
        assert "已发送" in result
