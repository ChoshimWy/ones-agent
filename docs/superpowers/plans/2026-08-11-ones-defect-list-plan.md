# ONES Defect List Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a standalone, read-only `ones-dev defects list` command for bounded ONES defect discovery.

**Architecture:** Route the new command before the full workflow factory and construct only the existing `DefectCandidateService` over `OnesGateway`. Reuse the gateway's open-status filtering and emit an explicit output DTO rather than internal snapshots.

**Tech Stack:** Python 3.11, argparse, asyncio, Pydantic contracts, pytest.

---

### Task 1: Define CLI behavior with failing tests

**Files:**
- Modify: `tests/test_developer_workflow_cli.py`

- [ ] Add tests for parser help, table output, JSON allowlist, pagination forwarding, empty results, invalid bounds, factory isolation, and redacted errors.
- [ ] Run `uv run pytest tests/test_developer_workflow_cli.py -k defects_list -q` and confirm failures are caused by the missing `defects list` command.

### Task 2: Implement the read-only command

**Files:**
- Modify: `src/developer_workflow/cli.py`
- Modify: `src/developer_workflow/defect_flow.py`

- [ ] Add the nested parser and strict numeric validation.
- [ ] Add a read-only factory protocol and production factory requiring only supported ONES email/password authentication.
- [ ] Dispatch listing without loading `DeveloperWorkflowConfig` or constructing the orchestrator.
- [ ] Emit table/JSON from an explicit allowlist and close the gateway on completion.
- [ ] Allow per-call candidate limit/page size while preserving existing defaults and bounds.
- [ ] Run the focused tests until green.

### Task 3: Document and verify

**Files:**
- Modify: `docs/ones_dev_cli.md`

- [ ] Document the new command, its read-only behavior, required ONES variables, output modes, and distinction from `defect`.
- [ ] Run CLI, candidate-service, gateway, and configuration regression tests.
- [ ] Run `uv run ones-dev --help`, `uv lock --check --offline`, Python compilation, and `git diff --check`.
