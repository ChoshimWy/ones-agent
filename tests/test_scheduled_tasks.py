"""定时任务 + AI Trigger 端点测试"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from main import app
    return TestClient(app)


@pytest.fixture
def admin_headers(client):
    resp = client.post("/api/v1/auth/login", json={"email": "admin@aputure.com", "password": "admin"})
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def viewer_headers(client):
    resp = client.post("/api/v1/auth/login", json={"email": "Viewer", "password": "viewer"})
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


class TestScheduledTasksCRUD:
    def test_list_empty(self, client, admin_headers):
        resp = client.get("/api/v1/scheduled-tasks", headers=admin_headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_create_task(self, client, admin_headers):
        resp = client.post("/api/v1/scheduled-tasks", json={
            "name": "Daily Defect Scan",
            "cronExpr": "0 9 * * *",
            "projectId": "proj-1",
            "itemType": "defect",
            "action": "plan",
            "notifyEmails": "dev@test.com",
            "notifyWechat": True,
            "enabled": True,
        }, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Daily Defect Scan"
        assert data["cronExpr"] == "0 9 * * *"
        assert data["projectId"] == "proj-1"
        assert data["itemType"] == "defect"
        assert data["action"] == "plan"
        assert data["notifyEmails"] == "dev@test.com"
        assert data["notifyWechat"] is True
        assert data["enabled"] is True
        assert data["id"]

    def test_get_task(self, client, admin_headers):
        create = client.post("/api/v1/scheduled-tasks", json={
            "name": "Test Task",
            "cronExpr": "30m",
        }, headers=admin_headers)
        task_id = create.json()["id"]

        resp = client.get(f"/api/v1/scheduled-tasks/{task_id}", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "Test Task"

    def test_get_task_not_found(self, client, admin_headers):
        resp = client.get("/api/v1/scheduled-tasks/nonexistent", headers=admin_headers)
        assert resp.status_code == 404

    def test_update_task(self, client, admin_headers):
        create = client.post("/api/v1/scheduled-tasks", json={
            "name": "Old Name",
            "cronExpr": "1h",
        }, headers=admin_headers)
        task_id = create.json()["id"]

        resp = client.put(f"/api/v1/scheduled-tasks/{task_id}", json={
            "name": "New Name",
            "cronExpr": "2h",
            "enabled": False,
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "New Name"
        assert resp.json()["cronExpr"] == "2h"
        assert resp.json()["enabled"] is False

    def test_update_task_not_found(self, client, admin_headers):
        resp = client.put("/api/v1/scheduled-tasks/nonexistent", json={"name": "X"}, headers=admin_headers)
        assert resp.status_code == 404

    def test_delete_task(self, client, admin_headers):
        create = client.post("/api/v1/scheduled-tasks", json={
            "name": "To Delete",
            "cronExpr": "30m",
        }, headers=admin_headers)
        task_id = create.json()["id"]

        resp = client.delete(f"/api/v1/scheduled-tasks/{task_id}", headers=admin_headers)
        assert resp.status_code == 200

        resp2 = client.get(f"/api/v1/scheduled-tasks/{task_id}", headers=admin_headers)
        assert resp2.status_code == 404

    def test_delete_task_not_found(self, client, admin_headers):
        resp = client.delete("/api/v1/scheduled-tasks/nonexistent", headers=admin_headers)
        assert resp.status_code == 404

    def test_viewer_cannot_create(self, client, viewer_headers):
        resp = client.post("/api/v1/scheduled-tasks", json={
            "name": "Unauthorized",
            "cronExpr": "1h",
        }, headers=viewer_headers)
        assert resp.status_code == 403

    def test_viewer_cannot_delete(self, client, admin_headers, viewer_headers):
        create = client.post("/api/v1/scheduled-tasks", json={
            "name": "Admin Task",
            "cronExpr": "1h",
        }, headers=admin_headers)
        task_id = create.json()["id"]

        resp = client.delete(f"/api/v1/scheduled-tasks/{task_id}", headers=viewer_headers)
        assert resp.status_code == 403

    def test_trigger_task_not_found(self, client, admin_headers):
        resp = client.post("/api/v1/scheduled-tasks/nonexistent/trigger", headers=admin_headers)
        assert resp.status_code == 404

    def test_list_task_runs(self, client, admin_headers):
        from main import engine

        created = client.post("/api/v1/scheduled-tasks", json={
            "name": "Run History Task",
            "cronExpr": "30m",
        }, headers=admin_headers)
        task_id = created.json()["id"]
        run = engine.create_scheduled_task_run(task_id)
        engine.finish_scheduled_task_run(run["id"], status="success", item_count=2)

        resp = client.get(f"/api/v1/scheduled-tasks/{task_id}/runs", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["taskId"] == task_id

    def test_list_all_task_runs_paginated(self, client, admin_headers):
        from main import engine

        created = client.post("/api/v1/scheduled-tasks", json={
            "name": "Global Run History Task",
            "cronExpr": "30m",
            "action": "analyze",
        }, headers=admin_headers)
        task_id = created.json()["id"]
        run = engine.create_scheduled_task_run(task_id)
        engine.finish_scheduled_task_run(run["id"], status="partial", item_count=3, error_message="1 failed")

        resp = client.get(
            f"/api/v1/scheduled-task-runs?taskId={task_id}&status=partial&page=1&pageSize=10",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        assert data["page"] == 1
        assert data["pageSize"] == 10
        assert data["items"][0]["taskId"] == task_id
        assert data["items"][0]["taskName"] == "Global Run History Task"
        assert data["items"][0]["taskAction"] == "analyze"

    def test_list_all_task_runs_supports_search_on_task_and_items(self, client, admin_headers):
        from main import engine

        created = client.post("/api/v1/scheduled-tasks", json={
            "name": "Searchable History Task",
            "cronExpr": "30m",
            "action": "plan",
        }, headers=admin_headers)
        task_id = created.json()["id"]
        run = engine.create_scheduled_task_run(task_id)
        engine.add_scheduled_task_run_item(
            run["id"],
            task_id,
            item_uuid="bug-search-1",
            item_name="Export timeout issue",
            item_type="缺陷",
            action="plan",
            plan_summary="Investigate export timeout",
            item_snapshot={"uuid": "bug-search-1"},
        )
        engine.finish_scheduled_task_run(run["id"], status="success", item_count=1)

        by_task = client.get(
            "/api/v1/scheduled-task-runs?search=searchable%20history",
            headers=admin_headers,
        )
        assert by_task.status_code == 200
        assert any(item["id"] == run["id"] for item in by_task.json()["items"])

        by_item = client.get(
            "/api/v1/scheduled-task-runs?search=export%20timeout",
            headers=admin_headers,
        )
        assert by_item.status_code == 200
        assert any(item["id"] == run["id"] for item in by_item.json()["items"])

    def test_list_task_run_items(self, client, admin_headers):
        from main import engine

        created = client.post("/api/v1/scheduled-tasks", json={
            "name": "Run Item Task",
            "cronExpr": "1h",
        }, headers=admin_headers)
        task_id = created.json()["id"]
        run = engine.create_scheduled_task_run(task_id)
        engine.add_scheduled_task_run_item(
            run["id"],
            task_id,
            item_uuid="bug-1",
            item_name="Bug 1",
            item_type="缺陷",
            action="analyze",
            analysis_markdown="### 分析结果\n建议修复",
            item_snapshot={"uuid": "bug-1"},
        )

        resp = client.get(f"/api/v1/scheduled-task-runs/{run['id']}/items", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["itemUuid"] == "bug-1"
        assert data[0]["analysisMarkdown"]


class TestAITrigger:
    def test_trigger_no_ones_credentials(self, client, admin_headers):
        """AI trigger should fail gracefully when ONES not configured"""
        resp = client.post("/api/v1/ai/trigger", json={
            "itemId": "some-id",
            "action": "plan",
        }, headers=admin_headers)
        # Depending on local .env, this can fail because ONES is not configured,
        # upstream fetch fails, or the synthetic item does not exist.
        assert resp.status_code in (400, 404, 502)

    def test_trigger_unauthorized(self, client):
        resp = client.post("/api/v1/ai/trigger", json={
            "itemId": "some-id",
            "action": "plan",
        })
        assert resp.status_code == 401


class TestEmailConfig:
    def test_config_includes_email(self, client, admin_headers):
        resp = client.get("/api/v1/config", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "email" in data
        assert "smtpHost" in data["email"]
        assert "smtpPort" in data["email"]

    def test_update_email_config(self, client, admin_headers):
        resp = client.put("/api/v1/config", json={
            "email": {
                "smtpHost": "smtp.test.com",
                "smtpPort": 587,
                "smtpUser": "test@test.com",
                "sender": "test@test.com",
            }
        }, headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["email"]["smtpHost"] == "smtp.test.com"
