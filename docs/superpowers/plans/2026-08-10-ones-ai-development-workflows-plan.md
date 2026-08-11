# ONES AI 需求开发与缺陷修复工作流 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个独立本地 `ones-dev` CLI，复用现有 ONES 认证与数据边界，安全完成需求开发和单缺陷修复，并在人工批准后幂等地创建 PR、评论 ONES。

**Architecture:** 新增 `src/developer_workflow/`，用文件状态库驱动显式状态机；需求流和缺陷流共享 ONES 读取、仓库 worktree、Codex、审批与发布组件。Codex 只能修改隔离 worktree，远端 Git 和 ONES 写操作集中在审批后的 `Publisher`，当前 FastAPI、前端、`Engine`、`Scheduler`、`ScheduleManager` 均不参与。

**Tech Stack:** Python 3.11、Pydantic 2、httpx/requests、GitPython 与 Git CLI、Codex CLI、pytest、respx、标准库 `argparse`/JSON/文件原子替换。

---

## 文件结构

新增文件及单一职责：

- `src/developer_workflow/contracts.py`：状态、快照、审批包和发布结果契约。
- `src/developer_workflow/config.py`：读取无密钥 JSON 配置并解析项目/迭代仓库映射。
- `src/developer_workflow/state_store.py`：每个 run 一个 JSON 文件的原子持久化和状态转换校验。
- `src/developer_workflow/repository.py`：镜像仓库、隔离 worktree、分支、diff 和安全指纹。
- `src/developer_workflow/codex_runner.py`：非交互 Codex 子进程、Schema 校验和 Git 边界检查。
- `src/developer_workflow/approval.py`：审批包、审批指纹、失效检查。
- `src/developer_workflow/requirement_flow.py`：需求/Wiki 校验和执行阶段。
- `src/developer_workflow/defect_flow.py`：未关闭缺陷单选、证据门禁和修复阶段。
- `src/developer_workflow/publisher.py`：审批后 commit、push、PR 的唯一入口和幂等恢复。
- `src/developer_workflow/ones_comment.py`：PR 成功后的 ONES 评论与重复检查。
- `src/developer_workflow/orchestrator.py`：创建、继续、修改、批准、取消的状态编排。
- `src/developer_workflow/cli.py`：`ones-dev` 命令和终端交互。
- `src/developer_workflow/schemas/workflow-result.schema.json`：Codex 结构化输出契约。

现有文件仅作边界补强：

- `src/integrations/ones.py`、`src/integrations/ones_api.py`：Wiki 只读请求、评论查询、可证明完整的缺陷分页。
- `src/services/ones_gateway.py`：需求/Wiki 规范化、未关闭状态解析和稳定错误映射。
- `src/services/__init__.py`：保持 Gateway 公共导出。
- `pyproject.toml`：注册 `ones-dev`。
- `tests/`：所有单元、契约、集成、安全和 LAN smoke 测试。

## Task 1: 完成缺陷列表分页与未关闭状态门禁

**Files:**
- Modify: `src/integrations/ones.py`
- Modify: `src/integrations/ones_api.py`
- Modify: `src/services/ones_gateway.py`
- Test: `tests/test_ones.py`
- Test: `tests/test_phase2.py`
- Test: `tests/test_ones_gateway.py`
- Test: `tests/test_ones_lan_smoke.py`

- [ ] **Step 1: 为同步和异步客户端编写两页结果、重复项去重和截断拒绝测试**

测试构造第一页 `hasNextPage=true/endCursor="c1"`、第二页 `hasNextPage=false`，断言请求二带 `after="c1"`；构造 `hasNextPage=true/endCursor=""` 时断言抛出 `OnesPaginationError`，不能静默返回不完整列表。

```python
def test_fetch_defects_follows_cursor_and_deduplicates(client, mocker):
    call = mocker.patch.object(client, "_graphql", side_effect=[
        {"buckets": [{"tasks": [{"uuid": "a"}], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}]},
        {"buckets": [{"tasks": [{"uuid": "a"}, {"uuid": "b"}], "pageInfo": {"hasNextPage": False, "endCursor": ""}}]},
    ])
    assert [item["uuid"] for item in client.fetch_defects(limit=10, page_size=2)] == ["a", "b"]
    assert call.call_args_list[1].args[1]["pagination"]["after"] == "c1"

def test_fetch_defects_rejects_unpageable_response(client, mocker):
    mocker.patch.object(client, "_graphql", return_value={
        "buckets": [{"tasks": [{"uuid": "a"}], "pageInfo": {"hasNextPage": True, "endCursor": ""}}],
    })
    with pytest.raises(OnesPaginationError, match="cursor"):
        client.fetch_defects(limit=10, page_size=1)
```

- [ ] **Step 2: 运行测试确认当前实现失败**

Run: `uv run pytest tests/test_ones.py tests/test_phase2.py -k "cursor or unpageable" -v`

Expected: FAIL，原因是客户端没有 `page_size`/游标循环或 `OnesPaginationError`。

- [ ] **Step 3: 在两个客户端实现相同的严格游标循环**

