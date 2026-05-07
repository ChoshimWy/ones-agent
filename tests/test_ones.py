"""ones.py 测试 - mock HTTP 请求"""

from unittest.mock import MagicMock, patch

import pytest


# ── 登录流程 mock 数据 ──────────────────────────────────

LOGIN_RESP = {
    "org_users": [{
        "org_uuid": "DzjQUVNd",
        "org_user": {"org_user_uuid": "Q6kE8A2m"},
    }]
}

AUTHORIZE_HEADERS = {"Location": "http://x/auth?id=req123&other=1"}
CALLBACK_BODY = '<meta http-equiv="refresh" content="0;url=?code=auth_code_abc&state=x">'
TOKEN_RESP = {"access_token": "jwt_token_abc"}


def _mock_session():
    """创建一个 mock session，模拟完整登录流程"""
    session = MagicMock()
    session.headers = {}
    session.post.return_value.raise_for_status = MagicMock()

    authorize_resp = MagicMock()
    authorize_resp.headers = AUTHORIZE_HEADERS
    authorize_resp.raise_for_status = MagicMock()

    callback_resp = MagicMock()
    callback_resp.text = CALLBACK_BODY
    callback_resp.raise_for_status = MagicMock()

    token_resp = MagicMock()
    token_resp.json.return_value = TOKEN_RESP
    token_resp.raise_for_status = MagicMock()

    login_resp = MagicMock()
    login_resp.json.return_value = LOGIN_RESP
    login_resp.raise_for_status = MagicMock()

    # 登录流程: login → authorize → finalize → token
    session.post.side_effect = [login_resp, authorize_resp, MagicMock(), token_resp]
    session.get.side_effect = [MagicMock(), MagicMock(), callback_resp]

    return session


def _no_login_session():
    """不触发登录的 mock session（headers 已有 Authorization）"""
    session = MagicMock()
    session.headers = {"Authorization": "Bearer test_token"}
    return session


# ── GraphQL mock 数据 ───────────────────────────────────

GQL_DEFECTS_RESP = {"data": {
    "buckets": [
        {
            "key": "todo",
            "tasks": [
                {
                    "uuid": "defect-1", "name": "登录页面崩溃", "number": 101,
                    "status": {"uuid": "s1", "name": "待处理", "category": "todo"},
                    "priority": {"uuid": "p1", "value": "高", "position": 1},
                    "assign": {"uuid": "u1", "name": "张三", "avatar": ""},
                    "owner": {"uuid": "u2", "name": "李四", "avatar": ""},
                    "issueType": {"uuid": "it1", "name": "缺陷"},
                    "project": {"uuid": "proj1", "name": "主项目"},
                    "createTime": 1700000000, "deadline": "2026-05-01",
                    "estimatedHours": 4.0, "remainingManhour": 2.0,
                    "subTaskCount": 2, "subTaskDoneCount": 1,
                },
                {
                    "uuid": "defect-2", "name": "导出功能异常", "number": 102,
                    "status": {"uuid": "s2", "name": "进行中", "category": "in_progress"},
                    "priority": {"uuid": "p2", "value": "中", "position": 2},
                    "assign": {"uuid": "u3", "name": "王五", "avatar": ""},
                    "owner": {"uuid": "u2", "name": "李四", "avatar": ""},
                    "issueType": {"uuid": "it1", "name": "缺陷"},
                    "project": {"uuid": "proj1", "name": "主项目"},
                    "createTime": 1700001000, "deadline": None,
                    "estimatedHours": 2.0, "remainingManhour": 1.5,
                    "subTaskCount": 0, "subTaskDoneCount": 0,
                },
            ],
            "pageInfo": {"count": 2, "totalCount": 2, "hasNextPage": False, "endCursor": "end"},
        },
        {
            "key": "done",
            "tasks": [
                {
                    "uuid": "defect-3", "name": "已修复的Bug", "number": 99,
                    "status": {"uuid": "s3", "name": "已完成", "category": "done"},
                    "priority": {"uuid": "p3", "value": "低", "position": 3},
                    "assign": {"uuid": "u1", "name": "张三", "avatar": ""},
                    "owner": {"uuid": "u1", "name": "张三", "avatar": ""},
                    "issueType": {"uuid": "it1", "name": "缺陷"},
                    "project": {"uuid": "proj1", "name": "主项目"},
                    "createTime": 1699990000, "deadline": None,
                    "estimatedHours": 1.0, "remainingManhour": 0,
                    "subTaskCount": 0, "subTaskDoneCount": 0,
                },
            ],
            "pageInfo": {"count": 1, "totalCount": 1, "hasNextPage": False, "endCursor": "end2"},
        },
    ]
}}


