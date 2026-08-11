# ONES Dev 终端图形界面设计

日期：2026-08-11

## 1. 目标

为现有 `ones-dev` 增加一个全屏终端图形界面（TUI），通过以下命令启动：

```powershell
uv run ones-dev tui --config <path>
```

TUI 覆盖查询缺陷、创建需求/缺陷工作流、选择单仓或仓库组、查看运行状态、恢复、修订、审批、发布与取消的完整生命周期。现有非交互 CLI 保留，继续作为自动化和脚本化入口。

TUI 是展示与调度适配层，不复制、不替换、不绕过现有 `DeveloperWorkflowOrchestrator`、`FileRunStore`、Requirement/Defect Flow、Approval、Publisher、operation lock 或版本 CAS。

## 2. 已确认的产品决策

- 使用“仪表盘主页 + 新建/修订分步向导”的混合模式。
- 覆盖完整工作流生命周期。
- 键盘优先，同时支持鼠标点击和滚轮。
- 不同 run 可在后台并行；同一 run 的变更操作必须串行。
- 启动入口固定为 `ones-dev tui`，不增加独立可执行文件。
- 使用 Python Textual 框架实现。
- 主页采用三栏控制台；窄终端逐级折叠为双栏和单栏。

## 3. 非目标

- 不移除或改变现有 CLI 命令语义。
- 不在 TUI 中实现第二套 ONES、Git、Codex、PR 或评论客户端。
- 不允许 TUI 直接修改持久化 WorkflowRun JSON。
- 不展示未经脱敏的 Codex、subprocess、HTTP 或 Git 原始输出。
- 不自动批准、发布、取消或修改 ONES 状态。
- 不将 TUI 进程内状态作为工作流事实来源。

## 4. 技术选择

### 4.1 Textual

项目新增 `textual` 运行依赖。选择 Textual 的原因：

- 原生支持全屏终端、键盘、鼠标、滚轮、焦点和响应式布局。
- Worker 模型可承载现有同步 Orchestrator 的后台执行。
- Screen、Modal、DataTable、Tabs、RichLog 等组件适合仪表盘和向导。
- 官方提供无头 `run_test()`、`Pilot` 键鼠模拟和可选快照测试。
- 与项目现有 Python 3.11、`asyncio`、pytest 和 pytest-asyncio 体系一致。

不采用 prompt_toolkit，是因为它更适合 REPL 和高级命令行，完整仪表盘需要维护较多自定义组件。不采用 Urwid，是因为组件层级更底层，而且 Windows asyncio 集成约束不利于当前跨平台后台并发需求。

## 5. 架构

### 5.1 `DeveloperWorkflowTuiApp`

Textual 应用根组件，仅负责：

- Screen 与 Modal 生命周期；
- 键盘绑定、鼠标事件、焦点和响应式布局；
- 将用户意图传给 Controller；
- 渲染 Controller 返回的安全 ViewModel；
- 显示脱敏通知。

它不得直接调用 ONES、Git、Codex、PR provider 或 Publisher。

### 5.2 `TuiController`

Controller 是 TUI 与现有应用服务之间的唯一边界：

- 加载只读缺陷候选；
- 调用 `DeveloperWorkflowOrchestrator` 的 start/show/confirm/resume/revise/approve/cancel；
- 将 WorkflowRun 转换为只含白名单字段的 ViewModel；
- 在提交动作前执行界面级重复操作检查；
- 把耗时操作交给 `RunTaskSupervisor`。

Controller 不实现状态机逻辑。状态是否合法仍由 Orchestrator、FileRunStore 与 Flow 决定。

### 5.3 `RunTaskSupervisor`

Supervisor 管理 Textual 后台 worker：

- 不同 run 可并行执行；
- 同一 run 同时最多一个变更操作；
- 默认并行上限为 3，配置范围为 1–8；
- 超出上限的操作按提交顺序排队；
- worker 完成或失败后触发 run 列表刷新；
- TUI 关闭时不将未完成 worker 解释为工作流取消。

底层 operation lock 和 CAS 是最终并发门禁；TUI 的禁用按钮与任务表只是改善用户体验。

### 5.4 `RunIndex`

为 FileRunStore 增加只读 run 索引能力：