新增公开异常 `OnesPaginationError(RuntimeError)`；每页使用 `pagination={"limit": page_size, "after": cursor, "preciseCount": True}`。每个 bucket 的 `pageInfo` 都必须可判定；只要任一 bucket 声明还有下一页却没有新 cursor，立即失败。按 `uuid` 去重，在达到调用方 `limit` 或所有 bucket 完成后返回。

为避免“按状态分 bucket 后每个 bucket 拥有不同 cursor”的歧义，分页方法改用 `groupBy={"tasks": {}}` 的单 bucket 查询；任务自身仍返回完整 `status { uuid name category }`，因此不丢失状态字段。若局域网契约测试证明该部署不接受单 bucket 查询，本 Task 不得降级为截断数组，必须保持失败门禁并基于实测响应调整游标协议。

```python
class OnesPaginationError(RuntimeError):
    pass

def _next_cursor(buckets: list[dict]) -> str | None:
    pending = [bucket.get("pageInfo", {}) for bucket in buckets if bucket.get("pageInfo", {}).get("hasNextPage")]
    if not pending:
        return None
    cursors = {str(info.get("endCursor") or "").strip() for info in pending}
    cursors.discard("")
    if len(cursors) != 1:
        raise OnesPaginationError("ONES defect response cannot provide one stable continuation cursor")
    return cursors.pop()
```

- [ ] **Step 4: 让 Gateway 传递 `page_size` 并提供未关闭状态集合**

`list_open_defects(project_id, issue_type_id, sprint_id, assignee)` 先调用 `list_defect_statuses`，排除 `category` 为 `done`、`completed`、`cancelled`、`discarded` 的定义，再把剩余 status UUID 传给 `list_normalized_defects`。若状态定义为空或分类未知，抛出 `OnesGatewayPayloadError`，禁止猜测“未关闭”。

```python
_OPEN_STATUS_CATEGORIES = {"open", "todo", "to_do", "doing", "in_progress", "pending"}
_CLOSED_STATUS_CATEGORIES = {"done", "completed", "cancelled", "discarded", "closed"}

async def list_open_defects(self, *, project_id: str, issue_type_id: str, sprint_id: str, assignee: str) -> list[DefectRecord]:
    statuses = await self.list_defect_statuses(project_id, issue_type_id)
    if not statuses or any(not status.category.strip() for status in statuses):
        raise OnesGatewayPayloadError("Cannot prove ONES open-status set from workflow definitions")
    categories = {status.category.lower() for status in statuses}
    unknown = categories - _OPEN_STATUS_CATEGORIES - _CLOSED_STATUS_CATEGORIES
    if unknown:
        raise OnesGatewayPayloadError(f"Unknown ONES workflow status categories: {sorted(unknown)}")
    open_ids = [status.id for status in statuses if status.category.lower() not in _CLOSED_STATUS_CATEGORIES]
    return await self.list_normalized_defects(
        project_id=project_id,
        issue_type_id=issue_type_id,
        sprint_id=sprint_id,
        assignee=assignee,
        status_ids=open_ids,
        limit=5000,
        page_size=200,
    )
```

- [ ] **Step 5: 添加显式启用的 LAN 完整性 smoke test**

使用环境变量 `ONES_LAN_PROJECT_ID`、`ONES_LAN_ITERATION_ID`、`ONES_LAN_ASSIGNEE_ID`、`ONES_LAN_ISSUE_TYPE_ID`，断言每条缺陷都匹配项目、迭代、负责人和未关闭状态；若未提供变量则 skip，不写 ONES。

- [ ] **Step 6: 运行 ONES 回归并提交**

Run: `uv run pytest tests/test_ones.py tests/test_phase2.py tests/test_ones_gateway.py -v`

Expected: PASS。

```bash
git add src/integrations/ones.py src/integrations/ones_api.py src/services/ones_gateway.py tests/test_ones.py tests/test_phase2.py tests/test_ones_gateway.py tests/test_ones_lan_smoke.py
git commit -m "fix: guarantee complete ONES defect filtering"
```

## Task 2: 增加 ONES Wiki 只读能力和需求快照

**Files:**
- Modify: `src/integrations/ones.py`
- Modify: `src/integrations/ones_api.py`
- Modify: `src/services/ones_gateway.py`
- Test: `tests/test_ones.py`
- Test: `tests/test_phase2.py`
- Test: `tests/test_ones_gateway.py`
- Test: `tests/test_ones_lan_smoke.py`

- [ ] **Step 1: 编写三个 Wiki 接口的同步/异步契约测试**

断言路径分别为 `/wiki/api/wiki/team/{team}/space/{space}/page/{page}`、`/wiki/api/wiki/team/{team}/page/{page}/detail`、`/wiki/api/wiki/team/{team}/space/{space}/pages_with_history`，并复用已登录 session/client。

- [ ] **Step 2: 运行契约测试确认失败**

Run: `uv run pytest tests/test_ones.py tests/test_phase2.py -k wiki -v`

Expected: FAIL，三个方法尚不存在。

- [ ] **Step 3: 实现同步和异步 Wiki GET 方法**

两个客户端公开相同名称；请求必须 `raise_for_status()`，返回值必须为 JSON object，否则抛出 payload 错误。日志只记录 team/space/page ID 和状态码，不记录 Cookie、Authorization、邮箱或密码。