GQL_PROJECTS_RESP = {"data": {
    "buckets": [
        {
            "key": "active",
            "projects": [
                {
                    "uuid": "proj1", "name": "主项目", "icon": "icon1",
                    "status": {"uuid": "st1", "name": "进行中", "category": "active"},
                    "isPin": True, "isArchive": False,
                    "assign": {"uuid": "u1", "name": "张三", "avatar": ""},
                    "owner": {"uuid": "u2", "name": "李四", "avatar": ""},
                    "createTime": 1700000000,
                    "planStartTime": "2026-01-01", "planEndTime": "2026-06-01",
                    "type": "scrum",
                },
                {
                    "uuid": "proj2", "name": "工具项目", "icon": "icon2",
                    "status": {"uuid": "st2", "name": "待启动", "category": "planning"},
                    "isPin": False, "isArchive": False,
                    "assign": None, "owner": {"uuid": "u1", "name": "张三", "avatar": ""},
                    "createTime": 1700001000,
                    "planStartTime": None, "planEndTime": None,
                    "type": "kanban",
                },
            ],
            "pageInfo": {"count": 2, "totalCount": 2, "hasNextPage": False},
        },
    ]
}}


class TestOnesClientLogin:
    @patch("src.integrations.ones.requests.Session")
    def test_login_sets_bearer_token(self, MockSession):
        """登录成功后设置 Authorization 头"""
        mock_session = _mock_session()
        MockSession.return_value = mock_session

        from src.integrations.ones import OnesClient
        client = OnesClient(email="test@test.com", password="pass")

        assert mock_session.headers["Authorization"] == "Bearer jwt_token_abc"

    @patch("src.integrations.ones.requests.Session")
    def test_login_without_credentials_skips_auth(self, MockSession):
        """无账号密码时不尝试登录"""
        mock_session = MagicMock()
        mock_session.headers = {}
        MockSession.return_value = mock_session

        from src.integrations.ones import OnesClient
        client = OnesClient(email="", password="")

        assert "Authorization" not in mock_session.headers


