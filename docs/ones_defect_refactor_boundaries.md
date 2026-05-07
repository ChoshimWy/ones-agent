# ONES defect refactor migration boundaries

This note defines the current backend boundaries for the ONES defect refactor. It is based on the live code paths in `main.py`, `server.py`, `src/core/scheduler.py`, `src/core/schedule_manager.py`, `src/core/agent.py`, `src/llm/planner.py`, `src/llm/analyzer.py`, `src/integrations/git_ops.py`, and the canonical contracts in `src/contracts.py`.

## Boundary baseline

- `src/contracts.py` is the authoritative neutral contract layer for the new workflow. New M1 to M7 backend work should attach to these contracts instead of inventing more ad hoc dict shapes.
- `main.py` is the active FastAPI runtime entry point today. It starts both `Scheduler` and `ScheduleManager`, exposes manual AI trigger routes, and keeps current scheduled-task APIs alive during migration.
- `server.py` is a separate MCP runtime. It is still valid as a data and action surface, but it is not the authority for the new defect analysis workflow contracts.
- ONES access is still duplicated across async and sync integrations. That duplication is preserved for now and gets unified in M2, not in this documentation slice.
- `GitOps` is a later execution boundary. It should not be pulled into analysis or scheduling work before M5.

## Current runtime map

### `main.py`

- Owns the active FastAPI app lifecycle.
- Starts `Scheduler` and `ScheduleManager` in `lifespan()`.
- Keeps current task, scheduled-task, scheduler-trigger, and manual `/api/v1/ai/trigger` routes active.
- Uses `Planner` directly for planning paths and `Analyzer` directly for manual single-item analysis.

### `server.py`

- Owns the standalone MCP server surface through `FastMCP`.
- Exposes ONES data fetch, codebase search/read, and WeChat push tools.
- Uses the sync `OnesClient` and optional `Codebase`, not the FastAPI scheduler flow.

## Component boundaries

### `src/llm/planner.py`, `Planner`

**Current responsibility**

- Produces a `DevPlan` from a single ONES work item.
- Shapes branch name, steps, risk, approval flag, and summary.
- Handles both async `plan()` and sync `plan_sync()` calls, with local fallback behavior.

**Current runtime status**

- Active in the current FastAPI runtime.
- Used by `src/core/scheduler.py` when new defects are polled.
- Used by `src/core/schedule_manager.py` for scheduled tasks with `action == "plan"`.
- Used by `main.py` in `/api/v1/ai/trigger` and LLM connection testing.

**Relation to target architecture**

- Treat `Planner` as a reusable planning primitive, not as the owner of the new end-to-end defect workflow.
- M4 may reuse planner internals where compatible, but the new analysis workflow should emit `AnalysisResult` and markdown rendering through `src/contracts.py` boundaries.
- Planning output can inform future execution requests, but it is not the canonical execution contract.

**Do not move in the first wave**

- Do not merge `Planner` with `Analyzer`.
- Do not make `Planner` own repo resolution, ONES ingestion, or git execution.
- Do not refactor fallback branch naming logic into `GitOps` yet.

### `src/llm/analyzer.py`, `Analyzer`

**Current responsibility**

- Produces markdown analysis for a defect.
- Runs a brief analysis when no codebase is provided.
- Runs root-cause analysis with codebase tree, candidate file selection, and code excerpts when a `Codebase` is present.
- Returns plain strings and batch result dicts, not canonical `AnalysisResult` objects.

**Current runtime status**

- Active in the current runtime, but only on selected paths.
- Used by `src/core/schedule_manager.py` for scheduled tasks with `action == "analyze"`.
- Used by `main.py` for manual `/api/v1/ai/trigger` analysis.
- Not used by `src/core/scheduler.py`, which only plans polled defects.

**Relation to target architecture**

- Keep `Analyzer` as an implementation detail that M4 may wrap or split.
- The new analysis service should own stage boundaries such as defect understanding, evidence collection, root-cause hypothesis, fix suggestions, confidence, and insufficient-evidence handling.
- Existing analyzer prompts and codebase lookup behavior are source material, not the new contract boundary.

