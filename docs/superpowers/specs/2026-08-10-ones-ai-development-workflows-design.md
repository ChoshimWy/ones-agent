# ONES AI 需求开发与缺陷修复工作流设计

## 1. 背景

日常开发包含两条主要路径：

1. 从 ONES 需求任务进入其关联的 Wiki/项目文档，理解需求并完成代码开发。
2. 按 ONES 项目、迭代和账号读取缺陷列表，人工选择一个缺陷后完成分析、修复和 review。

本设计不以当前 FastAPI、前端、调度器、分析会话或持久化运行时为基础。它只复用当前项目已有的 ONES 登录、任务详情、项目、迭代、成员、缺陷和评论实现，并在同一 ONES 集成边界补充已确认缺失的 Wiki 读取能力。

首个 AI 执行引擎为 Codex CLI。工作流采用本地手动触发，AI 可以修改隔离工作区中的代码并运行测试，但只有人工批准后，外层发布器才能 commit、push、创建 PR 和评论 ONES。

## 2. 已确认决策

- 工作流形态：独立本地 CLI 运行器。
- 触发方式：仅手动触发，不轮询、不定时扫描。
- 需求入口：ONES 需求任务 ID。
- 需求正文：需求任务中关联的 ONES Wiki/项目文档。
- 仓库定位：`ONES 项目 + 迭代` 映射到仓库和基线分支，启动时允许人工确认或改选。
- 缺陷处理：按项目、迭代和账号拉取清单，人工单选后处理；一次运行只处理一个缺陷。
- 自动化边界：Codex 自动分析、修改、测试和自 review；人工批准后才发布。
- ONES 回写：PR 创建成功后只添加评论，不自动修改需求或缺陷状态。
- AI 引擎：Codex CLI。

## 3. 目标

### 3.1 功能目标

- 支持从一个 ONES 需求 ID 完成“读取 Wiki → 校验需求 → 修改代码 → 测试 → review → 人工审批 → PR → ONES 评论”的闭环。
- 支持从“项目 + 迭代 + 账号”获取缺陷清单，人工选择一个缺陷后完成证据化分析和修复闭环。
- 复用现有 ONES 认证和数据访问实现，避免形成第二套登录或凭据体系。
- 每次运行使用独立 Git worktree 和独立分支，避免污染用户当前工作目录。
- 所有发布动作均可审计、可恢复并具备幂等保护。
- 需求和缺陷工作流共享编排、仓库、Codex、审批、发布和运行记录能力。

### 3.2 安全目标

- Codex 运行期间不得 commit、push、创建 PR 或回写 ONES。
- 未经人工批准不得产生任何远端代码或 ONES 副作用。
- Wiki 无权限、需求不完整、仓库未映射或缺陷证据不足时必须阻塞，不得猜测执行。
- 审批后如果代码、基线、测试结果或来源文档发生变化，原审批自动失效。

## 4. 非目标

- 不建设新的 Web 管理台。
- 不复用或改造当前 FastAPI API、前端页面、`Engine`、`Scheduler`、`ScheduleManager` 或分析会话。
- 不提供后台轮询、定时扫描或自动认领任务。
- 不批量修复同一迭代中的全部缺陷。
- 不自动合并 PR。
- 不自动修改 ONES 工作项状态、负责人、迭代或其他字段。
- 第一版不同时支持多个编码 Agent。

## 5. 总体架构

```text
人工输入/选择
     │
     ▼
Developer Workflow CLI
     │
     ├── ONES Adapter
     │     ├── 任务详情
     │     ├── Wiki 页面与版本
     │     ├── 项目、迭代和成员
     │     ├── 缺陷列表与详情
     │     └── 评论回写
     │
     ├── Repository Resolver
     │     └── 项目 + 迭代 → 仓库 + 基线分支
     │
     ├── Codex Agent Runner
     │     ├── 需求理解/缺陷分析
     │     ├── 代码检索与修改
     │     ├── 测试
     │     └── AI 自 review
     │
     ├── Human Approval Gate
     │
     └── Publisher
           ├── commit
           ├── push
           ├── 创建 PR
           └── 评论 ONES
```

ONES 只负责提供工作项、文档和评论能力，不承担 AI 编排。工作流编排器负责状态转换，但不直接实现 ONES、Git 或 Codex 细节。发布器是唯一可以执行远端副作用的模块。