- 列举私有 run root 下合法、可验证的 run；
- 按状态、类型、work item、更新时间过滤和排序；
- 单个损坏 run 映射为脱敏的“存储损坏”条目，不阻止读取其他 run；
- 不自动修复、迁移或删除 run 文件；
- 不信任文件名、符号链接、重解析点或目录外路径。

持久化 WorkflowRun 始终是唯一事实来源。TUI 重启后必须仅从 FileRunStore 重建仪表盘。

### 5.5 `SafeEventSink`

耗时阶段可向 TUI 发送进度事件。允许字段仅包括：

- run ID、work item ID；
- workflow state 与安全阶段名称；
- repository key；
- 测试命令的配置标识和脱敏摘要；
- 开始、完成、阻塞、失败等事件类型；
- 固定或已脱敏错误消息。

事件不得包含环境变量、凭据、HTTP body、Codex prompt、完整 patch 或 subprocess 原始输出。事件只用于刷新和展示，不参与状态恢复或审批指纹。

## 6. 页面与交互

### 6.1 主仪表盘

宽度不小于 100 列时使用三栏：

1. 左栏：Runs、Defects、New Run、Settings 与状态过滤器；
2. 中栏：run 列表，显示状态、work item、更新时间和后台活动；
3. 右栏：当前 run 的证据、测试、审批和发布详情。

右栏包含以下标签：

- Overview
- Repositories
- Tests
- Review
- Publication
- History

宽度 70–99 列时隐藏导航栏，保留 run 列表与详情。宽度小于 70 列时，run 列表与详情使用独立 Screen，并通过快捷键返回。功能不得因终端变窄而消失。

### 6.2 新建缺陷向导

固定步骤：

1. 筛选并精确选择缺陷；
2. 选择单仓映射或仓库组；
3. 查看来源、拓扑、测试与副作用摘要；
4. 明确确认后启动。

状态筛选只使用 ONES 状态 ID，不按状态名称匹配。第一步只读 ONES，不创建 run 或 worktree。只有选择结果与当前候选快照一致，且用户确认仓库映射后，才进入现有 Orchestrator 流程。

需求向导复用相同骨架，但第一步输入并验证 requirement ID。

### 6.3 危险操作

以下动作必须使用独立 Modal，普通 Enter 不得直接触发：

- approve
- revise
- cancel
- 从 PARTIAL_SUCCESS 恢复发布

审批 Modal 必须展示：fingerprint、工作项、仓库数量、变更文件数量、测试摘要、风险和未解决项，并要求输入 actor。多仓审批还必须显示每仓 base/HEAD、签名 tree hash、测试和 PR 目标。

### 6.4 快捷键

最低键盘契约：

- `↑/↓` 或 `j/k`：移动选择；
- `Tab/Shift+Tab`：切换焦点或详情标签；
- `Enter`：打开详情或执行无副作用的下一步；
- `/`：搜索；
- `f`：过滤；
- `n`：新建；
- `r`：恢复；
- `v`：修订；
- `a`：打开审批 Modal；
- `x`：打开取消 Modal；
- `?`：帮助；
- `q`：退出 TUI。

鼠标可完成选择、标签切换、按钮点击和滚动，但所有功能必须存在键盘路径。右键和组合鼠标操作不得成为必要输入。

## 7. 数据流

### 7.1 启动

1. argparse 解析 `ones-dev tui --config <path>`；
2. 加载 DeveloperWorkflowConfig；
3. 按现有生产工厂验证私有目录、ONES 登录、评论端点、PR provider、Git identity、Codex 认证和 sandbox profile；
4. 构造与 CLI 相同的 Orchestrator 服务图；
5. 初始化 RunIndex 并读取已有 run；
6. 启动 Textual App。

TUI 不提供“缺少生产配置时只读降级”的隐式模式。配置不完整时 fail closed，并显示可操作但不含秘密的错误。

### 7.2 状态刷新

- worker 事件触发立即刷新；
- 同时以低频轮询读取 FileRunStore，处理其他进程产生的变化；
- ViewModel 带 run version，界面动作提交前重新加载权威 run；
- 若 version 已变化，丢弃陈旧界面动作并要求用户重新确认。

### 7.3 后台执行

