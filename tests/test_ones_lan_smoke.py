"""Explicit opt-in, read-only acceptance test for an authorized LAN ONES."""

from __future__ import annotations

import os
import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest
import requests
from graphql import OperationType, parse, print_ast
from graphql.language.ast import FieldNode, FragmentDefinitionNode, FragmentSpreadNode, InlineFragmentNode, OperationDefinitionNode, VariableNode

from config.settings import OnesSettings
from src.integrations.ones import GQL_FETCH_TASKS as SYNC_GQL_FETCH_TASKS
from src.integrations.ones_api import GQL_FETCH_TASKS as ASYNC_GQL_FETCH_TASKS
from src.services.ones_gateway import OnesGateway


_FILTER_ENV_NAMES = (
    "ONES_LAN_PROJECT_ID",
    "ONES_LAN_ITERATION_ID",
    "ONES_LAN_ASSIGNEE_ID",
    "ONES_LAN_ISSUE_TYPE_ID",
)
_MISSING_FILTERS = [name for name in _FILTER_ENV_NAMES if not os.getenv(name, "").strip()]
_OPEN_CATEGORIES = {"open", "todo", "to_do", "doing", "in_progress", "pending"}
_FORBIDDEN_GRAPHQL_FIELDS = {
    "comment", "comments", "addcomment", "updatecomment", "updatetask",
    "deletetask", "createtask", "transittask",
}

pytestmark = pytest.mark.ones_lan


def _set_local_audit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "ONES_TEAM_ID": "TEAM",
        "ONES_LAN_PROJECT_ID": "PROJECT",
        "ONES_LAN_ITERATION_ID": "ITERATION",
        "ONES_LAN_ASSIGNEE_ID": "ASSIGNEE",
        "ONES_LAN_ISSUE_TYPE_ID": "DEFECT",
        "ONES_LAN_WIKI_SPACE_ID": "SPACE",
        "ONES_LAN_WIKI_PAGE_ID": "PAGE",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _valid_graphql_payload() -> dict:
    return {
        "query": "query Tasks($filterGroup: [Filter!]) { buckets(filter: $filterGroup) { tasks { uuid } pageInfo { hasNextPage endCursor } } }",
        "variables": {
            "groupBy": {"tasks": {}}, "groupOrderBy": {},
            "orderBy": {"position": "ASC", "createTime": "DESC"},
            "filterGroup": [{
                "project_in": ["PROJECT"], "issueType_in": ["DEFECT"],
                "sprint_in": ["ITERATION"], "assign_in": ["ASSIGNEE"],
                "status_in": ["OPEN"],
            }],
            "pagination": {"limit": 100, "after": "", "preciseCount": True},
            "limit": 100,
        },
    }


def _production_graphql_payload() -> dict:
    payload = _valid_graphql_payload()
    payload["query"] = ASYNC_GQL_FETCH_TASKS
    return payload


