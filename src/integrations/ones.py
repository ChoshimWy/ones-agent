"""ONES API 客户端 - 账号密码登录 + GraphQL 缺陷获取"""

from __future__ import annotations

import base64
import hashlib
import json
import os

import requests

from config import (
    ONES_BASE_URL, ONES_EMAIL, ONES_PASSWORD,
    ONES_TEAM_ID, ONES_PROJECT_ID, ONES_ISSUE_TYPE_ID,
)
from src.utils.encrypt import JSEncryptPython

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
      uuid
      name
      number
      createTime
      serverUpdateStamp
      deadline(unit: ONESDATE)
      path
      subTaskCount
      subTaskDoneCount
      status { uuid name category }
      issueType { uuid name }
      subIssueType { uuid name }
      project { uuid name }
      parent { uuid name }
      assign { uuid name avatar }
      owner { uuid name avatar }
      priority { uuid value position }
      estimatedHours
      remainingManhour
      totalEstimatedHours
      totalRemainingHours
      issueTypeScope { uuid }
    }
    pageInfo {
      count totalCount hasNextPage endCursor
    }
  }
}"""

GQL_FETCH_PROJECTS = """{
  buckets(groupBy: $groupBy, orderBy: $orderBy, pagination: {limit: 50, after: "", preciseCount: true}) {
    key
    projects(limit: 10000, orderBy: $projectOrderBy, filterGroup: $projectFilterGroup) {
      uuid
      name
      icon
      status { uuid name category }
      isPin
      isArchive
      assign { uuid name avatar }
      owner { uuid name avatar }
      createTime
      planStartTime(unit: ONESDATE)
      planEndTime(unit: ONESDATE)
      type
    }
    pageInfo { count totalCount hasNextPage }
  }
}"""


def _encrypt_password(password: str) -> str:
    rsa = JSEncryptPython()
    rsa.setPublicKey(RSA_PUBLIC_KEY)
    return rsa.encrypt(password)


def _code_verifier(length: int = 43) -> str:
    return "".join(_CHARS[os.urandom(1)[0] % len(_CHARS)] for _ in range(length))


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


class OnesClient:
    def __init__(
        self,
        base_url: str = ONES_BASE_URL,
        email: str = ONES_EMAIL,
        password: str = ONES_PASSWORD,
        team_id: str = ONES_TEAM_ID,
        project_id: str = ONES_PROJECT_ID,
        issue_type_id: str = ONES_ISSUE_TYPE_ID,
    ):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.team_id = team_id
        self.project_id = project_id
        self.issue_type_id = issue_type_id

        if email and password:
            self._login(email, password)

    def _login(self, email: str, password: str) -> None:
        resp = self.session.post(
            f"{self.base_url}/identity/api/login",
            json={"email": email, "password": _encrypt_password(password)},
        )
        resp.raise_for_status()
        org_user = resp.json()["org_users"][0]
        org_uuid = org_user["org_uuid"]
        org_user_uuid = org_user["org_user"]["org_user_uuid"]

        verifier = _code_verifier()
        challenge = _code_challenge(verifier)
        resp = self.session.post(
            f"{self.base_url}/identity/authorize",
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
            allow_redirects=False,
        )
        request_id = resp.headers["Location"].split("id=")[1].split("&")[0]

        self.session.get(f"{self.base_url}/identity/api/auth_request/{request_id}")
        self.session.get(f"{self.base_url}/identity/api/org_users")
        self.session.post(
            f"{self.base_url}/identity/api/auth_request/finalize",
            data=json.dumps({
                "auth_request_id": request_id,
                "region_uuid": "default",
                "org_uuid": org_uuid,
                "org_user_uuid": org_user_uuid,
            }),
            headers={"Content-Type": "application/json;charset=UTF-8"},
        )

        resp = self.session.get(
            f"{self.base_url}/identity/authorize/callback",
            params={"id": request_id, "lang": "zh"},
            allow_redirects=False,
        )
        code = resp.text.split("code=")[1].split("&")[0]

        resp = self.session.post(
            f"{self.base_url}/identity/oauth/token",
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
        self.session.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

    def _graphql_url(self, t: str = "group-task-data") -> str:
        return f"{self.base_url}/project/api/project/team/{self.team_id}/items/graphql?t={t}"

    def _graphql(self, query: str, variables: dict, t: str = "group-task-data") -> dict:
        resp = self.session.post(
            self._graphql_url(t),
            json={"query": query, "variables": variables},
            headers={"Content-Type": "application/json;charset=UTF-8"},
        )
        resp.raise_for_status()
        result = resp.json()
        return result.get("data", result)

    def fetch_projects(self, include_archived: bool = False) -> list[dict]:
        filters: list[dict] = [{"visibleInProject_equal": True}]
        if not include_archived:
            filters.append({"isArchive_equal": False})

        data = self._graphql(GQL_FETCH_PROJECTS, {
            "groupBy": {"projects": {}},
            "orderBy": {},
            "projectOrderBy": {"isPin": "DESC", "namePinyin": "ASC", "createTime": "DESC"},
            "projectFilterGroup": [filters],
        }, t="projects-group-list-for-project-view")

        projects: list[dict] = []
        for bucket in data.get("buckets", []):
            for p in bucket.get("projects", []):
                p["_group"] = bucket.get("key", "")
                projects.append(p)
        return projects

    def fetch_defects(
        self,
        project_id: str | None = None,
        issue_type_id: str | None = None,
        sprint_id: str | None = None,
        assign: str | None = None,
        limit: int = 1000
    ) -> list[dict]:
        pid = project_id or self.project_id
        itid = issue_type_id or self.issue_type_id

        filter = {}
        if pid:
            filter["project_in"] = [pid]
        if itid:
            filter["issueType_in"] = [itid]
        if sprint_id:
            filter["sprint_in"] = [sprint_id]
        if assign:
            filter["assign_in"] = [assign]

        data = self._graphql(GQL_FETCH_TASKS, {
            "groupBy": {"tasks": {"status": {}}},
            "groupOrderBy": {"status": {"category": "DESC", "namePinyin": "ASC"}},
            "orderBy": {"position": "ASC", "createTime": "DESC"},
            "filterGroup": [filter] if filter else [],
            "pagination": {"limit": limit, "preciseCount": False},
            "limit": limit,
        })

        tasks: list[dict] = []
        for bucket in data.get("buckets", []):
            for task in bucket.get("tasks", []):
                task["_status_group"] = bucket.get("key", "")
                tasks.append(task)
        return tasks

    def fetch_issue_detail(self, issue_id: str) -> dict:
        tasks = self.fetch_defects()
        return next((t for t in tasks if t["uuid"] == issue_id), {})

    def fetch_my_defects(self, **kwargs) -> list[dict]:
        return self.fetch_defects(assign="$currentUser", **kwargs)

    def fetch_all_defects(self) -> list[dict]:
        return self.fetch_defects(limit=5000)
