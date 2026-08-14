# TUI 运行时安全权限配置设计

## 背景

`ones-dev tui` 已能在 Git 不可用时启动，但首次启动仍可能停在 `Profile` 步骤：界面只允许选择预先安装的 Codex managed permission profile；当目录为空或目录探测失败时，选择框和后续按钮全部禁用。

这与产品目标冲突：用户应能先启动 TUI，再在运行中完成配置，不应被要求提前编辑 Codex 配置文件或安装管理员配置。

## 目标

- 没有预装 managed profile 时，TUI 仍提供可完成的首次配置路径。
- 用户必须明确确认后，才能启用应用内置的安全工作区权限配置。
- 内置配置只能使用固定的 Codex workspace 权限语义，不能由用户自由扩大。
- 配置必须经过真实能力探测，探测成功后才能进入后续步骤。
- 不修改用户的 `~/.codex/config.toml`，不修复或放宽其 ACL，也不改变 `PATH`。
- Windows 上支持通过 npm shim 安装的 Codex，同时保持 `shell=False` 和结构化参数边界。
- 已有的可信 managed profile 发现、选择和验证流程继续工作。

## 非目标

- 不在 TUI 中提供通用 Codex TOML 编辑器。
- 不允许用户自定义内置 profile 的 filesystem、network 或其他权限字段。
- 不自动提升权限，不写入 `ProgramData` 管理员目录。
- 不绕过现有 sandbox capability probe，也不把探测失败当作成功。
- 不解决与 Profile 步骤无关的 ONES、Provider、Codex 凭据配置问题。

## 方案比较

### 方案 A：应用管理的固定 profile（采用）

TUI 在用户确认后启用固定标识的 `ones-dev-workspace` profile。运行 Codex 时通过结构化配置覆盖提供固定定义，并仍用 `--permission-profile` 选择该命名 profile。

优点：不修改用户配置；无需管理员权限；权限定义可审计；首次运行可完成。缺点：运行时需要携带 profile 来源信息，并统一构造 Codex 命令。

### 方案 B：修改用户 `config.toml`

向 `~/.codex/config.toml` 合并 `[permissions.ones-dev-workspace]`。

优点：配置对 Codex CLI 原生可见。缺点：必须安全保留未知 TOML、处理并发写和回滚，还会受到用户目录 ACL 的影响；可能破坏现有配置，因此不采用。

### 方案 C：写管理员 profile catalog

把固定 profile 写入 `ProgramData/ones-dev`。

优点：集中管理。缺点：需要管理员权限，而且仍属于启动前配置，不符合需求，因此不采用。

## 核心模型

### Profile 来源

持久化配置不能只保存一个裸 profile 名称。新增严格枚举来源：

- `managed`：来自现有用户或管理员可信目录。
- `builtin_workspace`：由应用提供的固定工作区 profile。

工作流配置保存 profile 名称和来源。`builtin_workspace` 只允许固定名称 `ones-dev-workspace`；`managed` 继续使用现有命名和目录校验规则。任何未知来源、名称与来源不匹配、缺字段或多字段都固定拒绝。

这样可以避免仅凭名称推断来源，也避免用户配置中同名 profile 与内置定义发生混淆。

### 固定内置定义

内置 profile 的定义是代码常量，等价于：

```toml
[permissions.ones-dev-workspace]
extends = ":workspace"
```

运行时通过 Codex 的结构化 `-c` 参数注入该定义，再使用 `--permission-profile ones-dev-workspace`。定义不可从表单、环境变量、模板或持久化 JSON 覆盖。

## 组件设计

### 1. Codex 命令解析器

新增只返回不可变 argv 前缀的解析边界，例如 `CodexCommand(prefix: tuple[str, ...])`。所有 doctor、sandbox probe 和正式运行共用该边界，不再各自执行裸字符串 `codex`。

Windows 解析顺序：

1. 可直接安全执行的受信任 `codex.exe`。
2. 标准 npm 安装布局：定位 `codex.cmd` 仅用于识别安装根，不执行该脚本；验证相邻 Node 可执行文件和 `node_modules/@openai/codex/bin/codex.js` 的规范路径、普通文件和稳定身份，然后构造 `[node.exe, codex.js]`。

解析器不调用 shell、不拼接命令字符串、不修改 `PATH`，也不从项目工作区接受任意可执行文件。解析失败返回固定、脱敏的“Codex CLI 不可用”类别。

### 2. Profile catalog

现有 `ManagedProfileCatalog` 保持只返回经过真实探测的外部 managed profiles。新增独立的内置 profile 描述，不把它伪装成已安装 profile。

Controller 对外提供冻结的 profile 选项，每项包含显示名称、持久化名称和来源。目录加载失败时：

- 外部列表为空并显示脱敏提示；
- 内置 profile 的“创建安全工作区配置”动作仍可用；
- 不把目录错误自动转成内置 profile 成功。

### 3. TUI Profile 页面

页面状态分为：

- `loading`：加载可信外部 profiles。
- `selectable`：可选择一个已经探测通过的外部 profile。
- `unconfigured`：没有可用 profile，显示“创建安全工作区配置”。
- `confirming`：显示固定权限摘要和确认/返回按钮，普通 Enter 不执行确认。
- `probing`：禁用重复操作，可取消等待，但不能把迟到结果提交到已卸载页面。
- `ready`：自动选中内置 profile，启用 Test/Next。
- `failed`：显示固定错误和 Retry，仍停留在 Profile 步骤。

确认对话只展示固定定义的含义：允许 Codex 在工作区内完成任务；工作区外写入和网络访问继续由 probe 验证。界面不显示命令行、环境值或原始异常。