```python
def fetch_wiki_page(self, space_id: str, page_id: str) -> dict:
    response = self.session.get(f"{self.base_url}/wiki/api/wiki/team/{self.team_id}/space/{space_id}/page/{page_id}")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("ONES Wiki page payload must be an object")
    return payload
```

- [ ] **Step 4: 在 Gateway 中规范化 Wiki URL、正文和版本快照**

新增 `parse_wiki_url(url) -> tuple[team_id, space_id, page_id]`，仅接受当前 `ONES_BASE_URL` 主机和上述页面路径；正文按稳定 JSON 序列化或文本字段归一化，`content_sha256=sha256(normalized_content.encode()).hexdigest()`。需求详情必须保留项目、迭代、状态、描述和所有 Wiki 引用。

```python
@dataclass(frozen=True, slots=True)
class WikiPageSnapshot:
    team_id: str
    space_id: str
    page_id: str
    title: str
    normalized_content: str
    version: str
    updated_at: str
    source_url: str
    content_sha256: str
```

- [ ] **Step 5: 覆盖 401/403/404/429/5xx/timeout 映射和凭据脱敏**

401/403 映射为 `OnesGatewayAuthError`，404 映射为 `OnesGatewayNotFoundError`，429/5xx/timeout 经现有 retry 策略最多三次后映射为稳定 Gateway 错误；`caplog`/structlog 捕获中不得出现测试 token 和密码。

- [ ] **Step 6: 运行回归并提交**

Run: `uv run pytest tests/test_ones.py tests/test_phase2.py tests/test_ones_gateway.py -v`

Expected: PASS。

```bash
git add src/integrations/ones.py src/integrations/ones_api.py src/services/ones_gateway.py tests/test_ones.py tests/test_phase2.py tests/test_ones_gateway.py tests/test_ones_lan_smoke.py
git commit -m "feat: add read-only ONES Wiki snapshots"
```

## Task 3: 定义独立工作流契约和无密钥配置

**Files:**
- Create: `src/developer_workflow/__init__.py`
- Create: `src/developer_workflow/contracts.py`
- Create: `src/developer_workflow/config.py`
- Test: `tests/test_developer_workflow_config.py`
- Test: `tests/test_developer_workflow_contracts.py`

- [ ] **Step 1: 编写状态、映射优先级和明文密钥拒绝测试**

精确优先级为 `(project, iteration)` 高于 `(project, "*")`；配置包含键名 `password`、`token`、`secret`、`pat` 时加载失败。

- [ ] **Step 2: 运行测试确认模块不存在**

Run: `uv run pytest tests/test_developer_workflow_config.py tests/test_developer_workflow_contracts.py -v`

Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 定义枚举和不可变输入契约**

```python
class WorkflowState(str, Enum):
    CREATED = "CREATED"
    READING_ONES = "READING_ONES"
    VALIDATING = "VALIDATING"
    PREPARING_REPO = "PREPARING_REPO"
    IMPLEMENTING = "IMPLEMENTING"
    TESTING = "TESTING"
    AI_REVIEW = "AI_REVIEW"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"

class RepositoryMapping(BaseModel):
    project_id: str
    iteration_id: str
    repo_url: str
    repo_name: str
    base_branch: str = "main"
    test_commands: tuple[str, ...] = ()
    lint_commands: tuple[str, ...] = ()
    build_commands: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ()
```

同时定义 `RequirementRecord`、`WikiPageSnapshot`、`CommandResult`、`CodexResult`、`ApprovalPackage`、`PublicationResult`、`StateEvent`、`WorkflowRun`；`WorkflowRun` 包含设计中的来源快照、repo、基线、分支、修改、测试、review、审批、发布、错误和重试字段。

`WorkflowRun.new(workflow_type, work_item_id)` 和 `new_defect(project_id, iteration_id, assignee, candidate_id)` 使用 `uuid.uuid4().hex` 生成 run ID，并初始化 `CREATED/version=0/history=[]`；`for_revision(feedback)` 清除批准人、批准时间和旧指纹，把反馈追加到 revision history 并设置恢复状态为 `IMPLEMENTING`；`with_approval()` 只写审批人和 UTC 时间，不执行发布。后续任务只能调用这些模型方法，不在 Orchestrator 里手工拼不完整字典。

- [ ] **Step 4: 实现 JSON 配置加载器**

配置根字段固定为 `run_root`、`worktree_root`、`mirror_root`、`max_codex_attempts`、`repositories`、`publishing`；路径相对配置文件解析。递归扫描配置 key，发现密钥字段即失败；实际凭据只从现有环境/`GitSettings` 读取。

- [ ] **Step 5: 提供可运行的示例配置并验证**

新增 `docs/examples/ones-dev.config.json`，只使用假仓库 URL，不含任何凭据；测试加载后能解析精确映射与项目默认映射。

- [ ] **Step 6: 提交**

```bash
git add src/developer_workflow docs/examples/ones-dev.config.json tests/test_developer_workflow_config.py tests/test_developer_workflow_contracts.py
git commit -m "feat: define developer workflow contracts and config"
```

## Task 4: 实现原子状态库和合法恢复点

**Files:**
- Create: `src/developer_workflow/state_store.py`
- Test: `tests/test_developer_workflow_state_store.py`

- [ ] **Step 1: 编写正常转换、非法跳转、并发版本和原子写测试**