## 6. 现有 ONES 能力与补充能力

### 6.1 直接复用

当前项目已有能力包括：

- ONES 登录和认证会话。
- 项目列表。
- 迭代列表。
- 项目角色成员和团队成员。
- 工作项/缺陷列表和任务详情。
- 按迭代、经办人和状态筛选缺陷。
- 缺陷详情规范化。
- ONES 工作项评论。

这些能力继续以 `src/integrations/ones_api.py` 和 `src/services/ones_gateway.py` 为主要边界。

### 6.2 已只读验证的 Wiki 接口

在用户授权的局域网 ONES `http://aputureones.com:8088/` 上，已使用现有 `OnesAsyncClient` 登录态只读验证以下接口：

- 页面正文：`GET /wiki/api/wiki/team/{team_id}/space/{space_id}/page/{page_id}`
- 页面元数据：`GET /wiki/api/wiki/team/{team_id}/page/{page_id}/detail`
- 页面树及历史信息：`GET /wiki/api/wiki/team/{team_id}/space/{space_id}/pages_with_history`

页面正文响应包含 `content`、`version`、`updated_time`、`title`、`space_uuid`、`uuid` 等字段，能够支持内容读取、版本固化和审批前版本复查。

### 6.3 补充方式

在 `OnesAsyncClient` 增加以下只读方法：

- `fetch_wiki_page(space_id, page_id)`
- `fetch_wiki_page_info(page_id)`
- `fetch_wiki_pages_with_history(space_id)`

所有方法复用现有 `_get_client()`、登录会话、超时设置、日志脱敏和异常映射，不创建独立 Wiki 凭据体系。

在 `OnesGateway` 增加：

- 需求工作项规范化。
- 需求描述中的 Wiki URL 提取。
- Wiki 页面响应规范化。
- Wiki 版本快照生成。

工作流不得直接消费 ONES 原始 JSON。

## 7. 模块划分

建议新增独立包：

```text
src/developer_workflow/
├── cli.py
├── orchestrator.py
├── contracts.py
├── state_store.py
├── requirement_flow.py
├── defect_flow.py
├── codex_runner.py
├── repository.py
├── approval.py
└── ones_comment.py
```

### 7.1 `cli.py`

提供手动命令、缺陷列表选择、运行状态查看、恢复、修改反馈和审批。CLI 只负责参数解析和用户交互，不直接调用底层 HTTP 或 Git 命令。

### 7.2 `orchestrator.py`

驱动统一状态机，持久化每个阶段的输入和输出，保证阶段失败后从最后一个安全检查点恢复。

### 7.3 `requirement_flow.py`

负责需求工作项、Wiki 文档、需求完整性校验和验收标准覆盖关系，不负责 Git 发布。

### 7.4 `defect_flow.py`

负责缺陷筛选、人工单选、详情读取、证据门禁、复现和修复约束，不负责 Git 发布。

### 7.5 `codex_runner.py`

以非交互方式运行 Codex，提供结构化输入和 JSON Schema 输出，限制工作目录、网络和副作用权限。

### 7.6 `repository.py`

负责仓库映射、基线校验、Git worktree、分支名、diff、测试命令、审批指纹和批准后的发布。

### 7.7 `approval.py`

生成审批包，验证审批时看到的内容仍与当前工作区一致，记录批准、要求修改或取消。

### 7.8 `ones_comment.py`

只在 PR 创建成功后生成和提交 ONES 评论，使用幂等键防止重复评论。

## 8. 数据契约

### 8.1 `RequirementRecord`

- `requirement_id`
- `number`
- `title`
- `project`
- `iteration`
- `assignee`
- `status`
- `description`
- `wiki_refs`
- `source="ones"`

### 8.2 `WikiPageSnapshot`

- `team_id`
- `space_id`
- `page_id`
- `title`
- `normalized_content`
- `version`
- `updated_at`
- `source_url`
- `content_sha256`

正文规范化必须保留标题层级、列表、表格和代码块的语义。图片、附件或内嵌第三方资源无法转为文本时，应记录为未解析引用并参与需求完整性检查。

### 8.3 `RepositoryMapping`

- `project_id`
- `iteration_id`
- `repo_url`
- `repo_name`
- `base_branch`
- `test_commands`
- `lint_commands`
- `build_commands`
- `allowed_paths`（可选）