### 4. Controller 事务

新增“确认并探测内置 profile”异步操作，复用 Controller 的 operation lock、revision CAS、关闭状态和取消收割机制：

1. 记录当前 draft revision。
2. 在受管后台操作中构造固定 profile runner。
3. 在私有临时目录执行完整 capability probe。
4. 返回 UI 后检查 controller revision、页面 generation 和 attached 状态。
5. 仅在全部一致且 probe 成功时，原子写入 profile 名称与来源，并按 `PROFILE` 步骤使下游验证结果失效。

取消、页面卸载、Controller 关闭或 revision 改变都不会提交迟到结果。

### 5. Sandbox 与 Runtime

`SandboxCommandExecutor` 接受结构化的 Codex argv 前缀和严格 profile descriptor，而不是可执行字符串。对于：

- `managed`：沿用现有命令，不注入 profile 定义。
- `builtin_workspace`：追加固定 `-c` profile 定义，再选择固定 profile。

Profile 测试、RuntimeBootstrapper preflight、Repository/Test runner 必须走同一构造函数，防止“配置步骤通过但正式运行使用不同命令”。

## 数据流

1. TUI 启动并加载外部 profile 目录。
2. 目录为空或不可用时，用户点击“创建安全工作区配置”。
3. TUI 展示固定权限摘要；用户点击明确的 Confirm。
4. Controller 使用固定 descriptor 和统一 Codex 命令解析器执行真实能力探测。
5. 探测成功后，Controller 原子保存 `{name, source}` 到 draft；页面自动选中并解锁后续步骤。
6. 最终配置保存时，SetupStore 持久化来源和名称。
7. 运行时加载后严格校验 descriptor，并用同一命令构造器执行 preflight 和业务命令。

## 能力探测

内置 profile 必须通过现有安全门，至少覆盖：

- 私有临时工作区内允许预期的受限写入。
- 工作区外写入被拒绝。
- 网络访问被拒绝。
- 敏感环境变量不进入 sandbox 子进程。
- 子命令退出码、超时和输出上限严格处理。

任一项失败都不写入 draft，不启用 Next，不降级为跳过验证。

## 错误与生命周期

- 用户取消确认：无状态变化、无 profile 写入。
- Codex CLI 无法解析：固定提示“Codex CLI is unavailable”，允许 Retry。
- Git 不可用：继续使用已有的 `git_unavailable` 可恢复分类，不与 profile 错误混淆。
- capability probe 失败：固定提示“Safe workspace profile could not be verified”。
- 超时或取消：保留操作所有权直到后台任务完成或被受管收割；不报告虚假成功。
- 页面卸载、TUI 关闭或重新配置：清除临时选择和任务引用，不保留命令环境或异常对象。
- 已有配置加载：名称和来源必须严格匹配；旧配置缺少来源时按 `managed` 迁移，但仍必须重新通过 managed catalog 验证，绝不自动转换为内置 profile。

所有面向用户的错误不保留原始 `cause`/`context`，不得包含路径、用户名、环境变量值或命令输出。

## 安全约束

- 内置定义不可编辑，不能从导入源或环境覆盖。
- 所有子进程保持 `shell=False`。
- 不执行 `.cmd`、`.ps1` 或工作区内 shim。
- 解析和执行之间核对可执行文件/入口脚本身份，竞态时失败关闭。
- 不写 `~/.codex/config.toml`，不改变其 ACL。
- profile 来源参与持久化校验、测试和 runtime 构造，不能仅用字符串名称推断。
- capability probe 和正式 runtime 使用同一 executor factory。

## 测试设计

### 单元测试

- Profile descriptor 的合法组合、未知来源、名称冲突和旧配置迁移。
- Windows 直接 exe、标准 npm 布局、恶意/异常 shim、路径竞态和无 Codex。
- 内置 profile 生成精确 argv，保持 `shell=False`，无用户输入拼接。
- managed 与 builtin 两种来源使用正确命令，不互相回退。
- 公开异常消息及 `cause/context` 脱敏。

### Controller 测试

- Confirm 前零 mutation、Cancel 零 mutation。
- probe 成功后一次原子提交并正确失效下游步骤。
- probe 失败、超时、取消、revision 冲突、关闭竞态均不提交。
- 迟到任务被收割，不更新 detached UI。

### TUI 测试

- 空目录时显示创建入口而非死锁页面。
- 确认、返回、重试和重复点击行为。
- 普通 Enter 不确认危险动作。
- 成功后自动选择并解锁 Test/Next。
- 外部 profiles 仍可选择，两个 profile Select 保持同步。
- 固定错误不泄露路径、环境或异常 canary。

### 集成测试

- 无预装 profile、无 Git 的冷启动能够进入可操作 Profile 页面。
- 使用真实 Codex CLI 解析边界完成内置 profile probe。
- 完成向导、保存、重启后能按来源重建同一 sandbox 配置。
- 验证用户 Codex 配置文件内容、身份和 ACL 前后不变。
- 正式 runtime 的 preflight 与配置步骤使用相同 argv/profile descriptor。

## 验收标准

1. `uv run ones-dev tui` 在没有预装 managed profile 时仍可操作。
2. 用户明确确认后可以在 TUI 内启用并验证固定安全工作区 profile。
3. 未确认或验证失败时不能进入后续步骤。
4. 用户 Codex 配置文件及 ACL 不被修改。
5. Windows npm 安装的 Codex 可被安全执行，整个链路不使用 shell。
6. 保存并重启后使用同一 profile 来源和权限语义。
7. 外部 managed profiles、Git 不可用恢复以及现有首次配置流程无回归。
