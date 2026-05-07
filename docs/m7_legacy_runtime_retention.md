# M7 legacy runtime retention decisions

This note records which pre-M7 backend entry points remain after M1 to M6, how they are classified, and how they should relate to the new defect-centric backend architecture.

It is grounded in the current code in `main.py`, `server.py`, `src/core/scheduler.py`, `src/core/schedule_manager.py`, `src/core/agent.py`, `src/contracts.py`, `src/services/ones_gateway.py`, `src/services/repo_resolver.py`, `src/services/defect_analysis_workflow.py`, and `src/services/execution_service.py`.

## Current M7 baseline

- `src/contracts.py` is the authoritative schema layer for `DefectRecord`, `RepoResolution`, `AnalysisResult`, and `ExecutionRequest`.
- `src/services/ones_gateway.py`, `src/services/repo_resolver.py`, `src/services/defect_analysis_workflow.py`, and `src/services/execution_service.py` are the intended backend service seams for runtime adoption.
- `main.py` is still the live FastAPI runtime shell. It starts `Scheduler` and `ScheduleManager`, exposes scheduled-task APIs, exposes `/api/v1/ai/trigger`, and still owns the current scheduler control routes.
- `server.py` is still a separate MCP runtime and remains outside the FastAPI process.
- The old sync `src/core/agent.py` path still exists, but it is not started by `main.py` and is not retained as an M7 runtime target.

## Retention summary

| Legacy path | Current owner | M7 classification | Why |
| --- | --- | --- | --- |
| Background scheduler polling | `src/core/scheduler.py`, started from `main.py` lifespan, controlled by `/api/v1/scheduler/status` and `/api/v1/scheduler/trigger` | Keep | It is still the only built-in background discovery path for newly assigned defects. |
| Scheduled task runs | `src/core/schedule_manager.py`, plus `main.py` scheduled-task and run-history routes | Secondary | It still supports saved cron jobs, notifications, and history, but it is no longer the primary defect workflow surface. |
| Manual AI trigger | `main.py` `POST /api/v1/ai/trigger` | Later deprecate | It overlaps the target defect-centric on-demand analysis and execution flow, and it still calls legacy planner or analyzer logic directly. |
| MCP tools and server | `server.py` `FastMCP("ONES Defect Agent")` | Secondary | It remains a valid external integration surface, but it should consume the same backend services rather than define parallel workflow behavior. |

## Path-by-path decisions

### 1. Background scheduler polling

- **Current owner and file**: `src/core/scheduler.py::Scheduler`, started in `main.py` `lifespan()`, observed through `GET /api/v1/scheduler/status`, and manually kicked through `POST /api/v1/scheduler/trigger`.
- **Decision**: **Keep**.
- **Why it remains**: this is still the repo's only live background discovery loop for new ONES defects. It polls ONES on an interval, filters new items through `Store`, creates `Engine` work items, and gives operators a runtime shell for automatic intake.
- **Why it is not primary business logic anymore**: `Scheduler._poll_once()` still fetches via `OnesAsyncClient`, resolves repo mappings via `Engine.get_repo_for_project()`, and calls `Planner` directly. That means it still owns ingestion and planning details that now belong in the M2 to M5 service layer.
- **Required relation to the defect-centric architecture**: keep the scheduler as an orchestration shell only. Its retained role should be: discover candidate defects, call `OnesGateway` for normalized defect intake, call `RepoResolver` and `DefectAnalysisWorkflowService.analyze_result(...)` for analysis, and only reach execution through an explicit `ExecutionRequest` plus `ExecutionService` path when that becomes part of the adopted runtime.

### 2. Scheduled task runs

- **Current owner and file**: `src/core/schedule_manager.py::ScheduleManager`, started in `main.py` `lifespan()`, configured through `GET/POST/PUT/DELETE /api/v1/scheduled-tasks`, triggered through `POST /api/v1/scheduled-tasks/{task_id}/trigger`, and read through `/api/v1/scheduled-tasks/{task_id}/runs`, `GET /api/v1/scheduled-task-runs`, and `GET /api/v1/scheduled-task-runs/{run_id}/items`.
- **Decision**: **Secondary**.
- **Why it remains**: it still provides saved cron scheduling, persisted run history, notification fanout, and project-bound codebase access. Those are real runtime features that are not yet replaced by the newer defect-centric service seams.
- **Why it is demoted**: `ScheduleManager` still owns too much domain logic. It fetches ONES items itself, branches between `Planner` and `Analyzer`, resolves a `Codebase` directly, and persists scheduled run output in legacy task-shaped records. That makes it useful for continuity, but wrong as the canonical workflow owner after M1 to M6.
- **Required relation to the defect-centric architecture**: retain scheduled runs as an optional batch and history surface. After rewiring, they should call the same normalized intake, repo-resolution, analysis, and execution boundaries as the main defect flow. Scheduled history should describe work done by the new services, not preserve a separate planner-versus-analyzer runtime model.