合法主链严格遵循设计；`BLOCKED` 只能恢复到记录的 `resume_state`，`PARTIAL_SUCCESS` 只能进入 `PUBLISHING`，终态不能再改变。保存时用 `version` 比较，旧版本写入抛出 `ConcurrentRunUpdateError`。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_developer_workflow_state_store.py -v`

Expected: FAIL，状态库不存在。

- [ ] **Step 3: 实现 `FileRunStore`**

每次写入 `<run_root>/<run_id>/run.json.tmp`，执行 flush/fsync 后 `os.replace` 到 `run.json`；事件追加到模型中的 `history`，写入 UTC ISO 时间。`transition(run_id, expected_version, target, reason)` 在同一原子保存中增加版本。

```python
class FileRunStore:
    def create(self, run: WorkflowRun) -> WorkflowRun:
        path = self._path(run.run_id)
        if path.exists():
            raise RunAlreadyExistsError(run.run_id)
        created = run.model_copy(update={"version": 1})
        self._write(path, created)
        return created

    def load(self, run_id: str) -> WorkflowRun:
        path = self._path(run_id)
        if not path.exists():
            raise RunNotFoundError(run_id)
        return WorkflowRun.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, run: WorkflowRun, expected_version: int) -> WorkflowRun:
        current = self.load(run.run_id)
        if current.version != expected_version:
            raise ConcurrentRunUpdateError(f"expected {expected_version}, found {current.version}")
        saved = run.model_copy(update={"version": expected_version + 1})
        self._write(self._path(run.run_id), saved)
        return saved

    def transition(self, run_id: str, expected_version: int, target: WorkflowState, reason: str) -> WorkflowRun:
        run = self.load(run_id)
        if run.version != expected_version:
            raise ConcurrentRunUpdateError(f"expected {expected_version}, found {run.version}")
        assert_transition_allowed(run, target)
        event = StateEvent(source=run.state, target=target, reason=reason, occurred_at=utc_now())
        changed = run.model_copy(update={"state": target, "history": [*run.history, event]})
        return self.save(changed, expected_version)
```

同一步实现 `_path()` 的 run ID 字符白名单和根目录约束、`_write()` 的临时文件/fsync/`os.replace`，以及 `assert_transition_allowed()` 的转换表；这里的四个签名是后续任务必须保持的公共契约。

- [ ] **Step 4: 运行测试并提交**

Run: `uv run pytest tests/test_developer_workflow_state_store.py -v`

Expected: PASS。

```bash
git add src/developer_workflow/state_store.py tests/test_developer_workflow_state_store.py
git commit -m "feat: persist developer workflow state atomically"
```

## Task 5: 建立镜像仓库和隔离 worktree

**Files:**
- Create: `src/developer_workflow/repository.py`
- Test: `tests/test_developer_workflow_repository.py`

- [ ] **Step 1: 用临时 bare remote 编写 worktree 生命周期测试**

测试镜像 fetch、从 `origin/<base>` 创建 `requirement/<id>-<slug>` 或 `bugfix/<id>-<slug>`、不触碰调用者当前仓库、路径越界拒绝、基线 commit 固化、diff/hash/HEAD 检查。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_developer_workflow_repository.py -v`

Expected: FAIL，`WorktreeRepository` 不存在。

- [ ] **Step 3: 实现镜像和 worktree 创建**

仓库缓存位于配置的 `mirror_root/<repo_name>.git`；使用参数数组调用 Git，不拼 shell。创建前验证 worktree 目标经 `resolve()` 后位于 `worktree_root`，执行 `git worktree add -b <branch> <path> origin/<base>`。

```python
@dataclass(slots=True)
class WorktreeRepository:
    mirror_root: Path
    worktree_root: Path

    def prepare(self, run_id: str, mapping: RepositoryMapping, branch: str) -> PreparedWorktree:
        mirror = self._ensure_mirror(mapping)
        self._run(["git", "--git-dir", str(mirror), "fetch", "--prune", "origin"])
        base_ref = f"origin/{mapping.base_branch}"
        base_commit = self._output(["git", "--git-dir", str(mirror), "rev-parse", base_ref])
        target = self._safe_target(run_id)
        self._run(["git", "--git-dir", str(mirror), "worktree", "add", "-b", branch, str(target), base_ref])
        return PreparedWorktree(path=target, branch=branch, base_commit=base_commit, head_commit=base_commit)
```

- [ ] **Step 4: 实现安全快照**

`snapshot()` 返回当前 HEAD、`git diff --binary <base>` 的 SHA-256、修改文件列表和工作区外写入检查结果；`allowed_paths` 非空时，每个修改路径必须落在允许前缀内，否则抛出 `RepositoryBoundaryError`。

- [ ] **Step 5: 运行测试并提交**

Run: `uv run pytest tests/test_developer_workflow_repository.py -v`

Expected: PASS。

```bash
git add src/developer_workflow/repository.py tests/test_developer_workflow_repository.py
git commit -m "feat: isolate developer runs in git worktrees"
```

## Task 6: 实现 Codex 非交互适配器和结构化输出

**Files:**
- Create: `src/developer_workflow/codex_runner.py`
- Create: `src/developer_workflow/schemas/workflow-result.schema.json`
- Modify: `pyproject.toml`
- Test: `tests/test_developer_workflow_codex_runner.py`

