"""API v1 端点测试"""

import gc
import tempfile

import pytest
from fastapi.testclient import TestClient

from src.contracts import AnalysisResult, DefectRecord, EvidenceReference, ExecutionRequest, FixSuggestion, IdentityRef, IssueTypeRef, PriorityRef, ProjectRef, RepoResolution, RepoTarget, StatusRef
from src.core.engine import Engine
from src.services.execution_service import ExecutionService


@pytest.fixture
def client():
    from main import app
    return TestClient(app)


@pytest.fixture
def auth_headers(client):
    resp = client.post("/api/v1/auth/login", json={"email": "admin@aputure.com", "password": "admin"})
    assert resp.status_code == 200
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_headers(auth_headers):
    return auth_headers


@pytest.fixture
def viewer_headers(client):
    resp = client.post("/api/v1/auth/login", json={"email": "Viewer", "password": "viewer"})
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


class TestAuth:
    def test_login_success(self, client):
        resp = client.post("/api/v1/auth/login", json={"email": "admin@aputure.com", "password": "admin"})
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert data["user"]["role"] == "admin"

    def test_login_failure(self, client):
        resp = client.post("/api/v1/auth/login", json={"email": "nobody", "password": "wrong"})
        assert resp.status_code == 401

    def test_me(self, client, auth_headers):
        resp = client.get("/api/v1/auth/me", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    def test_me_unauthorized(self, client):
        resp = client.get("/api/v1/auth/me")
        assert resp.status_code == 401


class TestTasks:
    def test_list_tasks(self, client, auth_headers):
        resp = client.get("/api/v1/tasks", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data
        assert "page" in data

    def test_list_tasks_filters_by_type(self, client, auth_headers):
        from main import engine
        unique_suffix = "type-filter"
        bug_id = f"bug-{unique_suffix}"
        req_id = f"req-{unique_suffix}"

        engine.delete(bug_id)
        engine.delete(req_id)
        try:
            engine.start_work(bug_id)
            engine.start_work(req_id)

            defect_resp = client.get(
                "/api/v1/tasks",
                params={"type": "defect", "search": unique_suffix},
                headers=auth_headers,
            )
            assert defect_resp.status_code == 200
            defect_items = defect_resp.json()["items"]
            assert defect_items
            assert all(item["type"] == "defect" for item in defect_items)

            requirement_resp = client.get(
                "/api/v1/tasks",
                params={"type": "requirement", "search": unique_suffix},
                headers=auth_headers,
            )
            assert requirement_resp.status_code == 200
            requirement_items = requirement_resp.json()["items"]
            assert requirement_items
            assert all(item["type"] == "requirement" for item in requirement_items)
        finally:
            engine.delete(bug_id)
            engine.delete(req_id)

    def test_list_tasks_unauthorized(self, client):
        resp = client.get("/api/v1/tasks")
        assert resp.status_code == 401

    def test_task_action(self, client, auth_headers):
        resp = client.post(
            "/api/v1/tasks/nonexistent/action",
            json={"action": "retry"},
            headers=auth_headers,
        )
        assert resp.status_code == 404


class TestMetrics:
    def test_metrics_summary(self, client, auth_headers):
        resp = client.get("/api/v1/metrics/summary", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "activeTasks" in data
        assert "successRate" in data

    def test_metrics_prometheus(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200
    def test_list_logs(self, client, auth_headers):
        resp = client.get("/api/v1/logs", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data


class TestConfig:
    def test_get_config_admin(self, client, admin_headers):
        resp = client.get("/api/v1/config", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "ones" in data
        assert "git" in data

    def test_get_config_viewer_forbidden(self, client, viewer_headers):
        resp = client.get("/api/v1/config", headers=viewer_headers)
        assert resp.status_code == 403

    def test_test_connection(self, client, admin_headers):
        resp = client.post("/api/v1/config/test/git", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_update_config(self, client, admin_headers):
        resp = client.put("/api/v1/config", json={"ones": {"baseUrl": "http://test"}}, headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ones"]["baseUrl"] == "http://test"

    def test_update_config_masks_secrets(self, client, admin_headers):
        resp = client.put("/api/v1/config", json={"llm": {"apiKey": "••••••••"}}, headers=admin_headers)
        assert resp.status_code == 200


class TestSSE:
    def test_sse_endpoint(self, client, auth_headers):
        resp = client.get("/api/v1/stream/events", headers=auth_headers)
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")


class TestHealth:
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestProjectRepos:
    def test_list_empty(self, client, auth_headers, monkeypatch):
        import main

        original_engine = main.engine
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_engine = Engine(db_path=f"{temp_dir}/agent.db")
            monkeypatch.setattr(main, "engine", temp_engine)

            resp = client.get("/api/v1/project-repos", headers=auth_headers)
            assert resp.status_code == 200
            assert resp.json() == []
            monkeypatch.setattr(main, "engine", original_engine)
            del temp_engine
            gc.collect()


class TestScheduledTasks:
    def test_create_scheduled_task_with_assignee(self, client, admin_headers, monkeypatch):
        import main

        original_engine = main.engine
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_engine = Engine(db_path=f"{temp_dir}/agent.db")
            monkeypatch.setattr(main, "engine", temp_engine)

            response = client.post(
                "/api/v1/scheduled-tasks",
                json={
                    "name": "Daily Defect Scan",
                    "cronExpr": "0 9 * * *",
                    "projectId": "proj-1",
                    "assigneeId": "user-1",
                    "assigneeName": "Alice",
                    "itemType": "defect",
                    "action": "plan",
                    "notifyEmails": "",
                    "notifyWechat": False,
                    "enabled": True,
                },
                headers=admin_headers,
            )

            assert response.status_code == 200
            payload = response.json()
            assert payload["assigneeId"] == "user-1"
            assert payload["assigneeName"] == "Alice"

            stored = temp_engine.get_scheduled_task(payload["id"])
            assert stored is not None
            assert stored["assigneeId"] == "user-1"
            assert stored["assigneeName"] == "Alice"

            monkeypatch.setattr(main, "engine", original_engine)
            del stored
            del temp_engine
            gc.collect()

    def test_add_and_list(self, client, admin_headers, monkeypatch):
        import main

        original_engine = main.engine
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_engine = Engine(db_path=f"{temp_dir}/agent.db")
            monkeypatch.setattr(main, "engine", temp_engine)

            resp = client.post("/api/v1/project-repos", json={
                "projectId": "proj-1",
                "projectName": "Test",
                "repoUrl": "https://gitlab.com/test.git",
                "branch": "main",
                "iterationId": "sprint-1",
                "iterationName": "Sprint 1",
                "iterationKey": "SPR-1",
            }, headers=admin_headers)
            assert resp.status_code == 200
            assert resp.json()["projectId"] == "proj-1"
            assert resp.json()["iterationId"] == "sprint-1"
            assert resp.json()["iterationName"] == "Sprint 1"

            resp = client.get("/api/v1/project-repos", headers=admin_headers)
            assert resp.status_code == 200
            assert resp.json() == [{
                "projectId": "proj-1",
                "projectName": "Test",
                "repoUrl": "https://gitlab.com/test.git",
                "branch": "main",
                "iterationId": "sprint-1",
                "iterationName": "Sprint 1",
                "iterationKey": "SPR-1",
            }]
            monkeypatch.setattr(main, "engine", original_engine)
            del temp_engine
            gc.collect()

    def test_remove(self, client, admin_headers, monkeypatch):
        import main

        original_engine = main.engine
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_engine = Engine(db_path=f"{temp_dir}/agent.db")
            monkeypatch.setattr(main, "engine", temp_engine)

            client.post("/api/v1/project-repos", json={
                "projectId": "proj-del",
                "projectName": "Delete Project",
                "repoUrl": "https://gitlab.com/del.git",
                "branch": "main",
                "iterationId": "sprint-del",
                "iterationName": "Delete Sprint",
                "iterationKey": "DEL-1",
            }, headers=admin_headers)
            resp = client.request("DELETE", "/api/v1/project-repos", json={
                "projectId": "proj-del", "repoUrl": "https://gitlab.com/del.git",
            }, headers=admin_headers)
            assert resp.status_code == 200
            assert temp_engine.list_project_repos() == []
            monkeypatch.setattr(main, "engine", original_engine)
            del temp_engine
            gc.collect()


class TestOnesGatewayAdoption:
    def test_fetch_ones_projects_uses_gateway(self, client, auth_headers, monkeypatch):
        import main

        main.settings.ones.email = "tester@example.com"
        main.settings.ones.password = "secret"

        class FakeGateway:
            calls = []

            def __init__(self, settings=None, **kwargs):
                self.settings = settings

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def list_projects(self, include_archived=False):
                self.calls.append((self.settings, include_archived))
                return [{"uuid": "proj-1", "name": "Project One"}]

        monkeypatch.setattr(main, "OnesGateway", FakeGateway)

        resp = client.get("/api/v1/ones/projects", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json() == [{"id": "proj-1", "name": "Project One"}]
        assert FakeGateway.calls == [(main.settings.ones, False)]

    def test_fetch_ones_project_iterations_uses_gateway(self, client, auth_headers, monkeypatch):
        import main

        main.settings.ones.email = "tester@example.com"
        main.settings.ones.password = "secret"

        class FakeGateway:
            calls = []

            def __init__(self, settings=None, **kwargs):
                self.settings = settings

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def list_iterations(self, project_id):
                self.calls.append(project_id)
                return [{
                    "uuid": "sprint-1",
                    "title": "Sprint 1",
                    "key": "SPR-1",
                    "project": {"uuid": project_id, "name": "Project One"},
                    "statusInfo": {"name": "进行中", "category": "open"},
                }]

        monkeypatch.setattr(main, "OnesGateway", FakeGateway)

        resp = client.get("/api/v1/ones/projects/proj-1/iterations", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json() == [{
            "id": "sprint-1",
            "name": "Sprint 1",
            "key": "SPR-1",
            "projectId": "proj-1",
            "projectName": "Project One",
            "statusName": "进行中",
            "statusCategory": "open",
        }]
        assert FakeGateway.calls == ["proj-1"]

    def test_fetch_ones_team_members_uses_gateway(self, client, auth_headers, monkeypatch):
        import main

        main.settings.ones.email = "tester@example.com"
        main.settings.ones.password = "secret"

        original_engine = main.engine
        temp_dir = tempfile.TemporaryDirectory()
        temp_engine = Engine(db_path=f"{temp_dir.name}/agent.db")
        monkeypatch.setattr(main, "engine", temp_engine)
        temp_engine.add_project_repo("proj-1", "Project One", "https://example.com/repo.git", "main", "sprint-1", "Sprint 1", "SPR-1")

        class FakeGateway:
            calls = []

            def __init__(self, settings=None, **kwargs):
                self.settings = settings

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def list_role_members(self, project_id):
                self.calls.append(("list_role_members", {"project_id": project_id}))
                return [
                    {"role": {"uuid": "role-1", "name": "项目成员"}, "members": ["user-2", "user-1", "user-1"]},
                    {"role": {"uuid": "role-2", "name": "测试工程师"}, "members": []},
                ]

            async def list_team_members(self, uuids=None):
                self.calls.append(("list_team_members", {"uuids": uuids}))
                return [
                    {"uuid": "user-2", "name": "Bob"},
                    {"uuid": "user-1", "name": "Alice"},
                    {"uuid": "user-1", "name": "Alice"},
                ]

        monkeypatch.setattr(main, "OnesGateway", FakeGateway)

        resp = client.get("/api/v1/ones/team-members", params={"projectId": "proj-1"}, headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json() == [
            {"id": "user-1", "name": "Alice"},
            {"id": "user-2", "name": "Bob"},
        ]
        assert FakeGateway.calls[0] == ("list_role_members", {"project_id": "proj-1"})
        assert FakeGateway.calls[1] == ("list_team_members", {"uuids": ["user-1", "user-2"]})
        monkeypatch.setattr(main, "engine", original_engine)
        del resp
        del temp_engine
        gc.collect()
        temp_dir.cleanup()

    def test_ai_trigger_plan_fetches_single_item_through_gateway(self, client, auth_headers, monkeypatch):
        import main

        item_id = "gateway-item-1"
        main.engine.delete(item_id)
        main.settings.ones.email = "tester@example.com"
        main.settings.ones.password = "secret"
        main.settings.llm.api_key = "llm-key"

        class FakeGateway:
            requested_ids = []

            def __init__(self, settings=None, **kwargs):
                self.settings = settings

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def get_defect_detail(self, issue_id):
                self.requested_ids.append(issue_id)
                return {
                    "uuid": issue_id,
                    "name": "Gateway defect",
                    "status": {"name": "Open"},
                    "priority": {"name": "P1"},
                    "assign": {"name": "Alice"},
                }

        class FakePlan:
            summary = "summary"
            steps = ["step one"]
            risk_level = "low"
            branch_name = "bugfix/gateway-item-1"
            requires_human_approval = False

            def model_dump_json(self):
                return "{}"

        class FakePlanner:
            def __init__(self, llm_settings):
                self.llm_settings = llm_settings

            async def plan(self, item):
                assert item["uuid"] == item_id
                return FakePlan()

        monkeypatch.setattr(main, "OnesGateway", FakeGateway)
        monkeypatch.setattr(main, "Planner", FakePlanner)

        try:
            resp = client.post(
                "/api/v1/ai/trigger",
                json={"itemId": item_id, "action": "plan"},
                headers=auth_headers,
            )
        finally:
            main.engine.delete(item_id)

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["itemId"] == item_id
        assert payload["name"] == "Gateway defect"
        assert payload["plan"]["branch"] == "bugfix/gateway-item-1"
        assert payload["deprecated"]["route"] == "/api/v1/ai/trigger"
        assert payload["deprecated"]["status"] == "deprecated"
        assert payload["deprecated"]["replacement"]["analysis"] == f"/api/v1/defects/{item_id}"
        assert payload["deprecated"]["replacement"]["execution"] == f"/api/v1/defects/{item_id}/execution"
        assert resp.headers["Deprecation"] == "true"
        assert "/api/v1/defects/gateway-item-1" in resp.headers["Warning"]
        assert resp.headers["Link"] == f'</api/v1/defects/{item_id}>; rel="successor-version"'
        assert FakeGateway.requested_ids == [item_id]


def _make_normalized_defect(defect_id: str = "ONES-BUG-123") -> DefectRecord:
    return DefectRecord(
        defect_id=defect_id,
        title="Tenant context disappears after login",
        description="Users lose tenant scope after SSO redirect.",
        project=ProjectRef(id="proj-1", name="Auth Platform"),
        status=StatusRef(id="st-open", name="Open", category="open"),
        issue_type=IssueTypeRef(id="it-bug", name="Defect"),
        priority=PriorityRef(id="p1", value="high"),
        created_at="2026-05-07T12:00:00+00:00",
        updated_at="2026-05-07T12:05:00+00:00",
    )


def _make_resolution(defect_id: str = "ONES-BUG-123") -> RepoResolution:
    return RepoResolution(
        defect_id=defect_id,
        project=ProjectRef(id="proj-1", name="Auth Platform"),
        selected_repo=RepoTarget(
            repo_url="https://example.com/acme/auth-platform.git",
            repo_name="auth-platform",
            default_branch="main",
        ),
        selected_branch="main",
        confidence=1.0,
        source="project_repo_mapping",
        rationale="Resolved from project repo mapping.",
    )


def _make_analysis(defect_id: str = "ONES-BUG-123") -> AnalysisResult:
    return AnalysisResult(
        defect_id=defect_id,
        project=ProjectRef(id="proj-1", name="Auth Platform"),
        repo_resolution=_make_resolution(defect_id),
        analysis_summary="The login callback drops tenant scope before the dashboard redirect.",
        root_cause="src/auth/callback.py redirects without preserving the validated tenant context.",
        evidence=[
            EvidenceReference(
                kind="file",
                file_path="src/auth/callback.py",
                start_line=10,
                end_line=24,
                snippet="request.state.tenant = tenant_id\nreturn redirect('/dashboard')",
                description="Login callback stores tenant context and redirects immediately.",
                source="keyword_search",
            ),
            EvidenceReference(
                kind="repo_resolution",
                snippet="https://example.com/acme/auth-platform.git",
                description="Resolved repository for project proj-1.",
                source="project_repo_mapping",
            ),
        ],
        confidence=0.82,
        impacted_files=["src/auth/callback.py"],
        fix_suggestions=[
            FixSuggestion(
                title="Guard tenant state",
                description="Validate tenant context before redirecting to the dashboard.",
                impacted_files=["src/auth/callback.py"],
                steps=["Check tenant state before redirect."],
                risk_level="medium",
            )
        ],
        insufficient_evidence=False,
        rendered_markdown="## Analysis Summary\nTenant context is lost before redirect.",
    )


class TestCanonicalDefectRoutes:
    def test_list_defects_uses_gateway_normalization(self, client, auth_headers, monkeypatch):
        import main

        defect = _make_normalized_defect()
        resolution = _make_resolution()

        class FakeGateway:
            calls = []

            def __init__(self, settings=None, **kwargs):
                self.settings = settings

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def list_normalized_defects(self, **kwargs):
                self.calls.append(kwargs)
                return [defect]

        class FakeResolver:
            calls = []

            def resolve(self, *, defect=None, project=None):
                self.calls.append((defect.defect_id if defect else "", project.id if project else ""))
                return resolution

        original_engine = main.engine
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_engine = Engine(db_path=f"{temp_dir}/agent.db")
            monkeypatch.setattr(main, "engine", temp_engine)
            temp_engine.add_project_repo(
                "proj-1",
                "Auth Platform",
                "https://example.com/acme/auth-platform.git",
                "main",
                "sprint-1",
                "Sprint 1",
                "SPR-1",
            )

            monkeypatch.setattr(main, "OnesGateway", FakeGateway)
            monkeypatch.setattr(main, "_create_repo_resolver", lambda: FakeResolver())

            response = client.get("/api/v1/defects", params={"projectId": "proj-1"}, headers=auth_headers)

            assert response.status_code == 200
            payload = response.json()
            assert payload["total"] == 1
            assert payload["items"][0]["id"] == "ONES-BUG-123"
            assert payload["items"][0]["onesId"] == "ONES-BUG-123"
            assert payload["items"][0]["projectName"] == "Auth Platform"
            assert payload["items"][0]["mappingStatus"] == "mapped"
            assert payload["items"][0]["analysisStatus"] == "pending"
            assert payload["items"][0]["selectedRepo"]["iterationId"] == "sprint-1"
            assert payload["items"][0]["selectedRepo"]["iterationName"] == "Sprint 1"
            assert FakeGateway.calls == [{"project_id": "proj-1", "sprint_id": "sprint-1", "assignee": None, "limit": 1000}]
            assert FakeResolver.calls == [("ONES-BUG-123", "")]
            monkeypatch.setattr(main, "engine", original_engine)
            del temp_engine
            gc.collect()

    def test_list_defects_forwards_assignee_filter(self, client, auth_headers, monkeypatch):
        import main

        defect = _make_normalized_defect()

        captured = []

        class FakeGateway:
            def __init__(self, settings=None, **kwargs):
                self.settings = settings

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

        async def fake_fetch_filtered_defects(gateway, *, project_id, assignee=None, limit=1000):
            captured.append({"project_id": project_id, "assignee": assignee, "limit": limit})
            return [defect]

        class FakeResolver:
            def resolve(self, *, defect=None, project=None):
                return _make_resolution()

        monkeypatch.setattr(main, "OnesGateway", FakeGateway)
        monkeypatch.setattr(main, "_fetch_filtered_defects", fake_fetch_filtered_defects)
        monkeypatch.setattr(main, "_create_repo_resolver", lambda: FakeResolver())

        response = client.get("/api/v1/defects", params={"assignee": "alice"}, headers=auth_headers)

        assert response.status_code == 200
        assert captured == [{"project_id": None, "assignee": "alice", "limit": 1000}]

    def test_get_defect_detail_uses_repo_resolution_and_returns_canonical_analysis(self, client, auth_headers, monkeypatch):
        import main

        defect = _make_normalized_defect()
        resolution = _make_resolution()
        analysis = _make_analysis()

        class FakeGateway:
            requested_ids = []

            def __init__(self, settings=None, **kwargs):
                self.settings = settings

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def get_normalized_defect(self, defect_id, **kwargs):
                self.requested_ids.append(defect_id)
                return defect

        class FakeResolver:
            defect_ids = []

            def resolve(self, *, defect=None, project=None):
                self.defect_ids.append(defect.defect_id)
                return resolution

        class FakeWorkflow:
            calls = []

            def analyze_result(self, resolved_defect, resolved_resolution):
                self.calls.append((resolved_defect.defect_id, resolved_resolution.selected_repo.repo_url))
                return analysis

        fake_workflow = FakeWorkflow()
        monkeypatch.setattr(main, "OnesGateway", FakeGateway)
        monkeypatch.setattr(main, "_create_repo_resolver", lambda: FakeResolver())
        monkeypatch.setattr(main, "_create_analysis_workflow_service", lambda: fake_workflow)

        response = client.get(f"/api/v1/defects/{defect.defect_id}", headers=auth_headers)

        assert response.status_code == 200
        payload = response.json()
        assert payload["id"] == defect.defect_id
        assert payload["selectedRepo"]["repoUrl"] == resolution.selected_repo.repo_url
        assert payload["mappingStatus"] == "mapped"
        assert payload["analysisStatus"] == "analyzed"
        assert payload["analysis"]["summary"] == analysis.analysis_summary
        assert payload["analysis"]["rootCause"] == analysis.root_cause
        assert payload["analysis"]["confidence"] == analysis.confidence
        assert payload["analysis"]["markdown"] == analysis.rendered_markdown
        assert payload["analysis"]["fixSuggestions"] == ["Validate tenant context before redirecting to the dashboard."]
        assert payload["analysis"]["evidence"][0]["path"] == "src/auth/callback.py"
        assert FakeGateway.requested_ids == [defect.defect_id]
        assert FakeResolver.defect_ids == [defect.defect_id]
        assert fake_workflow.calls == [(defect.defect_id, resolution.selected_repo.repo_url)]

    def test_create_defect_execution_persists_status_via_live_route(self, client, auth_headers, monkeypatch):
        import main

        defect = _make_normalized_defect()
        resolution = _make_resolution()
        analysis = _make_analysis()

        class FakeGateway:
            def __init__(self, settings=None, **kwargs):
                self.settings = settings

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def get_normalized_defect(self, defect_id, **kwargs):
                return defect

        class FakeResolver:
            def resolve(self, *, defect=None, project=None):
                return resolution

        class FakeWorkflow:
            def analyze_result(self, resolved_defect, resolved_resolution):
                return analysis

        class FakeGitOps:
            def __init__(self, settings, work_dir):
                self.settings = settings
                self.work_dir = work_dir

            def clone_repo(self):
                return "E:/workspace/fake/auth-platform"

            def checkout_branch(self, repo_dir, work_item_id, work_type, title):
                assert work_item_id == defect.defect_id
                return "fix/ONES-BUG-123-investigate-tenant-context"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_engine = Engine(db_path=f"{temp_dir}/agent.db")
            monkeypatch.setattr(main, "engine", temp_engine)
            monkeypatch.setattr(main, "OnesGateway", FakeGateway)
            monkeypatch.setattr(main, "_create_repo_resolver", lambda: FakeResolver())
            monkeypatch.setattr(main, "_create_analysis_workflow_service", lambda: FakeWorkflow())
            monkeypatch.setattr(
                main,
                "_create_execution_service",
                lambda: ExecutionService(
                    engine=temp_engine,
                    work_dir=temp_dir,
                    git_ops_factory=lambda settings, work_dir: FakeGitOps(settings, work_dir),
                ),
            )

            response = client.post(
                f"/api/v1/defects/{defect.defect_id}/execution",
                json={
                    "requestType": "bugfix",
                    "branchName": "fix/frontend-preview-name",
                    "baseBranch": "main",
                    "notes": "Investigate tenant context",
                },
                headers=auth_headers,
            )

            assert response.status_code == 200
            payload = response.json()
            assert payload["executionStatus"] == "created"
            assert payload["execution"]["status"] == "created"
            assert payload["execution"]["baseBranch"] == "main"
            assert payload["execution"]["branchName"] == "fix/ONES-BUG-123-investigate-tenant-context"
            assert payload["executionId"]

            record = temp_engine.get_execution_record(payload["executionId"])
            assert record is not None
            assert record["status"] == "completed"
            assert record["defectId"] == defect.defect_id
            assert record["requestType"] == "bugfix"
            assert record["branchName"] == "fix/ONES-BUG-123-investigate-tenant-context"
            assert record["metadata"]["requested_operations"] == ["branch_create"]

            del record
            del temp_engine
            gc.collect()
