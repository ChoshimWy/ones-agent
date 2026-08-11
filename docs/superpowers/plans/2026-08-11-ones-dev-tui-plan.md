# ONES Dev TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不绕过现有 Orchestrator、FileRunStore、审批与发布安全边界的前提下，为 `ones-dev` 增加键盘优先、支持鼠标的全屏 Textual 工作流控制台。

**Architecture:** 新增 `src/developer_workflow/tui/` 展示适配包，依次分离安全 ViewModel、只读 RunIndex、Orchestrator Controller、跨 run 并发 Supervisor 和 Textual screens。所有变更动作仍只调用 `DeveloperWorkflowOrchestrator`；TUI 仅从持久化 run 重建状态，并在危险动作前重新加载权威版本。

**Tech Stack:** Python 3.11、Textual、Pydantic v2、pytest、pytest-asyncio、Textual `run_test()`/Pilot、现有 FileRunStore 与 DeveloperWorkflowOrchestrator。

---

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `src/developer_workflow/tui/__init__.py` | 只导出稳定 TUI 公共入口与类型 |
| `src/developer_workflow/tui/models.py` | 脱敏文本、不可变 ViewModel、筛选与动作请求 |
| `src/developer_workflow/tui/run_index.py` | 从 FileRunStore 安全列举、过滤、排序 run |
| `src/developer_workflow/tui/controller.py` | 唯一 Orchestrator 适配边界、候选快照会话、权威版本复核 |
| `src/developer_workflow/tui/supervisor.py` | 每 run 串行、跨 run 有界并发、UI 安全事件 |
| `src/developer_workflow/tui/screens.py` | 仪表盘、详情标签、向导和危险操作 Modal |
| `src/developer_workflow/tui/app.py` | Textual App、响应式布局、刷新、快捷键和启动入口 |
| `src/developer_workflow/state_store.py` | 增加只读安全 run ID 枚举，不改变写入语义 |
| `src/developer_workflow/config.py` | 增加 1–8 范围的 TUI 并发配置 |
| `src/developer_workflow/cli.py` | 注册 `tui` 子命令并复用生产工厂 |
| `tests/test_developer_workflow_tui_*.py` | 按边界拆分的单元、无头 UI、集成和安全测试 |
| `docs/ones_dev_cli.md` | 启动、页面、快捷键、并发、退出与恢复说明 |

### Task 1: Textual 依赖、配置与 CLI 命令骨架

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/developer_workflow/config.py`
- Modify: `src/developer_workflow/cli.py`
- Modify: `tests/test_developer_workflow_config.py`
- Modify: `tests/test_developer_workflow_cli.py`

- [ ] **Step 1: 写配置和解析器失败测试**

```python
def test_tui_concurrency_defaults_to_three(valid_config: dict[str, object]) -> None:
    config = DeveloperWorkflowConfig.model_validate(valid_config)
    assert config.tui_max_concurrency == 3


@pytest.mark.parametrize("value", [0, 9, True, "3"])
def test_tui_concurrency_is_strictly_bounded(
    valid_config: dict[str, object], value: object
) -> None:
    valid_config["tui_max_concurrency"] = value
    with pytest.raises(ValidationError):
        DeveloperWorkflowConfig.model_validate(valid_config)


def test_parser_accepts_tui_command() -> None:
    args = _parser(io.StringIO(), io.StringIO()).parse_args(
        ["tui", "--config", "custom.json"]
    )
    assert args.command == "tui"
    assert args.config == "custom.json"
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_config.py tests/test_developer_workflow_cli.py -k "tui_concurrency or parser_accepts_tui" -q`

Expected: FAIL，分别显示 `tui_max_concurrency` 和 `tui` 子命令尚不存在。

- [ ] **Step 3: 添加严格配置字段和命令骨架**

```python
# src/developer_workflow/config.py
from pydantic import Field, StrictInt

tui_max_concurrency: StrictInt = Field(default=3, ge=1, le=8)
```

```python
# src/developer_workflow/cli.py，_parser 内
tui = command("tui")
```

```toml
# pyproject.toml
"textual>=0.89,<2",
```

- [ ] **Step 4: 更新锁文件并验证 GREEN**

Run: `uv lock && uv run pytest tests/test_developer_workflow_config.py tests/test_developer_workflow_cli.py -k "tui_concurrency or parser_accepts_tui" -q`

Expected: PASS；`uv lock --check` 返回 0。

- [ ] **Step 5: 提交依赖与命令骨架**

```bash
git add pyproject.toml uv.lock src/developer_workflow/config.py src/developer_workflow/cli.py tests/test_developer_workflow_config.py tests/test_developer_workflow_cli.py
git commit -m "feat(tui): register terminal console configuration"
```

### Task 2: 安全且不可变的 TUI ViewModel

**Files:**
- Create: `src/developer_workflow/tui/__init__.py`
- Create: `src/developer_workflow/tui/models.py`
- Create: `tests/test_developer_workflow_tui_models.py`

- [ ] **Step 1: 写文本边界和 ViewModel 失败测试**

```python
@pytest.mark.parametrize("value", ["TOKEN\nforged", "x\u202eabc", "x\ud800"])
def test_safe_tui_text_rejects_control_and_invalid_unicode(value: str) -> None:
    with pytest.raises(TuiDisplayError, match="display value is invalid"):
        safe_tui_text(value)


def test_run_summary_contains_only_display_whitelist(waiting_run: WorkflowRun) -> None:
    summary = RunSummary.from_run(waiting_run, activity=RunActivity.IDLE)
    assert summary.run_id == waiting_run.run_id
    assert summary.state is WorkflowState.WAITING_APPROVAL
    assert not hasattr(summary, "requirement")
    assert not hasattr(summary, "defect")
    assert not hasattr(summary, "wiki_snapshots")
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_tui_models.py -q`

Expected: collection FAIL，`src.developer_workflow.tui.models` 尚不存在。

- [ ] **Step 3: 实现严格文本边界和核心模型**

```python
# src/developer_workflow/tui/models.py
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from ..contracts import CommandResult, WorkflowRun, WorkflowState, WorkflowType