- [ ] **Step 1: 编写命令、Schema、超时和 HEAD 变化测试**

Fake subprocess 断言命令包含 `codex exec --cd <worktree> --sandbox workspace-write --output-schema <schema>`；输出缺字段、非 JSON、退出非零、超时或前后 HEAD 不同都不得返回成功。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_developer_workflow_codex_runner.py -v`

Expected: FAIL，适配器和 Schema 不存在。

- [ ] **Step 3: 写入严格 JSON Schema**

根对象 `additionalProperties=false`，必需字段为 `summary`、`changed_files`、`commands`、`evidence`、`review_findings`、`risks`、`unresolved_items`；每条 command 必须有 `command`、`exit_code`、`summary`。Schema 不接受 Codex 自报的 commit/push/PR 结果字段。

- [ ] **Step 4: 实现 `CodexRunner.run`**

将结构化 prompt 写入 run 目录，不含 Git/ONES 凭据；环境显式删除 `GIT_ASKPASS`、`GITHUB_TOKEN`、`GH_TOKEN`、`GITLAB_TOKEN` 和所有 `ONES_*` 凭据，只保留 Codex 自身认证环境。运行前后读取 HEAD；结果用 `jsonschema` 校验，因此把 `jsonschema>=4.0` 加入 `pyproject.toml` dependencies。

```python
command = [
    "codex", "exec", "--cd", str(worktree),
    "--sandbox", "workspace-write",
    "--output-schema", str(schema_path),
    prompt,
]
completed = subprocess.run(command, cwd=worktree, env=safe_env, capture_output=True, text=True, timeout=timeout_seconds)
```

- [ ] **Step 5: 运行测试并提交**

Run: `uv run pytest tests/test_developer_workflow_codex_runner.py -v`

Expected: PASS。

```bash
git add pyproject.toml src/developer_workflow/codex_runner.py src/developer_workflow/schemas/workflow-result.schema.json tests/test_developer_workflow_codex_runner.py
git commit -m "feat: run Codex with structured safety boundaries"
```

## Task 7: 生成审批包并自动判定审批失效

**Files:**
- Create: `src/developer_workflow/approval.py`
- Test: `tests/test_developer_workflow_approval.py`

- [ ] **Step 1: 编写稳定指纹和每种失效条件测试**

分别改变 ONES 内容、Wiki hash、base commit、HEAD、diff、测试命令/退出码、风险、未解决事项和 PR 内容，断言旧审批失效；字典顺序变化不得改变指纹。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_developer_workflow_approval.py -v`

Expected: FAIL，审批模块不存在。

- [ ] **Step 3: 实现 canonical JSON 指纹**

```python
def approval_fingerprint(package: ApprovalPackage) -> str:
    payload = package.model_dump(mode="json", exclude={"fingerprint", "approved_by", "approved_at"})
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def verify_approval(expected: str, current: ApprovalPackage) -> None:
    actual = approval_fingerprint(current)
    if not hmac.compare_digest(expected, actual):
        raise ApprovalInvalidatedError("Source, repository, tests, risks, or publication content changed")
```

- [ ] **Step 4: 运行测试并提交**

Run: `uv run pytest tests/test_developer_workflow_approval.py -v`

Expected: PASS。

```bash
git add src/developer_workflow/approval.py tests/test_developer_workflow_approval.py
git commit -m "feat: fingerprint developer workflow approvals"
```

## Task 8: 实现需求读取、完整性校验和 Codex 阶段

**Files:**
- Create: `src/developer_workflow/requirement_flow.py`
- Test: `tests/test_developer_workflow_requirement.py`

- [ ] **Step 1: 编写缺 Wiki、无权限、无验收标准和成功路径测试**

失败路径必须在调用 repository/Codex 前进入 `BLOCKED`；成功路径按 `READING_ONES → VALIDATING → PREPARING_REPO → IMPLEMENTING → TESTING → AI_REVIEW → WAITING_APPROVAL` 写状态。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_developer_workflow_requirement.py -v`

Expected: FAIL，需求流不存在。

- [ ] **Step 3: 实现需求来源快照和确定性门禁**

需求必须存在项目、迭代、标题、至少一个可读 Wiki；规范化内容必须能提取至少一条验收标准（标题为“验收标准/Acceptance Criteria”后的非空列表）。内容冲突由 Codex 结构化预检返回 `unresolved_items`，非空则阻塞且不创建 worktree。

仓库映射尚未写入 run 时，流程只保存校验后的来源快照和候选映射并停在 `VALIDATING`；只有 Task 11 的 `confirm_repository()` 将人工确认的映射写入 run 后，才允许进入 `PREPARING_REPO`。这保证 CLI 能展示、确认或改选仓库和基线，而不是在后台自动猜测。

- [ ] **Step 4: 实现三次 Codex 阶段调用**

实现阶段要求“验收标准→文件/测试”映射并修改；测试阶段只能运行映射配置的命令；review 阶段检查覆盖、异常路径、回归、安全和无关改动。每次调用后读取真实 diff 和命令退出码；失败修复循环不超过 `max_codex_attempts`。

- [ ] **Step 5: 生成审批包并保存安全检查点**

审批包包含设计第 16 节全部字段；只有真实测试 exit code 全为 0、HEAD 未变化、allowed paths 合法且无 unresolved items 才能进入 `WAITING_APPROVAL`。

- [ ] **Step 6: 运行测试并提交**

Run: `uv run pytest tests/test_developer_workflow_requirement.py -v`

Expected: PASS。

```bash
git add src/developer_workflow/requirement_flow.py tests/test_developer_workflow_requirement.py
git commit -m "feat: implement ONES requirement development flow"
```

## Task 9: 实现缺陷单选、证据门禁和 TDD 修复

**Files:**
- Create: `src/developer_workflow/defect_flow.py`
- Test: `tests/test_developer_workflow_defect.py`
- Reference: `docs/analysis_acceptance_rules.md`

- [ ] **Step 1: 编写过滤完整性、单选和证据不足测试**

Fake Gateway 返回未关闭缺陷；断言项目/迭代/账号传参完整、只能选择返回列表中的一个 UUID、一次 run 仅记录一个 work item。根因缺少文件位置加代码/调用链/复现测试之一时，在 worktree 修改前阻塞。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_developer_workflow_defect.py -v`