现有 Orchestrator 是同步边界，因此使用 Textual thread worker 调用。worker 不直接更新 Textual widget；结果通过消息传回 UI 线程。UI 线程不得阻塞等待 worker。

## 8. 错误与恢复

- 配置、认证、网络、沙箱、Git、审批漂移继续 fail closed。
- BLOCKED 视图显示 blocked reason、resume state 与允许的下一步，不自动恢复。
- PARTIAL_SUCCESS 显示每仓 commit、push、PR 和 error 事实，以及 group/comment error。
- PUBLISHING 恢复仍重新验证审批、签名 tree、远端 base、commit、remote branch、PR marker 和 provider 身份。
- TUI 退出不修改 run 状态；已经持久化的检查点由下次启动恢复展示。
- 损坏 run、未知状态或读取异常只显示固定脱敏错误，不显示文件内容或异常原文。
- TUI 不提供删除 run、删除 worktree 或回滚已创建 PR 的操作。

## 9. 安全要求

- 所有展示值经过现有 `_safe_value` 等价边界，拒绝或替换控制字符、双向文本控制和不可编码字符。
- 组件状态、通知、日志和剪贴板不得包含 secret。
- 不提供复制 Codex prompt、原始环境、HTTP 请求或完整 patch 的快捷动作。
- 本地 source_path 继续只读；TUI 明确标注修改发生在隔离 managed worktree。
- 危险动作在 Modal 打开后重新加载 run，并以最新 version 和证据渲染确认内容。
- 多仓审批必须展示并绑定签名 tree hash，防止 publication intent 自证未审批内容。

## 10. 测试

### 10.1 单元测试

- Controller 动作精确映射到 Orchestrator；
- ViewModel 只包含白名单字段；
- RunIndex 拒绝路径逃逸、符号链接、损坏 JSON 和类型强转；
- Supervisor 的每 run 串行、跨 run 并行和并发上限；
- actor、feedback、过滤器和搜索输入的 UTF-8/控制字符边界。

### 10.2 Textual 无头测试

使用 `App.run_test()` 和 Pilot 覆盖：

- 键盘与鼠标导航；
- 新建缺陷/需求向导；
- approve/revise/cancel Modal；
- 70、99、100 列等响应式边界；
- BLOCKED、PARTIAL_SUCCESS、WAITING_APPROVAL、COMPLETED 展示；
- worker 完成、失败和状态刷新；
- 退出与重新启动恢复。

可选使用 `pytest-textual-snapshot` 对关键布局做视觉快照，但行为断言是必须项。

### 10.3 集成与回归

- 真实 FileRunStore + Fake Orchestrator 的多 run 并发；
- 真实 Orchestrator + Fake ONES/Codex/PR/comment 的完整 TUI 操作链；
- 审批前零 commit/push/PR/comment；
- 多仓每仓独立 commit/PR 与 PARTIAL_SUCCESS 恢复；
- 敏感环境值不得出现在任何 widget 文本；
- 全部现有 CLI、workflow、ONES 与 repository 测试继续通过；
- `uv run ones-dev tui --help` 和打包后入口 smoke 通过。

## 11. 文档与兼容性

`docs/ones_dev_cli.md` 增加 TUI 启动、快捷键、页面说明、后台并发、退出语义和故障恢复。现有 CLI 命令示例保持有效。

Python 版本继续为 3.11+。支持 Windows Terminal、主流 Linux/macOS 终端和 SSH 终端。鼠标是增强能力，键盘是完整能力。Textual inline 模式不在范围内，始终使用全屏 application mode。

## 12. 验收标准

1. `uv run ones-dev tui --config <path>` 可启动完整 TUI；
2. 用户可从 TUI 完成缺陷查询、精确选择、仓库组确认、运行、恢复、修订、审批、发布与取消；
3. 不同 run 可并行，同一 run 不产生重复副作用；
4. TUI 重启后从 FileRunStore 恢复全部可见状态；
5. 多仓 run 清晰展示每仓证据、测试、commit、push、PR 和错误；
6. 危险动作必须经过 Modal 且重新读取权威状态；
7. 审批前保持零远端副作用；
8. 敏感值不进入界面、通知、日志或测试快照；
9. 窄终端仍可访问全部功能；
10. 新增 TUI 测试与现有回归全部通过。