### 3. Manual AI trigger

- **Current owner and file**: `main.py` `POST /api/v1/ai/trigger`.
- **Decision**: **Deprecated compatibility shim**.
- **Why it remains for now**: it is still the only explicit single-item operator trigger in the live backend. It also already moved one step toward M2 by fetching item detail through `OnesGateway.get_defect_detail(...)` instead of scanning raw ONES lists.
- **Why it is a deprecation candidate**: this route still directly instantiates `Planner` for `plan` and `Analyzer` for `analyze`, and it writes legacy engine state by hand. That overlaps the intended defect-centric runtime shape, where a defect detail endpoint, analysis action, and execution action should sit behind canonical services and canonical contracts.
- **Required relation to the defect-centric architecture**: keep it only as a temporary compatibility shim. The canonical primary flow is `GET /api/v1/defects/{id}` for defect analysis state and `POST /api/v1/defects/{id}/execution` for branch execution. `POST /api/v1/ai/trigger` must stay explicitly secondary or deprecated in code and responses until it can be removed.
- **Current compatibility signaling**: the live route now returns `Deprecation: true`, a deprecation `Warning` header, a `Link` header to the canonical defect detail route, and a `deprecated` payload block that points callers to the canonical defect analysis and execution endpoints.

### 4. MCP tools and server

- **Current owner and file**: `server.py`, which runs `FastMCP("ONES Defect Agent")` and exposes tools such as `fetch_defects`, `fetch_my_defects`, `check_new_defects`, `get_defect_detail`, `list_projects`, `search_codebase`, and `push_to_wechat`.
- **Decision**: **Secondary**.
- **Why it remains**: it is a real integration surface for external agents and is intentionally separate from the FastAPI app. It should survive M7 because external agent adoption still needs a stable tool-facing surface.
- **Why it is demoted**: it still uses the sync `OnesClient`, optional direct `Codebase` access, and its own incremental defect check behavior through `Store`. It does not consume `DefectRecord`, `RepoResolution`, `AnalysisResult`, or `ExecutionRequest` as the system-of-record contracts.
- **Required relation to the defect-centric architecture**: keep the MCP server as a transport layer, not a competing workflow layer. Its retained future is to wrap `OnesGateway`, `RepoResolver`, `DefectAnalysisWorkflowService`, and `ExecutionService` so external agents can invoke the same backend semantics as FastAPI callers.

## What is not retained as an M7 target

- `src/core/agent.py::DefectAgent` is not started by `main.py` and should stay outside the retained M7 runtime plan. It is best treated as legacy reference behavior only.

## Immediate M7 runtime adoption targets

The next rewiring step should focus on the runtime surfaces that most directly overlap the new defect-centric design.

1. Add or finish canonical backend defect endpoints under `/api/v1/defects` for list, detail, analysis status or result, and branch execution status so the frontend no longer depends on generic task payloads or compatibility shims.
2. Rewire the single-item backend path so current `POST /api/v1/ai/trigger` behavior is replaced by service-backed defect actions that use `OnesGateway`, `RepoResolver`, and `DefectAnalysisWorkflowService.analyze_result(...)`.
3. Rewire execution adoption so the live backend reaches `ExecutionService.execute(...)` for branch creation and exposes persisted execution status from `Engine.execution_records`.
4. Rewire retained scheduler and scheduled-task shells to call the same service layer, then keep or remove thin compatibility routes based on whether they still add unique operator value.

## Authoritative M7 interpretation

- **Retained and supported**: background scheduler polling.
- **Retained but secondary**: scheduled task runs, MCP tools and server.
- **Retained only as a deprecated compatibility shim while canonical defect actions exist**: manual AI trigger.

That split keeps automatic discovery and external integration available, preserves scheduled history and notification behavior during migration, and prevents `/api/v1/ai/trigger` from becoming a second long-term defect workflow beside the canonical `/defects` runtime.