Expected: FAIL，缺陷流不存在。

- [ ] **Step 3: 实现列表摘要和人工选择接口**

`list_candidates()` 只调用 Task 1 的 `list_open_defects`，返回 UUID、编号、标题、优先级、状态和更新时间；`select(candidate_id)` 精确匹配 UUID/key，否则失败，不采用模糊选择。

- [ ] **Step 4: 实现证据门禁**

根因结果必须包含 `file_path`、非空 `location`、`mechanism`，以及 `code_excerpt`、`call_chain`、`reproduction_test` 中至少一项；证据引用必须能在当前 base commit 工作树验证。门禁失败只保存调查建议，不调用修改阶段。

- [ ] **Step 5: 实现失败测试优先的最小修复阶段**

Codex 第一阶段增加或运行复现测试并记录修复前失败 exit code，第二阶段做最小修改，第三阶段运行复现和配置回归测试，第四阶段 review 根因、改动和测试一致性。审批包额外记录修复前后行为、影响范围和风险等级。

- [ ] **Step 6: 运行测试并提交**

Run: `uv run pytest tests/test_developer_workflow_defect.py -v`

Expected: PASS。

```bash
git add src/developer_workflow/defect_flow.py tests/test_developer_workflow_defect.py
git commit -m "feat: implement evidence-gated defect repair flow"
```

## Task 10: 实现审批后的幂等发布与 ONES 评论

**Files:**
- Create: `src/developer_workflow/publisher.py`
- Create: `src/developer_workflow/ones_comment.py`
- Modify: `src/integrations/ones.py`
- Modify: `src/integrations/ones_api.py`
- Modify: `src/services/ones_gateway.py`
- Test: `tests/test_developer_workflow_publisher.py`
- Test: `tests/test_developer_workflow_ones_comment.py`

- [ ] **Step 1: 编写“未审批零副作用”和发布恢复测试**

覆盖：未审批不 commit；commit 后 push 失败重试不重复 commit；已有远端分支不重复 push 内容；已有 PR 复用 URL；PR 失败不评论；PR 成功评论失败进入 `PARTIAL_SUCCESS` 且重试只评论。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_developer_workflow_publisher.py tests/test_developer_workflow_ones_comment.py -v`

Expected: FAIL，发布组件不存在。

- [ ] **Step 3: 为评论增加读取和稳定标识**

同步/异步客户端及 Gateway 增加 `list_comments(item_id)` 和 `add_comment(item_id, text)`；评论结尾包含 `<!-- ones-dev-run:{run_id} -->`。添加前读取评论并查稳定标识，存在则返回既有评论而不重复 POST；绝不调用 `update_status`。

- [ ] **Step 4: 实现 Publisher 的分阶段幂等记录**

进入 `PUBLISHING` 前重新读取 ONES/Wiki、base、HEAD、diff 和测试，重建审批包并验证指纹。随后逐步保存 `commit_hash`、`push_completed_at`、`pr_url`、`comment_id`；每步重试先检查记录和远端事实。

```python
def publish(self, run: WorkflowRun) -> WorkflowRun:
    self._assert_approved_and_current(run)
    if not run.publication.commit_hash:
        run.publication.commit_hash = self.repository.commit(run)
        run = self.store.save(run, expected_version=run.version)
    if not run.publication.push_completed_at:
        self.repository.push(run)
        run.publication.push_completed_at = utc_now()
        run = self.store.save(run, expected_version=run.version)
    if not run.publication.pr_url:
        run.publication.pr_url = self.pr_client.find_or_create(run)
        run = self.store.save(run, expected_version=run.version)
    return self.commenter.ensure_comment(run)