@dataclass
class _ReadOnlyAudit:
    requests: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def request_count(self) -> int:
        return len(self.requests)

    def record(self, request: object) -> None:
        method = request.method.upper()
        raw_url = str(request.url)
        parsed_url = urlsplit(raw_url)
        assert (
            parsed_url.scheme == "http"
            and parsed_url.hostname == "aputureones.com"
            and parsed_url.port == 8088
            and parsed_url.username is None
            and parsed_url.password is None
            and not parsed_url.fragment
        ), "LAN request escaped the authorized origin"
        assert "%" not in parsed_url.path and unquote(parsed_url.path) == parsed_url.path
        assert not any(part in {".", ".."} for part in parsed_url.path.split("/"))
        path = parsed_url.path
        team_id = os.getenv("ONES_TEAM_ID", "").strip()
        graphql_path = f"/project/api/project/team/{team_id}/items/graphql"
        statuses_path = f"/project/api/project/team/{team_id}/task_statuses"
        operation = ""
        if method == "POST" and path == graphql_path:
            self._validate_graphql_url_query(parsed_url.query)
            try:
                payload = json.loads(self._body(request).decode("utf-8", "strict"))
            except (UnicodeError, ValueError):
                pytest.fail("LAN GraphQL request body is not strict JSON")
            assert isinstance(payload, dict) and set(payload) == {"query", "variables"}
            operation = self._validate_graphql(payload["query"], payload["variables"])
        elif method == "POST" and path == statuses_path:
            try:
                payload = json.loads(self._body(request).decode("utf-8", "strict"))
            except (UnicodeError, ValueError):
                pytest.fail("LAN status metadata request body is not strict JSON")
            assert isinstance(payload, dict) and set(payload) == {"project_uuids"}
            assert payload["project_uuids"] == [os.environ["ONES_LAN_PROJECT_ID"].strip()]
            operation = "read-status-configs"
        elif method == "GET":
            self._validate_get_query(path, parsed_url.query)
        auth_paths = {
            "/identity/api/login", "/identity/authorize",
            "/identity/api/auth_request/finalize", "/identity/oauth/token",
        }
        allowed = (
            (method == "GET" and self._allowed_get(path))
            or (method == "POST" and path in auth_paths)
            or (method == "POST" and path == graphql_path and bool(operation))
            or (method == "POST" and path == statuses_path and operation == "read-status-configs")
        )
        assert allowed, f"unexpected LAN request: {method} {path}"
        self.requests.append((method, path, operation))

    @staticmethod
    def _validate_get_query(path: str, query: str) -> None:
        if path == "/identity/authorize/callback":
            values = parse_qs(query, strict_parsing=True)
            assert set(values) == {"id", "lang"}
            assert values["lang"] == ["zh"]
            assert len(values["id"]) == 1 and re.fullmatch(r"[A-Za-z0-9_-]+", values["id"][0])
        else:
            assert not query

    @staticmethod
    def _body(request: object) -> bytes:
        value = getattr(request, "content", None)
        if value is None:
            value = getattr(request, "body", b"")
        if isinstance(value, str):
            return value.encode("utf-8", "strict")
        assert isinstance(value, bytes)
        return value

    @staticmethod
    def _validate_graphql_url_query(query: str) -> None:
        values = parse_qs(query, keep_blank_values=True, strict_parsing=True)
        assert values == {"t": ["group-task-data"]}

    @staticmethod
    def _validate_graphql(query: object, variables: object) -> str:
        assert isinstance(query, str) and query
        document = parse(query)
        sync_document = parse(SYNC_GQL_FETCH_TASKS)
        async_document = parse(ASYNC_GQL_FETCH_TASKS)
        expected_canonical = print_ast(async_document)
        assert print_ast(sync_document) == expected_canonical
        assert print_ast(document) == expected_canonical
        operations = [item for item in document.definitions if isinstance(item, OperationDefinitionNode)]
        fragments = {
            item.name.value: item for item in document.definitions
            if isinstance(item, FragmentDefinitionNode)
        }
        assert len(operations) == 1 and operations[0].operation is OperationType.QUERY
        assert len(document.definitions) == 1 + len(fragments)
        operation_node = operations[0]
        expected_operation = next(
            item for item in async_document.definitions if isinstance(item, OperationDefinitionNode)
        )
        definitions = tuple(
            (
                item.variable.name.value,
                print_ast(item.type),
                print_ast(item.default_value) if item.default_value is not None else None,
            )
            for item in operation_node.variable_definitions or ()
        )
        expected_definitions = tuple(
            (
                item.variable.name.value,
                print_ast(item.type),
                print_ast(item.default_value) if item.default_value is not None else None,
            )
            for item in expected_operation.variable_definitions or ()
        )
        assert definitions == expected_definitions
        assert not operation_node.directives
        root_fields = [item for item in operations[0].selection_set.selections if isinstance(item, FieldNode)]
        assert len(root_fields) == 1 and root_fields[0].name.value == "buckets"

        def variable_arguments(field: FieldNode, expected: dict[str, str]) -> None:
            assert not field.alias and not field.directives
            assert {argument.name.value for argument in field.arguments} == set(expected)
            for argument in field.arguments:
                assert isinstance(argument.value, VariableNode)
                assert argument.value.name.value == expected[argument.name.value]

        buckets = root_fields[0]
        variable_arguments(buckets, {
            "groupBy": "groupBy", "orderBy": "groupOrderBy",
            "pagination": "pagination", "filter": "groupFilter",
        })
        bucket_fields = {
            field.name.value: field
            for field in buckets.selection_set.selections
            if isinstance(field, FieldNode)
        }
        assert set(bucket_fields) == {"key", "tasks", "pageInfo"}
        assert len(bucket_fields) == len(buckets.selection_set.selections)
        assert not bucket_fields["key"].arguments and not bucket_fields["key"].directives
        assert bucket_fields["key"].selection_set is None
        tasks = bucket_fields["tasks"]
        variable_arguments(tasks, {
            "filterGroup": "filterGroup", "orderBy": "orderBy", "limit": "limit",
        })
        assert tasks.selection_set is not None
        page_info = bucket_fields["pageInfo"]
        assert not page_info.alias and not page_info.arguments and not page_info.directives
        assert page_info.selection_set is not None
        page_fields = tuple(
            field.name.value for field in page_info.selection_set.selections
            if isinstance(field, FieldNode)
        )
        assert set(page_fields) == {"count", "totalCount", "hasNextPage", "endCursor"}
        assert len(page_fields) == len(page_info.selection_set.selections) == 4
        for field in page_info.selection_set.selections:
            assert isinstance(field, FieldNode)
            assert not field.alias and not field.arguments and not field.directives
            assert field.selection_set is None

        task_fields = tuple(
            field.name.value for field in tasks.selection_set.selections
            if isinstance(field, FieldNode)
        )
        assert set(task_fields) == {
            "key", "uuid", "name", "number", "createTime", "serverUpdateStamp",
            "deadline", "path", "subTaskCount", "subTaskDoneCount", "status",
            "issueType", "subIssueType", "project", "sprint", "parent", "assign",
            "owner", "priority", "estimatedHours", "remainingManhour",
            "totalEstimatedHours", "totalRemainingHours", "issueTypeScope",
        }
        assert len(task_fields) == len(tasks.selection_set.selections) == 24

        def inspect(selection_set, seen: set[str]) -> None:
            for selection in selection_set.selections:
                if isinstance(selection, FieldNode):
                    name = selection.name.value
                    assert selection.alias is None and not selection.directives
                    assert not name.startswith("__")
                    assert name.casefold() not in _FORBIDDEN_GRAPHQL_FIELDS
                    if selection.selection_set is not None:
                        inspect(selection.selection_set, seen)
                elif isinstance(selection, InlineFragmentNode):
                    inspect(selection.selection_set, seen)
                elif isinstance(selection, FragmentSpreadNode):
                    assert selection.name.value in fragments and selection.name.value not in seen
                    inspect(fragments[selection.name.value].selection_set, {*seen, selection.name.value})
                else:
                    pytest.fail("unsupported GraphQL selection")

        inspect(operations[0].selection_set, set())
        assert isinstance(variables, dict) and set(variables) == {
            "groupBy", "groupOrderBy", "orderBy", "filterGroup", "pagination", "limit"
        }
        assert variables["groupBy"] == {"tasks": {}}
        assert variables["groupOrderBy"] == {}
        assert variables["orderBy"] == {"position": "ASC", "createTime": "DESC"}
        assert type(variables["limit"]) is int and 1 <= variables["limit"] <= 200
        assert isinstance(variables["pagination"], dict)
        assert set(variables["pagination"]) == {"limit", "after", "preciseCount"}
        assert isinstance(variables["pagination"]["after"], str)
        assert variables["pagination"] == {
            "limit": variables["limit"], "after": variables["pagination"]["after"], "preciseCount": True
        }
        assert isinstance(variables["filterGroup"], list) and len(variables["filterGroup"]) == 1
        filters = variables["filterGroup"][0]
        assert isinstance(filters, dict) and set(filters) == {
            "project_in", "issueType_in", "sprint_in", "assign_in", "status_in"
        }
        assert filters["project_in"] == [os.environ["ONES_LAN_PROJECT_ID"].strip()]
        assert filters["issueType_in"] == [os.environ["ONES_LAN_ISSUE_TYPE_ID"].strip()]
        assert filters["sprint_in"] == [os.environ["ONES_LAN_ITERATION_ID"].strip()]
        assert filters["assign_in"] == [os.environ["ONES_LAN_ASSIGNEE_ID"].strip()]
        assert isinstance(filters["status_in"], list) and filters["status_in"]
        assert all(isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_-]+", item) for item in filters["status_in"])
        return operations[0].name.value if operations[0].name else "anonymous-query"

    def assert_read_only(self) -> None:
        assert self.requests, "LAN smoke did not make an auditable request"
        team_id = os.environ["ONES_TEAM_ID"].strip()
        graphql_path = f"/project/api/project/team/{team_id}/items/graphql"
        statuses_path = f"/project/api/project/team/{team_id}/task_statuses"
        auth_paths = {
            "/identity/api/login",
            "/identity/authorize",
            "/identity/api/auth_request/finalize",
            "/identity/oauth/token",
        }
        for method, path, _ in self.requests:
            if method == "GET" and self._allowed_get(path):
                continue
            if method == "POST" and path == graphql_path:
                continue
            if method == "POST" and path == statuses_path and _ == "read-status-configs":
                continue
            # Authentication bootstrap is not a business mutation.  It is the
            # only permitted non-GET/non-GraphQL request family.
            assert method == "POST" and path in auth_paths, f"unexpected LAN request: {method} {path}"
            lowered = path.casefold()
            assert not any(token in lowered for token in ("comment", "status", "task/update"))

    @staticmethod
    def _allowed_get(path: str) -> bool:
        team = os.environ["ONES_TEAM_ID"].strip()
        space = os.environ.get("ONES_LAN_WIKI_SPACE_ID", "").strip()
        page = os.environ.get("ONES_LAN_WIKI_PAGE_ID", "").strip()
        exact = {
            "/identity/api/org_users",
            "/identity/authorize/callback",
            f"/project/api/project/team/{team}/task_statuses",
        }
        if space and page:
            exact.update({
                f"/wiki/api/wiki/team/{team}/space/{space}/page/{page}",
                f"/wiki/api/wiki/team/{team}/page/{page}/detail",
            })
        return path in exact or re.fullmatch(r"/identity/api/auth_request/[A-Za-z0-9_-]+", path) is not None


