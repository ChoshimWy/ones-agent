"""Phase 2 测试 - ONES 异步客户端 + Webhook + 任务队列"""

import hashlib
import hmac
import json

import pytest
import httpx
import respx

from config.settings import OnesSettings, AgentSettings
from src.integrations.ones_api import OnesAsyncClient
from src.core.queue import TaskQueue


# ── OnesAsyncClient 测试 ──────────────────────────────────


class TestOnesAsyncClientGraphQL:
    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_defects(self):
        settings = OnesSettings(
            base_url="http://ones.test",
            email="",
            password="",
            team_id="team1",
            project_id="proj1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)

        gql_resp = {
            "data": {
                "buckets": [
                    {"key": "待处理", "tasks": [
                        {"uuid": "d1", "name": "Bug1", "status": {"name": "待处理"},
                         "priority": {"value": "高"}, "assign": {"name": "张三"},
                         "issueType": {"name": "缺陷"}, "project": {"name": "P"},
                         "createTime": 0, "deadline": None, "estimatedHours": 0,
                         "subTaskCount": 0, "subTaskDoneCount": 0},
                    ]}
                ]
            }
        }
        respx.post("http://ones.test/project/api/project/team/team1/items/graphql").mock(
            return_value=httpx.Response(200, json=gql_resp)
        )

        result = await client.fetch_defects()
        assert len(result) == 1
        assert result[0]["uuid"] == "d1"
        assert result[0]["_status_group"] == "待处理"
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_projects(self):
        settings = OnesSettings(
            base_url="http://ones.test", email="", password="", team_id="team1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)

        gql_resp = {
            "data": {
                "buckets": [
                    {"key": "active", "projects": [
                        {"uuid": "p1", "name": "Project1", "isArchive": False},
                    ]}
                ]
            }
        }
        respx.post("http://ones.test/project/api/project/team/team1/items/graphql").mock(
            return_value=httpx.Response(200, json=gql_resp)
        )

        result = await client.fetch_projects()
        assert len(result) == 1
        assert result[0]["name"] == "Project1"
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_add_comment(self):
        settings = OnesSettings(
            base_url="http://ones.test", email="", password="", team_id="team1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)

        respx.post("http://ones.test/project/api/project/team/team1/task/item1/comment").mock(
            return_value=httpx.Response(200, json={"id": "c1"})
        )

        result = await client.add_comment("item1", "分析完成")
        assert result["id"] == "c1"
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_update_status(self):
        settings = OnesSettings(
            base_url="http://ones.test", email="", password="", team_id="team1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)

        respx.patch("http://ones.test/project/api/project/team/team1/task/item1").mock(
            return_value=httpx.Response(200, json={"uuid": "item1", "status": "done"})
        )

        result = await client.update_status("item1", "done")
        assert result["status"] == "done"
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_team_members_uses_post(self):
        settings = OnesSettings(
            base_url="http://ones.test", email="", password="", team_id="team1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)
        client._org_uuid = "org1"

        respx.post("http://ones.test/project/api/project/organization/org1/users?team_uuid=team1").mock(
            return_value=httpx.Response(200, json=[{"uuid": "u1", "name": "Alice"}])
        )

        result = await client.fetch_team_members()
        assert result == [{"uuid": "u1", "name": "Alice"}]
        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_fetch_role_members_uses_project_endpoint(self):
        settings = OnesSettings(
            base_url="http://ones.test", email="", password="", team_id="team1",
            _env_file=None,
        )
        client = OnesAsyncClient(settings)

        respx.get("http://ones.test/project/api/project/team/team1/project/proj1/role_members").mock(
            return_value=httpx.Response(200, json=[{"uuid": "role-member-1"}])
        )

        result = await client.fetch_role_members("proj1")
        assert result == [{"uuid": "role-member-1"}]
        await client.close()


# ── Webhook 测试 ──────────────────────────────────────────


class TestWebhook:
    @pytest.mark.asyncio
    async def test_webhook_accepts_valid_payload(self):
        from httpx import ASGITransport, AsyncClient
        from main import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/webhook/ones", json={
                "work_item_id": "item-123",
                "type": "defect",
                "status_change": "new",
            })
        assert resp.status_code == 200
        assert resp.json()["work_item_id"] == "item-123"
        assert resp.json()["status"] == "queued"

    @pytest.mark.asyncio
    async def test_webhook_rejects_missing_id(self):
        from httpx import ASGITransport, AsyncClient
        from main import app

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post("/webhook/ones", json={"type": "defect"})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_webhook_validates_signature(self, monkeypatch):
        monkeypatch.setenv("AGENT_WEBHOOK_SECRET", "test-secret")
        from config.settings import AgentSettings
        from main import app, settings

        settings.agent = AgentSettings(webhook_secret="test-secret", _env_file=None)

        from httpx import ASGITransport, AsyncClient

        body = json.dumps({"work_item_id": "item-1", "type": "defect"}).encode()
        sig = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/webhook/ones",
                content=body,
                headers={"Content-Type": "application/json", "X-ONES-Signature": sig},
            )
        assert resp.status_code == 200

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.post(
                "/webhook/ones",
                content=body,
                headers={"Content-Type": "application/json", "X-ONES-Signature": "bad"},
            )
        assert resp.status_code == 401

        settings.agent = AgentSettings(_env_file=None)


# ── TaskQueue 测试 ──────────────────────────────────────────


class TestTaskQueue:
    @pytest.mark.asyncio
    async def test_enqueue_and_process(self):
        results = []
        queue = TaskQueue(max_workers=1)

        async def task(n):
            results.append(n)

        queue.start()
        await queue.enqueue(task(1))
        await queue.enqueue(task(2))
        import asyncio
        await asyncio.sleep(0.5)
        assert 1 in results
        assert 2 in results
        await queue.stop()

    @pytest.mark.asyncio
    async def test_queue_handles_errors(self):
        results = []
        queue = TaskQueue(max_workers=1)

        async def bad_task():
            raise ValueError("boom")

        async def good_task():
            results.append("ok")

        queue.start()
        await queue.enqueue(bad_task())
        await queue.enqueue(good_task())
        import asyncio
        await asyncio.sleep(0.5)
        assert "ok" in results
        await queue.stop()
