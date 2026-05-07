"""Unified backend-facing ONES gateway.

This service provides one canonical interface for ONES project and defect
access while preserving the existing sync and async client implementations
behind an adapter boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from src.contracts import DefectRecord, IdentityRef, IssueTypeRef, PriorityRef, ProjectRef, StatusRef

if TYPE_CHECKING:
    from config.settings import OnesSettings
    from src.integrations.ones import OnesClient
    from src.integrations.ones_api import OnesAsyncClient


_DEFAULT_DEFECT_LIMIT = 1000
_CURRENT_USER_TOKEN = "$currentUser"


class OnesGatewayError(Exception):
    """Base exception for stable gateway-level ONES failures."""


class OnesGatewayAuthError(OnesGatewayError):
    """Raised when ONES authentication or authorization fails."""


class OnesGatewayTimeoutError(OnesGatewayError):
    """Raised when ONES does not respond before the gateway timeout."""


class OnesGatewayPayloadError(OnesGatewayError):
    """Raised when ONES returns a structurally invalid payload."""


class OnesGatewayNotFoundError(OnesGatewayError):
    """Raised when a requested ONES entity cannot be resolved."""


@dataclass(frozen=True, slots=True)
class _DefectQuery:
    project_ids: tuple[str, ...]
    issue_type_id: str | None
    sprint_id: str | None
    assignee: str | None
    mine: bool
    limit: int
    page_size: int


@dataclass(slots=True)
class OnesGateway:
    """Canonical backend-facing interface for ONES access.

    The active runtime can use the async methods directly. Legacy sync callers
    can use the sync wrappers without importing the underlying client modules.
    """

    settings: OnesSettings | None = None
    async_client: OnesAsyncClient | None = None
    sync_client: OnesClient | None = None
    _managed_async_client: OnesAsyncClient | None = field(default=None, init=False, repr=False)
    _managed_sync_client: OnesClient | None = field(default=None, init=False, repr=False)

    async def __aenter__(self) -> OnesGateway:
        await self._get_async_client()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def list_projects(self, include_archived: bool = False) -> list[dict]:
        return await self._call_async("fetch_projects", include_archived=include_archived)

    async def list_defects(
        self,
        *,
        project_id: str | None = None,
        project_ids: Iterable[str] | None = None,
        issue_type_id: str | None = None,
        sprint_id: str | None = None,
        assignee: str | None = None,
        assign: str | None = None,
        current_user: bool | None = None,
        mine: bool = False,
        limit: int = _DEFAULT_DEFECT_LIMIT,
        page_size: int | None = None,
    ) -> list[dict]:
        query = self._build_defect_query(
            project_id=project_id,
            project_ids=project_ids,
            issue_type_id=issue_type_id,
            sprint_id=sprint_id,
            assignee=assignee,
            assign=assign,
            current_user=current_user,
            mine=mine,
            limit=limit,
            page_size=page_size,
        )
        return await self._list_defects_async(query)

    async def list_current_user_defects(
        self,
        *,
        project_id: str | None = None,
        project_ids: Iterable[str] | None = None,
        issue_type_id: str | None = None,
        sprint_id: str | None = None,
        limit: int = _DEFAULT_DEFECT_LIMIT,
        page_size: int | None = None,
    ) -> list[dict]:
        return await self.list_defects(
            project_id=project_id,
            project_ids=project_ids,
            issue_type_id=issue_type_id,
            sprint_id=sprint_id,
            current_user=True,
            limit=limit,
            page_size=page_size,
        )

    async def get_defect_detail(
        self,
        issue_id: str,
        *,
        project_id: str | None = None,
        project_ids: Iterable[str] | None = None,
        issue_type_id: str | None = None,
        sprint_id: str | None = None,
        assignee: str | None = None,
        assign: str | None = None,
        current_user: bool | None = None,
        mine: bool = False,
        limit: int = _DEFAULT_DEFECT_LIMIT,
        page_size: int | None = None,
    ) -> dict:
        query = self._build_defect_query(
            project_id=project_id,
            project_ids=project_ids,
            issue_type_id=issue_type_id,
            sprint_id=sprint_id,
            assignee=assignee,
            assign=assign,
            current_user=current_user,
            mine=mine,
            limit=limit,
            page_size=page_size,
        )
        if self._uses_scoped_fetch(query):
            defect = await self._find_defect_async(issue_id, query)
            if defect:
                return self._validate_issue_payload(defect, issue_id, context="scoped defect detail")

        detail = await self._call_async("fetch_issue_detail", issue_id)
        return self._resolve_issue_detail(detail, issue_id)

    async def list_project_refs(self, include_archived: bool = False) -> list[ProjectRef]:
        projects = await self.list_projects(include_archived=include_archived)
        return self.normalize_projects(projects)

    async def list_team_members(self, uuids: list[str] | None = None) -> list[dict]:
        return await self._call_async("fetch_team_members", uuids=uuids)

    async def list_role_members(self, project_id: str) -> list[dict]:
        return await self._call_async("fetch_role_members", project_id)

    async def list_normalized_defects(
        self,
        *,
        project_id: str | None = None,
        project_ids: Iterable[str] | None = None,
        issue_type_id: str | None = None,
        sprint_id: str | None = None,
        assignee: str | None = None,
        assign: str | None = None,
        current_user: bool | None = None,
        mine: bool = False,
        limit: int = _DEFAULT_DEFECT_LIMIT,
        page_size: int | None = None,
    ) -> list[DefectRecord]:
        defects = await self.list_defects(
            project_id=project_id,
            project_ids=project_ids,
            issue_type_id=issue_type_id,
            sprint_id=sprint_id,
            assignee=assignee,
            assign=assign,
            current_user=current_user,
            mine=mine,
            limit=limit,
            page_size=page_size,
        )
        return self.normalize_defects(defects)

    async def get_normalized_defect(self, issue_id: str, **kwargs) -> DefectRecord:
        defect = await self.get_defect_detail(issue_id, **kwargs)
        return self.normalize_defect(defect)

    async def list_iterations(self, project_id: str) -> list[dict]:
        return await self._call_async("fetch_sprints", project_id)

    def list_projects_sync(self, include_archived: bool = False) -> list[dict]:
        return self._call_sync("fetch_projects", include_archived=include_archived)

    def list_defects_sync(
        self,
        *,
        project_id: str | None = None,
        project_ids: Iterable[str] | None = None,
        issue_type_id: str | None = None,
        sprint_id: str | None = None,
        assignee: str | None = None,
        assign: str | None = None,
        current_user: bool | None = None,
        mine: bool = False,
        limit: int = _DEFAULT_DEFECT_LIMIT,
        page_size: int | None = None,
    ) -> list[dict]:
        query = self._build_defect_query(
            project_id=project_id,
            project_ids=project_ids,
            issue_type_id=issue_type_id,
            sprint_id=sprint_id,
            assignee=assignee,
            assign=assign,
            current_user=current_user,
            mine=mine,
            limit=limit,
            page_size=page_size,
        )
        return self._list_defects_sync_internal(query)

    def list_current_user_defects_sync(
        self,
        *,
        project_id: str | None = None,
        project_ids: Iterable[str] | None = None,
        issue_type_id: str | None = None,
        sprint_id: str | None = None,
        limit: int = _DEFAULT_DEFECT_LIMIT,
        page_size: int | None = None,
    ) -> list[dict]:
        return self.list_defects_sync(
            project_id=project_id,
            project_ids=project_ids,
            issue_type_id=issue_type_id,
            sprint_id=sprint_id,
            current_user=True,
            limit=limit,
            page_size=page_size,
        )

    def get_defect_detail_sync(
        self,
        issue_id: str,
        *,
        project_id: str | None = None,
        project_ids: Iterable[str] | None = None,
        issue_type_id: str | None = None,
        sprint_id: str | None = None,
        assignee: str | None = None,
        assign: str | None = None,
        current_user: bool | None = None,
        mine: bool = False,
        limit: int = _DEFAULT_DEFECT_LIMIT,
        page_size: int | None = None,
    ) -> dict:
        query = self._build_defect_query(
            project_id=project_id,
            project_ids=project_ids,
            issue_type_id=issue_type_id,
            sprint_id=sprint_id,
            assignee=assignee,
            assign=assign,
            current_user=current_user,
            mine=mine,
            limit=limit,
            page_size=page_size,
        )
        if self._uses_scoped_fetch(query):
            defect = self._find_defect_sync(issue_id, query)
            if defect:
                return self._validate_issue_payload(defect, issue_id, context="scoped defect detail")

        detail = self._call_sync("fetch_issue_detail", issue_id)
        return self._resolve_issue_detail(detail, issue_id)

    def get_normalized_defect_sync(self, issue_id: str, **kwargs) -> DefectRecord:
        return self.normalize_defect(self.get_defect_detail_sync(issue_id, **kwargs))

    def list_project_refs_sync(self, include_archived: bool = False) -> list[ProjectRef]:
        projects = self.list_projects_sync(include_archived=include_archived)
        return self.normalize_projects(projects)

    async def close(self) -> None:
        if self._managed_async_client is not None:
            await self._managed_async_client.close()
            self._managed_async_client = None

    async def _list_defects_async(self, query: _DefectQuery) -> list[dict]:
        client = await self._get_async_client()
        return await self._collect_defects_async(client, query)

    async def _find_defect_async(self, issue_id: str, query: _DefectQuery) -> dict:
        defects = await self._list_defects_async(query)
        return self._select_issue(defects, issue_id)

    def _list_defects_sync_internal(self, query: _DefectQuery) -> list[dict]:
        client = self._get_sync_client()
        return self._collect_defects_sync(client, query)

    def _find_defect_sync(self, issue_id: str, query: _DefectQuery) -> dict:
        defects = self._list_defects_sync_internal(query)
        return self._select_issue(defects, issue_id)

    def normalize_defects(self, defects: list[dict]) -> list[DefectRecord]:
        return [self.normalize_defect(defect) for defect in defects]

    def normalize_projects(self, projects: list[dict]) -> list[ProjectRef]:
        return [self.normalize_project(project) for project in projects]

    def normalize_project(self, project: dict) -> ProjectRef:
        project = self._require_mapping(project, context="project payload")
        project_id = self._require_field(project, "uuid", context="project payload")
        return ProjectRef(
            id=project_id,
            name=project.get("name", ""),
        )

    def normalize_defect(self, defect: dict) -> DefectRecord:
        defect = self._require_mapping(defect, context="defect payload")
        defect_id = self._require_field(defect, "uuid", context="defect payload")
        title = self._require_field(defect, "name", context=f"defect payload {defect_id}")
        project = self._require_nested_mapping(defect, "project", context=f"defect payload {defect_id}")
        status = self._require_nested_mapping(defect, "status", context=f"defect payload {defect_id}")
        issue_type = self._require_nested_mapping(defect, "issueType", context=f"defect payload {defect_id}")
        priority = self._require_nested_mapping(defect, "priority", context=f"defect payload {defect_id}")

        return DefectRecord(
            defect_id=defect_id,
            title=title,
            number=str(defect.get("number", "") or ""),
            project=ProjectRef(
                id=project.get("uuid", ""),
                name=project.get("name", ""),
            ),
            status=StatusRef(
                id=status.get("uuid", ""),
                name=status.get("name", ""),
                category=status.get("category", ""),
            ),
            issue_type=IssueTypeRef(
                id=issue_type.get("uuid", ""),
                name=issue_type.get("name", ""),
            ),
            priority=PriorityRef(
                id=priority.get("uuid", ""),
                value=self._priority_value(priority),
                position=priority.get("position"),
            ),
            assignee=self._identity_ref(defect.get("assign")),
            owner=self._identity_ref(defect.get("owner")),
            parent=self._identity_ref(defect.get("parent")),
            path=defect.get("path", ""),
            description=defect.get("description", ""),
            deadline=defect.get("deadline", ""),
            created_at=defect.get("createTime", ""),
            updated_at=defect.get("serverUpdateStamp", ""),
            source="ones",
            raw=defect,
        )

    async def _call_async(self, method_name: str, *args, **kwargs):
        try:
            client = await self._get_async_client()
            method = getattr(client, method_name)
            return await method(*args, **kwargs)
        except Exception as exc:
            raise self._map_exception(exc, context=f"async ONES {method_name}") from exc

    def _call_sync(self, method_name: str, *args, **kwargs):
        try:
            client = self._get_sync_client()
            method = getattr(client, method_name)
            return method(*args, **kwargs)
        except Exception as exc:
            raise self._map_exception(exc, context=f"sync ONES {method_name}") from exc

    async def _collect_defects_async(self, client, query: _DefectQuery) -> list[dict]:
        if query.limit <= 0:
            return []

        project_ids = query.project_ids or (None,)
        defects: list[dict] = []
        seen_issue_ids: set[str] = set()
        remaining = query.limit

        for project_id in project_ids:
            fetch_limit = min(remaining, query.page_size)
            fetched = await self._fetch_defects_async(
                client,
                query,
                project_id=project_id,
                limit=fetch_limit,
            )
            self._extend_unique_defects(defects, fetched, seen_issue_ids)
            remaining = query.limit - len(defects)
            if remaining <= 0:
                break

        return defects[:query.limit]

    def _collect_defects_sync(self, client, query: _DefectQuery) -> list[dict]:
        if query.limit <= 0:
            return []

        project_ids = query.project_ids or (None,)
        defects: list[dict] = []
        seen_issue_ids: set[str] = set()
        remaining = query.limit

        for project_id in project_ids:
            fetch_limit = min(remaining, query.page_size)
            fetched = self._fetch_defects_sync(
                client,
                query,
                project_id=project_id,
                limit=fetch_limit,
            )
            self._extend_unique_defects(defects, fetched, seen_issue_ids)
            remaining = query.limit - len(defects)
            if remaining <= 0:
                break

        return defects[:query.limit]

    async def _fetch_defects_async(
        self,
        client,
        query: _DefectQuery,
        *,
        project_id: str | None,
        limit: int,
    ) -> list[dict]:
        kwargs = {
            "project_id": project_id,
            "issue_type_id": query.issue_type_id,
            "limit": limit,
        }
        if query.sprint_id:
            kwargs["sprint_id"] = query.sprint_id
        if query.mine:
            try:
                return await client.fetch_my_defects(**kwargs)
            except Exception as exc:
                raise self._map_exception(exc, context="async ONES fetch_my_defects") from exc

        if query.assignee:
            kwargs["assign"] = query.assignee
        try:
            return await client.fetch_defects(**kwargs)
        except Exception as exc:
            raise self._map_exception(exc, context="async ONES fetch_defects") from exc

    def _fetch_defects_sync(
        self,
        client,
        query: _DefectQuery,
        *,
        project_id: str | None,
        limit: int,
    ) -> list[dict]:
        kwargs = {
            "project_id": project_id,
            "issue_type_id": query.issue_type_id,
            "limit": limit,
        }
        if query.sprint_id:
            kwargs["sprint_id"] = query.sprint_id
        if query.mine:
            try:
                return client.fetch_my_defects(**kwargs)
            except Exception as exc:
                raise self._map_exception(exc, context="sync ONES fetch_my_defects") from exc

        if query.assignee:
            kwargs["assign"] = query.assignee
        try:
            return client.fetch_defects(**kwargs)
        except Exception as exc:
            raise self._map_exception(exc, context="sync ONES fetch_defects") from exc

    async def _get_async_client(self) -> OnesAsyncClient:
        if self.async_client is not None:
            return self.async_client

        try:
            from config.settings import OnesSettings
            from src.integrations.ones_api import OnesAsyncClient

            if self._managed_async_client is None:
                self._managed_async_client = OnesAsyncClient(self.settings or OnesSettings())
                await self._managed_async_client._get_client()
            return self._managed_async_client
        except Exception as exc:
            raise self._map_exception(exc, context="async ONES client initialization") from exc

    def _get_sync_client(self) -> OnesClient:
        if self.sync_client is not None:
            return self.sync_client

        try:
            from src.integrations.ones import OnesClient

            if self._managed_sync_client is None:
                self._managed_sync_client = OnesClient()
            return self._managed_sync_client
        except Exception as exc:
            raise self._map_exception(exc, context="sync ONES client initialization") from exc

    @staticmethod
    def _build_defect_query(
        *,
        project_id: str | None,
        project_ids: Iterable[str] | None,
        issue_type_id: str | None,
        sprint_id: str | None,
        assignee: str | None,
        assign: str | None,
        current_user: bool | None,
        mine: bool,
        limit: int,
        page_size: int | None,
    ) -> _DefectQuery:
        normalized_project_ids = OnesGateway._normalize_project_ids(project_id, project_ids)
        normalized_assignee = assignee if assignee is not None else assign
        use_current_user = bool(current_user) or mine or normalized_assignee == _CURRENT_USER_TOKEN
        if use_current_user:
            normalized_assignee = None

        normalized_limit = OnesGateway._normalize_limit(limit)
        normalized_page_size = OnesGateway._normalize_limit(
            page_size if page_size is not None else normalized_limit,
        )
        if normalized_page_size <= 0 and normalized_limit > 0:
            normalized_page_size = normalized_limit

        return _DefectQuery(
            project_ids=normalized_project_ids,
            issue_type_id=issue_type_id or None,
            sprint_id=sprint_id or None,
            assignee=normalized_assignee or None,
            mine=use_current_user,
            limit=normalized_limit,
            page_size=normalized_page_size,
        )

    @staticmethod
    def _normalize_project_ids(project_id: str | None, project_ids: Iterable[str] | None) -> tuple[str, ...]:
        normalized: list[str] = []
        seen: set[str] = set()

        for raw_value in (project_id, *(project_ids or [])):
            value = (raw_value or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)

        return tuple(normalized)

    @staticmethod
    def _normalize_limit(value: int | None) -> int:
        if value is None:
            return _DEFAULT_DEFECT_LIMIT
        if value <= 0:
            return 0
        return value

    @staticmethod
    def _uses_scoped_fetch(query: _DefectQuery) -> bool:
        return bool(
            query.mine
            or query.project_ids
            or query.issue_type_id
            or query.sprint_id
            or query.assignee
        )

    @staticmethod
    def _extend_unique_defects(defects: list[dict], fetched: list[dict], seen_issue_ids: set[str]) -> None:
        for defect in fetched:
            issue_id = defect.get("uuid")
            if issue_id and issue_id in seen_issue_ids:
                continue
            if issue_id:
                seen_issue_ids.add(issue_id)
            defects.append(defect)

    @staticmethod
    def _select_issue(defects: list[dict], issue_id: str) -> dict:
        for defect in defects:
            if not isinstance(defect, dict):
                raise OnesGatewayPayloadError(
                    f"Malformed ONES payload in defect lookup: expected mapping entries while resolving {issue_id}",
                )
            current_issue_id = defect.get("uuid")
            if current_issue_id is None:
                raise OnesGatewayPayloadError(
                    f"Malformed ONES payload in defect lookup: missing 'uuid' while resolving {issue_id}",
                )
            if current_issue_id == issue_id:
                return defect

        return {}

    @staticmethod
    def _resolve_issue_detail(detail: dict, issue_id: str) -> dict:
        if not detail:
            raise OnesGatewayNotFoundError(f"ONES issue not found: {issue_id}")

        validated = OnesGateway._validate_issue_payload(detail, issue_id, context="issue detail")
        if validated.get("uuid") != issue_id:
            raise OnesGatewayNotFoundError(f"ONES issue not found: {issue_id}")
        return validated

    @staticmethod
    def _validate_issue_payload(defect: dict, issue_id: str, *, context: str) -> dict:
        defect = OnesGateway._require_mapping(defect, context=context)
        defect_uuid = OnesGateway._require_field(defect, "uuid", context=context)
        if defect_uuid != issue_id:
            raise OnesGatewayNotFoundError(f"ONES issue not found: {issue_id}")
        return defect

    @staticmethod
    def _require_mapping(payload: object, *, context: str) -> dict:
        if not isinstance(payload, dict):
            raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}: expected mapping")
        return payload

    @staticmethod
    def _require_nested_mapping(payload: dict, field_name: str, *, context: str) -> dict:
        nested = payload.get(field_name)
        if not isinstance(nested, dict):
            raise OnesGatewayPayloadError(
                f"Malformed ONES payload for {context}: missing or invalid '{field_name}'",
            )
        return nested

    @staticmethod
    def _require_field(payload: dict, field_name: str, *, context: str) -> str:
        value = payload.get(field_name)
        if value is None:
            raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}: missing '{field_name}'")
        normalized = str(value).strip()
        if not normalized:
            raise OnesGatewayPayloadError(f"Malformed ONES payload for {context}: empty '{field_name}'")
        return normalized

    @staticmethod
    def _map_exception(exc: Exception, *, context: str) -> OnesGatewayError:
        if isinstance(exc, OnesGatewayError):
            return exc

        if OnesGateway._is_timeout_error(exc):
            return OnesGatewayTimeoutError(f"ONES upstream timeout during {context}")

        if OnesGateway._is_auth_error(exc):
            return OnesGatewayAuthError(f"ONES authentication failed during {context}")

        if isinstance(exc, (KeyError, IndexError, TypeError, ValueError)):
            return OnesGatewayPayloadError(f"Malformed ONES payload during {context}: {exc}")

        return OnesGatewayError(f"ONES gateway request failed during {context}: {exc}")

    @staticmethod
    def _is_timeout_error(exc: Exception) -> bool:
        if isinstance(exc, TimeoutError):
            return True

        class_name = exc.__class__.__name__.lower()
        return "timeout" in class_name

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        if status_code is None and response is not None:
            status_code = getattr(response, "status_code", None)

        return status_code in {401, 403}

    @staticmethod
    def _identity_ref(payload: dict | None) -> IdentityRef | None:
        if not payload:
            return None
        return IdentityRef(
            id=payload.get("uuid", ""),
            name=payload.get("name", ""),
            avatar=payload.get("avatar", ""),
        )

    @staticmethod
    def _priority_value(priority: dict) -> str:
        return str(priority.get("value", "") or priority.get("name", ""))


__all__ = [
    "OnesGateway",
    "OnesGatewayAuthError",
    "OnesGatewayError",
    "OnesGatewayNotFoundError",
    "OnesGatewayPayloadError",
    "OnesGatewayTimeoutError",
]
