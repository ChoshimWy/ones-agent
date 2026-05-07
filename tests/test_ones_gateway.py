from __future__ import annotations

import unittest
from unittest.mock import patch

from src.services.ones_gateway import (
    OnesGateway,
    OnesGatewayAuthError,
    OnesGatewayNotFoundError,
    OnesGatewayPayloadError,
    OnesGatewayTimeoutError,
)


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class FakeHTTPStatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.response = FakeResponse(status_code)


class FakeAsyncClient:
    def __init__(self, mine_results: dict[str | None, list[dict]] | None = None, defects_results: dict[str | None, list[dict]] | None = None):
        self.mine_results = mine_results or {}
        self.defects_results = defects_results or {}
        self.calls: list[tuple[str, dict]] = []
        self.detail_calls: list[str] = []

    async def fetch_my_defects(self, **kwargs) -> list[dict]:
        self.calls.append(("fetch_my_defects", kwargs))
        return list(self.mine_results.get(kwargs.get("project_id"), []))

    async def fetch_defects(self, **kwargs) -> list[dict]:
        self.calls.append(("fetch_defects", kwargs))
        return list(self.defects_results.get(kwargs.get("project_id"), []))

    async def fetch_issue_detail(self, issue_id: str) -> dict:
        self.detail_calls.append(issue_id)
        return {"uuid": issue_id, "name": "fallback"}


class FakeSyncClient:
    def __init__(self, mine_results: dict[str | None, list[dict]] | None = None, defects_results: dict[str | None, list[dict]] | None = None):
        self.mine_results = mine_results or {}
        self.defects_results = defects_results or {}
        self.calls: list[tuple[str, dict]] = []
        self.detail_calls: list[str] = []

    def fetch_my_defects(self, **kwargs) -> list[dict]:
        self.calls.append(("fetch_my_defects", kwargs))
        return list(self.mine_results.get(kwargs.get("project_id"), []))

    def fetch_defects(self, **kwargs) -> list[dict]:
        self.calls.append(("fetch_defects", kwargs))
        return list(self.defects_results.get(kwargs.get("project_id"), []))

    def fetch_issue_detail(self, issue_id: str) -> dict:
        self.detail_calls.append(issue_id)
        return {"uuid": issue_id, "name": "fallback"}


class RaisingAsyncClient(FakeAsyncClient):
    def __init__(self, *, exception: Exception):
        super().__init__()
        self.exception = exception

    async def fetch_projects(self, include_archived: bool = False) -> list[dict]:
        raise self.exception

    async def fetch_defects(self, **kwargs) -> list[dict]:
        raise self.exception

    async def fetch_issue_detail(self, issue_id: str) -> dict:
        self.detail_calls.append(issue_id)
        raise self.exception


class RaisingSyncClient(FakeSyncClient):
    def __init__(self, *, exception: Exception):
        super().__init__()
        self.exception = exception

    def fetch_projects(self, include_archived: bool = False) -> list[dict]:
        raise self.exception

    def fetch_defects(self, **kwargs) -> list[dict]:
        raise self.exception


