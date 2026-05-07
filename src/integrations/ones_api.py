"""ONES 异步 API 客户端 - httpx async + GraphQL + 评论/状态更新"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

import httpx
import structlog

from src.utils.encrypt import JSEncryptPython
from config.settings import OnesSettings

log = structlog.get_logger()

RSA_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0orxr+Larwt3bqq0yt5D
DgNlOh3D5kDSmidNbr3nHe/ktgr4sTWoVJAFtn2fgLB6e9zf571eeOJJ4hqp5Su2
RRTOhOojE98gEjBAi1fB7OPLR0d2TYzE/P9ahaOhT89noIGQz+Pu2n9wBK/7dg6A
MeJ51Edn4p4WlP+XKWyfH78T6v5hQ9snt5Vtz5wbpEOu+X414ENswIAhLCOCqBzj
khNqfJG/fNH/SjsjbmsqCdedirZAu8DYWBPv1x+vFn7hBOd2G40FnsWAAR8ekHgB
b+wB0DkHlDhIGK6QmbVZh4vKCcPk4QDrGY3rQPGrECGqmIi9BZK75sUeNTec6jp6
gQIDAQAB
-----END PUBLIC KEY-----"""

REDIRECT_URI = "http://aputureones.com:8088/auth/authorize/callback"
_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~"

GQL_FETCH_TASKS = """{
  buckets(
    groupBy: $groupBy
    orderBy: $groupOrderBy
    pagination: $pagination
    filter: $groupFilter
  ) {
    key
    tasks(
      filterGroup: $filterGroup
      orderBy: $orderBy
      limit: $limit
    ) {
      uuid name number createTime serverUpdateStamp
      deadline(unit: ONESDATE) path subTaskCount subTaskDoneCount
      status { uuid name category }
      issueType { uuid name }
      subIssueType { uuid name }
      project { uuid name }
      parent { uuid name }
      assign { uuid name avatar }
      owner { uuid name avatar }
      priority { uuid value position }
      estimatedHours remainingManhour totalEstimatedHours totalRemainingHours
      issueTypeScope { uuid }
    }
    pageInfo { count totalCount hasNextPage endCursor }
  }
}"""

GQL_FETCH_PROJECTS = """{
  buckets(groupBy: $groupBy, orderBy: $orderBy, pagination: {limit: 50, after: "", preciseCount: true}) {
    key
    projects(limit: 10000, orderBy: $projectOrderBy, filterGroup: $projectFilterGroup) {
      uuid name icon status { uuid name category } isPin isArchive
      assign { uuid name avatar } owner { uuid name avatar }
      createTime planStartTime(unit: ONESDATE) planEndTime(unit: ONESDATE) type
    }
    pageInfo { count totalCount hasNextPage }
  }
}"""

GQL_FETCH_SPRINTS = """
query SPRINTS($filterGroup: [Filter!], $orderBy: SprintOrderBy) {
  list: sprints(filterGroup: $filterGroup, orderBy: $orderBy) {
    title: name
    uuid
    key
    project {
      name
      uuid
    }
    statusInfo: status {
      name
      uuid
      category
      key
      categoryValue
    }
    sprintStatusList
  }
}
"""


def _encrypt_password(password: str) -> str:
    rsa = JSEncryptPython()
    rsa.setPublicKey(RSA_PUBLIC_KEY)
    return rsa.encrypt(password)


def _code_verifier(length: int = 43) -> str:
    return "".join(_CHARS[os.urandom(1)[0] % len(_CHARS)] for _ in range(length))


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