**Do not move in the first wave**

- Do not wire `Analyzer` directly to branch creation or any git mutation.
- Do not preserve markdown-only output as the new system of record.
- Do not fold ONES fetching into `Analyzer`.

### `src/core/scheduler.py`, `Scheduler`

**Current responsibility**

- Polls ONES on an interval through `OnesAsyncClient`.
- Filters new defects through `Store`.
- Creates engine work items for new defects.
- Invokes `Planner` to produce a plan and advances `Engine` state.

**Current runtime status**

- Active in the FastAPI app today.
- Started in `main.py` lifespan and exposed through `/api/v1/scheduler/status` and `/api/v1/scheduler/trigger`.
- Represents the current background polling path for newly discovered defects.

**Relation to target architecture**

- Keep `Scheduler` as a legacy entry point that can later call new ingestion and workflow services.
- In M7 it may remain as a trigger/orchestration shell, but it should stop owning raw normalization and direct planner coupling once M2 to M4 land.
- Its future role is scheduling and dispatch, not business logic ownership.

**Do not move in the first wave**

- Do not combine polling concerns with analysis result shaping.
- Do not move scheduled persistence concerns from `Engine` into the new workflow layer yet.
- Do not unify sync and async ONES clients inside `Scheduler`; that belongs to M2.

### `src/core/schedule_manager.py`, `ScheduleManager`

**Current responsibility**

- Manages multiple saved scheduled tasks and checks cron-like schedules every minute.
- Fetches ONES items with `OnesAsyncClient`.
- Chooses between planning and analysis actions.
- Optionally resolves a project-bound `Codebase`, caches it, and sends notifications.
- Persists scheduled run results through `Engine`.

**Current runtime status**

- Active in the FastAPI app today.
- Started in `main.py` lifespan.
- Driven by the scheduled-task REST APIs and manual trigger route in `main.py`.

**Relation to target architecture**

- Keep `ScheduleManager` as the legacy scheduled orchestration shell during migration.
- New M2 to M4 services should eventually sit behind its fetch/process steps instead of being reimplemented inside it.
- It should become a caller of the new services, not the owner of ONES normalization, repo resolution, or structured analysis output.

**Do not move in the first wave**

- Do not mix notification formatting with new contract design.
- Do not preserve its current planner-vs-analyzer branch as the target domain model.
- Do not couple scheduled-task management to branch creation.

### `src/core/agent.py`, `DefectAgent`

**Current responsibility**

- Encapsulates the older sync loop: fetch new defects, analyze them, and optionally push a WeChat report.
- Uses sync `OnesClient`, `Analyzer`, `Store`, `WeChatBot`, and optional `Codebase`.
- Supports one-shot and background thread execution.

**Current runtime status**

- Legacy or parallel path.
- It is not started by `main.py`.
- It is separate from the active FastAPI scheduler and scheduled-task runtime.

**Relation to target architecture**

- Treat `DefectAgent` as legacy reference behavior only.
- It can provide migration clues for old sync monitoring and notification behavior, but it should not become the base class or orchestration root for the new contracts-driven workflow.

**Do not move in the first wave**

- Do not refactor the new workflow around this class.
- Do not merge its sync ONES path with the async FastAPI path during M1.
- Do not expand it to own planner, repo resolution, or execution responsibilities.

### `server.py`, MCP server responsibilities

**Current responsibility**

- Exposes tool-style access for external agents.
- Serves ONES fetches, project listing, incremental defect checks, codebase search/file reads, and WeChat push.
- Acts as a pure data and action provider without embedded LLM analysis.

**Current runtime status**

- Active as a separate runtime when launched directly.
- Parallel to the FastAPI app, not embedded into `main.py`.

**Relation to target architecture**

- Keep the MCP server as an integration surface that can later call the same backend services and contracts.
- It should eventually consume normalized defect and analysis services, not define competing shapes or orchestration rules.

**Do not move in the first wave**

- Do not make MCP tools the source of truth for the new workflow.
- Do not couple MCP tool signatures to unstable interim contract changes.
- Do not collapse MCP and FastAPI entry points into one refactor step.

