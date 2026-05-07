"""定时调度器 - 周期拉取 ONES 缺陷 → 创建 Engine 任务 → LLM 规划"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog

from config.settings import Settings
from src.core.engine import Engine, State
from src.core.store import Store
from src.integrations.ones_api import OnesAsyncClient
from src.llm.planner import Planner

log = structlog.get_logger()

_MASK = "••••••••"


class Scheduler:
    def __init__(self, settings: Settings, engine: Engine):
        self._settings = settings
        self._engine = engine
        self._store = Store()
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_check: datetime | None = None
        self._last_count = 0

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_check(self) -> datetime | None:
        return self._last_check

    @property
    def last_new_count(self) -> int:
        return self._last_count

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info("scheduler_started", interval=self._settings.agent.check_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("scheduler_stopped")

    async def _loop(self) -> None:
        interval = self._settings.agent.check_interval
        while self._running:
            try:
                await self._poll_once()
            except Exception as e:
                log.error("scheduler_poll_failed", error=str(e))
            await asyncio.sleep(interval)

    async def _poll_once(self) -> None:
        s = self._settings
        if not s.ones.email or not s.ones.password:
            log.debug("scheduler_skip_no_credentials")
            return
        if not s.ones.team_id:
            log.debug("scheduler_skip_no_team")
            return

        log.info("scheduler_poll_start")
        async with OnesAsyncClient(s.ones) as client:
            mappings = self._resolve_project_mappings()
            if mappings:
                all_defects: list[dict] = []
                for mapping in mappings:
                    all_defects.extend(
                        await client.fetch_my_defects(
                            project_id=mapping["projectId"],
                            sprint_id=mapping.get("iterationId") or None,
                        )
                    )
                defects = all_defects
            elif s.ones.project_id:
                defects = await client.fetch_my_defects(project_id=s.ones.project_id)
            else:
                defects = await client.fetch_my_defects()

        new = self._store.filter_new(defects)
        self._store.update_check_time()
        self._last_check = datetime.now(timezone.utc)
        self._last_count = len(new)

        if not new:
            log.info("scheduler_poll_no_new")
            return

        log.info("scheduler_new_defects", count=len(new))
        planner = Planner(s.llm) if s.llm.api_key else None

        for defect in new:
            uuid = defect.get("uuid", "")
            name = defect.get("name", "")
            if not uuid:
                continue
            # Double-check: skip if Engine already has this item
            if self._engine.get(uuid):
                continue
            project_id = defect.get("project", {}).get("uuid", "")
            repo_mapping = self._engine.get_repo_for_project(project_id) if project_id else None
            self._engine.start_work(uuid, State.PARSING)
            log.info("scheduler_created_task", uuid=uuid, name=name,
                     project_id=project_id, repo_url=repo_mapping.get("repoUrl") if repo_mapping else None)

            if planner:
                try:
                    self._engine.transition(uuid, State.PLANNING)
                    plan = await planner.plan(defect)
                    branch = plan.branch_name
                    if repo_mapping and not plan.branch_name:
                        branch = repo_mapping.get("branch", "main")
                    self._engine.transition(
                        uuid,
                        State.WAITING_APPROVAL if plan.requires_human_approval else State.CODING,
                        plan_json=plan.model_dump_json(),
                        branch=branch,
                    )
                    log.info("scheduler_planned", uuid=uuid, branch=branch)
                except Exception as e:
                    log.error("scheduler_plan_failed", uuid=uuid, error=str(e))
                    self._engine.transition(uuid, State.FAILED)

    def _resolve_project_ids(self) -> list[str]:
        mappings = self._resolve_project_mappings()
        if mappings:
            return [mapping["projectId"] for mapping in mappings]
        if self._settings.ones.project_id:
            return [self._settings.ones.project_id]
        return []

    def _resolve_project_mappings(self) -> list[dict]:
        mappings = self._engine.list_project_repos()
        if not mappings:
            return []
        seen: set[str] = set()
        unique: list[dict] = []
        for mapping in mappings:
            project_id = (mapping.get("projectId") or "").strip()
            if not project_id or project_id in seen:
                continue
            seen.add(project_id)
            unique.append(mapping)
        return unique

    async def poll_now(self) -> int:
        await self._poll_once()
        return self._last_count

    def status(self) -> dict:
        project_ids = self._resolve_project_ids()
        return {
            "running": self._running,
            "interval": self._settings.agent.check_interval,
            "lastCheck": self._last_check.isoformat() if self._last_check else None,
            "lastNewCount": self._last_count,
            "hasCredentials": bool(self._settings.ones.email and self._settings.ones.password),
            "hasProject": bool(project_ids),
            "projectIds": project_ids,
        }