class TestOnesGatewayAsync(unittest.IsolatedAsyncioTestCase):
    async def test_list_current_user_defects_aggregates_projects_and_respects_total_limit(self):
        client = FakeAsyncClient(
            mine_results={
                "proj-1": [{"uuid": "d1"}, {"uuid": "d2"}],
                "proj-2": [{"uuid": "d2"}, {"uuid": "d3"}],
            },
        )
        gateway = OnesGateway(async_client=client)

        defects = await gateway.list_current_user_defects(
            project_ids=["proj-1", "proj-2"],
            limit=3,
            page_size=2,
        )

        self.assertEqual([defect["uuid"] for defect in defects], ["d1", "d2", "d3"])
        self.assertEqual(
            client.calls,
            [
                (
                    "fetch_my_defects",
                    {"project_id": "proj-1", "issue_type_id": None, "limit": 2},
                ),
                (
                    "fetch_my_defects",
                    {"project_id": "proj-2", "issue_type_id": None, "limit": 1},
                ),
            ],
        )

    async def test_list_defects_normalizes_current_user_token_to_mine_path(self):
        client = FakeAsyncClient(mine_results={"proj-1": [{"uuid": "d1"}]})
        gateway = OnesGateway(async_client=client)

        defects = await gateway.list_defects(
            project_id="proj-1",
            assign="$currentUser",
            limit=5,
        )

        self.assertEqual(defects, [{"uuid": "d1"}])
        self.assertEqual(
            client.calls,
            [
                (
                    "fetch_my_defects",
                    {"project_id": "proj-1", "issue_type_id": None, "limit": 5},
                ),
            ],
        )

    async def test_list_defects_forwards_sprint_id_on_async_path(self):
        client = FakeAsyncClient(defects_results={"proj-1": [{"uuid": "d1"}]})
        gateway = OnesGateway(async_client=client)

        defects = await gateway.list_defects(project_id="proj-1", sprint_id="sprint-1", limit=5)

        self.assertEqual(defects, [{"uuid": "d1"}])
        self.assertEqual(
            client.calls,
            [
                (
                    "fetch_defects",
                    {"project_id": "proj-1", "issue_type_id": None, "limit": 5, "sprint_id": "sprint-1"},
                ),
            ],
        )

    async def test_get_defect_detail_uses_scoped_current_user_fetch_before_fallback(self):
        client = FakeAsyncClient(mine_results={"proj-1": [{"uuid": "d2", "name": "mine defect"}]})
        gateway = OnesGateway(async_client=client)

        defect = await gateway.get_defect_detail("d2", project_id="proj-1", mine=True, limit=10)

        self.assertEqual(defect, {"uuid": "d2", "name": "mine defect"})
        self.assertEqual(client.detail_calls, [])

    async def test_list_projects_maps_request_auth_failures_to_gateway_auth_error(self):
        gateway = OnesGateway(async_client=RaisingAsyncClient(exception=FakeHTTPStatusError(401)))

        with self.assertRaises(OnesGatewayAuthError):
            await gateway.list_projects()

    async def test_list_projects_maps_managed_client_login_failures_to_gateway_auth_error(self):
        class FakeManagedAsyncClient:
            def __init__(self, settings):
                self.settings = settings

            async def _get_client(self):
                raise FakeHTTPStatusError(403)

        gateway = OnesGateway(settings=object())

        with patch("src.integrations.ones_api.OnesAsyncClient", FakeManagedAsyncClient):
            with self.assertRaises(OnesGatewayAuthError):
                await gateway.list_projects()

    async def test_list_defects_maps_timeout_to_gateway_timeout_error(self):
        gateway = OnesGateway(async_client=RaisingAsyncClient(exception=TimeoutError("slow upstream")))

        with self.assertRaises(OnesGatewayTimeoutError):
            await gateway.list_defects(project_id="proj-1")

    async def test_get_defect_detail_raises_not_found_when_scoped_and_fallback_resolution_are_empty(self):
        client = FakeAsyncClient(defects_results={"proj-1": []})

        async def empty_detail(issue_id: str) -> dict:
            client.detail_calls.append(issue_id)
            return {}

        client.fetch_issue_detail = empty_detail
        gateway = OnesGateway(async_client=client)

        with self.assertRaises(OnesGatewayNotFoundError):
            await gateway.get_defect_detail("missing-1", project_id="proj-1")

    async def test_normalize_defect_raises_payload_error_for_missing_required_fields(self):
        gateway = OnesGateway()

        with self.assertRaises(OnesGatewayPayloadError):
            gateway.normalize_defect({"uuid": "bug-1", "name": "Broken payload"})


class TestOnesGatewaySync(unittest.TestCase):
    def test_list_defects_sync_preserves_non_mine_filters_across_projects(self):
        client = FakeSyncClient(
            defects_results={
                "proj-1": [{"uuid": "d1"}],
                "proj-2": [{"uuid": "d2"}],
            },
        )
        gateway = OnesGateway(sync_client=client)

        defects = gateway.list_defects_sync(
            project_ids=["proj-1", "proj-2"],
            assign="user-1",
            sprint_id="sprint-1",
            limit=2,
            page_size=1,
        )

        self.assertEqual([defect["uuid"] for defect in defects], ["d1", "d2"])
        self.assertEqual(
            client.calls,
            [
                (
                    "fetch_defects",
                    {
                        "project_id": "proj-1",
                        "issue_type_id": None,
                        "limit": 1,
                        "sprint_id": "sprint-1",
                        "assign": "user-1",
                    },
                ),
                (
                    "fetch_defects",
                    {
                        "project_id": "proj-2",
                        "issue_type_id": None,
                        "limit": 1,
                        "sprint_id": "sprint-1",
                        "assign": "user-1",
                    },
                ),
            ],
        )

    def test_get_defect_detail_sync_uses_scoped_fetch_for_current_user_queries(self):
        client = FakeSyncClient(mine_results={"proj-1": [{"uuid": "d3", "name": "scoped"}]})
        gateway = OnesGateway(sync_client=client)

        defect = gateway.get_defect_detail_sync("d3", project_id="proj-1", current_user=True, limit=4)

        self.assertEqual(defect, {"uuid": "d3", "name": "scoped"})
        self.assertEqual(client.detail_calls, [])

    def test_list_projects_sync_maps_request_auth_failures_to_gateway_auth_error(self):
        gateway = OnesGateway(sync_client=RaisingSyncClient(exception=FakeHTTPStatusError(401)))

        with self.assertRaises(OnesGatewayAuthError):
            gateway.list_projects_sync()