```

- [ ] **Step 5: 使用本地 bare remote 验证 commit/push，PR 使用 Fake provider**

断言提交只包含 worktree diff，commit message/PR title/body 与审批包完全一致；PR provider 根据配置显式选择 GitHub/GitLab，未知 host 直接阻塞，不返回空字符串冒充成功。

- [ ] **Step 6: 运行测试并提交**

Run: `uv run pytest tests/test_developer_workflow_publisher.py tests/test_developer_workflow_ones_comment.py -v`

Expected: PASS。

```bash
git add src/developer_workflow/publisher.py src/developer_workflow/ones_comment.py src/integrations/ones.py src/integrations/ones_api.py src/services/ones_gateway.py tests/test_developer_workflow_publisher.py tests/test_developer_workflow_ones_comment.py
git commit -m "feat: publish approved runs idempotently"
```

## Task 11: 实现 Orchestrator 的创建、恢复、修改、批准和取消

**Files:**
- Create: `src/developer_workflow/orchestrator.py`
- Test: `tests/test_developer_workflow_orchestrator.py`

- [ ] **Step 1: 编写所有命令行为和安全恢复点测试**

`resume` 从 `BLOCKED.resume_state` 或未完成主链阶段继续；不能跳过 `VALIDATING`、测试或审批。`revise` 仅允许 `WAITING_APPROVAL/BLOCKED`，清除旧审批并回到 `IMPLEMENTING`。`approve` 仅允许 `WAITING_APPROVAL`。`cancel` 不删除 worktree 和诊断。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_developer_workflow_orchestrator.py -v`

Expected: FAIL，编排器不存在。

- [ ] **Step 3: 实现依赖注入的 Orchestrator**

```python
@dataclass(slots=True)
class DeveloperWorkflowOrchestrator:
    store: FileRunStore
    requirement_flow: RequirementFlow
    defect_flow: DefectFlow
    publisher: Publisher
    config: DeveloperWorkflowConfig

    def start_requirement(self, requirement_id: str) -> WorkflowRun:
        run = self.store.create(WorkflowRun.new("requirement", requirement_id))
        return self.requirement_flow.execute(run)

    def start_defect(self, project_id: str, iteration_id: str, assignee: str, candidate_id: str) -> WorkflowRun:
        run = self.store.create(WorkflowRun.new_defect(project_id, iteration_id, assignee, candidate_id))
        return self.defect_flow.execute(run)

    def show(self, run_id: str) -> WorkflowRun:
        return self.store.load(run_id)

    def confirm_repository(self, run_id: str, mapping_key: str) -> WorkflowRun:
        run = self.store.load(run_id)
        if run.state is not WorkflowState.VALIDATING:
            raise InvalidWorkflowAction("repository confirmation requires VALIDATING")
        mapping = self.config.resolve_mapping_key(mapping_key, run.project_id, run.iteration_id)
        confirmed = run.model_copy(update={"repository": mapping})
        saved = self.store.save(confirmed, expected_version=run.version)
        return self._flow_for(saved).execute(saved)

    def resume(self, run_id: str) -> WorkflowRun:
        run = self.store.load(run_id)
        if run.state is WorkflowState.PARTIAL_SUCCESS:
            return self.publisher.retry_comment(run)
        if run.state is WorkflowState.PUBLISHING:
            return self.publisher.publish(run)
        if run.state is WorkflowState.BLOCKED and run.resume_state is None:
            raise InvalidWorkflowAction("Blocked run has no safe resume state")
        return self._flow_for(run).execute(run)

    def revise(self, run_id: str, feedback: str) -> WorkflowRun:
        run = self.store.load(run_id)
        if run.state not in {WorkflowState.WAITING_APPROVAL, WorkflowState.BLOCKED}:
            raise InvalidWorkflowAction("revise requires WAITING_APPROVAL or BLOCKED")
        revised = run.for_revision(feedback)
        saved = self.store.save(revised, expected_version=run.version)
        return self._flow_for(saved).execute(saved)

    def approve(self, run_id: str, approved_by: str) -> WorkflowRun:
        run = self.store.load(run_id)
        if run.state is not WorkflowState.WAITING_APPROVAL:
            raise InvalidWorkflowAction("approve requires WAITING_APPROVAL")
        approved = run.with_approval(approved_by=approved_by, approved_at=utc_now())
        saved = self.store.save(approved, expected_version=run.version)
        return self.publisher.publish(saved)

    def cancel(self, run_id: str, actor: str) -> WorkflowRun:
        run = self.store.load(run_id)
        return self.store.transition(run_id, run.version, WorkflowState.CANCELLED, f"cancelled by {actor}")
```

类中增加 `config: DeveloperWorkflowConfig` 依赖；同一步实现 `_flow_for()`，仅按 `run.workflow_type` 返回 requirement/defect flow，未知类型抛出 `InvalidWorkflowAction`。`confirm_repository()` 只能选择配置中匹配该项目/迭代的精确或项目默认映射，不能接受任意 URL；公共签名不得在 CLI 中复制业务规则。

- [ ] **Step 4: 运行测试并提交**

Run: `uv run pytest tests/test_developer_workflow_orchestrator.py -v`

Expected: PASS。

```bash
git add src/developer_workflow/orchestrator.py tests/test_developer_workflow_orchestrator.py
git commit -m "feat: orchestrate resumable developer workflows"
```

## Task 12: 提供 `ones-dev` CLI

**Files:**
- Create: `src/developer_workflow/cli.py`
- Modify: `pyproject.toml`
- Test: `tests/test_developer_workflow_cli.py`

- [ ] **Step 1: 编写七个子命令的 CLI 测试**

