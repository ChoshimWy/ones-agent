# ONES Dev TUI 启动配置向导设计

## 1. 背景

当前 `ones-dev tui` 在创建 Textual App 前执行完整生产预检。ONES 登录、评论读取端点、PR provider、Git 身份、Codex 认证、私有目录或 managed sandbox profile 任一缺失，命令都会以固定安全错误退出。

该行为保证了生产工作流不会在半配置状态运行，但也导致用户无法进入 TUI 完成首次配置。仓库中的示例 JSON 包含占位值，不能直接作为生产配置运行。

本设计将 TUI 改为双阶段启动：配置不完整时进入受限配置模式；只有全部生产能力验证通过后，才构建现有 Orchestrator 和 Dashboard。

## 2. 目标

- `ones-dev tui` 在生产配置缺失、过期或验证失败时仍能启动配置界面。
- 配置阶段不能创建 run、mirror、worktree，也不能执行 commit、push、PR、ONES 评论或状态更新。
- 敏感凭据保存到 Windows 凭据管理器，不写入 JSON、日志、通知或 widget。
- 非敏感配置保存到 `%LOCALAPPDATA%\ones-dev\config.json`，不改写仓库内示例文件或 `--config` 模板。
- 支持完整配置 ONES、单仓、仓库组、PR provider、Git 身份、Codex、私有目录和 managed sandbox profile。
- 全部生产预检通过后，复用现有生产服务图进入 Dashboard。
- 非交互 CLI 保持现有环境变量运行方式，避免扩大兼容性改动。

## 3. 非目标

- 不在 TUI 内创建、安装或修改 managed sandbox profile。
- 不提供配置不完整时的只读 Dashboard。
- 不允许部分能力逐项开放。
- 不自动删除原有环境变量或 `.env`。
- 不把 Windows 凭据管理器替换为项目自定义加密文件。
- 不改变现有工作流、审批、发布和恢复的安全语义。

## 4. 核心架构

### 4.1 双阶段 Host

顶层 TUI Host 管理两个互斥阶段。

配置阶段只创建：

- `SetupConfigService`
- `WindowsCredentialStore`
- `SetupController`
- 配置 Screen 和安全 ViewModel

配置阶段不得创建：

- `DeveloperWorkflowOrchestrator`
- `OnesGateway` 生产实例
- `WorktreeRepository` 或 `RepositoryGroupWorkspace`
- `CodexRunner`
- `Publisher`
- `FileRunStore` 生产实例

在“保存并启用”以前，不得创建 run、mirror 或 worktree root。保存阶段可以在用户明确确认后创建并验证三个空 private roots，但在生产服务图构建成功以前，仍不得创建任何 run、Git mirror、managed worktree 或发布事实。

运行阶段在完整预检成功后创建现有生产服务图，并将 Host 切换到 Dashboard。配置发生变更时，Host 必须先关闭当前 TUI Controller 和异步运行时，再重新验证并重建服务图；不得热修改现有依赖对象。

### 4.2 SetupConfigService

职责：

- 读取用户配置档案与可选导入模板。
- 构建不含秘密的配置草稿。
- 验证字段、仓库拓扑、路径和命令边界。
- 协调只读连接测试。
- 版本化提交凭据和非敏感配置。
- 生成固定、脱敏的配置状态 ViewModel。

该服务不持有生产 Orchestrator，也不直接执行工作流操作。

### 4.3 WindowsCredentialStore

使用 Windows Credential Manager 保存以下秘密：

- ONES 邮箱与密码
- GitHub/GitLab provider token
- Codex API key 或 token
- 允许的 Git 凭据传输秘密

Credential target 由配置档案 ID、generation 和固定字段名推导，不能包含用户输入的自由文本。JSON 只保存档案 ID、generation 和非敏感 provider 信息，不保存凭据值。

现有 Codex 文件登录可以作为外部认证来源。TUI 只记录“使用现有 Codex 登录”的选择并验证路径形态和可用性，不读取、复制或显示 `auth.json` 内容。

### 4.4 RuntimeBootstrapper

`RuntimeBootstrapper` 接收经过验证的非敏感配置和显式凭据对象，构建与当前 `build_production_orchestrator` 等价的完整服务图。

TUI 不应通过修改整个父进程环境来注入秘密。现有生产工厂需要抽取可复用的显式运行时输入边界；非交互 CLI 继续从环境变量构造同一输入对象。

## 5. 启动状态机