### `src/integrations/git_ops.py`, `GitOps`

**Current responsibility**

- Clones and updates repositories.
- Creates branches.
- Commits, pushes, and opens pull requests.
- Infers commit prefixes and branch slugs.

**Current runtime status**

- Present as a utility module.
- Not part of the active scheduler flow documented in `main.py`, `Scheduler`, or `ScheduleManager`.
- Not wired into manual analysis paths.

**Relation to target architecture**

- Keep `GitOps` as the implementation detail behind the future M5 execution service.
- The new workflow should reach it only through `ExecutionRequest` and an execution boundary that can stay idempotent and auditable.

**Do not move in the first wave**

- Do not use `GitOps` as part of M4 analysis.
- Do not let planning or analysis import git mutation concerns directly.
- Do not refactor commit or PR behavior together with branch-creation boundary work.

## Authoritative vs legacy paths

### Authoritative today

- Runtime entry: `main.py`
- Background polling: `src/core/scheduler.py`
- Scheduled task orchestration: `src/core/schedule_manager.py`
- Neutral workflow contracts: `src/contracts.py`

### Active but parallel

- MCP runtime: `server.py`
- LLM implementation helpers: `src/llm/planner.py`, `src/llm/analyzer.py`

### Legacy or not yet wired into the main defect workflow

- Sync monitoring wrapper: `src/core/agent.py`
- Git execution utility: `src/integrations/git_ops.py`

## Target flow ownership

The target architecture should own these boundaries, in this order:

1. M2, a unified ONES ingestion service normalizes raw ONES payloads into `DefectRecord`.
2. M3, a resolver and codebase access layer produces `RepoResolution` plus evidence inputs.
3. M4, an analysis workflow converts `DefectRecord` and `RepoResolution` into `AnalysisResult` and markdown rendering.
4. M5, an execution workflow converts approved intent into `ExecutionRequest` and only then calls `GitOps` for branch creation.
5. M7, legacy entry points such as scheduler polling, scheduled tasks, manual AI trigger, and MCP tools are rewired to call the new services.

## Do not couple yet

- Do not couple ONES ingestion and normalization to planner or analyzer prompts.
- Do not couple analysis output to git branch creation, commits, pushes, or PR creation.
- Do not couple scheduled-task persistence and notification formatting to canonical workflow contracts.
- Do not couple the sync MCP or `DefectAgent` ONES client paths to the async FastAPI migration in M1.
- Do not couple repo resolution with execution behavior; M3 resolves code targets, M5 performs controlled branch creation.

## Migration sequence, M1 to M7

### M1

- Keep legacy entry points in place.
- Use `src/contracts.py` as the only new authoritative schema layer.
- Document boundaries before changing behavior.

### M2

- Introduce one backend ingestion service for ONES data.
- Route existing callers toward normalized `DefectRecord` output.
- Leave sync and async client implementation details behind the new service boundary.

### M3

- Pull repo mapping and codebase access into dedicated services.
- Return explicit `RepoResolution` results and explicit failure states.
- Do not merge this with analysis or execution yet.

### M4

- Build a dedicated analysis workflow on top of `DefectRecord` and `RepoResolution`.
- Reuse useful planner or analyzer internals only where they fit the new contract.
- Keep the path read-only.

### M5

- Add a separate execution service for approved branch creation only.
- Put `GitOps` behind `ExecutionRequest`.
- Keep commit, push, and PR behavior outside the first execution cut unless explicitly added later.

### M6

- Rework frontend information architecture around defects, analysis, and execution.
- Consume the new backend contracts instead of scheduled-task-shaped payloads where possible.

### M7

- Rewire `Scheduler`, `ScheduleManager`, manual AI trigger routes, and MCP tools to call the new services.
- Remove or deprecate duplicate paths only after the new flow is verified.
- Keep any remaining legacy behavior explicitly documented.

## First-wave guardrails

- Preserve `main.py` entry points while the new flow is added beside them.
- Preserve `server.py` as a separate MCP surface.
- Preserve current planner and analyzer behavior until the new service boundaries exist.
- Treat any claim about runtime ownership as valid only when supported by the files listed in this note.