class TestOnesClientFetchDefects:
    def _make_client(self, MockSession, gql_resp):
        """创建已认证的 client，避免触发登录"""
        mock_session = _no_login_session()
        mock_session.post.return_value.json.return_value = gql_resp
        mock_session.post.return_value.raise_for_status = MagicMock()
        MockSession.return_value = mock_session

        from src.integrations.ones import OnesClient
        return OnesClient(email="", password="")

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_defects_returns_flattened_tasks(self, MockSession):
        """fetch_defects 展平 buckets 返回所有 task"""
        client = self._make_client(MockSession, GQL_DEFECTS_RESP)
        defects = client.fetch_defects()

        assert len(defects) == 3
        assert defects[0]["uuid"] == "defect-1"
        assert defects[0]["_status_group"] == "todo"
        assert defects[2]["_status_group"] == "done"

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_defects_with_project_filter(self, MockSession):
        """fetch_defects 传入 project_id 构建 filterGroup"""
        client = self._make_client(MockSession, {"data": {"buckets": []}})
        client.fetch_defects(project_id="proj1")

        call_args = MockSession.return_value.post.call_args
        body = call_args.kwargs.get("json") or call_args[1].get("json")
        filter_group = body["variables"]["filterGroup"]
        # filterGroup should contain project_in
        assert any("project_in" in f for f in filter_group)

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_defects_empty_response(self, MockSession):
        """空响应返回空列表"""
        client = self._make_client(MockSession, {"data": {}})
        assert client.fetch_defects() == []

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_issue_detail_by_uuid(self, MockSession):
        """fetch_issue_detail 按 uuid 查找"""
        client = self._make_client(MockSession, GQL_DEFECTS_RESP)
        result = client.fetch_issue_detail("defect-2")

        assert result["uuid"] == "defect-2"
        assert result["name"] == "导出功能异常"

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_issue_detail_not_found(self, MockSession):
        """fetch_issue_detail 找不到返回空 dict"""
        client = self._make_client(MockSession, GQL_DEFECTS_RESP)
        assert client.fetch_issue_detail("non-exist") == {}

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_my_defects_passes_current_user(self, MockSession):
        """fetch_my_defects 使用 $currentUser 过滤"""
        client = self._make_client(MockSession, {"data": {"buckets": []}})
        client.fetch_my_defects()

        call_args = MockSession.return_value.post.call_args
        body = call_args.kwargs.get("json") or call_args[1].get("json")
        filter_group = body["variables"]["filterGroup"]
        # filterGroup 包含 assign_in 条件（可能有其他条件如 project_in）
        assert len(filter_group) > 0
        assert filter_group[0].get("assign_in") == ["$currentUser"]


class TestOnesClientFetchProjects:
    def _make_client(self, MockSession, gql_resp):
        mock_session = _no_login_session()
        mock_session.post.return_value.json.return_value = gql_resp
        mock_session.post.return_value.raise_for_status = MagicMock()
        MockSession.return_value = mock_session
        from src.integrations.ones import OnesClient
        return OnesClient(email="", password="")

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_projects_returns_list(self, MockSession):
        """fetch_projects 展平 buckets 返回项目列表"""
        client = self._make_client(MockSession, GQL_PROJECTS_RESP)
        projects = client.fetch_projects()

        assert len(projects) == 2
        assert projects[0]["uuid"] == "proj1"
        assert projects[0]["name"] == "主项目"
        assert projects[0]["_group"] == "active"

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_projects_uses_correct_endpoint(self, MockSession):
        """fetch_projects 使用 projects-group-list-for-project-view 端点"""
        client = self._make_client(MockSession, GQL_PROJECTS_RESP)
        client.fetch_projects()

        call_args = MockSession.return_value.post.call_args
        url = call_args[0][0] if call_args[0] else call_args.kwargs.get("url", "")
        assert "projects-group-list-for-project-view" in url

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_projects_filters_archived_by_default(self, MockSession):
        """默认过滤已归档项目"""
        client = self._make_client(MockSession, {"data": {"buckets": []}})
        client.fetch_projects()

        call_args = MockSession.return_value.post.call_args
        body = call_args.kwargs.get("json") or call_args[1].get("json")
        filters = body["variables"]["projectFilterGroup"][0]
        assert {"isArchive_equal": False} in filters

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_projects_includes_archived_when_requested(self, MockSession):
        """include_archived=True 时不过滤归档"""
        client = self._make_client(MockSession, {"data": {"buckets": []}})
        client.fetch_projects(include_archived=True)

        call_args = MockSession.return_value.post.call_args
        body = call_args.kwargs.get("json") or call_args[1].get("json")
        filters = body["variables"]["projectFilterGroup"][0]
        assert not any("isArchive" in str(f) for f in filters)

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_projects_empty_response(self, MockSession):
        """空响应返回空列表"""
        client = self._make_client(MockSession, {"data": {}})
        assert client.fetch_projects() == []
