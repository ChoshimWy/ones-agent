"""ones.py 测试 - mock HTTP 请求"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from src.integrations.ones_api import _defect_detail_summary
from src.integrations.ones import (
    GQL_FETCH_TASKS,
    OnesPaginationError,
    _defect_detail_summary as _sync_defect_detail_summary,
)


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text


class _InvalidKeyError(Exception):
    def __init__(self):
        super().__init__("Invalid item key")
        self.response = _FakeResponse('{"code":400,"errcode":"InvalidParameter.Item.Key.InvalidFormat","field":"Key","graphql_path":["task","Task"],"model":"Item","reason":"InvalidFormat","type":"InvalidParameter"}')


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


GQL_DETAIL_RESP = {"data": {
    "task": {
        "uuid": "defect-2",
        "name": "导出功能异常",
        "number": 102,
        "status": {"uuid": "s2", "name": "进行中", "category": "in_progress"},
        "priority": {"uuid": "p2", "value": "中", "position": 2},
        "assign": {"uuid": "u3", "name": "王五", "avatar": ""},
        "owner": {"uuid": "u2", "name": "李四", "avatar": ""},
        "issueType": {"uuid": "it1", "name": "缺陷"},
        "project": {"uuid": "proj1", "name": "主项目"},
        "createTime": 1700001000,
        "deadline": None,
        "serverUpdateStamp": 1700001100,
        "path": "task-EmmYk6yxh3cHOZvI",
    }
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


def test_defect_detail_summary_does_not_log_business_fields():
    summary = _defect_detail_summary({
        "uuid": "uuid-1",
        "key": "task-key-1",
        "name": "sensitive title",
        "description": "sensitive description",
        "assign": {"name": "Alice"},
        "project": {"name": "Secret Project"},
    })

    assert summary == {"has_uuid": True, "has_key": True, "field_count": 6}
    assert "sensitive title" not in str(summary)
    assert "Alice" not in str(summary)


def test_sync_defect_detail_summary_does_not_log_business_fields():
    summary = _sync_defect_detail_summary({
        "uuid": "uuid-1",
        "key": "task-key-1",
        "name": "sensitive title",
        "assign": {"name": "Alice"},
    })

    assert summary == {"has_uuid": True, "has_key": True, "field_count": 4}


class TestOnesClientLogin:
    @patch("src.integrations.ones.requests.Session")
    def test_oauth_redirect_uri_follows_base_url(self, MockSession):
        MockSession.return_value = MagicMock()

        from src.integrations.ones import OnesClient

        client = OnesClient(
            base_url="http://ones.test:8088",
            email="",
            password="",
        )

        assert client._oauth_redirect_uri() == "http://ones.test:8088/auth/authorize/callback"

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


class TestOnesClientTaskStatuses:
    @patch("src.integrations.ones.requests.Session")
    def test_fetch_task_status_configs_uses_project_filter(self, MockSession):
        session = MagicMock()
        response = MagicMock()
        response.json.return_value = {
            "task_status_configs": [{"project_uuid": "proj1", "status_uuid": "status-1"}],
        }
        session.post.return_value = response
        MockSession.return_value = session

        from src.integrations.ones import OnesClient

        client = OnesClient(
            base_url="http://ones.test",
            email="",
            password="",
            team_id="team1",
        )
        result = client.fetch_task_status_configs(["proj1"])

        assert result == [{"project_uuid": "proj1", "status_uuid": "status-1"}]
        session.post.assert_called_once_with(
            "http://ones.test/project/api/project/team/team1/task_statuses",
            json={"project_uuids": ["proj1"]},
        )
        response.raise_for_status.assert_called_once_with()

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_task_status_definitions_uses_get(self, MockSession):
        session = MagicMock()
        response = MagicMock()
        response.json.return_value = {
            "task_statuses": [{"uuid": "status-1", "name": "待处理"}],
        }
        session.get.return_value = response
        MockSession.return_value = session

        from src.integrations.ones import OnesClient

        client = OnesClient(
            base_url="http://ones.test",
            email="",
            password="",
            team_id="team1",
        )
        result = client.fetch_task_status_definitions()

        assert result == [{"uuid": "status-1", "name": "待处理"}]
        session.get.assert_called_once_with(
            "http://ones.test/project/api/project/team/team1/task_statuses",
        )
        response.raise_for_status.assert_called_once_with()


class TestOnesClientFetchDefects:
    def test_fetch_tasks_query_includes_sprint_identity(self):
        assert "sprint { uuid name }" in GQL_FETCH_TASKS

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
    def test_fetch_defects_with_status_uuid_filter(self, MockSession):
        """fetch_defects 传入 status uuid 构建 status_in"""
        client = self._make_client(MockSession, {"data": {"buckets": []}})
        client.fetch_defects(project_id="proj1", status_in=["status-uuid-1"])

        call_args = MockSession.return_value.post.call_args
        body = call_args.kwargs.get("json") or call_args[1].get("json")
        filter_group = body["variables"]["filterGroup"]
        assert filter_group[0].get("status_in") == ["status-uuid-1"]

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_defects_empty_response(self, MockSession):
        """空响应返回空列表"""
        client = self._make_client(MockSession, {"data": {}})
        assert client.fetch_defects() == []

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_defects_paginates_deduplicates_and_preserves_status(self, MockSession):
        client = self._make_client(MockSession, {"data": {"buckets": []}})
        first = MagicMock()
        first.raise_for_status = MagicMock()
        first.json.return_value = {"data": {"buckets": [{
            "key": "tasks",
            "tasks": [
                {"uuid": "d1", "status": {"uuid": "s1", "category": "todo"}},
                {"uuid": "d2", "status": {"uuid": "s2", "category": "doing"}},
            ],
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
        }]}}
        second = MagicMock()
        second.raise_for_status = MagicMock()
        second.json.return_value = {"data": {"buckets": [{
            "key": "tasks",
            "tasks": [
                {"uuid": "d2", "status": {"uuid": "s2", "category": "doing"}},
                {"uuid": "d3", "status": {"uuid": "s3", "category": "pending"}},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": "cursor-2"},
        }]}}
        client.session.post.side_effect = [first, second]

        defects = client.fetch_defects(limit=10, page_size=2)

        assert [item["uuid"] for item in defects] == ["d1", "d2", "d3"]
        assert defects[2]["status"]["category"] == "pending"
        variables = [call.kwargs["json"]["variables"] for call in client.session.post.call_args_list]
        assert [item["pagination"] for item in variables] == [
            {"limit": 2, "after": "", "preciseCount": True},
            {"limit": 2, "after": "cursor-1", "preciseCount": True},
        ]
        assert all(item["groupBy"] == {"tasks": {}} for item in variables)

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_defects_rejects_unstable_next_page_cursor(self, MockSession):
        response = {"data": {"buckets": [{
            "key": "tasks",
            "tasks": [{"uuid": "d1", "status": {"uuid": "s1"}}],
            "pageInfo": {"hasNextPage": True, "endCursor": ""},
        }]}}
        client = self._make_client(MockSession, response)

        with pytest.raises(OnesPaginationError, match="cursor"):
            client.fetch_defects(limit=10, page_size=2)

    @pytest.mark.parametrize(
        "bucket",
        [
            {"key": "tasks", "tasks": [{"uuid": "d1"}]},
            {"key": "tasks", "tasks": [{"uuid": "d1"}], "pageInfo": []},
            {"key": "tasks", "tasks": [{"uuid": "d1"}], "pageInfo": {"endCursor": "a"}},
            {
                "key": "tasks",
                "tasks": [{"uuid": "d1"}],
                "pageInfo": {"hasNextPage": "false", "endCursor": "a"},
            },
        ],
    )
    @patch("src.integrations.ones.requests.Session")
    def test_fetch_defects_rejects_indeterminate_page_info(self, MockSession, bucket):
        client = self._make_client(MockSession, {"data": {"buckets": [bucket]}})

        with pytest.raises(OnesPaginationError, match="pageInfo|hasNextPage"):
            client.fetch_defects(limit=10, page_size=2)

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_defects_rejects_cursor_cycles(self, MockSession):
        client = self._make_client(MockSession, {"data": {"buckets": []}})
        responses = []
        for uuid, cursor in (("d1", "a"), ("d2", "b"), ("d3", "a")):
            response = MagicMock()
            response.raise_for_status = MagicMock()
            response.json.return_value = {"data": {"buckets": [{
                "key": "tasks",
                "tasks": [{"uuid": uuid}],
                "pageInfo": {"hasNextPage": True, "endCursor": cursor},
            }]}}
            responses.append(response)
        client.session.post.side_effect = responses

        with pytest.raises(OnesPaginationError, match="cursor"):
            client.fetch_defects(limit=10, page_size=2)

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_issue_detail_by_uuid(self, MockSession):
        """fetch_issue_detail 走独立 Task GraphQL 详情接口"""
        client = self._make_client(MockSession, GQL_DETAIL_RESP)
        result = client.fetch_issue_detail("defect-2")

        assert result["uuid"] == "defect-2"
        assert result["name"] == "导出功能异常"
        call_args = MockSession.return_value.post.call_args
        url = call_args[0][0] if call_args[0] else call_args.kwargs.get("url", "")
        body = call_args.kwargs.get("json") or call_args[1].get("json")
        assert "/items/graphql?t=Task" in url
        assert body["variables"]["key"] == "defect-2"
        assert "task(key: $key)" in body["query"]
        assert "...TaskHeader_task1" in body["query"]
        assert "...TaskFieldList_task2" in body["query"]

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_issue_detail_not_found(self, MockSession):
        """fetch_issue_detail 找不到返回空 dict"""
        client = self._make_client(MockSession, GQL_DEFECTS_RESP)
        assert client.fetch_issue_detail("non-exist") == {}

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_issue_detail_falls_back_to_list_lookup_for_invalid_key(self, MockSession):
        """当 task(key) 失败时按 uuid -> key 退回详情查询"""
        mock_session = _no_login_session()
        MockSession.return_value = mock_session

        from src.integrations.ones import OnesClient

        client = OnesClient(email="", password="")
        client._graphql = MagicMock(side_effect=[
            _InvalidKeyError(),
            {"buckets": [{
                "tasks": [{"uuid": "3MbYY7DgIG4hP799", "key": "task-EmmYk6yxh3cHOZvI"}],
                "pageInfo": {"hasNextPage": False},
            }]},
            {"task": {"uuid": "3MbYY7DgIG4hP799", "key": "task-EmmYk6yxh3cHOZvI", "name": "detail"}},
        ])

        result = client.fetch_issue_detail("3MbYY7DgIG4hP799")

        assert result["key"] == "task-EmmYk6yxh3cHOZvI"
        assert client._graphql.call_count == 3

    @patch("src.integrations.ones.requests.Session")
    def test_fetch_my_defects_passes_current_user(self, MockSession):
        """fetch_my_defects 使用 $currentUser 过滤"""
        client = self._make_client(MockSession, {"data": {"buckets": []}})
        client.fetch_my_defects()

        call_args = MockSession.return_value.post.call_args
        body = call_args.kwargs.get("json") or call_args[1].get("json")
        filter_group = body["variables"]["filterGroup"]
        assert len(filter_group) > 0
        assert filter_group[0].get("assign_in") == ["$currentUser"]


class TestOnesClientWiki:
    @pytest.mark.parametrize("bad_id", ["", ".", "..", "a/b", "a\\b", "a%b", "a?b", "a#b", "a b", "\tbad", "中文"])
    @patch("src.integrations.ones.requests.Session")
    def test_wiki_read_rejects_unsafe_segments_without_request(self, MockSession, bad_id):
        session = MagicMock()
        MockSession.return_value = session
        from src.integrations.ones import OnesClient

        client = OnesClient(base_url="https://ones.test", team_id="team-1", email="", password="")

        with pytest.raises(ValueError, match="Wiki"):
            client.fetch_wiki_page(bad_id, "page-1")
        session.get.assert_not_called()

    @patch("src.integrations.ones.requests.Session")
    def test_wiki_read_methods_reuse_session_and_use_exact_paths(self, MockSession):
        session = MagicMock()
        MockSession.return_value = session
        responses = []
        for payload in ({"content": "body"}, {"title": "Page"}, {"pages": []}):
            response = MagicMock()
            response.json.return_value = payload
            responses.append(response)
        session.get.side_effect = responses
        from src.integrations.ones import OnesClient

        client = OnesClient(base_url="https://ones.test", team_id="team-1", email="", password="")

        with capture_logs() as logs:
            assert client.fetch_wiki_page("space-1", "page-1") == {"content": "body"}
            assert client.fetch_wiki_page_info("page-1") == {"title": "Page"}
            assert client.fetch_wiki_pages_with_history("space-1") == {"pages": []}
        assert [call.args[0] for call in session.get.call_args_list] == [
            "https://ones.test/wiki/api/wiki/team/team-1/space/space-1/page/page-1",
            "https://ones.test/wiki/api/wiki/team/team-1/page/page-1/detail",
            "https://ones.test/wiki/api/wiki/team/team-1/space/space-1/pages_with_history",
        ]
        assert all(response.raise_for_status.called for response in responses)
        MockSession.assert_called_once_with()
        allowed = {"event", "log_level", "team_id", "space_id", "page_id", "status"}
        assert logs and all(set(entry) <= allowed for entry in logs)

    @patch("src.integrations.ones.requests.Session")
    def test_wiki_read_rejects_non_mapping_json(self, MockSession):
        response = MagicMock()
        response.json.return_value = ["not", "mapping"]
        MockSession.return_value.get.return_value = response
        from src.integrations.ones import OnesClient, OnesPayloadError

        client = OnesClient(base_url="https://ones.test", team_id="team-1", email="", password="")

        with pytest.raises(OnesPayloadError, match="mapping"):
            client.fetch_wiki_page("space-1", "page-1")

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


class TestOnesAsyncClientFetchIssueDetail:
    @pytest.mark.asyncio
    async def test_oauth_redirect_uri_follows_base_url(self):
        from config.settings import OnesSettings
        from src.integrations.ones_api import OnesAsyncClient

        client = OnesAsyncClient(
            OnesSettings(
                base_url="http://ones.test:8088",
                email="",
                password="",
                _env_file=None,
            ),
        )

        assert client._oauth_redirect_uri() == "http://ones.test:8088/auth/authorize/callback"

    @pytest.mark.asyncio
    async def test_first_use_waits_for_shared_login(self):
        from config.settings import OnesSettings
        from src.integrations.ones_api import OnesAsyncClient

        client = OnesAsyncClient(
            OnesSettings(
                base_url="http://ones.test",
                team_id="team1",
                email="user@example.com",
                password="secret",
                _env_file=None,
            ),
        )
        login_started = asyncio.Event()
        release_login = asyncio.Event()
        login_calls = 0

        async def delayed_login(_client=None):
            nonlocal login_calls
            login_calls += 1
            login_started.set()
            await release_login.wait()

        client._login = delayed_login
        first = asyncio.create_task(client._get_client())
        await login_started.wait()
        second = asyncio.create_task(client._get_client())
        await asyncio.sleep(0)

        assert not second.done(), "concurrent callers must wait for authentication"

        release_login.set()
        first_client, second_client = await asyncio.gather(first, second)
        assert first_client is second_client
        assert login_calls == 1
        await client.close()

    @pytest.mark.asyncio
    async def test_graphql_reauthenticates_once_after_401(self):
        import httpx

        from config.settings import OnesSettings
        from src.integrations.ones_api import OnesAsyncClient

        client = OnesAsyncClient(
            OnesSettings(
                base_url="http://ones.test",
                team_id="team1",
                email="user@example.com",
                password="secret",
                _env_file=None,
            ),
        )
        request = httpx.Request("POST", "http://ones.test/graphql")
        fake_http_client = MagicMock()
        fake_http_client.post = AsyncMock(
            side_effect=[
                httpx.Response(401, request=request, json={"code": 401}),
                httpx.Response(200, request=request, json={"data": {"ok": True}}),
            ],
        )
        client._client = fake_http_client
        client._ready = True
        client._auth_generation = 1
        client._login = AsyncMock()

        result = await client._graphql("query Example { ok }", {})

        assert result == {"ok": True}
        client._login.assert_awaited_once_with(fake_http_client)
        assert fake_http_client.post.await_count == 2

    @pytest.mark.asyncio
    @patch("src.integrations.ones_api.httpx.AsyncClient")
    async def test_fetch_issue_detail_falls_back_to_list_lookup_for_invalid_key(self, MockAsyncClient):
        mock_httpx_client = MagicMock()
        mock_httpx_client.headers = {}
        MockAsyncClient.return_value = mock_httpx_client

        from config.settings import OnesSettings
        from src.integrations.ones_api import OnesAsyncClient

        client = OnesAsyncClient(OnesSettings(base_url="http://aputureones.com:8088", team_id="3WSQZdnB", email="", password=""))
        client._graphql = AsyncMock(side_effect=[
            _InvalidKeyError(),
            {"buckets": [{
                "tasks": [{"uuid": "3MbYY7DgIG4hP799", "key": "task-EmmYk6yxh3cHOZvI"}],
                "pageInfo": {"hasNextPage": False},
            }]},
            {"task": {"uuid": "3MbYY7DgIG4hP799", "key": "task-EmmYk6yxh3cHOZvI", "name": "detail"}},
        ])

        result = await client.fetch_issue_detail("3MbYY7DgIG4hP799")

        assert result["key"] == "task-EmmYk6yxh3cHOZvI"
        assert client._graphql.call_count == 3