使用 patch 的 Orchestrator 验证 `requirement`、`defect`、`show`、`resume`、`revise`、`approve`、`cancel`；`defect` 先打印编号化候选，再要求精确输入序号；非 TTY 必须提供 `--select <uuid>`，不得自动选第一个。

`requirement` 和 `defect` 在来源校验后打印候选仓库、URL、基线和命令；交互终端要求用户确认或选择配置内 mapping key，非 TTY 必须提供 `--mapping <key>`。拒绝确认时 run 保持 `VALIDATING`，不创建 worktree；确认后调用 `orchestrator.confirm_repository()`。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_developer_workflow_cli.py -v`

Expected: FAIL，CLI 不存在。

- [ ] **Step 3: 用 argparse 实现薄 CLI**

所有命令支持 `--config <path>`，默认 `ones-dev.config.json`；`approve` 增加必填 `--actor`，终端展示 run ID、状态、阻塞原因、worktree、测试、风险、审批指纹、PR URL，不显示凭据。

- [ ] **Step 4: 注册项目脚本并验证帮助**

在 `pyproject.toml` 增加：

```toml
ones-dev = "src.developer_workflow.cli:main"
```

Run: `uv run ones-dev --help`

Expected: 列出七个子命令且退出码为 0。

- [ ] **Step 5: 运行测试并提交**

Run: `uv run pytest tests/test_developer_workflow_cli.py -v`

Expected: PASS。

```bash
git add pyproject.toml src/developer_workflow/cli.py tests/test_developer_workflow_cli.py
git commit -m "feat: expose ones-dev command line workflow"
```

## Task 13: 端到端、安全与局域网验收

**Files:**
- Create: `tests/test_developer_workflow_e2e.py`
- Create: `tests/test_developer_workflow_security.py`
- Modify: `tests/test_ones_lan_smoke.py`
- Create: `docs/ones_dev_cli.md`

- [ ] **Step 1: 用 Fake ONES、Fake Codex 和 bare remote 编写两条 E2E 测试**

需求路径覆盖 Wiki 快照到审批、批准、commit/push/PR/comment；缺陷路径覆盖列表单选、证据门禁、失败测试、修复、审批和发布。Fake PR client 记录一次 create，Fake ONES 记录一次 comment 和零次 status update。

- [ ] **Step 2: 编写安全矩阵测试**

逐项断言：未审批零 commit/push/PR/comment；Codex 改 HEAD 阻塞；审批后 source/base/HEAD/diff/test/risk/PR 任一变化失效；证据不足/需求不可验证不创建 worktree；PR 失败不评论；`PARTIAL_SUCCESS` 只重试评论。

- [ ] **Step 3: 运行新工作流完整测试**

Run: `uv run pytest tests/test_developer_workflow_*.py -v`

Expected: PASS。

- [ ] **Step 4: 运行 ONES 和既有核心回归**

Run: `uv run pytest tests/test_ones.py tests/test_phase2.py tests/test_ones_gateway.py tests/test_config.py tests/test_execution_service.py tests/test_defect_analysis_workflow.py -v`

Expected: PASS，既有分析/执行边界不受影响。

- [ ] **Step 5: 显式执行局域网只读 smoke test**

先设置四个 LAN 过滤环境变量和一个已授权 Wiki 页面变量；再运行：

Run: `uv run pytest tests/test_ones_lan_smoke.py -m ones_lan -v`

Expected: PASS；网络日志中只有 GET/GraphQL 只读查询，没有 comment、status update 或其他写请求。

- [ ] **Step 6: 编写运维文档**

`docs/ones_dev_cli.md` 必须给出配置示例、七个命令、状态含义、恢复规则、审批失效条件、凭据来源、LAN smoke opt-in 方式，以及“Codex 无发布权限、Publisher 仅在审批后写远端”的安全边界。

- [ ] **Step 7: 最终验证与提交**

Run: `uv run pytest -v`

Expected: 全部 pytest 通过；若环境阻止前端工具启动，记录为环境限制，但本 CLI 不依赖前端构建。

Run: `uv run ones-dev --help`

Expected: exit code 0。

```bash
git add tests/test_developer_workflow_e2e.py tests/test_developer_workflow_security.py tests/test_ones_lan_smoke.py docs/ones_dev_cli.md
git commit -m "test: verify ONES developer workflows end to end"
```

## 实施约束和完成定义

- 每个 Task 严格按红—绿—重构执行；不得先写实现再补测试。
- 不修改 `main.py`、`agent-gui/`、`src/core/Engine`、`Scheduler`、`ScheduleManager` 来承载新流程。
- 不通过 `server.py` 暴露本地开发工作流，因此无需改变 `server.py`、`tools.json`、`skill.md` 的现有 MCP 契约。
- 不删除与本计划无关的用户代码或未提交修改；清理只能发生在明确属于新工作流的生成物和测试临时目录。
- 任何无法证明缺陷列表完整、状态集合准确、来源快照未变或审批仍有效的情况都进入 `BLOCKED`。
- 只有 `Publisher` 可以 commit、push、创建 PR；只有 `OnesCommenter` 可以评论，且 PR URL 存在后才可调用。
- `COMPLETED` 的必要条件是 PR URL 和 ONES 评论稳定标识都已持久化；PR 成功但评论失败必须为 `PARTIAL_SUCCESS`。