@pytest.mark.parametrize("url", [
    "https://aputureones.com:8088/identity/api/org_users",
    "http://evil.invalid:8088/identity/api/org_users",
    "http://user@aputureones.com:8088/identity/api/org_users",
    "http://aputureones.com:8088/identity/api/%6frg_users",
    "http://aputureones.com:8088/identity/api/../org_users",
])
def test_lan_audit_rejects_origin_and_path_bypasses(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    _set_local_audit_env(monkeypatch)
    with pytest.raises(AssertionError):
        _ReadOnlyAudit().record(httpx.Request("GET", url))


def test_lan_audit_covers_requests_and_httpx_request_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_local_audit_env(monkeypatch)
    audit = _ReadOnlyAudit()
    audit.record(httpx.Request("GET", "http://aputureones.com:8088/identity/api/org_users"))
    prepared = requests.Request(
        "POST", "http://aputureones.com:8088/project/api/project/team/TEAM/task_statuses",
        json={"project_uuids": ["PROJECT"]},
    ).prepare()
    audit.record(prepared)
    audit.assert_read_only()


@pytest.mark.parametrize("query", [
    "mutation { updateTask { uuid } }",
    "query { alias: unknownRoot { uuid } }",
    "query { __schema { types { name } } }",
    "query { buckets { ...WriteFields } } fragment WriteFields on Bucket { comment { uuid } }",
])
def test_lan_audit_rejects_graphql_ast_bypasses(
    monkeypatch: pytest.MonkeyPatch, query: str,
) -> None:
    _set_local_audit_env(monkeypatch)
    payload = _valid_graphql_payload()
    payload["query"] = query
    request = httpx.Request(
        "POST", "http://aputureones.com:8088/project/api/project/team/TEAM/items/graphql?t=group-task-data",
        json=payload,
    )
    with pytest.raises(AssertionError):
        _ReadOnlyAudit().record(request)


def test_lan_audit_rejects_graphql_batches_and_variable_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_local_audit_env(monkeypatch)
    url = "http://aputureones.com:8088/project/api/project/team/TEAM/items/graphql?t=group-task-data"
    with pytest.raises(AssertionError):
        _ReadOnlyAudit().record(httpx.Request("POST", url, json=[_valid_graphql_payload()]))
    changed = _production_graphql_payload()
    changed["variables"]["filterGroup"][0]["project_in"] = ["OTHER"]
    with pytest.raises(AssertionError):
        _ReadOnlyAudit().record(httpx.Request("POST", url, json=changed))
    changed = _production_graphql_payload()
    changed["variables"]["pagination"] = {"limit": 100, "after": "", "preciseCount": True, "extra": 1}
    with pytest.raises(AssertionError):
        _ReadOnlyAudit().record(httpx.Request("POST", url, json=changed))


def test_lan_audit_accepts_only_the_production_task_query(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_local_audit_env(monkeypatch)
    assert print_ast(parse(SYNC_GQL_FETCH_TASKS)) == print_ast(parse(ASYNC_GQL_FETCH_TASKS))
    request = httpx.Request(
        "POST",
        "http://aputureones.com:8088/project/api/project/team/TEAM/items/graphql?t=group-task-data",
        json=_production_graphql_payload(),
    )

    audit = _ReadOnlyAudit()
    audit.record(request)

    audit.assert_read_only()


@pytest.mark.parametrize(
    "query",
    [
        "query Extra($unused: String, $groupBy: String, $groupOrderBy: String, $pagination: String, $groupFilter: String, $filterGroup: String, $orderBy: String, $limit: Int) { buckets(groupBy: $groupBy, orderBy: $groupOrderBy, pagination: $pagination, filter: $groupFilter) { key tasks(filterGroup: $filterGroup, orderBy: $orderBy, limit: $limit) { uuid secretInternalField } pageInfo { count totalCount hasNextPage endCursor } } }",
        ASYNC_GQL_FETCH_TASKS.replace("limit: $limit", "limit: 100"),
        ASYNC_GQL_FETCH_TASKS.replace("limit: $limit", ""),
        ASYNC_GQL_FETCH_TASKS.replace("limit: $limit", "limit: $limit, extra: $limit"),
        ASYNC_GQL_FETCH_TASKS.replace("limit: $limit\n    ) {", "limit: $limit\n    ) @skip(if: false) {"),
    ],
)
def test_lan_audit_rejects_nonproduction_query_shapes(
    monkeypatch: pytest.MonkeyPatch, query: str,
) -> None:
    _set_local_audit_env(monkeypatch)
    payload = _production_graphql_payload()
    payload["query"] = query
    request = httpx.Request(
        "POST",
        "http://aputureones.com:8088/project/api/project/team/TEAM/items/graphql?t=group-task-data",
        json=payload,
    )

    with pytest.raises(AssertionError):
        _ReadOnlyAudit().record(request)


@pytest.mark.parametrize("query", [
    "", "?t=unexpected", "?t=group-task-data&t=group-task-data",
    "?t=group-task-data&extra=1",
])
def test_lan_audit_rejects_unexpected_graphql_url_query(
    monkeypatch: pytest.MonkeyPatch, query: str,
) -> None:
    _set_local_audit_env(monkeypatch)
    request = httpx.Request(
        "POST",
        "http://aputureones.com:8088/project/api/project/team/TEAM/items/graphql" + query,
        json=_production_graphql_payload(),
    )

    with pytest.raises(AssertionError):
        _ReadOnlyAudit().record(request)


@pytest.fixture
def lan_read_only_audit(monkeypatch: pytest.MonkeyPatch) -> _ReadOnlyAudit:
    audit = _ReadOnlyAudit()
    async_original = httpx.AsyncClient.send
    sync_original = httpx.Client.send
    requests_original = requests.Session.send

    async def audited_send(client, request, *args, **kwargs):
        audit.record(request)
        return await async_original(client, request, *args, **kwargs)

    def audited_sync_send(client, request, *args, **kwargs):
        audit.record(request)
        return sync_original(client, request, *args, **kwargs)

    def audited_requests_send(client, request, *args, **kwargs):
        audit.record(request)
        return requests_original(client, request, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", audited_send)
    monkeypatch.setattr(httpx.Client, "send", audited_sync_send)
    monkeypatch.setattr(requests.Session, "send", audited_requests_send)
    yield audit
    if os.getenv("RUN_ONES_LAN_SMOKE") == "1" and audit.requests:
        audit.assert_read_only()


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_ONES_LAN_SMOKE") != "1" or bool(_MISSING_FILTERS),
    reason=(
        "set RUN_ONES_LAN_SMOKE=1 and ONES_LAN_PROJECT_ID/ITERATION_ID/"
        "ASSIGNEE_ID/ISSUE_TYPE_ID to access the authorized LAN ONES"
    ),
)
async def test_open_defects_match_authorized_lan_filters(lan_read_only_audit: _ReadOnlyAudit):
    """An empty result is valid; every returned defect must match all filters."""
    before_requests = lan_read_only_audit.request_count
    project_id = os.environ["ONES_LAN_PROJECT_ID"].strip()
    iteration_id = os.environ["ONES_LAN_ITERATION_ID"].strip()
    assignee_id = os.environ["ONES_LAN_ASSIGNEE_ID"].strip()
    issue_type_id = os.environ["ONES_LAN_ISSUE_TYPE_ID"].strip()

    async with OnesGateway(settings=OnesSettings()) as gateway:
        defects = await gateway.list_open_defects(
            project_id=project_id,
            issue_type_id=issue_type_id,
            sprint_id=iteration_id,
            assignee=assignee_id,
        )

    for defect in defects:
        assert defect.project.id == project_id
        assert defect.issue_type.id == issue_type_id
        assert defect.assignee is not None and defect.assignee.id == assignee_id
        assert defect.status.category.strip().lower() in _OPEN_CATEGORIES
        assert defect.raw.get("sprint", {}).get("uuid") == iteration_id
    assert lan_read_only_audit.request_count > before_requests


@pytest.mark.asyncio
async def test_read_explicit_authorized_lan_wiki_page(lan_read_only_audit: _ReadOnlyAudit):
    if os.getenv("RUN_ONES_LAN_SMOKE") != "1":
        pytest.skip("set RUN_ONES_LAN_SMOKE=1 for the read-only Wiki smoke")
    space_id = os.getenv("ONES_LAN_WIKI_SPACE_ID", "").strip()
    page_id = os.getenv("ONES_LAN_WIKI_PAGE_ID", "").strip()
    if not space_id or not page_id:
        pytest.skip("set ONES_LAN_WIKI_SPACE_ID and ONES_LAN_WIKI_PAGE_ID for the read-only Wiki smoke")
    before_requests = lan_read_only_audit.request_count

    async with OnesGateway(settings=OnesSettings()) as gateway:
        snapshot = await gateway.get_wiki_snapshot_by_ids(space_id, page_id)

    assert snapshot.space_id == space_id
    assert snapshot.page_id == page_id
    assert len(snapshot.content_sha256) == 64
    assert lan_read_only_audit.request_count > before_requests
