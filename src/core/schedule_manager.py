"""定时任务管理器 - 管理多个 scheduled_task，按 cron 表达式触发扫描+AI+通知"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog

from config.settings import Settings
from src.core.engine import Engine
from src.integrations.codebase import Codebase
from src.integrations.ones_api import OnesAsyncClient
from src.integrations.notification import NotificationService, NotifyTarget
from src.llm.analyzer import Analyzer
from src.llm.planner import Planner

log = structlog.get_logger()


class ScheduleManager:
    """管理多个定时任务，每分钟检查一次是否有需要执行的任务"""

    def __init__(self, settings: Settings, engine: Engine):
        self._settings = settings
        self._engine = engine
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_check: datetime | None = None
        # 记录每个任务上次执行的小时+分钟，防止同一分钟内重复执行
        self._last_fired: dict[str, str] = {}
        self._codebase_cache: dict[tuple[str, str], Codebase] = {}
        self._codebase_lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_check(self) -> datetime | None:
        return self._last_check

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info("schedule_manager_started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("schedule_manager_stopped")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._check_and_run()
            except Exception as e:
                log.error("schedule_manager_check_failed", error=str(e))
            await asyncio.sleep(60)  # 每分钟检查一次

    async def _check_and_run(self) -> None:
        now = datetime.now(timezone.utc)
        self._last_check = now
        tasks = self._engine.list_enabled_scheduled_tasks()
        if not tasks:
            return

        for task in tasks:
            cron = task["cronExpr"]
            task_id = task["id"]
            fire_key = f"{task_id}:{now.strftime('%Y%m%d%H%M')}"
            if fire_key == self._last_fired.get(task_id):
                continue  # 本分钟已执行过
            if _should_fire(cron, now):
                self._last_fired[task_id] = fire_key
                asyncio.create_task(self._execute_task(task))

    async def _execute_task(self, task: dict) -> None:
        task_id = task["id"]
        name = task["name"]
        project_id = task["projectId"]
        assignee_id = task.get("assigneeId", "")
        item_type = task["itemType"]  # all / defect / requirement
        action = task["action"]  # plan / analyze
        notify_emails = [e.strip() for e in task["notifyEmails"].split(",") if e.strip()]
        notify_wechat = task["notifyWechat"]

        log.info("schedule_task_start", id=task_id, name=name)
        run = self._engine.create_scheduled_task_run(task_id)
        run_id = run["id"]

        try:
            # 1. 从 ONES 获取工作项
            items = await self._fetch_items(project_id, item_type, assignee_id=assignee_id)
            if not items:
                log.info("schedule_task_no_items", id=task_id)
                self._engine.update_scheduled_task_run(task_id, 0)
                self._engine.finish_scheduled_task_run(run_id, status="success", item_count=0)
                return

            # 2. AI 处理；分析任务会尽量使用项目绑定的 Git 仓库做代码根因分析
            codebase = await self._get_codebase(project_id) if action == "analyze" else None
            results = await self._ai_process(items, action, codebase=codebase)
            for result in results:
                self._engine.add_scheduled_task_run_item(run_id, task_id, **result)

            has_error = any(result.get("error_message") for result in results)
            status = "partial" if has_error and results else "success"

            # 3. 通知
            if notify_emails or notify_wechat:
                subject = f"[ONES Agent] {name} - 发现 {len(items)} 个工作项"
                markdown = _build_report(name, items, results, action)
                notifier = NotificationService(self._settings.email, self._settings.wechat)
                target = NotifyTarget(emails=notify_emails, wechat=notify_wechat)
                await notifier.notify(target, subject, markdown)

            self._engine.update_scheduled_task_run(task_id, len(items))
            self._engine.finish_scheduled_task_run(run_id, status=status, item_count=len(items))
            log.info("schedule_task_done", id=task_id, count=len(items))

        except Exception as e:
            log.error("schedule_task_failed", id=task_id, error=str(e))
            self._engine.update_scheduled_task_run(task_id, 0)
            self._engine.finish_scheduled_task_run(run_id, status="failed", item_count=0, error_message=str(e))

    async def _get_codebase(self, project_id: str) -> Codebase | None:
        repo = self._repo_mapping(project_id)
        if not repo:
            return None

        repo_url = repo["repo_url"]
        branch = repo["branch"]
        cache_key = (repo_url, branch)
        async with self._codebase_lock:
            if cache_key in self._codebase_cache:
                log.info("schedule_task_codebase_cache_hit", repo_url=repo_url, branch=branch)
                return self._codebase_cache[cache_key]

            codebase = await asyncio.to_thread(self._build_codebase_from_repo, repo_url, branch)
            if codebase:
                self._codebase_cache[cache_key] = codebase
            return codebase

    async def _fetch_items(self, project_id: str, item_type: str, assignee_id: str = "") -> list[dict]:
        s = self._settings
        if not s.ones.email or not s.ones.password:
            return []
        iteration_id = self._iteration_id_for_project(project_id)
        async with OnesAsyncClient(s.ones) as client:
            if assignee_id:
                items = await client.fetch_defects(
                    project_id=project_id or None,
                    sprint_id=iteration_id or None,
                    assign=assignee_id,
                )
            else:
                # 仅扫描当前 ONES 账号工作项；item_type 继续在获取后过滤
                items = await client.fetch_my_defects(
                    project_id=project_id or None,
                    sprint_id=iteration_id or None,
                )
        if item_type == "all":
            return items
        # 按 issueType 名称过滤
        type_keywords = {"defect": ["缺陷", "bug", "defect"], "requirement": ["需求", "requirement", "story", "task"]}
        keywords = type_keywords.get(item_type, [])
        return [i for i in items if any(
            k in (i.get("issueType", {}).get("name", "") or "").lower() for k in keywords
        )]

    def _repo_mapping(self, project_id: str) -> dict[str, str] | None:
        mapped_project_id = project_id or self._settings.ones.project_id
        if not mapped_project_id:
            log.info("schedule_task_no_project_for_repo_mapping")
            return None

        repo = self._engine.get_repo_for_project(mapped_project_id)
        if not repo:
            log.info("schedule_task_repo_mapping_not_found", project_id=mapped_project_id)
            return None

        repo_url = repo.get("repoUrl", "")
        if not repo_url:
            log.warning("schedule_task_repo_mapping_empty", project_id=mapped_project_id)
            return None

        branch = repo.get("branch") or self._settings.git.default_branch or "main"
        return {"repo_url": repo_url, "branch": branch}

    def _iteration_id_for_project(self, project_id: str) -> str:
        mapped_project_id = project_id or self._settings.ones.project_id
        if not mapped_project_id:
            return ""
        repo = self._engine.get_repo_for_project(mapped_project_id)
        if not repo:
            return ""
        return str(repo.get("iterationId", "") or "")

    def _build_codebase(self, project_id: str) -> Codebase | None:
        repo = self._repo_mapping(project_id)
        if not repo:
            return None
        return self._build_codebase_from_repo(repo["repo_url"], repo["branch"])

    def _build_codebase_from_repo(self, repo_url: str, branch: str) -> Codebase | None:
        try:
            return Codebase(repo_url=repo_url, branch=branch)
        except Exception as e:
            log.warning(
                "schedule_task_codebase_load_failed",
                repo_url=repo_url,
                branch=branch,
                error=str(e),
            )
            return None

    async def _ai_process(self, items: list[dict], action: str, codebase: Codebase | None = None) -> list[dict]:
        s = self._settings
        if not s.llm.api_key:
            return [
                self._result_base(item, action, error_message="LLM API key not configured")
                for item in items
            ]

        results = []
        if action == "plan":
            planner = Planner(s.llm)
            for item in items:
                try:
                    plan = await planner.plan(item)
                    results.append({
                        **self._result_base(item, action),
                        "name": item.get("name", ""),
                        "summary": plan.summary,
                        "steps": plan.steps,
                        "risk_level": plan.risk_level,
                        "branch": plan.branch_name,
                        "requires_human_approval": plan.requires_human_approval,
                        "plan_summary": plan.summary,
                        "plan_steps": plan.steps,
                        "branch_name": plan.branch_name,
                    })
                except Exception as e:
                    log.warning("schedule_plan_failed", uuid=item.get("uuid"), error=str(e))
                    results.append(self._result_base(item, action, error_message=str(e)))
        elif action == "analyze":
            analyzer = Analyzer(
                base_url=s.llm.base_url, api_key=s.llm.api_key, model=s.llm.model,
            )
            for item in items:
                try:
                    analysis = analyzer.analyze(item, codebase=codebase)
                    results.append({
                        **self._result_base(item, action),
                        "name": item.get("name", ""),
                        "analysis": analysis,
                        "with_codebase": codebase is not None,
                        "analysis_markdown": analysis,
                    })
                except Exception as e:
                    log.warning("schedule_analyze_failed", uuid=item.get("uuid"), error=str(e))
                    results.append(self._result_base(item, action, error_message=str(e), with_codebase=codebase is not None))
        return results

    def _result_base(
        self,
        item: dict,
        action: str,
        error_message: str = "",
        with_codebase: bool = False,
    ) -> dict:
        return {
            "name": item.get("name", ""),
            "item_uuid": item.get("uuid", ""),
            "item_name": item.get("name", ""),
            "item_type": item.get("issueType", {}).get("name", ""),
            "project_id": item.get("project", {}).get("uuid", ""),
            "project_name": item.get("project", {}).get("name", ""),
            "assignee": item.get("assign", {}).get("name", ""),
            "status_name": item.get("status", {}).get("name", ""),
            "priority_name": item.get("priority", {}).get("name", "") or item.get("priority", {}).get("value", ""),
            "action": action,
            "risk_level": "",
            "requires_human_approval": False,
            "analysis_markdown": "",
            "with_codebase": with_codebase,
            "error_message": error_message,
            "item_snapshot": item,
        }

    async def run_task_now(self, task_id: str) -> int:
        """手动触发指定定时任务"""
        task = self._engine.get_scheduled_task(task_id)
        if not task:
            raise ValueError(f"Scheduled task not found: {task_id}")
        await self._execute_task(task)
        return task.get("lastRunCount", 0)


def _should_fire(cron_expr: str, now: datetime) -> bool:
    """简易 cron 解析 - 支持 '*/30 * * * *' 格式（分 时 日 月 周）"""
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        # 简单 interval 格式如 '30m', '1h'
        return _should_fire_interval(cron_expr, now)

    fields = [now.minute, now.hour, now.day, now.month, now.weekday()]
    for i, part in enumerate(parts):
        if part == "*":
            continue
        if part.startswith("*/"):
            step = int(part[2:])
            if fields[i] % step != 0:
                return False
        elif "," in part:
            if fields[i] not in [int(v) for v in part.split(",")]:
                return False
        elif int(part) != fields[i]:
            return False
    return True


def _should_fire_interval(expr: str, now: datetime) -> bool:
    """简易 interval 格式: '30m', '1h', '2h30m'"""
    total_minutes = 0
    expr = expr.strip().lower()
    import re
    for m in re.finditer(r"(\d+)(h|m)", expr):
        val, unit = int(m.group(1)), m.group(2)
        total_minutes += val * (60 if unit == "h" else 1)
    if total_minutes <= 0:
        return False
    return (now.hour * 60 + now.minute) % total_minutes == 0


def _build_report(name: str, items: list[dict], results: list[dict], action: str) -> str:
    parts = [f"## 📋 {name}\n"]
    parts.append(f"**扫描到 {len(items)} 个工作项**\n")
    if action == "plan" and results:
        for r in results:
            parts.append(f"### {r['name']}\n")
            parts.append(f"- 摘要: {r.get('summary', '')}")
            parts.append(f"- 风险: {r.get('risk_level', '')}")
            parts.append(f"- 分支: {r.get('branch', '')}")
            if r.get("steps"):
                for j, step in enumerate(r["steps"], 1):
                    parts.append(f"  {j}. {step}")
            parts.append("---\n")
    elif action == "analyze" and results:
        for r in results:
            parts.append(f"### {r['name']}\n")
            parts.append(r.get("analysis", ""))
            parts.append("---\n")
    else:
        for item in items[:20]:
            status = item.get("status", {}).get("name", "?")
            priority = item.get("priority", {}).get("name", "?")
            assignee = item.get("assign", {}).get("name", "未分配")
            parts.append(f"- **{item.get('name', '?')}** `{status}` `{priority}` 负责人: {assignee}")
    return "\n".join(parts)