映射优先使用精确的“项目 + 迭代”；如果没有精确映射，可以展示项目级默认值供人工确认，但不得静默采用猜测结果。

### 8.4 `WorkflowRun`

- 运行 ID和工作流类型。
- 当前状态和状态历史。
- ONES 工作项快照和 Wiki 快照。
- 仓库映射、基线 commit 和工作分支。
- Codex 会话/运行标识。
- 修改文件、测试结果和 AI review。
- 审批包、审批人、审批时间和审批结论。
- commit、push、PR 和 ONES 评论结果。
- 错误、阻塞原因和重试记录。

### 8.5 `ApprovalPackage`

- 需求摘要或缺陷根因摘要。
- ONES 工作项版本和 Wiki 页面版本。
- 修改文件与 diff 摘要。
- 验收标准覆盖情况或缺陷根因证据。
- 测试命令、退出码和结果摘要。
- 风险、未解决事项和人工检查建议。
- 拟用 commit 信息和 PR 描述。
- 基线、diff、测试结果和来源快照组成的审批指纹。

## 9. 需求开发工作流

输入：`requirement_id`

1. 使用现有任务详情接口读取标题、状态、负责人、项目、迭代和描述。
2. 从描述中提取一个或多个 ONES Wiki 页面链接。
3. 使用相同 ONES 会话读取页面正文和元数据，保存页面版本、更新时间和内容哈希。
4. 将正文整理为目标、范围、验收标准、约束和非目标。
5. 校验需求完整性。Wiki 缺失或无权访问、验收标准不可验证、内容矛盾或关键技术约束缺失时进入 `BLOCKED`。
6. 根据项目和迭代解析仓库及基线分支，展示给用户确认或改选。
7. 从干净基线创建独立 worktree 和 `requirement/<编号>-<摘要>` 分支。
8. Codex 检查仓库规范、相关模块、已有测试和类似实现，形成“验收标准 → 代码/测试”映射。
9. Codex 修改代码并运行针对性测试、静态检查和配置要求的完整测试。
10. Codex 执行自 review，检查需求覆盖、异常路径、回归、安全和无关改动；发现问题后修复并重新测试。
11. 编排器生成审批包并进入 `WAITING_APPROVAL`。
12. 人工可以批准、要求修改或取消。要求修改时，将反馈交给同一运行继续处理并重新生成审批包。
13. 只有批准且审批指纹仍有效时，发布器才 commit、push 和创建 PR。
14. PR 创建成功后向 ONES 需求添加实现摘要、测试结果、分支和 PR 链接评论。

## 10. 缺陷分析修复工作流

输入：`project_id + iteration_id + assignee`

1. 使用现有 ONES 项目、迭代、成员和缺陷查询能力获取该账号的未关闭缺陷。
2. 展示缺陷编号、标题、优先级、状态和更新时间，由人工选择一个。
3. 获取完整缺陷详情，确认项目、迭代、仓库和基线分支。
4. 从缺陷描述提取现象、复现条件、期望行为和实际行为。
5. 搜索相关代码、测试和调用链，按现有 `docs/analysis_acceptance_rules.md` 执行证据门禁。
6. 根因必须有文件位置、代码片段、调用链或复现测试支持。证据不足时进入 `BLOCKED`，只输出调查建议，不修改代码。
7. 从干净基线创建独立 worktree 和 `bugfix/<编号>-<摘要>` 分支。
8. 优先增加或运行能够复现问题的失败测试，再进行最小修复。
9. 运行回归测试，并由 Codex 自 review 根因、修改和测试是否一致。
10. 生成审批包，额外包含根因证据、修复前后行为、影响范围和风险等级。
11. 只有人工批准且审批指纹仍有效时，发布器才 commit、push 和创建 PR。
12. PR 创建成功后向 ONES 缺陷添加分析摘要、测试结果、分支和 PR 链接评论。

## 11. 状态机

```text
CREATED
  → READING_ONES
  → VALIDATING
  → PREPARING_REPO
  → IMPLEMENTING
  → TESTING
  → AI_REVIEW
  → WAITING_APPROVAL
  → PUBLISHING
  → COMPLETED
```

补充终态和恢复状态：