class TuiDisplayError(ValueError):
    pass

class RunActivity(str, Enum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"

def safe_tui_text(value: object, *, maximum: int = 4096) -> str:
    if type(value) is not str:
        value = str(value)
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise TuiDisplayError("display value is invalid") from None
    if not value or len(value) > maximum or any(
        unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        for char in value
    ):
        raise TuiDisplayError("display value is invalid")
    return value

@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    workflow_type: WorkflowType
    work_item_id: str
    state: WorkflowState
    version: int
    updated_at: datetime
    activity: RunActivity
    corrupted: bool = False

    @classmethod
    def from_run(cls, run: WorkflowRun, *, activity: RunActivity) -> "RunSummary":
        return cls(
            run_id=safe_tui_text(run.run_id, maximum=64),
            workflow_type=run.type,
            work_item_id=safe_tui_text(run.work_item_id, maximum=256),
            state=run.state,
            version=run.version,
            updated_at=run.updated_at,
            activity=activity,
        )

    @classmethod
    def corrupted_entry(cls, run_id: str) -> "RunSummary":
        return cls(
            run_id=safe_tui_text(run_id, maximum=64),
            workflow_type=WorkflowType.REQUIREMENT,
            work_item_id="storage-corrupted",
            state=WorkflowState.BLOCKED,
            version=0,
            updated_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
            activity=RunActivity.IDLE,
            corrupted=True,
        )

@dataclass(frozen=True, slots=True)
class RepositoryView:
    key: str
    role: str
    base_commit: str
    head_commit: str
    tree_hash: str
    changed_files: tuple[str, ...]
    commit_hash: str
    pushed: bool
    pr_url: str
    error: str

@dataclass(frozen=True, slots=True)
class TestView:
    command: str
    outcome: str
    exit_code: int

@dataclass(frozen=True, slots=True)
class PublicationView:
    repositories: tuple[RepositoryView, ...]
    comment_id: str
    error: str

@dataclass(frozen=True, slots=True)
class HistoryView:
    source: str
    target: str
    occurred_at: datetime

@dataclass(frozen=True, slots=True)
class RunDetail:
    summary: RunSummary
    repositories: tuple[RepositoryView, ...]
    tests: tuple[TestView, ...]
    review: tuple[str, ...]
    publication: PublicationView
    history: tuple[HistoryView, ...]
    blocked_reason: str
    fingerprint: str

    @classmethod
    def from_run(cls, run: WorkflowRun) -> "RunDetail":
        return run_detail_from_run(run)

@dataclass(frozen=True, slots=True)
class DefectChoice:
    candidate_id: str
    title: str
    status_id: str
    priority: str

@dataclass(frozen=True, slots=True)
class RunFilter:
    states: tuple[WorkflowState, ...] = ()
    workflow_types: tuple[WorkflowType, ...] = ()
    query: str = ""

    def matches(self, item: RunSummary) -> bool:
        query = self.query.casefold().strip()
        return (
            (not self.states or item.state in self.states)
            and (not self.workflow_types or item.workflow_type in self.workflow_types)
            and (not query or query in item.work_item_id.casefold() or query in item.run_id)
        )

@dataclass(frozen=True, slots=True)
class DangerousActionRequest:
    run_id: str
    version: int
    action: str
    fingerprint: str
    repositories: tuple[RepositoryView, ...]
    work_item_id: str
    changed_file_count: int
    test_count: int
    risk_count: int
    unresolved_count: int

    @classmethod
    def from_run(cls, run: WorkflowRun, *, action: str) -> "DangerousActionRequest":
        if action not in {"approve", "revise", "cancel", "resume-publication"}:
            raise TuiDisplayError("workflow action is invalid")
        detail = run_detail_from_run(run)
        return cls(
            run_id=detail.summary.run_id,
            version=detail.summary.version,
            action=action,
            fingerprint=detail.fingerprint,
            repositories=detail.repositories,
            work_item_id=detail.summary.work_item_id,
            changed_file_count=sum(len(item.changed_files) for item in detail.repositories),
            test_count=len(detail.tests),
            risk_count=len(run.approval.risks) if run.approval else 0,
            unresolved_count=len(run.approval.unresolved_items) if run.approval else 0,
        )

def test_view(result: CommandResult) -> TestView:
    return TestView(
        command=safe_tui_text(result.command),
        outcome=result.outcome.value,
        exit_code=result.exit_code,
    )

def run_detail_from_run(run: WorkflowRun) -> RunDetail:
    approvals = {
        item.repository_key: item for item in (run.approval.repositories if run.approval else ())
    }
    publications = {
        item.repository_key: item
        for item in (run.group_publication.repositories if run.group_publication else ())
    }
    repositories: list[RepositoryView] = []
    for evidence in run.repository_evidence:
        approval = approvals.get(evidence.repository_key)
        publication = publications.get(evidence.repository_key)
        repositories.append(RepositoryView(
            key=evidence.repository_key,
            role=evidence.mapping.role.value,
            base_commit=evidence.prepared_worktree.base_commit,
            head_commit=evidence.prepared_worktree.head_commit,
            tree_hash=approval.tree_hash if approval else "",
            changed_files=evidence.changed_files,
            commit_hash=publication.commit_hash if publication else "",
            pushed=bool(publication and publication.push_completed_at),
            pr_url=safe_tui_text(publication.pr_url) if publication and publication.pr_url else "",
            error="publication failed safely" if publication and publication.error else "",
        ))
    tests = tuple(test_view(item) for item in (*run.test_results, *run.integration_test_results))
    review = tuple(safe_tui_text(item) for item in (run.approval.review if run.approval else ()))
    group_error = "publication failed safely" if run.group_publication and run.group_publication.error else ""
    return RunDetail(
        summary=RunSummary.from_run(run, activity=RunActivity.IDLE),
        repositories=tuple(repositories),
        tests=tests,
        review=review,
        publication=PublicationView(
            repositories=tuple(repositories),
            comment_id="delivered" if run.group_publication and run.group_publication.comment_id else "",
            error=group_error,
        ),
        history=tuple(
            HistoryView(
                source=item.source.value,
                target=item.target.value,
                occurred_at=item.occurred_at,
            )
            for item in run.history
        ),
        blocked_reason="workflow blocked safely" if run.blocked_reason else "",
        fingerprint=run.approval.fingerprint if run.approval else "",
    )
```

- [ ] **Step 4: 运行模型测试**

Run: `uv run pytest tests/test_developer_workflow_tui_models.py -q`

Expected: PASS，恶意 Unicode、超长文本、secret 字段探针和多仓 publication 映射均通过预期断言。

- [ ] **Step 5: 提交 ViewModel 边界**

```bash
git add src/developer_workflow/tui tests/test_developer_workflow_tui_models.py
git commit -m "feat(tui): add safe workflow view models"
```

### Task 3: FileRunStore 只读枚举与 RunIndex

**Files:**
- Modify: `src/developer_workflow/state_store.py`
- Create: `src/developer_workflow/tui/run_index.py`
- Create: `tests/test_developer_workflow_tui_run_index.py`

- [ ] **Step 1: 写安全枚举失败测试**

```python
def test_run_index_lists_valid_runs_and_isolates_corruption(
    private_run_root: Path, valid_run: WorkflowRun
) -> None:
    store = FileRunStore(private_run_root)
    store.create(valid_run)
    corrupt = private_run_root / ("f" * 32)
    corrupt.mkdir()
    (corrupt / "run.json").write_text("{", encoding="utf-8")

    entries = RunIndex(store).list(RunFilter())

    assert [entry.run_id for entry in entries] == [valid_run.run_id, "f" * 32]
    assert entries[1].corrupted is True


def test_list_run_ids_rejects_symlink_and_outside_entries(
    private_run_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = private_run_root / ("e" * 32)
    link.symlink_to(outside, target_is_directory=True)
    assert ("e" * 32) not in FileRunStore(private_run_root).list_run_ids()
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_tui_run_index.py -q`

Expected: FAIL，`FileRunStore.list_run_ids` 和 `RunIndex` 尚不存在。

- [ ] **Step 3: 实现无写副作用的安全枚举**

```python
# src/developer_workflow/state_store.py
def list_run_ids(self) -> tuple[str, ...]:
    self._validate_root_identity()
    result: list[str] = []
    for child in self._run_root.iterdir():
        if not child.is_dir() or child.is_symlink():
            continue
        try:
            self._validate_run_id(child.name)
            self._assert_contained(child)
            _reject_reparse_point(child, "run directory")
        except RunStoreError:
            continue
        result.append(child.name)
    return tuple(sorted(result))
```

```python
# src/developer_workflow/tui/run_index.py
@dataclass(slots=True)
class RunIndex:
    store: FileRunStore

    def list(self, filters: RunFilter) -> tuple[RunSummary, ...]:
        entries: list[RunSummary] = []
        for run_id in self.store.list_run_ids():
            try:
                run = self.store.load(run_id)
                item = RunSummary.from_run(run, activity=RunActivity.IDLE)
            except (RunCorruptedError, UnsafeRunPathError):
                item = RunSummary.corrupted_entry(run_id)
            if filters.matches(item):
                entries.append(item)
        return tuple(sorted(entries, key=lambda item: item.updated_at, reverse=True))
```

- [ ] **Step 4: 验证排序、过滤与损坏隔离**

Run: `uv run pytest tests/test_developer_workflow_tui_run_index.py tests/test_developer_workflow_state_store.py -q`

Expected: PASS；读取索引不创建、修改或删除任何 run 文件。

- [ ] **Step 5: 提交 RunIndex**

```bash
git add src/developer_workflow/state_store.py src/developer_workflow/tui/run_index.py tests/test_developer_workflow_tui_run_index.py
git commit -m "feat(tui): add read-only run index"
```

### Task 4: TuiController 与候选快照会话

**Files:**
- Create: `src/developer_workflow/tui/controller.py`
- Create: `tests/test_developer_workflow_tui_controller.py`

- [ ] **Step 1: 写 Controller 精确转发和陈旧版本失败测试**

```python
def test_defect_selection_uses_controller_owned_snapshot(
    controller: TuiController, candidate_service: FakeCandidates
) -> None:
    session = controller.query_defects("project", "iteration", "assignee", ("todo",))
    run = controller.start_defect(session.session_id, session.items[0].candidate_id)
    assert candidate_service.selected == (
        SNAPSHOT_TOKEN,
        session.items[0].candidate_id,
    )
    assert run.workflow_type is WorkflowType.DEFECT


def test_dangerous_action_rejects_stale_version(
    controller: TuiController, waiting_run: WorkflowRun
) -> None:
    request = controller.prepare_action(waiting_run.run_id, "approve")
    controller.orchestrator.store.save(
        waiting_run.validated_update(updated_at=utc_now()),
        waiting_run.version,
    )
    with pytest.raises(StaleTuiActionError, match="workflow changed; review again"):
        controller.approve(request, actor="operator")
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_tui_controller.py -q`

Expected: collection FAIL，Controller 尚不存在。

- [ ] **Step 3: 实现唯一 Orchestrator 适配边界**

```python
# src/developer_workflow/tui/controller.py
@dataclass(frozen=True, slots=True)
class CandidateSessionView:
    session_id: str
    items: tuple[DefectChoice, ...]

@dataclass(frozen=True, slots=True)
class CandidateSessionRecord:
    project_id: str
    iteration_id: str
    assignee_id: str
    snapshot_token: str
    candidate_ids: frozenset[str]

@dataclass(slots=True)
class TuiController:
    orchestrator: DeveloperWorkflowOrchestrator
    run_index: RunIndex
    _candidate_sessions: dict[str, CandidateSessionRecord] = field(default_factory=dict)

    def show(self, run_id: str) -> RunDetail:
        return RunDetail.from_run(self.orchestrator.show(run_id))

    def list_runs(self, filters: RunFilter) -> tuple[RunSummary, ...]:
        return self.run_index.list(filters)

    def query_defects(
        self,
        project_id: str,
        iteration_id: str,
        assignee_id: str,
        status_ids: tuple[str, ...],
    ) -> CandidateSessionView:
        candidates = asyncio.run(self.orchestrator.defect_candidates.list_candidates(
            project_id,
            iteration_id,
            assignee_id,
            status_ids=status_ids or None,
        ))
        if not candidates:
            return CandidateSessionView(session_id="", items=())
        tokens = {item.snapshot_token for item in candidates}
        if len(tokens) != 1:
            raise TuiControllerError("defect candidate snapshot is invalid")
        session_id = uuid.uuid4().hex
        self._candidate_sessions[session_id] = CandidateSessionRecord(
            project_id=project_id,
            iteration_id=iteration_id,
            assignee_id=assignee_id,
            snapshot_token=tokens.pop(),
            candidate_ids=frozenset(item.uuid for item in candidates),
        )
        return CandidateSessionView(
            session_id=session_id,
            items=tuple(DefectChoice(
                candidate_id=item.uuid,
                title=safe_tui_text(item.title, maximum=512),
                status_id=safe_tui_text(item.status_id, maximum=128),
                priority=safe_tui_text(item.priority, maximum=128),
            ) for item in candidates),
        )

    def start_defect(self, session_id: str, candidate_id: str) -> RunDetail:
        session = self._candidate_sessions.pop(session_id, None)
        if session is None or candidate_id not in session.candidate_ids:
            raise TuiControllerError("defect candidate selection is invalid")
        run = self.orchestrator.start_defect(
            session.project_id,
            session.iteration_id,
            session.assignee_id,
            session.snapshot_token,
            candidate_id,
        )
        return RunDetail.from_run(run)

    def start_requirement(self, requirement_id: str) -> RunDetail:
        return RunDetail.from_run(self.orchestrator.start_requirement(requirement_id))

    def confirm_repository(self, run_id: str, mapping_key: str) -> RunDetail:
        return RunDetail.from_run(self.orchestrator.confirm_repository(run_id, mapping_key))

    def resume(self, run_id: str) -> RunDetail:
        return RunDetail.from_run(self.orchestrator.resume(run_id))

    def revise(
        self,
        request: DangerousActionRequest,
        feedback: str,
        scope: str | None,
    ) -> RunDetail:
        self._authoritative(request)
        return RunDetail.from_run(
            self.orchestrator.revise(request.run_id, feedback, scope=scope)
        )

    def cancel(self, request: DangerousActionRequest, actor: str) -> RunDetail:
        self._authoritative(request)
        return RunDetail.from_run(self.orchestrator.cancel(request.run_id, actor))

    def prepare_action(self, run_id: str, action: str) -> DangerousActionRequest:
        run = self.orchestrator.show(run_id)
        return DangerousActionRequest.from_run(run, action=action)

    def _authoritative(self, request: DangerousActionRequest) -> WorkflowRun:
        run = self.orchestrator.show(request.run_id)
        if run.version != request.version:
            raise StaleTuiActionError("workflow changed; review again")
        return run

    def approve(self, request: DangerousActionRequest, actor: str) -> RunDetail:
        self._authoritative(request)
        return RunDetail.from_run(self.orchestrator.approve(request.run_id, actor))
```

真实 snapshot token 只存在 Controller 私有 session；UI 只得到随机 session ID 与 `DefectChoice`。所有动作只调用 Orchestrator，不直接调用 flow、store 写接口或 Publisher。

- [ ] **Step 4: 运行 Controller 测试**

Run: `uv run pytest tests/test_developer_workflow_tui_controller.py tests/test_developer_workflow_orchestrator.py -q`

Expected: PASS；同时断言候选会话错配、过期 session、非法 action、版本漂移和异常文本均 fail closed。

- [ ] **Step 5: 提交 Controller**

```bash
git add src/developer_workflow/tui/controller.py tests/test_developer_workflow_tui_controller.py
git commit -m "feat(tui): adapt orchestrator for interactive workflows"
```

### Task 5: 每 run 串行、跨 run 并行的 Supervisor

**Files:**
- Create: `src/developer_workflow/tui/supervisor.py`
- Create: `tests/test_developer_workflow_tui_supervisor.py`

- [ ] **Step 1: 写并发与事件失败测试**

```python
@pytest.mark.asyncio
async def test_supervisor_serializes_same_run_and_parallelizes_different_runs() -> None:
    gate = threading.Event()
    calls: list[str] = []
    supervisor = RunTaskSupervisor(max_concurrency=2)

    def action(name: str) -> str:
        calls.append(name)
        gate.wait(timeout=1)
        return name

    first = supervisor.submit("run-a", "resume", lambda: action("a1"))
    second = supervisor.submit("run-a", "approve", lambda: action("a2"))
    other = supervisor.submit("run-b", "resume", lambda: action("b1"))
    await asyncio.sleep(0.05)
    assert calls == ["a1", "b1"]
    gate.set()
    assert await asyncio.gather(first, second, other) == ["a1", "a2", "b1"]


def test_events_never_include_exception_payload() -> None:
    event = TaskEvent.failed("run-a", RuntimeError("TOKEN-SECRET"))
    assert event.message == "workflow action failed safely"
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_tui_supervisor.py -q`

Expected: collection FAIL，Supervisor 尚不存在。

- [ ] **Step 3: 实现有界队列和安全事件**

```python
# src/developer_workflow/tui/supervisor.py
@dataclass(frozen=True, slots=True)
class TaskEvent:
    run_id: str
    action: str
    activity: RunActivity
    message: str

    @classmethod
    def queued(cls, run_id: str, action: str) -> "TaskEvent":
        return cls(run_id, action, RunActivity.QUEUED, "workflow action queued")

    @classmethod
    def started(cls, run_id: str, action: str) -> "TaskEvent":
        return cls(run_id, action, RunActivity.RUNNING, "workflow action started")

    @classmethod
    def completed(cls, run_id: str, action: str) -> "TaskEvent":
        return cls(run_id, action, RunActivity.IDLE, "workflow action completed")

    @classmethod
    def failed(cls, run_id: str, error: BaseException) -> "TaskEvent":
        return cls(run_id, "failed", RunActivity.IDLE, "workflow action failed safely")

class RunTaskSupervisor:
    def __init__(
        self,
        max_concurrency: int,
        sink: Callable[[TaskEvent], None] = lambda event: None,
    ) -> None:
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 8:
            raise ValueError("max_concurrency must be between 1 and 8")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._run_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._sink = sink

    def submit(self, run_id: str, action: str, call: Callable[[], T]) -> asyncio.Task[T]:
        return asyncio.create_task(self._execute(run_id, action, call))

    async def run_readonly(self, call: Callable[..., T], *args: object) -> T:
        async with self._semaphore:
            return await asyncio.to_thread(call, *args)

    async def run_mutation(
        self, run_id: str, action: str, call: Callable[..., T], *args: object
    ) -> T:
        return await self.submit(run_id, action, lambda: call(*args))

    async def _execute(self, run_id: str, action: str, call: Callable[[], T]) -> T:
        self._sink(TaskEvent.queued(run_id, action))
        async with self._run_locks[run_id], self._semaphore:
            self._sink(TaskEvent.started(run_id, action))
            try:
                result = await asyncio.to_thread(call)
            except BaseException as error:
                self._sink(TaskEvent.failed(run_id, error))
                raise
            self._sink(TaskEvent.completed(run_id, action))
            return result
```

- [ ] **Step 4: 验证队列、取消语义和上限**

Run: `uv run pytest tests/test_developer_workflow_tui_supervisor.py -q`

Expected: PASS；关闭 Supervisor 不调用 Orchestrator.cancel，异常事件不包含原异常或环境值。

- [ ] **Step 5: 提交 Supervisor**

```bash
git add src/developer_workflow/tui/supervisor.py tests/test_developer_workflow_tui_supervisor.py
git commit -m "feat(tui): schedule bounded workflow actions"
```

### Task 6: 仪表盘、详情标签与响应式布局

**Files:**
- Create: `src/developer_workflow/tui/screens.py`
- Create: `src/developer_workflow/tui/app.py`
- Create: `tests/test_developer_workflow_tui_app.py`

- [ ] **Step 1: 写三种宽度和键鼠导航失败测试**

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("width", "expected_mode"), [(120, "three"), (80, "two"), (60, "one")]
)
async def test_dashboard_responsive_modes(
    app_factory: Callable[[], DeveloperWorkflowTuiApp], width: int, expected_mode: str
) -> None:
    async with app_factory().run_test(size=(width, 32)) as pilot:
        assert pilot.app.screen.query_one("#dashboard").get_class(expected_mode)


@pytest.mark.asyncio
async def test_keyboard_opens_run_and_switches_tabs(app_factory) -> None:
    async with app_factory().run_test(size=(120, 32)) as pilot:
        await pilot.press("j", "enter", "tab")
        assert pilot.app.screen.query_one("#run-detail").display
        assert pilot.app.screen.query_one("#detail-tabs").active == "repositories"


@pytest.mark.asyncio
async def test_settings_screen_is_read_only_and_contains_no_credentials(app_factory) -> None:
    async with app_factory().run_test(size=(120, 32)) as pilot:
        await pilot.click("#nav-settings")
        text = str(pilot.app.screen.render())
        assert "max concurrency: 3" in text
        assert "ONES_PASSWORD" not in text
        assert not pilot.app.screen.query("Input.password")
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_tui_app.py -k "responsive or keyboard" -q`

Expected: collection FAIL，App 和 screens 尚不存在。

- [ ] **Step 3: 实现全屏 App 与 DashboardScreen**

```python
# src/developer_workflow/tui/app.py
class TuiTaskMessage(Message):
    def __init__(self, event: TaskEvent) -> None:
        super().__init__()
        self.event = event

class DeveloperWorkflowTuiApp(App[None]):
    CSS_PATH = "tui.tcss"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("?", "help", "Help"),
        Binding("n", "new_run", "New"),
        Binding("/", "search", "Search"),
        Binding("f", "filter", "Filter"),
    ]

    def __init__(self, controller: TuiController, max_concurrency: int) -> None:
        super().__init__()
        self.controller = controller
        self.supervisor = RunTaskSupervisor(
            max_concurrency,
            lambda event: self.post_message(TuiTaskMessage(event)),
        )

    def on_mount(self) -> None:
        self.push_screen(DashboardScreen(self.controller, self.supervisor))
```

`DashboardScreen` 组合 `NavigationPane`、`RunListPane`、`RunDetailPane`；详情固定使用 Overview、Repositories、Tests、Review、Publication、History 六个 Tab。`on_resize` 仅切换 `three/two/one` CSS class，不删除动作能力；单栏用独立详情 Screen 和返回 binding。

`SettingsScreen` 只展示并发上限、run/mirror/worktree root 的脱敏标签、provider 类型和 sandbox profile 是否已配置；不展示路径全文、环境变量、邮箱、token、密码或任何可编辑凭据控件。

- [ ] **Step 4: 运行无头布局与导航测试**

Run: `uv run pytest tests/test_developer_workflow_tui_app.py -k "responsive or keyboard or mouse or tabs" -q`

Expected: PASS，70/99/100 列边界和鼠标点击路径均有行为断言。

- [ ] **Step 5: 提交仪表盘**

```bash
git add src/developer_workflow/tui/app.py src/developer_workflow/tui/screens.py src/developer_workflow/tui/tui.tcss tests/test_developer_workflow_tui_app.py
git commit -m "feat(tui): add responsive workflow dashboard"
```

### Task 7: 缺陷、需求与仓库映射向导

**Files:**
- Modify: `src/developer_workflow/tui/screens.py`
- Modify: `tests/test_developer_workflow_tui_app.py`

- [ ] **Step 1: 写完整向导失败测试**

```python
@pytest.mark.asyncio
async def test_defect_wizard_uses_status_ids_and_confirms_group(app_factory) -> None:
    async with app_factory().run_test(size=(120, 32)) as pilot:
        await pilot.press("n")
        await pilot.click("#workflow-defect")
        await pilot.click("#status-todo")
        await pilot.click("#status-fixing")
        await pilot.click("#query-defects")
        assert pilot.app.controller.last_status_ids == ("status-todo-id", "status-fixing-id")
        await pilot.click("#candidate-0")
        await pilot.click("#mapping-group-app")
        await pilot.click("#confirm-start")
        assert pilot.app.controller.started_group == "app-group"


@pytest.mark.asyncio
async def test_candidate_query_has_no_run_or_worktree_side_effect(app_factory) -> None:
    async with app_factory().run_test() as pilot:
        await open_and_query_defect_wizard(pilot)
        assert pilot.app.controller.mutation_calls == []
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_tui_app.py -k "wizard or candidate_query" -q`

Expected: FAIL，向导 Screen 和按钮尚不存在。

- [ ] **Step 3: 实现固定步骤向导**

```python
class DefectWizardScreen(Screen[RunDetail | None]):
    STEP_FILTER = 0
    STEP_CANDIDATE = 1
    STEP_MAPPING = 2
    STEP_CONFIRM = 3

    async def action_query(self) -> None:
        self.session = await self.supervisor.run_readonly(
            self.controller.query_defects,
            self.project.value,
            self.iteration.value,
            self.assignee.value,
            tuple(self.status_ids),
        )

    async def action_start(self) -> None:
        detail = await self.supervisor.run_mutation(
            self.preview.run_id,
            "confirm-repository",
            self.controller.confirm_repository,
            self.preview.run_id,
            self.mapping_key,
        )
        self.dismiss(detail)
```

第一步只调用 `query_defects`；选择后才调用 `start_defect` 生成 VALIDATING run；最终确认才调用 `confirm_repository`。RequirementWizard 使用 requirement ID 开始并复用映射、摘要和确认步骤。所有 status 值来自 ONES ID，界面名称只用于标签。

- [ ] **Step 4: 验证向导与陈旧候选阻断**

Run: `uv run pytest tests/test_developer_workflow_tui_app.py -k "wizard or candidate or mapping" -q`

Expected: PASS；候选 snapshot 漂移、重复 UUID、无 mapping、取消向导和窄屏路径均不越过 Controller。

- [ ] **Step 5: 提交向导**

```bash
git add src/developer_workflow/tui/screens.py tests/test_developer_workflow_tui_app.py
git commit -m "feat(tui): add workflow creation wizards"
```

### Task 8: 恢复、修订、审批、取消与危险操作 Modal

**Files:**
- Modify: `src/developer_workflow/tui/screens.py`
- Modify: `tests/test_developer_workflow_tui_app.py`

- [ ] **Step 1: 写危险动作权威重载失败测试**

```python
@pytest.mark.asyncio
async def test_approval_modal_renders_signed_multi_repo_facts_and_reloads(app_factory) -> None:
    async with app_factory().run_test() as pilot:
        await select_waiting_group_run(pilot)
        await pilot.press("a")
        modal = pilot.app.screen
        assert modal.query_one("#fingerprint").renderable == SIGNED_FINGERPRINT
        assert modal.query(".tree-hash").results() == [PRIMARY_TREE, DEPENDENCY_TREE]
        pilot.app.controller.advance_authoritative_version()
        await pilot.click("#confirm-approve")
        assert "workflow changed; review again" in pilot.app.screen.query_one("#notice").renderable
        assert pilot.app.controller.approve_calls == []


@pytest.mark.asyncio
async def test_plain_enter_never_confirms_dangerous_action(app_factory) -> None:
    async with app_factory().run_test() as pilot:
        await open_cancel_modal(pilot)
        await pilot.press("enter")
        assert pilot.app.controller.cancel_calls == []
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_tui_app.py -k "approval_modal or dangerous or partial" -q`

Expected: FAIL，Modal 尚未实现完整事实与确认流程。

- [ ] **Step 3: 实现四类 Modal 和状态动作矩阵**

```python
class ApprovalModal(ModalScreen[ApprovalSubmission | None]):
    BINDINGS = [Binding("escape", "dismiss", "Back")]

    def compose(self) -> ComposeResult:
        yield Static(self.request.fingerprint, id="fingerprint")
        for repository in self.request.repositories:
            yield Label(repository.key)
            yield Static(repository.tree_hash, classes="tree-hash")
            yield Static(repository.test_summary)
            yield Static(repository.pr_target)
        yield Input(id="actor")
        yield Button("Approve", id="confirm-approve", variant="warning")

    @on(Button.Pressed, "#confirm-approve")
    def confirm(self) -> None:
        self.dismiss(ApprovalSubmission(self.request, self.query_one("#actor").value))
```

Resume 无副作用 Modal，但 PARTIAL_SUCCESS/PUBLISHING 恢复必须使用独立恢复发布 Modal。RevisionModal 要求 feedback 与 scope；CancelModal 要求 actor。确认按钮回到 App 后，必须通过 Supervisor 调用 Controller，Controller 再次加载 version；Modal 自己不调用 Orchestrator。

- [ ] **Step 4: 验证所有状态和键盘路径**

Run: `uv run pytest tests/test_developer_workflow_tui_app.py -k "approve or revise or cancel or resume or partial" -q`

Expected: PASS；审批前没有 commit/push/PR/comment，PARTIAL 恢复显示逐仓事实和错误。

- [ ] **Step 5: 提交危险动作界面**

```bash
git add src/developer_workflow/tui/screens.py tests/test_developer_workflow_tui_app.py
git commit -m "feat(tui): add approval and recovery actions"
```

### Task 9: 轮询刷新、任务事件、退出与重启恢复

**Files:**
- Modify: `src/developer_workflow/tui/app.py`
- Modify: `src/developer_workflow/tui/screens.py`
- Create: `tests/test_developer_workflow_tui_recovery.py`

- [ ] **Step 1: 写跨进程刷新与退出失败测试**

```python
@pytest.mark.asyncio
async def test_external_store_update_refreshes_dashboard(app_factory, store, run) -> None:
    async with app_factory(poll_interval=0.05).run_test() as pilot:
        updated = store.transition(
            run.run_id,
            WorkflowState.BLOCKED,
            expected_version=run.version,
            reason="blocked safely",
            resume_state=WorkflowState.IMPLEMENTING,
        )
        await pilot.pause(0.1)
        assert pilot.app.screen.query_one("#state").renderable == updated.state.value


@pytest.mark.asyncio
async def test_quit_does_not_cancel_running_workflow(app_factory) -> None:
    app = app_factory()
    async with app.run_test() as pilot:
        await start_resume_action(pilot)
        await pilot.press("q")
    assert app.controller.cancel_calls == []
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_tui_recovery.py -q`

Expected: FAIL，轮询与恢复协调尚未实现。

- [ ] **Step 3: 添加低频刷新和安全 TaskEvent 消息处理**

```python
class DeveloperWorkflowTuiApp(App[None]):
    def on_mount(self) -> None:
        self.set_interval(self.poll_interval, self.refresh_runs)
        self.push_screen(DashboardScreen(self.controller, self.supervisor))

    async def on_tui_task_message(self, message: TuiTaskMessage) -> None:
        self.activities[message.event.run_id] = message.event.activity
        await self.refresh_runs()

    async def refresh_runs(self) -> None:
        summaries = await asyncio.to_thread(self.controller.list_runs, self.filters)
        self.screen.update_runs(summaries)

    async def action_quit(self) -> None:
        self.exit()
```

刷新只读 FileRunStore。关闭 App 停止轮询和 UI 任务引用，但不把 worker 解释为 workflow cancel；重新启动后只从持久化 run 重建视图。

- [ ] **Step 4: 运行恢复测试**

Run: `uv run pytest tests/test_developer_workflow_tui_recovery.py -q`

Expected: PASS；损坏 run 固定错误、外部更新、版本漂移、退出和重启均有断言。

- [ ] **Step 5: 提交恢复与刷新**

```bash
git add src/developer_workflow/tui/app.py src/developer_workflow/tui/screens.py tests/test_developer_workflow_tui_recovery.py
git commit -m "feat(tui): refresh and recover persisted workflows"
```

### Task 10: 生产装配与真正的 `ones-dev tui`

**Files:**
- Modify: `src/developer_workflow/tui/__init__.py`
- Modify: `src/developer_workflow/cli.py`
- Modify: `src/developer_workflow/__init__.py`
- Modify: `tests/test_developer_workflow_cli.py`
- Create: `tests/test_developer_workflow_tui_integration.py`

- [ ] **Step 1: 写生产工厂和命令分发失败测试**

```python
def test_tui_command_reuses_production_orchestrator(
    config_file: Path
) -> None:
    seen: list[object] = []
    factory = lambda config: seen.append(config) or ORCHESTRATOR
    runner = lambda controller, max_concurrency: seen.append((controller, max_concurrency))
    assert cli.main(
        ["tui", "--config", str(config_file)],
        factory=factory,
        tui_runner=runner,
    ) == 0
    assert seen[-1][1] == 3


def test_tui_factory_fails_before_app_on_incomplete_runtime(monkeypatch, config_file) -> None:
    monkeypatch.delenv("ONES_EMAIL", raising=False)
    calls: list[object] = []
    code = cli.main(
        ["tui", "--config", str(config_file)],
        tui_runner=lambda controller, limit: calls.append(controller),
    )
    assert code == 1
    assert calls == []
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_cli.py tests/test_developer_workflow_tui_integration.py -k "tui_command or tui_factory" -q`

Expected: FAIL，`run_tui` 与 `tui_runner` 尚未装配。

- [ ] **Step 3: 实现惰性导入和生产服务图复用**

```python
# src/developer_workflow/tui/__init__.py
def run_tui(controller: TuiController, max_concurrency: int) -> None:
    DeveloperWorkflowTuiApp(controller, max_concurrency).run()
```

```python
# src/developer_workflow/cli.py
class TuiRunner(Protocol):
    def __call__(self, controller: object, max_concurrency: int) -> None: ...

def _execute_tui(
    config: DeveloperWorkflowConfig,
    factory: OrchestratorFactory,
    tui_runner: TuiRunner,
) -> int:
    from .tui import RunIndex, TuiController

    orchestrator = factory(config)
    controller = TuiController(orchestrator, RunIndex(orchestrator.store))
    tui_runner(controller, config.tui_max_concurrency)
    return 0

def main(
    argv: Sequence[str] | None = None,
    *,
    factory: OrchestratorFactory = build_production_orchestrator,
    defect_list_factory: DefectListFactory = build_production_defect_list_client,
    tui_runner: TuiRunner | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = _parser(stdout, stderr)
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except _ParserExit as error:
        return error.status
    try:
        if args.command == "defects":
            return _execute_defect_list(
                args, defect_list_factory, stdout=stdout, stderr=stderr
            )
        config = DeveloperWorkflowConfig.load(args.config)
        if args.command == "tui":
            if tui_runner is None:
                from .tui import run_tui
                tui_runner = run_tui
            return _execute_tui(config, factory, tui_runner)
        orchestrator = factory(config)
        return _execute(
            args, orchestrator, stdin=stdin, stdout=stdout, stderr=stderr
        )
    except (KeyboardInterrupt, SystemExit):
        _write(stderr, "error: command interrupted safely\n")
        return 130
    except Exception:
        _write(stderr, "error: command failed safely\n")
        return 1
```

CLI 必须先执行现有生产配置、私有目录、邮箱密码登录、评论 endpoint、PR provider、Git identity、Codex auth 和 sandbox profile 校验；不得为 TUI 建立弱化工厂。非 TUI 命令保持原分发和输出。

- [ ] **Step 4: 运行 CLI、集成和打包入口测试**

Run: `uv run pytest tests/test_developer_workflow_cli.py tests/test_developer_workflow_tui_integration.py -q`

Expected: PASS。

Run: `uv run ones-dev tui --help`

Expected: exit 0，帮助中显示 `--config` 且不访问 ONES、Git、Codex 或网络。

- [ ] **Step 5: 提交生产装配**

```bash
git add src/developer_workflow/tui src/developer_workflow/cli.py src/developer_workflow/__init__.py tests/test_developer_workflow_cli.py tests/test_developer_workflow_tui_integration.py
git commit -m "feat(tui): wire production terminal console"
```

### Task 11: 安全矩阵、无副作用 E2E 与文档

**Files:**
- Create: `tests/test_developer_workflow_tui_security.py`
- Modify: `tests/test_developer_workflow_tui_integration.py`
- Modify: `docs/ones_dev_cli.md`

- [ ] **Step 1: 写安全与完整生命周期失败测试**

```python
@pytest.mark.asyncio
async def test_sensitive_parent_environment_never_appears_in_widgets(
    monkeypatch, app_factory
) -> None:
    monkeypatch.setenv("CODEX_API_KEY", "TUI-SECRET-VALUE")
    async with app_factory().run_test() as pilot:
        await pilot.press("?", "escape", "n")
        rendered = "\n".join(str(widget.render()) for widget in pilot.app.query("*"))
        assert "TUI-SECRET-VALUE" not in rendered


@pytest.mark.asyncio
async def test_approval_is_first_remote_side_effect(group_e2e_app) -> None:
    async with group_e2e_app.run_test() as pilot:
        await drive_group_run_to_waiting_approval(pilot)
        assert group_e2e_app.effects == []
        await approve_group_run(pilot, actor="operator")
        assert group_e2e_app.effects == [
            "commit:primary", "push:primary", "pr:primary",
            "commit:dependency", "push:dependency", "pr:dependency", "comment",
        ]
```

- [ ] **Step 2: 运行测试并确认 RED 或已有行为基线**

Run: `uv run pytest tests/test_developer_workflow_tui_security.py tests/test_developer_workflow_tui_integration.py -q`

Expected: 新测试先因缺少安全 fixture 或未覆盖的 UI 文本路径 FAIL；不接受“测试一开始即通过”而未证明 widget 全量扫描和副作用顺序。

- [ ] **Step 3: 修正发现的最小展示或装配缺口并补文档**

`docs/ones_dev_cli.md` 必须加入以下可直接运行的内容：

````markdown
## 全屏终端界面

```powershell
uv run ones-dev tui --config docs/examples/ones-dev.config.json
```

- `n` 新建需求或缺陷工作流；缺陷状态按 ONES 状态 ID 过滤。
- `r` 恢复，`v` 修订，`a` 审批，`x` 取消，`q` 仅退出界面。
- 不同 run 最多并行 `tui_max_concurrency` 个；同一 run 始终串行。
- 退出不会取消工作流；再次启动后从私有 run root 恢复。
- 本地 source workspace 只读，修改只发生在 managed worktree。
````

如安全测试发现 widget 泄漏，只允许在 `models.py` 白名单转换边界修复，禁止在测试中屏蔽敏感字符串。

- [ ] **Step 4: 运行 TUI 完整测试集**

Run: `uv run pytest tests/test_developer_workflow_tui_models.py tests/test_developer_workflow_tui_run_index.py tests/test_developer_workflow_tui_controller.py tests/test_developer_workflow_tui_supervisor.py tests/test_developer_workflow_tui_app.py tests/test_developer_workflow_tui_recovery.py tests/test_developer_workflow_tui_integration.py tests/test_developer_workflow_tui_security.py -q`

Expected: PASS；无未处理 coroutine、线程、worker 或临时目录残留。

- [ ] **Step 5: 提交安全矩阵与文档**

```bash
git add tests/test_developer_workflow_tui_security.py tests/test_developer_workflow_tui_integration.py docs/ones_dev_cli.md src/developer_workflow/tui
git commit -m "test(tui): verify safe interactive lifecycle"
```

### Task 12: 全量回归、发行物验证与最终审查

**Files:**
- Verify only unless a failing test identifies an in-scope defect

- [ ] **Step 1: 运行开发工作流快速回归**

Run: `uv run pytest tests/test_developer_workflow_config.py tests/test_developer_workflow_contracts.py tests/test_developer_workflow_state_store.py tests/test_developer_workflow_orchestrator.py tests/test_developer_workflow_requirement.py tests/test_developer_workflow_defect.py tests/test_developer_workflow_approval.py tests/test_developer_workflow_publisher.py tests/test_developer_workflow_multi_publisher.py tests/test_developer_workflow_cli.py tests/test_developer_workflow_tui_*.py -q`

Expected: PASS；平台或真实 managed profile 条件 skip 必须逐项说明。

- [ ] **Step 2: 运行 repository、ONES 与安全相邻回归**

Run: `uv run pytest tests/test_developer_workflow_repository.py tests/test_ones.py tests/test_ones_gateway.py tests/test_developer_workflow_security.py -q`

Expected: PASS；LAN smoke 不在未显式授权时运行。

- [ ] **Step 3: 验证锁文件、编译、空白和命令入口**

Run: `uv lock --check && uv run python -m compileall -q src/developer_workflow tests && git diff --check && uv run ones-dev tui --help`

Expected: 全部 exit 0；help 列出 TUI 入口且无外部调用。

- [ ] **Step 4: 验证 wheel/sdist 包含 TUI 与 schema**

Run: `uv build --offline`

Expected: wheel 与 sdist 构建成功，archive 包含 `src/developer_workflow/tui/`、`schemas/workflow-result.schema.json`、`server.py` 和 `main.py`，不包含 `.env`、`data/`、`.agents/` 或测试临时目录。

- [ ] **Step 5: 请求规范与质量双评审**

评审必须核对：TUI 是否仅为适配层、候选 snapshot 是否由 Controller 私有持有、危险动作是否重载 version、同 run 是否串行、跨 run 是否有界并发、多仓签名 tree/facts 是否完整显示、退出是否零状态写、所有 widget 是否不含 secret。

- [ ] **Step 6: 提交最终验证修正**

仅当上述验证发现本计划范围内缺陷时提交修正：

```bash
git add src/developer_workflow/tui/models.py src/developer_workflow/tui/app.py tests/test_developer_workflow_tui_security.py
git commit -m "fix(tui): close terminal console review gaps"
```

若没有修正，不创建空提交；记录最终测试计数与已解释的 skip 后结束实现。