class OnesAsyncClient:
    """ONES 异步 API 客户端

    用法:
        async with OnesAsyncClient(settings) as client:
            defects = await client.fetch_defects()
            await client.add_comment("item-id", "分析完成")
            await client.update_status("item-id", "done")
    """

    def __init__(self, settings: OnesSettings | None = None):
        self._settings = settings or OnesSettings()
        self._base_url = self._settings.base_url.rstrip("/")
        self._team_id = self._settings.team_id
        self._project_id = self._settings.project_id
        self._issue_type_id = self._settings.issue_type_id
        self._token: str | None = None
        self._org_uuid: str = ""
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(verify=False, timeout=30.0)
            if self._settings.email and self._settings.password:
                await self._login()
        return self._client

    async def __aenter__(self) -> OnesAsyncClient:
        await self._get_client()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _login(self) -> None:
        client = await self._get_client()
        log.info("ones_login", base_url=self._base_url)

        resp = await client.post(
            f"{self._base_url}/identity/api/login",
            json={"email": self._settings.email, "password": _encrypt_password(self._settings.password)},
        )
        resp.raise_for_status()
        org_user = resp.json()["org_users"][0]
        org_uuid = org_user["org_uuid"]
        self._org_uuid = org_uuid
        org_user_uuid = org_user["org_user"]["org_user_uuid"]

        verifier = _code_verifier()
        challenge = _code_challenge(verifier)
        resp = await client.post(
            f"{self._base_url}/identity/authorize",
            data={
                "client_id": "ones.v1",
                "scope": f"openid offline_access ones:org:{org_uuid}:{org_user_uuid}",
                "response_type": "code",
                "code_challenge_method": "S256",
                "code_challenge": challenge,
                "redirect_uri": REDIRECT_URI,
                "state": f"org_uuid={org_uuid}",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        request_id = resp.headers["Location"].split("id=")[1].split("&")[0]

        await client.get(f"{self._base_url}/identity/api/auth_request/{request_id}")
        await client.get(f"{self._base_url}/identity/api/org_users")
        await client.post(
            f"{self._base_url}/identity/api/auth_request/finalize",
            content=json.dumps({
                "auth_request_id": request_id,
                "region_uuid": "default",
                "org_uuid": org_uuid,
                "org_user_uuid": org_user_uuid,
            }),
            headers={"Content-Type": "application/json;charset=UTF-8"},
        )

        resp = await client.get(
            f"{self._base_url}/identity/authorize/callback",
            params={"id": request_id, "lang": "zh"},
            follow_redirects=False,
        )
        code = resp.text.split("code=")[1].split("&")[0]

        resp = await client.post(
            f"{self._base_url}/identity/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": "ones.v1",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        client.headers["Authorization"] = f"Bearer {self._token}"
        log.info("ones_login_success")

    async def _graphql(self, query: str, variables: dict, t: str = "group-task-data") -> dict:
        client = await self._get_client()
        resp = await client.post(
            f"{self._base_url}/project/api/project/team/{self._team_id}/items/graphql?t={t}",
            json={"query": query, "variables": variables},
            headers={"Content-Type": "application/json;charset=UTF-8"},
        )
        resp.raise_for_status()
        result = resp.json()
        return result.get("data", result)

    # ── 数据获取 ──────────────────────────────────────────

    async def fetch_projects(self, include_archived: bool = False) -> list[dict]:
        filters: list[dict] = [{"visibleInProject_equal": True}]
        if not include_archived:
            filters.append({"isArchive_equal": False})
        data = await self._graphql(GQL_FETCH_PROJECTS, {
            "groupBy": {"projects": {}},
            "orderBy": {},
            "projectOrderBy": {"isPin": "DESC", "namePinyin": "ASC", "createTime": "DESC"},
            "projectFilterGroup": [filters],
        }, t="projects-group-list-for-project-view")
        projects = []
        for bucket in data.get("buckets", []):
            for p in bucket.get("projects", []):
                p["_group"] = bucket.get("key", "")
                projects.append(p)
        return projects

    async def fetch_defects(
        self,
        project_id: str | None = None,
        issue_type_id: str | None = None,
        sprint_id: str | None = None,
        assign: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        pid = project_id or self._project_id
        itid = issue_type_id or self._issue_type_id
        filter_ = {}
        if pid:
            filter_["project_in"] = [pid]
        if itid:
            filter_["issueType_in"] = [itid]
        if sprint_id:
            filter_["sprint_in"] = [sprint_id]
        if assign:
            filter_["assign_in"] = [assign]

        data = await self._graphql(GQL_FETCH_TASKS, {
            "groupBy": {"tasks": {"status": {}}},
            "groupOrderBy": {"status": {"category": "DESC", "namePinyin": "ASC"}},
            "orderBy": {"position": "ASC", "createTime": "DESC"},
            "filterGroup": [filter_] if filter_ else [],
            "pagination": {"limit": limit, "preciseCount": False},
            "limit": limit,
        })
        tasks = []
        for bucket in data.get("buckets", []):
            for task in bucket.get("tasks", []):
                task["_status_group"] = bucket.get("key", "")
                tasks.append(task)
        return tasks

    async def fetch_issue_detail(self, issue_id: str) -> dict:
        tasks = await self.fetch_defects()
        return next((t for t in tasks if t["uuid"] == issue_id), {})

    async def fetch_sprints(self, project_id: str) -> list[dict]:
        project_id = (project_id or "").strip()
        if not project_id:
            return []

        data = await self._graphql(
            GQL_FETCH_SPRINTS,
            {
                "filterGroup": [{"project_in": [project_id], "visibleInProject_equal": True}],
                "orderBy": {"namePinyin": "ASC", "createTime": "ASC"},
            },
            t=f"sprint_select_{project_id}",
        )
        return list(data.get("list", []))

    async def fetch_role_members(self, project_id: str) -> list[dict[str, Any]]:
        project_id = (project_id or "").strip()
        if not project_id:
            return []

        client = await self._get_client()
        resp = await client.get(
            f"{self._base_url}/project/api/project/team/{self._team_id}/project/{project_id}/role_members",
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if isinstance(data.get("role_members"), list):
                return data["role_members"]
            if isinstance(data.get("data"), list):
                return data["data"]
        return []

    async def fetch_team_members(self, uuids: list[str] | None = None) -> list[dict[str, Any]]:
        client = await self._get_client()
        org_uuid = self._org_uuid.strip()
        if not org_uuid:
            return []

        resp = await client.request(
            "POST",
            f"{self._base_url}/project/api/project/organization/{org_uuid}/users",
            params={"team_uuid": self._team_id},
            json={"uuids": list(uuids or [])},
            headers={"Content-Type": "application/json;charset=UTF-8"},
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if isinstance(data.get("users"), list):
                return data["users"]
            if isinstance(data.get("data"), list):
                return data["data"]
        return []

    async def fetch_my_defects(self, **kwargs) -> list[dict]:
        return await self.fetch_defects(assign="$currentUser", **kwargs)

    # ── 评论 & 状态 ────────────────────────────────────────

    async def add_comment(self, item_id: str, text: str) -> dict:
        client = await self._get_client()
        resp = await client.post(
            f"{self._base_url}/project/api/project/team/{self._team_id}/task/{item_id}/comment",
            json={"content": text},
        )
        resp.raise_for_status()
        log.info("ones_add_comment", item_id=item_id)
        return resp.json()

    async def update_status(self, item_id: str, status: str) -> dict:
        client = await self._get_client()
        resp = await client.patch(
            f"{self._base_url}/project/api/project/team/{self._team_id}/task/{item_id}",
            json={"status": status},
        )
        resp.raise_for_status()
        log.info("ones_update_status", item_id=item_id, status=status)
        return resp.json()

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
