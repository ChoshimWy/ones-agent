"""CI/CD 触发与结果轮询 - GitHub Actions / GitLab CI / Jenkins"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Any

import httpx
import structlog

from config.settings import GitSettings

log = structlog.get_logger()


class PipelineStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"


class PipelineResult:
    __slots__ = ("status", "pipeline_id", "url", "duration", "pass_rate", "failures", "coverage")

    def __init__(self, **kwargs):
        for s in self.__slots__:
            setattr(self, s, kwargs.get(s))

    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}


class CICDClient:
    """CI/CD 客户端

    用法:
        client = CICDClient(git_settings)
        result = await client.trigger_and_wait("feat/ONES-1-test", timeout=300)
        if result.status == PipelineStatus.SUCCESS:
            ...
    """

    def __init__(self, git_settings: GitSettings | None = None):
        self._settings = git_settings or GitSettings()
        self._platform = self._detect_platform()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    def _detect_platform(self) -> str:
        url = self._settings.repo_url.lower()
        if "github" in url:
            return "github"
        if "gitlab" in url:
            return "gitlab"
        return "generic"

    async def trigger_pipeline(self, branch: str) -> str:
        if self._platform == "github":
            return await self._trigger_github(branch)
        if self._platform == "gitlab":
            return await self._trigger_gitlab(branch)
        log.warning("cicd_trigger_unsupported", platform=self._platform)
        return ""

    async def poll_status(self, pipeline_id: str) -> PipelineResult:
        if self._platform == "github":
            return await self._poll_github(pipeline_id)
        if self._platform == "gitlab":
            return await self._poll_gitlab(pipeline_id)
        return PipelineResult(status=PipelineStatus.PENDING, pipeline_id=pipeline_id)

    async def trigger_and_wait(
        self,
        branch: str,
        timeout: int = 300,
        interval: int = 15,
    ) -> PipelineResult:
        pipeline_id = await self.trigger_pipeline(branch)
        if not pipeline_id:
            return PipelineResult(status=PipelineStatus.FAILED)

        elapsed = 0
        while elapsed < timeout:
            result = await self.poll_status(pipeline_id)
            if result.status in (PipelineStatus.SUCCESS, PipelineStatus.FAILED):
                return result
            log.info("cicd_polling", pipeline_id=pipeline_id, status=result.status, elapsed=elapsed)
            await asyncio.sleep(interval)
            elapsed += interval

        log.warning("cicd_timeout", pipeline_id=pipeline_id, timeout=timeout)
        return PipelineResult(status=PipelineStatus.TIMEOUT, pipeline_id=pipeline_id, duration=elapsed)

    # ── GitHub Actions ──────────────────────────────────────

    async def _trigger_github(self, branch: str) -> str:
        repo = self._extract_repo()
        if not repo:
            return ""
        client = await self._get_client()
        headers = {
            "Authorization": f"Bearer {self._settings.pat}",
            "Accept": "application/vnd.github+json",
        }
        resp = await client.post(
            f"https://api.github.com/repos/{repo}/actions/workflows/ci.yml/dispatches",
            json={"ref": branch},
            headers=headers,
        )
        if resp.status_code in (200, 204):
            log.info("github_triggered", repo=repo, branch=branch)
            return f"github:{repo}:{branch}"
        log.error("github_trigger_failed", status=resp.status_code, body=resp.text[:200])
        return ""

    async def _poll_github(self, pipeline_id: str) -> PipelineResult:
        parts = pipeline_id.split(":")
        if len(parts) < 3:
            return PipelineResult(status=PipelineStatus.FAILED, pipeline_id=pipeline_id)
        repo = parts[1]
        branch = parts[2]
        client = await self._get_client()
        headers = {
            "Authorization": f"Bearer {self._settings.pat}",
            "Accept": "application/vnd.github+json",
        }
        resp = await client.get(
            f"https://api.github.com/repos/{repo}/actions/runs?branch={branch}&per_page=1",
            headers=headers,
        )
        if resp.status_code != 200:
            return PipelineResult(status=PipelineStatus.PENDING, pipeline_id=pipeline_id)
        runs = resp.json().get("workflow_runs", [])
        if not runs:
            return PipelineResult(status=PipelineStatus.PENDING, pipeline_id=pipeline_id)
        run = runs[0]
        status_map = {
            "completed": PipelineStatus.SUCCESS if run.get("conclusion") == "success" else PipelineStatus.FAILED,
            "in_progress": PipelineStatus.RUNNING,
            "queued": PipelineStatus.PENDING,
            "waiting": PipelineStatus.PENDING,
        }
        status = status_map.get(run.get("status", ""), PipelineStatus.PENDING)
        return PipelineResult(
            status=status,
            pipeline_id=str(run.get("id", "")),
            url=run.get("html_url", ""),
            duration=run.get("run_duration_ms", 0),
        )

    # ── GitLab CI ───────────────────────────────────────────

    async def _trigger_gitlab(self, branch: str) -> str:
        log.warning("gitlab_trigger_not_implemented")
        return ""

    async def _poll_gitlab(self, pipeline_id: str) -> PipelineResult:
        return PipelineResult(status=PipelineStatus.PENDING, pipeline_id=pipeline_id)

    # ── 辅助 ────────────────────────────────────────────────

    def _extract_repo(self) -> str:
        url = self._settings.repo_url
        if "github.com" in url:
            parts = url.replace("https://", "").replace("http://", "").split("/")
            if len(parts) >= 3:
                return f"{parts[1]}/{parts[2].removesuffix('.git')}"
        return ""

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
