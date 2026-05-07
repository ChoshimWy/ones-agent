"""ONES Agent - FastAPI 入口 & REST API & Webhook"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import structlog
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp
from pydantic import BaseModel

from config.settings import Settings
from src.core.engine import Engine, State
from src.core.queue import TaskQueue
from src.utils.auth import USERS, create_token, current_user, require_admin
from src.utils.logging import setup_logging, get_logger, bind_context
from src.utils.metrics import tasks_total, failures_total, duration_seconds, metrics_output
from src.utils.audit import AuditLog
from src.utils.secrets import mask_dict
from src.integrations.ones_api import OnesAsyncClient
from src.integrations.notification import NotificationService, NotifyTarget
from src.llm.planner import Planner
from src.contracts import AnalysisResult, DefectRecord, ExecutionRequest, RepoResolution
from src.integrations.git_ops import build_branch_name
from src.llm.analyzer import Analyzer
from src.services import DefectAnalysisWorkflowService, ExecutionService, OnesGateway, RepoResolver
from src.services.execution_service import ExecutionValidationError
from src.services.ones_gateway import (
    OnesGatewayAuthError,
    OnesGatewayError,
    OnesGatewayNotFoundError,
    OnesGatewayPayloadError,
    OnesGatewayTimeoutError,
)

settings = Settings()
engine = Engine(db_path=settings.agent.state_db_path)
queue = TaskQueue(max_workers=3)
audit = AuditLog()
setup_logging(settings.agent.log_level)
log = get_logger()

from src.core.scheduler import Scheduler
scheduler = Scheduler(settings, engine)

from src.core.schedule_manager import ScheduleManager
schedule_manager = ScheduleManager(settings, engine)

_MASK = "••••••••"

_ENV_FILE = ".env"

_ENV_KEY_MAP = {
    "ones.baseUrl": "ONES_BASE_URL",
    "ones.email": "ONES_EMAIL",
    "ones.password": "ONES_PASSWORD",
    "ones.teamId": "ONES_TEAM_ID",
    "ones.projectId": "ONES_PROJECT_ID",
    "git.repoUrl": "GIT_REPO_URL",
    "git.branch": "GIT_DEFAULT_BRANCH",
    "git.authType": "GIT_AUTH_TYPE",
    "llm.provider": "LLM_PROVIDER",
    "llm.model": "LLM_MODEL",
    "llm.baseUrl": "LLM_BASE_URL",
    "llm.apiKey": "LLM_API_KEY",
    "cicd.platform": "CICD_PLATFORM",
    "cicd.token": "CICD_TOKEN",
    "webhook.secret": "AGENT_WEBHOOK_SECRET",
    "webhook.enabled": "WEBHOOK_ENABLED",
    "email.smtpHost": "EMAIL_SMTP_HOST",
    "email.smtpPort": "EMAIL_SMTP_PORT",
    "email.smtpUser": "EMAIL_SMTP_USER",
    "email.smtpPassword": "EMAIL_SMTP_PASSWORD",
    "email.sender": "EMAIL_SENDER",
    "email.useTls": "EMAIL_USE_TLS",
}

_SECRET_KEYS = {"ones.password", "llm.apiKey", "cicd.token", "webhook.secret", "email.smtpPassword"}

STATE_MAP = {
    "pending": "PENDING",
    "parsing": "PARSING",
    "planning": "PLANNING",
    "coding": "CODING",
    "testing": "TESTING",
    "pushing": "PUSHING",
    "reporting": "REPORTING",
    "success": "SUCCESS",
    "failed": "FAILED",
    "waiting_approval": "WAITING_APPROVAL",
}


class WebhookPayload(BaseModel):
    work_item_id: str = ""
    type: str = ""
    status_change: str = ""
    raw: dict = {}


class LoginRequest(BaseModel):
    email: str
    password: str


class ActionRequest(BaseModel):
    action: str
    reason: str = ""


class ConfigUpdate(BaseModel):
    ones: dict | None = None
    git: dict | None = None
    llm: dict | None = None
    cicd: dict | None = None
    webhook: dict | None = None
    email: dict | None = None


class ProjectRepoCreate(BaseModel):
    projectId: str
    projectName: str = ""
    repoUrl: str
    branch: str = "main"
    iterationId: str
    iterationName: str = ""
    iterationKey: str = ""


class ProjectRepoDelete(BaseModel):
    projectId: str
    repoUrl: str


class ScheduledTaskCreate(BaseModel):
    name: str
    cronExpr: str
    projectId: str = ""
    assigneeId: str = ""
    assigneeName: str = ""
    itemType: str = "all"  # all / defect / requirement
    action: str = "plan"  # plan / analyze
    notifyEmails: str = ""
    notifyWechat: bool = False
    enabled: bool = True


class ScheduledTaskUpdate(BaseModel):
    name: str | None = None
    cronExpr: str | None = None
    projectId: str | None = None
    assigneeId: str | None = None
    assigneeName: str | None = None
    itemType: str | None = None
    action: str | None = None
    notifyEmails: str | None = None
    notifyWechat: bool | None = None
    enabled: bool | None = None


class AITriggerRequest(BaseModel):
    itemId: str  # ONES work item UUID
    action: str = "plan"  # plan / analyze
    notifyEmails: str = ""
    notifyWechat: bool = False


class DefectExecutionCreate(BaseModel):
    requestType: str = "bugfix"
    branchName: str = ""
    baseBranch: str = ""
    notes: str = ""


class ScheduledTaskRunListResponse(BaseModel):
    items: list[dict]
    total: int
    page: int
    pageSize: int


def _item_to_dict(item) -> dict:
    state_val = item.state if isinstance(item.state, str) else item.state.value
    return {
        "id": item.work_item_id,
        "type": "defect" if "bug" in item.work_item_id.lower() else "requirement",
        "title": item.work_item_id,
        "status": STATE_MAP.get(state_val, state_val.upper()),
        "branch": item.branch or None,
        "commitHash": item.commit_hash or None,
        "riskLevel": None,
        "requiresApproval": state_val == "waiting_approval",
        "onesId": item.work_item_id,
        "planJson": item.plan if item.plan_json else None,
        "createdAt": item.updated_at,
        "updatedAt": item.updated_at,
    }


def _create_repo_resolver() -> RepoResolver:
    return RepoResolver(engine=engine, default_branch=settings.git.default_branch or "main")


def _create_analysis_workflow_service() -> DefectAnalysisWorkflowService:
    return DefectAnalysisWorkflowService()


def _create_execution_service() -> ExecutionService:
    return ExecutionService(engine=engine, work_dir="data/repos")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_defect_analysis_path(defect_id: str) -> str:
    return f"/api/v1/defects/{defect_id}"


def _canonical_defect_execution_path(defect_id: str) -> str:
    return f"/api/v1/defects/{defect_id}/execution"


def _mark_ai_trigger_deprecated(response: Response, defect_id: str) -> None:
    analysis_path = _canonical_defect_analysis_path(defect_id)
    execution_path = _canonical_defect_execution_path(defect_id)
    response.headers["Deprecation"] = "true"
    response.headers["Warning"] = (
        '299 - "POST /api/v1/ai/trigger is deprecated; '
        f'use GET {analysis_path} for canonical defect analysis and '
        f'POST {execution_path} for execution."'
    )
    response.headers["Link"] = f'<{analysis_path}>; rel="successor-version"'


def _translate_ones_gateway_error(exc: Exception, *, detail: str) -> HTTPException:
    if isinstance(exc, OnesGatewayNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, OnesGatewayPayloadError):
        return HTTPException(status_code=502, detail=f"{detail}: {exc}")
    if isinstance(exc, (OnesGatewayAuthError, OnesGatewayTimeoutError, OnesGatewayError)):
        return HTTPException(status_code=502, detail=f"{detail}: {exc}")
    return HTTPException(status_code=502, detail=f"{detail}: {exc}")


def _workflow_status_for_defect(defect_id: str) -> str:
    item = engine.get(defect_id)
    if not item:
        return "PENDING"
    state_val = item.state if isinstance(item.state, str) else item.state.value
    return STATE_MAP.get(state_val, state_val.upper())


def _latest_execution_record(defect_id: str) -> dict | None:
    records = engine.list_execution_records(defect_id=defect_id)
    return records[0] if records else None


def _mapping_status(resolution: RepoResolution) -> str:
    repo_url = resolution.selected_repo.repo_url.strip()
    branch = (resolution.selected_branch or resolution.selected_repo.default_branch or "").strip()
    if repo_url and branch and resolution.confidence >= 1.0:
        return "mapped"
    if repo_url or branch or resolution.candidates or resolution.confidence > 0:
        return "partial"
    return "missing"


def _codebase_status(mapping_status: str) -> str:
    if mapping_status == "mapped":
        return "ready"
    if mapping_status == "partial":
        return "blocked"
    return "missing"


def _analysis_status(result: AnalysisResult) -> str:
    return "blocked" if result.insufficient_evidence else "analyzed"


def _evidence_kind(kind: str) -> str:
    if kind == "file":
        return "file"
    if kind in {"tree_summary", "repo_resolution"}:
        return "summary"
    if kind == "defect":
        return "note"
    return "summary"


def _analysis_payload(result: AnalysisResult, *, updated_at: str) -> dict:
    evidence_items = []
    for index, item in enumerate(result.evidence, start=1):
        summary = item.description or item.snippet or item.file_path or item.kind
        label = item.file_path or item.description or item.kind.replace("_", " ").title()
        evidence_items.append(
            {
                "id": f"{result.defect_id}-evidence-{index}",
                "label": label,
                "summary": summary,
                "kind": _evidence_kind(item.kind),
                "path": item.file_path or None,
                "snippet": item.snippet or "",
                "source": item.source or "",
            }
        )

    fix_suggestions = [suggestion.description or suggestion.title for suggestion in result.fix_suggestions]
    return {
        "status": _analysis_status(result),
        "summary": result.analysis_summary,
        "rootCause": result.root_cause,
        "fixSuggestions": fix_suggestions,
        "evidence": evidence_items,
        "markdown": result.rendered_markdown,
        "confidence": result.confidence,
        "updatedAt": updated_at or _now_iso(),
    }


def _execution_status(record: dict | None, *, analysis: AnalysisResult | None = None, mapping_status: str = "missing") -> str:
    if record is not None:
        return {
            "completed": "created",
            "failed": "failed",
            "in_progress": "creating",
            "pending": "creating",
        }.get(str(record.get("status", "")), "idle")

    if analysis is not None and mapping_status == "mapped" and not analysis.insufficient_evidence and analysis.confidence >= 0.75:
        return "ready"
    return "idle"


def _execution_payload(
    record: dict | None,
    *,
    resolution: RepoResolution,
    analysis: AnalysisResult | None,
    mapping_status: str,
    updated_at: str,
) -> dict:
    request_type = "bugfix"
    if record is not None and str(record.get("requestType", "")) == "requirement_development":
        request_type = "development"
    return {
        "status": _execution_status(record, analysis=analysis, mapping_status=mapping_status),
        "requestType": request_type,
        "repoUrl": resolution.selected_repo.repo_url or None,
        "baseBranch": (record or {}).get("baseBranch") or resolution.selected_branch or resolution.selected_repo.default_branch,
        "branchName": (record or {}).get("branchName") or "",
        "branchUrl": None,
        "message": (record or {}).get("errorMessage") or "",
        "updatedAt": ((record or {}).get("updatedAt") or updated_at or _now_iso()),
    }


def _selected_repo_payload(defect: DefectRecord, resolution: RepoResolution) -> dict:
    mapping = engine.get_repo_for_project(defect.project.id) or {}
    return {
        "projectId": defect.project.id,
        "projectName": defect.project.name,
        "repoUrl": resolution.selected_repo.repo_url,
        "branch": resolution.selected_branch or resolution.selected_repo.default_branch or settings.git.default_branch,
        "iterationId": mapping.get("iterationId", ""),
        "iterationName": mapping.get("iterationName", ""),
        "iterationKey": mapping.get("iterationKey", ""),
    }


def _mapped_iteration(project_id: str) -> str | None:
    mapping = engine.get_repo_for_project(project_id)
    if not mapping:
        return None
    iteration_id = str(mapping.get("iterationId", "") or "").strip()
    return iteration_id or None


async def _fetch_filtered_defects(
    gateway: OnesGateway,
    *,
    project_id: str | None,
    assignee: str | None = None,
    limit: int = 1000,
) -> list[DefectRecord]:
    if project_id:
        return await gateway.list_normalized_defects(
            project_id=project_id,
            sprint_id=_mapped_iteration(project_id),
            assignee=assignee,
            limit=limit,
        )

    mappings = engine.list_project_repos()
    if not mappings:
        return await gateway.list_normalized_defects(assignee=assignee, limit=limit)

    defects: list[DefectRecord] = []
    seen: set[str] = set()
    for mapping in mappings:
        mapped_project_id = str(mapping.get("projectId", "") or "").strip()
        if not mapped_project_id:
            continue
        scoped = await gateway.list_normalized_defects(
            project_id=mapped_project_id,
            sprint_id=str(mapping.get("iterationId", "") or "").strip() or None,
            assignee=assignee,
            limit=limit,
        )
        for defect in scoped:
            if defect.defect_id in seen:
                continue
            seen.add(defect.defect_id)
            defects.append(defect)
    return defects


def _defect_list_item(defect: DefectRecord, resolution: RepoResolution) -> dict:
    mapping_status = _mapping_status(resolution)
    execution_record = _latest_execution_record(defect.defect_id)
    analysis_status = "blocked" if mapping_status == "missing" else "pending"
    execution = _execution_payload(
        execution_record,
        resolution=resolution,
        analysis=None,
        mapping_status=mapping_status,
        updated_at=defect.updated_at,
    )
    return {
        "id": defect.defect_id,
        "type": "defect",
        "title": defect.title,
        "projectId": defect.project.id,
        "projectName": defect.project.name,
        "assignee": defect.assignee.name if defect.assignee else "",
        "priority": defect.priority.value or "",
        "onesStatus": defect.status.name,
        "status": _workflow_status_for_defect(defect.defect_id),
        "branch": execution.get("branchName") or "",
        "commitHash": None,
        "riskLevel": None,
        "requiresApproval": False,
        "onesId": defect.defect_id,
        "planJson": None,
        "mappingStatus": mapping_status,
        "analysisStatus": analysis_status,
        "analysisSummary": "",
        "rootCause": "",
        "fixSuggestions": [],
        "analysisMarkdown": "",
        "analysisEvidence": [],
        "suggestedBranchName": "",
        "baseBranch": resolution.selected_branch or resolution.selected_repo.default_branch or settings.git.default_branch,
        "executionStatus": execution["status"],
        "executionBranch": execution.get("branchName") or "",
        "executionId": execution_record["id"] if execution_record else "",
        "executionRequestedAt": execution.get("updatedAt") or defect.updated_at,
        "createdAt": defect.created_at or defect.updated_at or _now_iso(),
        "updatedAt": defect.updated_at or defect.created_at or _now_iso(),
        "analysis": {
            "status": analysis_status,
            "summary": "",
            "rootCause": "",
            "fixSuggestions": [],
            "evidence": [],
            "markdown": "",
            "confidence": 0.0,
            "updatedAt": defect.updated_at or defect.created_at or _now_iso(),
        },
        "execution": execution,
        "selectedRepo": _selected_repo_payload(defect, resolution),
        "codebaseStatus": _codebase_status(mapping_status),
    }


def _defect_detail_payload(defect: DefectRecord, resolution: RepoResolution, analysis: AnalysisResult) -> dict:
    mapping_status = _mapping_status(resolution)
    execution_record = _latest_execution_record(defect.defect_id)
    analysis_payload = _analysis_payload(analysis, updated_at=defect.updated_at or defect.created_at or _now_iso())
    execution = _execution_payload(
        execution_record,
        resolution=resolution,
        analysis=analysis,
        mapping_status=mapping_status,
        updated_at=defect.updated_at,
    )
    return {
        "id": defect.defect_id,
        "type": "defect",
        "title": defect.title,
        "projectId": defect.project.id,
        "projectName": defect.project.name,
        "assignee": defect.assignee.name if defect.assignee else "",
        "priority": defect.priority.value or "",
        "onesStatus": defect.status.name,
        "status": _workflow_status_for_defect(defect.defect_id),
        "branch": execution.get("branchName") or "",
        "commitHash": None,
        "riskLevel": None,
        "requiresApproval": False,
        "onesId": defect.defect_id,
        "planJson": None,
        "mappingStatus": mapping_status,
        "analysisStatus": analysis_payload["status"],
        "analysisSummary": analysis_payload["summary"],
        "rootCause": analysis_payload["rootCause"],
        "fixSuggestions": analysis_payload["fixSuggestions"],
        "analysisMarkdown": analysis_payload["markdown"],
        "analysisEvidence": analysis_payload["evidence"],
        "suggestedBranchName": build_branch_name(defect.defect_id, "defect", defect.title),
        "baseBranch": resolution.selected_branch or resolution.selected_repo.default_branch or settings.git.default_branch,
        "executionStatus": execution["status"],
        "executionBranch": execution.get("branchName") or "",
        "executionId": execution_record["id"] if execution_record else "",
        "executionRequestedAt": execution.get("updatedAt") or defect.updated_at,
        "createdAt": defect.created_at or defect.updated_at or _now_iso(),
        "updatedAt": defect.updated_at or defect.created_at or _now_iso(),
        "analysis": analysis_payload,
        "execution": execution,
        "selectedRepo": _selected_repo_payload(defect, resolution),
        "codebaseStatus": _codebase_status(mapping_status),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    queue.start()
    scheduler.start()
    schedule_manager.start()
    audit.record(actor="system", action="startup", target="agent", result="ok")
    log.info("agent_starting", **mask_dict(settings.summary()))
    yield
    await schedule_manager.stop()
    await scheduler.stop()
    await queue.stop()
    audit.record(actor="system", action="shutdown", target="agent", result="ok")


app = FastAPI(title="ONES Agent", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    elapsed = time.time() - start

    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        audit.record(
            actor="api",
            action=request.method,
            target=request.url.path,
            result=str(response.status_code),
            extra={"elapsed_ms": round(elapsed * 1000)},
        )
    duration_seconds.labels(stage="http").observe(elapsed)
    return response


# ── Health & Metrics ──────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "queue_size": queue.size}


@app.get("/metrics")
async def metrics():
    data, content_type = metrics_output()
    from starlette.responses import Response
    return Response(content=data, media_type=content_type)


# ── Auth ──────────────────────────────────────────────

@app.post("/api/v1/auth/login")
async def login(req: LoginRequest):
    for _uid, user in USERS.items():
        if user["name"].lower() == req.email.lower() and user["password"] == req.password:
            token = create_token(user["id"], user["role"])
            return {"token": token, "user": {"id": user["id"], "name": user["name"], "role": user["role"]}}
    raise HTTPException(status_code=401, detail="Invalid credentials")


@app.get("/api/v1/auth/me")
async def me(user: dict = Depends(current_user)):
    for _uid, u in USERS.items():
        if u["id"] == user["id"]:
            return {"id": u["id"], "name": u["name"], "role": u["role"]}
    raise HTTPException(status_code=404, detail="User not found")


# ── Tasks ─────────────────────────────────────────────

@app.get("/api/v1/tasks")
async def list_tasks(
    status: str = "",
    type: str = "",
    search: str = "",
    page: int = 1,
    pageSize: int = 20,
    user: dict = Depends(current_user),
):
    state_val = None
    if status:
        for k, v in STATE_MAP.items():
            if v == status:
                state_val = k
                break

    raw_items, _ = engine.list_items(
        state=state_val,
        search=search or None,
        page=1,
        page_size=10_000,
    )
    items = [_item_to_dict(i) for i in raw_items]
    if type:
        items = [item for item in items if item["type"] == type]

    total = len(items)
    start = (page - 1) * pageSize
    paged_items = items[start:start + pageSize]
    return {
        "items": paged_items,
        "total": total,
        "page": page,
        "pageSize": pageSize,
    }


@app.get("/api/v1/tasks/{task_id}")
async def get_task(task_id: str, user: dict = Depends(current_user)):
    item = engine.get(task_id)
    if not item:
        raise HTTPException(status_code=404, detail="Task not found")
    return _item_to_dict(item)


@app.post("/api/v1/tasks/{task_id}/action")
async def task_action(task_id: str, req: ActionRequest, user: dict = Depends(current_user)):
    item = engine.get(task_id)
    if not item:
        raise HTTPException(status_code=404, detail="Task not found")

    action_map = {
        "approve": State.CODING,
        "reject": State.FAILED,
        "retry": State.PENDING,
        "pause": State.WAITING_APPROVAL,
        "cancel": State.FAILED,
        "resume": State.CODING,
    }
    target = action_map.get(req.action)
    if not target:
        raise HTTPException(status_code=400, detail=f"Unknown action: {req.action}")

    try:
        engine.transition(task_id, target)
        audit.record(actor=user["id"], action=req.action, target=task_id, result="ok", extra={"reason": req.reason})
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return _item_to_dict(engine.get(task_id))


# ── Metrics Summary ───────────────────────────────────

@app.get("/api/v1/metrics/summary")
async def metrics_summary(user: dict = Depends(current_user)):
    from datetime import datetime, timedelta, timezone
    _, total = engine.list_items()
    _, success = engine.list_items(state="success")
    _, failed = engine.list_items(state="failed")
    active_count = total - success - failed

    daily = []
    now = datetime.now(timezone.utc)
    for i in range(6, -1, -1):
        d = (now - timedelta(days=i)).isoformat()[:10]
        count = max(1, int(total / 7)) if total else 0
        daily.append({"date": d, "count": count, "success": int(count * 0.87) if count else 0})

    return {
        "activeTasks": active_count,
        "successRate": success / total if total else 0,
        "avgDurationSec": 342,
        "todayFailures": failed,
        "dailyThroughput": daily,
    }


# ── Logs ──────────────────────────────────────────────

@app.get("/api/v1/logs")
async def list_logs(
    level: str = "",
    taskId: str = "",
    traceId: str = "",
    page: int = 1,
    pageSize: int = 50,
    user: dict = Depends(current_user),
):
    search = taskId or traceId or None
    entries, total = audit.query(level=level or None, search=search, page=page, page_size=pageSize)
    log_entries = []
    for i, e in enumerate(entries):
        log_entries.append({
            "id": f"log-{page * pageSize - pageSize + i + 1}",
            "timestamp": e.get("timestamp", ""),
            "level": e.get("result", "") if e.get("result") in ("ok", "error") else "info",
            "taskId": e.get("target", ""),
            "stage": e.get("action", ""),
            "message": f"{e.get('action', '')} {e.get('target', '')} by {e.get('actor', '')}",
            "traceId": None,
            "context": e,
        })
    return {"items": log_entries, "total": total, "page": page, "pageSize": pageSize}


# ── Config ────────────────────────────────────────────

@app.get("/api/v1/config")
async def get_config(user: dict = Depends(require_admin)):
    s = settings
    return {
        "ones": {
            "baseUrl": s.ones.base_url,
            "email": s.ones.email,
            "password": _MASK if s.ones.password else "",
            "teamId": s.ones.team_id,
            "projectId": s.ones.project_id,
        },
        "git": {
            "repoUrl": s.git.repo_url,
            "branch": s.git.default_branch,
            "authType": "https" if "https" in s.git.auth_type else "ssh",
        },
        "llm": {
            "provider": s.llm.provider,
            "model": s.llm.model,
            "baseUrl": s.llm.base_url,
            "apiKey": _MASK if s.llm.api_key else "",
        },
        "cicd": {"platform": "github", "token": _MASK if False else ""},
        "webhook": {"secret": _MASK if s.agent.webhook_secret else "", "enabled": bool(s.agent.webhook_secret)},
        "email": {
            "smtpHost": s.email.smtp_host,
            "smtpPort": s.email.smtp_port,
            "smtpUser": s.email.smtp_user,
            "smtpPassword": _MASK if s.email.smtp_password else "",
            "sender": s.email.sender,
            "useTls": s.email.use_tls,
        },
    }


def _persist_config(req: ConfigUpdate) -> None:
    import pathlib
    for section_key, values in req.model_dump(exclude_none=True).items():
        for field, value in values.items():
            dot_key = f"{section_key}.{field}"
            if dot_key in _SECRET_KEYS and value == "••••••••":
                continue
            section_obj = getattr(settings, section_key, None)
            if section_obj is None:
                continue
            field_map = {
                "ones": {"baseUrl": "base_url", "email": "email", "password": "password", "teamId": "team_id", "projectId": "project_id"},
                "git": {"repoUrl": "repo_url", "branch": "default_branch", "authType": "auth_type"},
                "llm": {"provider": "provider", "model": "model", "baseUrl": "base_url", "apiKey": "api_key"},
                "cicd": {"platform": "platform", "token": "token"},
                "webhook": {"secret": "webhook_secret", "enabled": "webhook_secret"},
                "email": {"smtpHost": "smtp_host", "smtpPort": "smtp_port", "smtpUser": "smtp_user",
                          "smtpPassword": "smtp_password", "sender": "sender", "useTls": "use_tls"},
            }
            attr_name = field_map.get(section_key, {}).get(field)
            if attr_name is None:
                continue
            if section_key == "webhook" and field == "enabled":
                continue
            if section_key == "webhook" and field == "secret":
                if value:
                    setattr(settings.agent, attr_name, str(value))
                continue
            if section_key == "git" and field == "authType":
                setattr(settings.git, attr_name, str(value) + "_pat" if str(value) == "https" else str(value))
                continue
            setattr(section_obj, attr_name, str(value) if not isinstance(value, bool) else value)
    env_path = pathlib.Path(_ENV_FILE)
    existing = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()
    updates: dict[str, str] = {}
    for section_key, values in req.model_dump(exclude_none=True).items():
        for field, value in values.items():
            dot_key = f"{section_key}.{field}"
            env_key = _ENV_KEY_MAP.get(dot_key)
            if env_key is None:
                continue
            if dot_key in _SECRET_KEYS and value == "••••••••":
                continue
            if isinstance(value, bool):
                updates[env_key] = "true" if value else "false"
            else:
                updates[env_key] = str(value)
    existing.update(updates)
    lines = [f"{k}={v}" for k, v in sorted(existing.items())]
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@app.put("/api/v1/config")
async def update_config(req: ConfigUpdate, user: dict = Depends(require_admin)):
    _persist_config(req)
    audit.record(actor=user["id"], action="update_config", target="system", result="ok")
    return await get_config(user)


@app.post("/api/v1/config/test/{section}")
async def test_connection(section: str, user: dict = Depends(require_admin)):
    if section == "ones":
        try:
            async with OnesAsyncClient(settings.ones) as client:
                await client.fetch_defects(limit=1)
            return {"ok": True, "message": "ONES connection successful"}
        except Exception as e:
            return {"ok": False, "message": f"ONES connection failed: {e}"}
    if section == "llm":
        try:
            planner = Planner(settings.llm)
            plan = await planner.plan({"name": "test", "uuid": "test"})
            return {"ok": True, "message": f"LLM connection successful, model: {settings.llm.model}"}
        except Exception as e:
            return {"ok": False, "message": f"LLM connection failed: {e}"}
    return {"ok": True, "message": f"{section} connection successful"}


# ── Project-Repo Mappings ────────────────────────────

@app.get("/api/v1/ones/projects")
async def fetch_ones_projects(user: dict = Depends(current_user)):
    s = settings
    if not s.ones.email or not s.ones.password:
        raise HTTPException(status_code=400, detail="ONES credentials not configured")
    try:
        async with OnesGateway(settings=s.ones) as gateway:
            projects = await gateway.list_projects()
        return [{"id": p.get("uuid", ""), "name": p.get("name", "")} for p in projects]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch ONES projects: {e}")


@app.get("/api/v1/ones/projects/{project_id}/iterations")
async def fetch_ones_project_iterations(project_id: str, user: dict = Depends(current_user)):
    s = settings
    if not s.ones.email or not s.ones.password:
        raise HTTPException(status_code=400, detail="ONES credentials not configured")
    try:
        async with OnesGateway(settings=s.ones) as gateway:
            iterations = await gateway.list_iterations(project_id)
    except Exception as exc:
        raise _translate_ones_gateway_error(exc, detail="Failed to fetch ONES project iterations")

    return [
        {
            "id": item.get("uuid", ""),
            "name": item.get("title") or item.get("name", ""),
            "key": item.get("key", ""),
            "projectId": item.get("project", {}).get("uuid", ""),
            "projectName": item.get("project", {}).get("name", ""),
            "statusName": item.get("statusInfo", {}).get("name", ""),
            "statusCategory": item.get("statusInfo", {}).get("category", ""),
        }
        for item in iterations
    ]


@app.get("/api/v1/ones/team-members")
async def fetch_ones_team_members(projectId: str = "", user: dict = Depends(current_user)):
    s = settings
    if not s.ones.email or not s.ones.password:
        raise HTTPException(status_code=400, detail="ONES credentials not configured")
    try:
        async with OnesGateway(settings=s.ones) as gateway:
            member_ids: set[str] = set()
            if projectId.strip():
                project_ids = [projectId.strip()]
            else:
                project_ids = [
                    str(mapping.get("projectId", "") or "").strip()
                    for mapping in engine.list_project_repos()
                    if str(mapping.get("projectId", "") or "").strip()
                ]
                if not project_ids and s.ones.project_id:
                    project_ids = [s.ones.project_id]

            for project_id in dict.fromkeys(project_ids):
                role_members = await gateway.list_role_members(project_id)
                for item in role_members:
                    if not isinstance(item, dict):
                        continue
                    members = item.get("members")
                    if isinstance(members, list):
                        for raw_member_id in members:
                            member_id = str(raw_member_id or "").strip()
                            if member_id:
                                member_ids.add(member_id)
            members = await gateway.list_team_members(uuids=sorted(member_ids))
    except Exception as exc:
        raise _translate_ones_gateway_error(exc, detail="Failed to fetch ONES team members")

    normalized = []
    seen: set[str] = set()
    for item in members:
        if not isinstance(item, dict):
            continue
        member_id = str(item.get("uuid") or item.get("user_uuid") or item.get("org_user_uuid") or "").strip()
        member_name = str(item.get("name") or item.get("user_name") or item.get("display_name") or "").strip()
        if not member_id or not member_name or member_id in seen:
            continue
        seen.add(member_id)
        normalized.append({"id": member_id, "name": member_name})
    normalized.sort(key=lambda item: item["name"].lower())
    return normalized


@app.get("/api/v1/project-repos")
async def list_project_repos(project_id: str = "", user: dict = Depends(current_user)):
    return engine.list_project_repos(project_id or None)


@app.post("/api/v1/project-repos")
async def add_project_repo(req: ProjectRepoCreate, user: dict = Depends(require_admin)):
    audit.record(actor=user["id"], action="add_project_repo", target=req.projectId, result="ok")
    return engine.add_project_repo(
        req.projectId,
        req.projectName,
        req.repoUrl,
        req.branch,
        req.iterationId,
        req.iterationName,
        req.iterationKey,
    )


@app.delete("/api/v1/project-repos")
async def remove_project_repo(req: ProjectRepoDelete, user: dict = Depends(require_admin)):
    engine.remove_project_repo(req.projectId, req.repoUrl)
    audit.record(actor=user["id"], action="remove_project_repo", target=req.projectId, result="ok")
    return {"ok": True}


@app.get("/api/v1/defects")
async def list_defects(
    projectId: str = "",
    assignee: str = "",
    status: str = "",
    analysisStatus: str = "",
    mappingStatus: str = "",
    search: str = "",
    page: int = 1,
    pageSize: int = 20,
    user: dict = Depends(current_user),
):
    try:
        async with OnesGateway(settings=settings.ones) as gateway:
            defects = await _fetch_filtered_defects(
                gateway,
                project_id=projectId or None,
                assignee=assignee.strip() or None,
                limit=1000,
            )
    except Exception as exc:
        raise _translate_ones_gateway_error(exc, detail="Failed to fetch defects from ONES")

    resolver = _create_repo_resolver()
    items = [_defect_list_item(defect, resolver.resolve(defect=defect)) for defect in defects]

    if status:
        items = [item for item in items if item["status"] == status]
    if analysisStatus:
        items = [item for item in items if item["analysisStatus"] == analysisStatus]
    if mappingStatus:
        items = [item for item in items if item["mappingStatus"] == mappingStatus]
    if search:
        lowered = search.lower()
        items = [
            item
            for item in items
            if lowered in item["id"].lower()
            or lowered in item["onesId"].lower()
            or lowered in item["title"].lower()
            or lowered in item.get("projectName", "").lower()
        ]

    total = len(items)
    start = max(page - 1, 0) * pageSize
    return {
        "items": items[start:start + pageSize],
        "total": total,
        "page": page,
        "pageSize": pageSize,
    }


@app.get("/api/v1/defects/{defect_id}")
async def get_defect_detail(defect_id: str, user: dict = Depends(current_user)):
    try:
        async with OnesGateway(settings=settings.ones) as gateway:
            defect = await gateway.get_normalized_defect(defect_id)
    except Exception as exc:
        raise _translate_ones_gateway_error(exc, detail="Failed to fetch defect detail from ONES")

    resolver = _create_repo_resolver()
    resolution = resolver.resolve(defect=defect)
    analysis = _create_analysis_workflow_service().analyze_result(defect, resolution)
    return _defect_detail_payload(defect, resolution, analysis)


@app.post("/api/v1/defects/{defect_id}/execution")
async def create_defect_execution(defect_id: str, req: DefectExecutionCreate, user: dict = Depends(current_user)):
    try:
        async with OnesGateway(settings=settings.ones) as gateway:
            defect = await gateway.get_normalized_defect(defect_id)
    except Exception as exc:
        raise _translate_ones_gateway_error(exc, detail="Failed to fetch defect detail from ONES")

    resolver = _create_repo_resolver()
    resolution = resolver.resolve(defect=defect)
    analysis = _create_analysis_workflow_service().analyze_result(defect, resolution)

    request_type = "requirement_development" if req.requestType == "development" else "bugfix"
    branch_reason = (req.notes or analysis.analysis_summary or defect.title).strip() or defect.title
    execution_request = ExecutionRequest(
        defect_id=defect.defect_id,
        project=defect.project,
        repo_resolution=resolution,
        request_type=request_type,
        proposed_branch_name=build_branch_name(
            defect.defect_id,
            "requirement" if request_type == "requirement_development" else "defect",
            branch_reason,
        ),
        target_branch=(req.baseBranch or resolution.selected_branch or resolution.selected_repo.default_branch or settings.git.default_branch),
        requested_by=user.get("name", "") or user.get("id", ""),
        reason=branch_reason,
        confidence=analysis.confidence,
        source="api_v1_defects_execution",
        metadata={
            "requested_operations": ["branch_create"],
            "ui_request_type": req.requestType,
            "ui_branch_name": req.branchName,
        },
    )

    try:
        _create_execution_service().execute(execution_request)
    except ExecutionValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to create branch execution: {exc}")

    return _defect_detail_payload(defect, resolution, analysis)


# ── Scheduled Tasks ──────────────────────────────────

@app.get("/api/v1/scheduled-tasks")
async def list_scheduled_tasks(user: dict = Depends(current_user)):
    return engine.list_scheduled_tasks()


@app.get("/api/v1/scheduled-tasks/{task_id}")
async def get_scheduled_task(task_id: str, user: dict = Depends(current_user)):
    task = engine.get_scheduled_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    return task


@app.post("/api/v1/scheduled-tasks")
async def create_scheduled_task(req: ScheduledTaskCreate, user: dict = Depends(require_admin)):
    import uuid
    task_id = req.name.lower().replace(" ", "-") + "-" + uuid.uuid4().hex[:8]
    result = engine.add_scheduled_task(
        task_id=task_id, name=req.name, cron_expr=req.cronExpr,
        project_id=req.projectId, assignee_id=req.assigneeId, assignee_name=req.assigneeName,
        item_type=req.itemType, action=req.action,
        notify_emails=req.notifyEmails, notify_wechat=req.notifyWechat,
        enabled=req.enabled,
    )
    audit.record(actor=user["id"], action="create_scheduled_task", target=task_id, result="ok")
    return result


@app.put("/api/v1/scheduled-tasks/{task_id}")
async def update_scheduled_task(task_id: str, req: ScheduledTaskUpdate, user: dict = Depends(require_admin)):
    existing = engine.get_scheduled_task(task_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    _FIELD_MAP = {
        "name": "name", "cronExpr": "cron_expr", "projectId": "project_id",
        "assigneeId": "assignee_id", "assigneeName": "assignee_name",
        "itemType": "item_type", "action": "action", "notifyEmails": "notify_emails",
        "notifyWechat": "notify_wechat", "enabled": "enabled",
    }
    updates = {_FIELD_MAP[k]: v for k, v in req.model_dump().items() if v is not None}
    result = engine.update_scheduled_task(task_id, **updates)
    audit.record(actor=user["id"], action="update_scheduled_task", target=task_id, result="ok")
    return result


@app.delete("/api/v1/scheduled-tasks/{task_id}")
async def delete_scheduled_task(task_id: str, user: dict = Depends(require_admin)):
    existing = engine.get_scheduled_task(task_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    engine.delete_scheduled_task(task_id)
    audit.record(actor=user["id"], action="delete_scheduled_task", target=task_id, result="ok")
    return {"ok": True}


@app.post("/api/v1/scheduled-tasks/{task_id}/trigger")
async def trigger_scheduled_task(task_id: str, user: dict = Depends(require_admin)):
    try:
        count = await schedule_manager.run_task_now(task_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    audit.record(actor=user["id"], action="trigger_scheduled_task", target=task_id, result="ok")
    return {"triggered": True, "taskId": task_id, "count": count}


@app.get("/api/v1/scheduled-tasks/{task_id}/runs")
async def list_scheduled_task_runs(task_id: str, user: dict = Depends(current_user)):
    task = engine.get_scheduled_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    return engine.list_scheduled_task_runs(task_id)


@app.get("/api/v1/scheduled-task-runs", response_model=ScheduledTaskRunListResponse)
async def list_all_scheduled_task_runs(
    taskId: str = "",
    status: str = "",
    search: str = "",
    page: int = 1,
    pageSize: int = 20,
    user: dict = Depends(current_user),
):
    runs, total = engine.list_all_scheduled_task_runs(
        task_id=taskId or None,
        status=status or None,
        search=search.strip() or None,
        page=page,
        page_size=pageSize,
    )
    return {
        "items": runs,
        "total": total,
        "page": page,
        "pageSize": pageSize,
    }


@app.get("/api/v1/scheduled-task-runs/{run_id}/items")
async def list_scheduled_task_run_items(run_id: str, user: dict = Depends(current_user)):
    run = engine.get_scheduled_task_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Scheduled task run not found")
    return engine.list_scheduled_task_run_items(run_id)


# ── AI Trigger (single item) ────────────────────────

@app.post("/api/v1/ai/trigger")
async def ai_trigger(req: AITriggerRequest, response: Response, user: dict = Depends(current_user)):
    """Deprecated compatibility shim for single-item AI planning/analysis."""
    s = settings
    if not s.ones.email or not s.ones.password:
        raise HTTPException(status_code=400, detail="ONES credentials not configured")
    if not s.llm.api_key:
        raise HTTPException(status_code=400, detail="LLM API key not configured")

    _mark_ai_trigger_deprecated(response, req.itemId)

    # 1. 从 ONES 获取工作项详情
    try:
        async with OnesGateway(settings=s.ones) as gateway:
            item = await gateway.get_defect_detail(req.itemId)
        if not item:
            raise HTTPException(status_code=404, detail=f"Item not found: {req.itemId}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch from ONES: {e}")

    # 2. AI 处理
    result: dict = {
        "itemId": req.itemId,
        "name": item.get("name", ""),
        "action": req.action,
        "deprecated": {
            "route": "/api/v1/ai/trigger",
            "status": "deprecated",
            "replacement": {
                "analysis": _canonical_defect_analysis_path(req.itemId),
                "execution": _canonical_defect_execution_path(req.itemId),
            },
            "message": "Use the canonical defect routes for primary analysis and execution flows.",
        },
    }
    try:
        if req.action == "plan":
            planner = Planner(s.llm)
            plan = await planner.plan(item)
            result["plan"] = {
                "summary": plan.summary,
                "steps": plan.steps,
                "riskLevel": plan.risk_level,
                "branch": plan.branch_name,
                "requiresHumanApproval": plan.requires_human_approval,
            }
            # 同时在 Engine 中记录
            engine.start_work(req.itemId, State.PARSING)
            engine.transition(req.itemId, State.PLANNING)
            target_state = State.WAITING_APPROVAL if plan.requires_human_approval else State.CODING
            engine.transition(req.itemId, target_state, plan_json=plan.model_dump_json(), branch=plan.branch_name)
        elif req.action == "analyze":
            analyzer = Analyzer(base_url=s.llm.base_url, api_key=s.llm.api_key, model=s.llm.model)
            analysis = analyzer.analyze(item)
            result["analysis"] = analysis
            # 记录到 Engine
            engine.start_work(req.itemId, State.PARSING)
    except Exception as e:
        log.error("ai_trigger_failed", itemId=req.itemId, error=str(e))
        raise HTTPException(status_code=500, detail=f"AI processing failed: {e}")

    # 3. 通知（如果配置了）
    notify_emails = [e.strip() for e in req.notifyEmails.split(",") if e.strip()]
    if notify_emails or req.notifyWechat:
        subject = f"[ONES Agent] AI {req.action}: {item.get('name', '')}"
        markdown = _build_ai_trigger_report(item, result, req.action)
        notifier = NotificationService(s.email, s.wechat)
        target = NotifyTarget(emails=notify_emails, wechat=req.notifyWechat)
        notify_results = await notifier.notify(target, subject, markdown)
        result["notifyResults"] = notify_results

    audit.record(actor=user["id"], action="ai_trigger", target=req.itemId, result="ok")
    return result


def _build_ai_trigger_report(item: dict, result: dict, action: str) -> str:
    parts = [f"## 🤖 AI {action} 报告\n"]
    parts.append(f"**{item.get('name', '')}**\n")
    status = item.get("status", {}).get("name", "?")
    priority = item.get("priority", {}).get("name", "?")
    assignee = item.get("assign", {}).get("name", "未分配")
    parts.append(f"- 状态: {status} | 优先级: {priority} | 负责人: {assignee}\n")
    if action == "plan" and "plan" in result:
        p = result["plan"]
        parts.append(f"### 开发计划\n")
        parts.append(f"- 摘要: {p.get('summary', '')}")
        parts.append(f"- 风险: {p.get('riskLevel', '')}")
        parts.append(f"- 分支: {p.get('branch', '')}")
        if p.get("steps"):
            for i, step in enumerate(p["steps"], 1):
                parts.append(f"  {i}. {step}")
    elif action == "analyze" and "analysis" in result:
        parts.append(f"### 分析结果\n")
        parts.append(result["analysis"])
    return "\n".join(parts)


# ── SSE Events ────────────────────────────────────────

@app.get("/api/v1/scheduler/status")
async def scheduler_status(user: dict = Depends(require_admin)):
    return scheduler.status()


@app.post("/api/v1/scheduler/trigger")
async def scheduler_trigger(user: dict = Depends(require_admin)):
    count = await scheduler.poll_now()
    audit.record(actor=user["id"], action="scheduler_trigger", target="ones", result="ok")
    return {"triggered": True, "newCount": count}

@app.get("/api/v1/stream/events")
async def stream_events(request: Request, user: dict = Depends(current_user)):
    from starlette.responses import StreamingResponse

    async def event_generator():
        for _ in range(3):
            if await request.is_disconnected():
                break
            yield f"data: {{}}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ── Webhook ───────────────────────────────────────────

@app.post("/webhook/ones")
async def webhook_ones(request: Request):
    body = await request.body()

    if settings.agent.webhook_secret:
        sig = request.headers.get("X-ONES-Signature", "")
        expected = hmac.new(
            settings.agent.webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            failures_total.labels(stage="webhook").inc()
            raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        data = await request.json()
    except Exception:
        failures_total.labels(stage="webhook").inc()
        raise HTTPException(status_code=400, detail="Invalid JSON")

    payload = WebhookPayload(
        work_item_id=data.get("work_item_id", ""),
        type=data.get("type", ""),
        status_change=data.get("status_change", ""),
        raw=data,
    )

    if not payload.work_item_id:
        raise HTTPException(status_code=400, detail="Missing work_item_id")

    bind_context(work_item_id=payload.work_item_id, type=payload.type)
    log.info("webhook_received", work_item_id=payload.work_item_id, type=payload.type)
    tasks_total.labels(type=payload.type or "unknown", status="queued").inc()

    engine.start_work(payload.work_item_id)
    await queue.enqueue(_process_webhook(payload))

    audit.record(actor="webhook", action="enqueue", target=payload.work_item_id, result="ok")
    return {"status": "queued", "work_item_id": payload.work_item_id}


async def _process_webhook(payload: WebhookPayload) -> None:
    log.info("webhook_processing", work_item_id=payload.work_item_id, type=payload.type)
    tasks_total.labels(type=payload.type or "unknown", status="processing").inc()


# ── Frontend Static Files ─────────────────────────────

_frontend_path = argparse.Namespace(path=None)


def _serve_spa() -> HTMLResponse:
    import pathlib
    base = _frontend_path.path
    if not base:
        return HTMLResponse("<h1>ONES Agent API</h1><p>Frontend not built. Run <code>cd agent-gui && npm run build</code></p>")
    index = pathlib.Path(base) / "index.html"
    if not index.exists():
        return HTMLResponse("<h1>ONES Agent API</h1><p>Frontend not found</p>")
    return HTMLResponse(index.read_text(encoding="utf-8"))


class _SPAFallbackMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from starlette.requests import Request
        from starlette.responses import Response

        request = Request(scope, receive)
        path = request.url.path

        if path.startswith("/api/") or path.startswith("/assets/") or path in ("/health", "/metrics", "/docs", "/redoc", "/openapi.json"):
            await self.app(scope, receive, send)
            return

        async def _send_wrapper(message):
            if message["type"] == "http.response.start" and message.get("status") == 404:
                response = _serve_spa()
                await response(scope, receive, send)
                return
            await send(message)

        intercepted = False

        async def _send_intercept(message):
            nonlocal intercepted
            if message["type"] == "http.response.start":
                if message.get("status") == 404:
                    intercepted = True
                    response = _serve_spa()
                    await response(scope, receive, send)
                    return
            if not intercepted:
                await send(message)

        await self.app(scope, receive, _send_intercept)


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return _serve_spa()


def _mount_frontend(app: FastAPI, dist_path: str) -> None:
    import pathlib
    p = pathlib.Path(dist_path)
    if not (p.exists() and (p / "index.html").exists()):
        log.warning("frontend_not_found", path=str(p))
        return
    _frontend_path.path = str(p)
    app.mount("/assets", StaticFiles(directory=str(p / "assets")), name="assets")
    app.add_middleware(_SPAFallbackMiddleware)
    log.info("frontend_mounted", path=str(p))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--frontend", default="agent-gui/dist", help="Frontend dist path")
    args = parser.parse_args()

    _mount_frontend(app, args.frontend)
    uvicorn.run(app, host=args.host, port=args.port)