```text
读取用户配置
  ├─ 不存在、不完整或不安全
  │    └─ 配置模式
  └─ 完整
       └─ 加载引用的凭据 generation
            ├─ 缺失或不可用 → 配置模式并定位步骤
            └─ 完整生产预检
                 ├─ 成功 → 构建服务图 → Dashboard
                 └─ 失败 → 配置模式并定位步骤
```

`--config` 只指定首次导入模板。模板只能读取，不得原地修改。成功保存后，用户私有配置成为 TUI 的权威配置来源。

## 6. 配置向导

### 6.1 配置档案与迁移

- 创建稳定的配置档案 ID。
- 检测 `--config` 模板、进程环境和 `.env` 中是否存在可迁移字段。
- 界面只显示“检测到”或“未检测到”，不显示值。
- 只有用户明确确认后，才把支持的秘密导入 Windows 凭据管理器。
- 不自动清除原来源。

### 6.2 ONES

配置：

- Base URL
- Team ID
- 缺陷类型 ID
- 评论列表路径模板
- 邮箱
- 密码

邮箱和密码使用密码控件及临时安全缓冲区。连接测试只允许登录和读取授权范围内的元数据；禁止评论、状态修改和其他业务写请求。

### 6.3 单仓与仓库组

单仓配置包括：

- key
- project/iteration 作用域
- repo URL
- 可选本地只读 source
- base branch
- allowed paths
- lint/build/test 命令

仓库组额外包括：

- 唯一 primary
- 每仓 role
- `depends_on`
- 拓扑顺序
- integration test 命令

向导必须拒绝重复 key、循环依赖、非法相对路径、source 路径逃逸和带凭据参数的命令。远端仓库测试只允许使用只读引用查询。本地 source 在测试前后必须保持 HEAD、索引和工作区状态不变。

### 6.4 PR Provider 与 Git 身份

配置：

- GitHub 或 GitLab
- provider host
- HTTPS API URL
- provider token
- Git author/committer name
- Git author/committer email
- 允许的 Git 凭据传输方式

Provider 连接测试只允许认证 GET。Git 身份复用现有严格 UTF-8、控制字符和邮箱格式校验。

### 6.5 Codex

支持：

- 从 Windows 凭据管理器提供 API key/token
- 使用经过验证的现有 Codex 文件登录

界面不得读取或展示文件登录内容。认证测试不得把秘密传入日志、通知、TaskEvent 或错误文本。

### 6.6 私有目录与 Sandbox

配置 run、mirror 和 worktree roots。保存前复用现有 private path 安全边界，验证：

- canonical 路径
- 非 symlink/reparse
- owner
- protected DACL
- 仅受信主体 ACE
- OI/CI
- Full Control

TUI 通过受支持的 Codex CLI/managed-config 读取边界列出已安装的 managed sandbox profiles；不得扫描或解析任意用户文件来猜测 profile。用户只能从列表选择，不允许自由输入不存在的名称。无法可靠枚举时该步骤固定失败，不退化为自由输入。选择后执行现有完整能力探测：

- worktree 内写入成功
- worktree 外写入失败
- 网络不可达
- 敏感环境未进入沙箱
- 测试命令可受控执行

TUI 不创建或修改 profile。

### 6.7 审核并启用

审核页只显示脱敏摘要、字段完成状态和连接测试结果。普通 Enter 不执行保存；用户必须点击明确的“保存并启用”按钮或使用带提示的组合键。

保存完成后立即从持久化存储重新加载，并执行一次完整生产预检。只有预检成功才进入 Dashboard。

## 7. 版本化提交与恢复

Windows Credential Manager 与 JSON 文件不能形成原生事务，因此使用 generation 协议：

1. 生成新的 generation ID。
2. 将新凭据写入该 generation 的独立 target。
3. 原子写入新的非敏感配置文件，引用新 generation。
4. 重新加载并执行完整生产预检。
5. 成功后清理不再引用的旧 generation。
6. 失败时恢复旧配置指针，并删除新 generation 凭据。

如果进程在步骤 2 与步骤 3 之间崩溃，旧配置仍然有效。下次启动检测未被配置引用的凭据 generation，只显示数量并要求用户明确确认后清理，不展示 target 或值。

配置文件损坏、路径不安全或凭据存储不可用时进入受限恢复页。用户可以：

- 恢复上一 generation
- 导入模板创建新档案
- 新建空档案

不得自动覆盖损坏文件。

## 8. 界面设计