- `BLOCKED`：缺少权限、数据、仓库映射、证据或安全前提，需要人工处理后恢复。
- `CANCELLED`：人工取消，不再继续。
- `PARTIAL_SUCCESS`：PR 已成功，但 ONES 评论失败；只允许重试评论。
- `FAILED`：不可恢复的内部错误，保留工作区和诊断记录。

任一阶段完成后都以原子方式写入运行记录。`resume` 只能从已定义的安全检查点恢复，不能跳过校验或审批。

## 12. Codex 执行边界

Codex 使用非交互执行模式，概念调用如下：

```text
codex exec
  --cd <isolated-worktree>
  --sandbox workspace-write
  --output-schema <workflow-result-schema.json>
  <structured-prompt>
```

约束如下：

- Codex 只获得隔离 worktree 的写权限。
- 默认不开放网络；缺失依赖导致测试无法运行时进入阻塞状态，由人工决定如何处理。
- Git 元数据保持只读；Codex 运行前后必须验证 `HEAD` 未变化。
- Codex 不获得 ONES 评论和 Git 远端发布凭据。
- Codex 不得运行 `git commit`、`git push`、PR 创建或 ONES 写操作。
- Codex 的最终结果必须通过 JSON Schema 校验。
- 自然语言中的“完成”“通过”或高置信度陈述不能替代真实 diff、测试退出码和证据。
- 如果检测到 `HEAD` 变化、越权发布尝试或工作区外写入，运行进入 `BLOCKED` 并保留审计记录。

## 13. CLI 设计

```text
# 需求开发
ones-dev requirement <requirement-id>

# 缺陷清单与单选修复
ones-dev defect --project <id> --iteration <id> --assignee <id>

# 查看状态
ones-dev show <run-id>

# 从安全检查点继续
ones-dev resume <run-id>

# 根据人工反馈继续修改
ones-dev revise <run-id> --feedback "..."

# 审批并发布
ones-dev approve <run-id>

# 取消
ones-dev cancel <run-id>
```

`approve` 在发布前必须重新验证：来源版本、基线 commit、当前 `HEAD`、diff 哈希、测试结果和审批指纹。

## 14. 配置

继续复用现有 ONES 环境变量：

- `ONES_BASE_URL`
- `ONES_EMAIL`
- `ONES_PASSWORD`
- `ONES_TEAM_ID`

新增独立工作流配置文件，至少包含：

- 项目与迭代到仓库、基线分支的映射。
- 各仓库的测试、lint 和构建命令。
- 可选路径限制。
- 分支名和 commit/PR 模板。
- Git 远端发布方式。
- 本地运行记录和 worktree 根目录。

配置不得包含明文 ONES 密码、Git token 或其他密钥。凭据由现有安全来源或运行环境提供。

## 15. 错误处理与恢复

### 15.1 ONES

- 超时、连接中断和 5xx：最多重试三次，并使用退避。
- 401/403：进入 `BLOCKED`，明确记录认证或权限问题。
- 404、Wiki URL 解析失败或页面已删除：进入 `BLOCKED`。
- Wiki 在开发期间改变版本：审批包失效，重新读取并展示变更后再决定是否继续。

### 15.2 仓库

- 映射缺失：暂停并要求人工选择，不自动猜测。
- 用户当前目录有未提交修改：不触碰该目录；从确认的基线创建独立 worktree。
- 基线分支更新：已生成的审批包失效，人工决定继续当前基线还是重新同步。
- worktree 创建失败：保留诊断信息，不修改原仓库。

### 15.3 Codex 和测试

- Codex 中断：保留 worktree、结构化输入和阶段输出，可使用 `resume`。
- 结构化输出不符合 Schema：当前阶段失败，不进入下一阶段。
- 测试失败：允许有限的 Codex 修复循环；超过配置次数后进入 `BLOCKED`。
- 无法运行测试：审批包必须明确标记，默认不允许发布；只有后续设计显式引入例外审批时才能放宽，第一版不提供例外。

### 15.4 发布和回写

- commit、push、PR 创建使用运行 ID和内容指纹保证幂等。
- push 失败：可从 `PUBLISHING` 重试，不重复 commit。
- PR 已存在：复用已记录的 PR，不重复创建。
- PR 成功但 ONES 评论失败：进入 `PARTIAL_SUCCESS`，只能重试评论。
- ONES 评论包含运行 ID或稳定标识，重试前检查是否已回写。