配置模式采用三栏布局：

- 左栏：七步导航和 `未配置 / 待验证 / 已通过 / 失败` 状态。
- 中栏：当前步骤表单。
- 右栏：脱敏摘要、测试结果和下一步条件。

窄屏切换为单栏步骤 Screen，不减少字段、测试和审核能力。

交互约束：

- `Tab/Shift+Tab` 导航。
- `Escape` 取消当前编辑，不保存草稿秘密。
- 密码控件不显示明文，不进入复制、帮助、通知或历史。
- 测试连接、保存启用和清理孤立凭据均使用明确按钮。
- 配置模式中不渲染 Dashboard 的工作流动作。

## 9. 错误模型

### 9.1 字段错误

定位具体字段并使用固定文案，例如“Git 邮箱格式无效”。不得回显输入。

### 9.2 连接错误

只显示固定类别：

- 认证失败
- 主机不可达
- TLS/证书验证失败
- 响应格式不兼容
- 超时

不得显示响应正文、URL 中的 userinfo、邮箱、token、密码或底层异常消息。

### 9.3 系统错误

配置文件、ACL、Credential Manager 或 sandbox profile 的系统错误进入受限恢复页。错误必须脱敏，且不能退化到未隔离运行。

## 10. 安全不变量

- 配置阶段零 run、mirror、worktree 和业务远端写。
- 工作流服务图只在完整预检成功后构建。
- 凭据不进入 JSON、日志、通知、TaskEvent、异常或 widget。
- 密码/token 缓冲区在测试、保存、取消和页面卸载后清空引用。
- 所有连接测试都使用明确只读 allowlist。
- 不确定写请求不自动重试。
- 用户配置文件与 private roots 使用同等级别的 nofollow/reparse/owner/DACL 检查。
- 配置变更前关闭旧 Controller；旧服务图和新服务图不能同时活动。
- 非交互 CLI 的现有安全语义不变。

## 11. 测试策略

### 11.1 模型与存储

- generation 原子切换、回滚和崩溃窗口。
- Credential target 隔离与孤立 generation 检测。
- JSON、日志和异常不含秘密。
- 配置文件 nofollow/reparse/identity/ACL。
- 环境与 `.env` 导入必须显式确认。

### 11.2 SetupController

- 每项连接测试只调用允许的只读接口。
- 任一未通过项都不能创建生产 Orchestrator。
- 全部通过后只构建一次服务图。
- 旧 Controller 在重建前关闭。
- 错误类别固定且脱敏。

### 11.3 Textual

- 空配置首次启动。
- 七步键盘、鼠标和窄屏路径。
- 重新编辑、取消、恢复上一 generation。
- 普通 Enter 不保存凭据。
- 全量扫描 widget、renderable、notification、TaskEvent 和日志，不得出现测试秘密。
- 配置模式与 Dashboard 不同时持有生产 Controller。

### 11.4 安全 E2E

- 从空配置启动并显式导入已检测到的 ONES 凭据。
- 配置仓库组、provider、Codex 和 sandbox。
- 连接测试阶段断言零 run/worktree/远端业务写。
- 保存后进入 Dashboard，并执行现有完整工作流。
- 覆盖保存中崩溃、Credential Manager 失败、配置损坏、profile 消失和网络不确定结果。
- wheel/sdist 包含配置界面资源，不包含用户配置、凭据或临时目录。

## 12. 兼容性与迁移

- `ones-dev tui --config <path>` 将该文件视为首次导入模板。
- 已存在用户配置时，TUI 以用户配置为权威，并允许用户显式重新导入模板。
- 非交互命令继续使用现有环境变量和 `--config` 语义。
- 环境变量和 `.env` 不自动删除。
- 现有 Dashboard、向导、危险动作、轮询、恢复和发布流程保持不变。

## 13. 验收标准

- 在示例配置仍含占位符且发布凭据缺失时，`ones-dev tui` 成功进入配置模式，而不是退出。
- 用户可在 TUI 内完成全部生产配置并通过连接测试。
- 完整配置通过后，无需重启进程即可进入 Dashboard。
- 未通过完整预检时无法访问任何工作流、审批或发布操作。
- Windows 凭据管理器之外的持久化文件不含任何秘密。
- 配置测试阶段不存在 run/worktree/commit/push/PR/ONES 评论或状态写。
- 配置保存具有 generation 回滚和崩溃恢复能力。
- 所有现有非交互 CLI 与工作流安全回归继续通过。