## 16. 审批包和审批失效条件

审批包必须包含：

- ONES 工作项 ID、标题和当前状态。
- 需求 Wiki URL、页面版本和更新时间，或缺陷根因证据。
- 仓库、基线分支、基线 commit 和工作分支。
- 修改文件、diff 摘要和无关改动检查。
- 验收标准覆盖关系或缺陷修复证据。
- 测试命令、退出码和结果摘要。
- AI review 发现、已修复项、风险和未解决事项。
- 拟用 commit message 和 PR 标题、正文。

以下任一变化使审批自动失效：

- ONES 需求/缺陷关键内容变化。
- Wiki 页面版本或内容哈希变化。
- 基线 commit 或当前 `HEAD` 变化。
- diff 内容变化。
- 测试命令、测试结果或退出码变化。
- 审批包中的风险、未解决事项或 PR 内容变化。

## 17. 测试策略

### 17.1 单元测试

- Wiki URL 提取与 `team/space/page` 标识解析。
- Wiki 原始响应规范化和内容哈希。
- 项目/迭代仓库映射优先级。
- 分支名生成和输入清理。
- 状态转换和非法跳转。
- 审批指纹生成与失效。
- 幂等键和重复发布保护。

### 17.2 契约测试

- 使用 mock HTTP 响应验证 Wiki 三个只读接口。
- 验证现有认证 Cookie/headers 被复用且日志不泄露凭据。
- 验证 401、403、404、429、5xx 和超时映射。
- 验证 Codex 输出 Schema 的成功和失败路径。

### 17.3 集成测试

- 在临时 Git 仓库中创建 worktree、修改、测试、审批和发布到本地 bare remote。
- 使用 Fake Codex 执行器覆盖需求和缺陷全流程。
- 验证 `resume` 从各安全检查点恢复。
- 验证 PR 成功、评论失败后的 `PARTIAL_SUCCESS` 恢复路径。

### 17.4 安全测试

- 未审批不得产生 commit。
- 未审批不得 push 或创建 PR。
- PR 失败不得评论 ONES。
- 任何流程不得自动修改 ONES 状态。
- Codex 导致 `HEAD` 变化时必须阻塞。
- 审批后代码或来源变化必须使审批失效。
- 缺陷证据不足时不得修改代码。
- Wiki 无权访问或需求不可验证时不得修改代码。

### 17.5 局域网 smoke test

真实局域网 ONES 测试必须显式启用，且默认只读。它只验证认证、指定 Wiki 页面读取和指定过滤条件下的缺陷查询，不遍历或修改无关业务数据。

## 18. 验收标准

### 18.1 需求流程

- 给定有效需求 ID和 Wiki 链接，可以读取并固化 Wiki 页面版本。
- 需求不完整时在创建 worktree 或修改代码前阻塞。
- 仓库映射经确认后，Codex 只在独立 worktree 修改代码。
- AI 完成测试和自 review 后生成完整审批包。
- 人工批准前没有 commit、push、PR 或 ONES 评论。
- 批准后成功创建 PR，并仅以评论形式回写 ONES。

### 18.2 缺陷流程

- 可以按项目、迭代和账号读取未关闭缺陷并供人工单选。
- 每次运行只处理一个缺陷和一个分支。
- 根因证据不足时返回阻塞结果且不修改代码。
- 有足够证据时，修复包含可复现或回归测试。
- 人工批准后创建 PR，并将根因摘要、测试结果和 PR 链接评论到 ONES。

### 18.3 恢复和安全

- 中断后可以从最后一个安全检查点继续。
- 重试不会重复创建 commit、PR 或 ONES 评论。
- 来源、代码或测试变化会使旧审批失效。
- 当前系统的 FastAPI、前端和调度运行时不参与此工作流。

## 19. 实施顺序建议

1. ONES Wiki 只读接口、规范化契约及测试。
2. 独立 CLI、运行状态存储和仓库映射。
3. worktree、审批指纹和发布安全边界。
4. Codex 非交互适配器和结构化输出。
5. 需求工作流。
6. 缺陷筛选、证据门禁和修复工作流。
7. commit、push、PR、ONES 评论及幂等恢复。
8. 端到端和局域网只读 smoke 验证。
