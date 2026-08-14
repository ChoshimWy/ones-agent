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
- Windows 上支持从标准 npm 安装发现 OpenAI 签名的原生 Codex CLI，并把它准备到应用私有运行时目录；整个链路保持 `shell=False` 和结构化参数边界。
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

### 1. Codex 私有运行时准备器

新增 `CodexRuntimePreparer` 和只返回不可变 argv 前缀的 `CodexCommand(prefix: tuple[str, ...])`。所有 doctor、sandbox probe 和正式运行共用该边界，不再各自执行裸字符串 `codex`。

Windows 不执行 npm 的 `node.exe`、`codex.js`、`.cmd`、`.ps1` 或 extensionless shim。Node 会沿入口祖先搜索 `node_modules`；即使入口文件本身安全，可写祖先仍能抢先注入平台包，因此 `node.exe + codex.js` 不属于可接受执行链。

准备器按以下顺序工作：

1. 检查应用私有缓存中是否已有完整验证的原生 Codex CLI。
2. 如无可用缓存，定位标准 npm 固定布局中的原生平台二进制：`node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe`。`codex.cmd` 只用于识别标准安装根，不读取或执行其内容。
3. 使用 Windows `CreateFile` 打开源文件，允许共享读取但禁止共享写入和删除，并在该句柄保持打开期间完成验证和复制。
4. 拒绝非普通文件、reparse、工作区内候选、打开前后身份变化和非固定布局。
5. 使用 Windows `WinVerifyTrust` 验证 Authenticode 信任链；签名发布者必须精确为 `OpenAI OpCo, LLC`。允许证书正常轮换，不固定单一证书指纹。
6. 在 `%LOCALAPPDATA%\ones-dev\codex-runtime` 下创建受保护的 staging 目录。目录禁止继承，只允许当前用户、SYSTEM 和 Administrators 写入。
7. 从已锁定的源句柄流式复制到随机临时文件，同时计算 SHA-256；目标目录命名为 `<sha256>`。
8. 复制完成后再次验证目标文件的 SHA-256、Authenticode、发布者、普通文件身份和 ACL，再以原子替换发布为 `<sha256>\codex.exe`。
9. 使用私有副本执行有界 `--version` smoke；成功后才返回 `CodexCommand((private_codex_exe,))`。

私有缓存包含受保护的最小 manifest：schema version、源稳定身份元数据、目标 SHA-256 和已验证发布者。后续启动在源稳定身份未变化时复核私有副本的 ACL、哈希和签名后直接复用，不重复复制。源发生变化时先完整准备新版本；新版本发布成功前继续保留旧的可信版本。没有安装源但已有通过复核的私有副本时，允许继续使用该副本。

准备器不调用 shell、不拼接命令字符串、不修改 `PATH`，不修改 npm/NVM 文件或 ACL，也不从项目工作区接受任意可执行文件。解析、签名、复制或 smoke 失败返回固定、脱敏的“Codex CLI 不可用”类别。

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
4. Controller 调用统一运行时准备器。若尚无可信私有副本，TUI 的确认摘要明确显示将从已安装的 OpenAI 签名二进制准备约 299 MB 的私有副本。
5. 准备器锁定源、验证签名、流式复制、保护 ACL、复核目标并运行 `--version` smoke。
6. Controller 使用准备器返回的同一 `CodexCommand` 和固定 descriptor 执行真实能力探测。
7. 探测成功后，Controller 原子保存 `{name, source}` 到 draft；页面自动选中并解锁后续步骤。
8. 最终配置保存时，SetupStore 只持久化 profile 来源和名称，不持久化外部可执行路径。
9. 运行时加载后重新复核私有副本，并用同一命令构造器执行 preflight 和业务命令。

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
- Codex CLI 无法定位、签名无效、发布者不匹配、私有缓存不安全或 smoke 失败：固定提示“Codex CLI is unavailable”，允许 Retry。
- 用户取消、TUI 退出或页面卸载：取消 UI 等待并收割后台准备任务；未原子发布的临时文件不成为可用运行时。
- 源在复制期间被写入、替换或删除：锁定句柄或身份复核失败，固定拒绝；不使用部分副本。
- staging 写入、磁盘空间、签名复核或原子替换失败：逐项尝试清理本次随机临时文件；清理失败仍不发布 manifest，不影响既有可信版本。
- Git 不可用：继续使用已有的 `git_unavailable` 可恢复分类，不与 profile 错误混淆。
- capability probe 失败：固定提示“Safe workspace profile could not be verified”。
- 超时或取消：保留操作所有权直到后台任务完成或被受管收割；不报告虚假成功。
- 页面卸载、TUI 关闭或重新配置：清除临时选择和任务引用，不保留命令环境或异常对象。
- 已有配置加载：名称和来源必须严格匹配；旧配置缺少来源时按 `managed` 迁移，但仍必须重新通过 managed catalog 验证，绝不自动转换为内置 profile。

所有面向用户的错误不保留原始 `cause`/`context`，不得包含路径、用户名、环境变量值或命令输出。

## 安全约束

- 内置定义不可编辑，不能从导入源或环境覆盖。
- 所有子进程保持 `shell=False`。
- 不执行 Node、JavaScript、`.cmd`、`.ps1`、extensionless shim 或工作区内候选。
- npm 源目录即使具有宽松 ACL 也不会被直接执行；只有在禁止并发写/删的锁定句柄上验证有效 OpenAI Authenticode 后，内容才可进入私有 staging。
- 签名检查使用 Windows `WinVerifyTrust` API，不调用 PowerShell；必须同时满足有效系统信任链和固定 OpenAI 发布者身份。
- 私有运行时根及所有组件拒绝 reparse，使用受保护 DACL，并在每次使用前核对身份、ACL、哈希和签名。
- 复制采用固定大小缓冲区并在异常出口释放句柄；最终文件只通过同目录原子替换发布。
- manifest 不存储外部路径、命令输出或凭据；不把不可信 manifest 字段用作文件操作目标。
- 不写 `~/.codex/config.toml`，不改变其 ACL。
- profile 来源参与持久化校验、测试和 runtime 构造，不能仅用字符串名称推断。
- capability probe 和正式 runtime 使用同一 executor factory。

## 测试设计

### 单元测试

- Profile descriptor 的合法组合、未知来源、名称冲突和旧配置迁移。
- Windows 固定 npm 原生二进制布局、恶意/异常 shim、路径别名、reparse、身份竞态和无 Codex。
- `WinVerifyTrust` 成功、无效签名、错误发布者、证书轮换和 API 失败；普通错误固定脱敏，Memory/control-flow 原样传播。
- 源句柄禁止共享写/删；复制期间替换/截断、哈希不一致、短写、磁盘不足、目标签名失败和原子替换失败。
- 私有目录 DACL、祖先 reparse、目标篡改、manifest 篡改和随机临时文件清理。
- 首次约 299 MB 复制、相同源复用缓存、源变化准备新版本、失败保留旧版本、仅缓存可用时启动。
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
- 使用真实安装的 OpenAI 签名原生 Codex，完成私有准备、`--version` smoke 和内置 profile probe；测试不得执行 node/JS/shim。
- 完成向导、保存、重启后能按来源重建同一 sandbox 配置。
- 验证用户 Codex 配置文件内容、身份和 ACL 前后不变。
- 验证 npm/NVM 安装目录内容和 ACL 前后不变，所有正式命令只使用私有副本。
- 正式 runtime 的 preflight 与配置步骤使用相同 argv/profile descriptor。

## 验收标准

1. `uv run ones-dev tui` 在没有预装 managed profile 时仍可操作。
2. 用户明确确认后可以在 TUI 内启用并验证固定安全工作区 profile。
3. 未确认或验证失败时不能进入后续步骤。
4. 用户 Codex 配置文件及 ACL 不被修改。
5. Windows npm 安装中的原生 Codex 经有效 OpenAI Authenticode 验证后被复制到受保护私有目录并执行；整个链路不使用 Node、JavaScript、shim 或 shell。
6. 保存并重启后使用同一 profile 来源和权限语义。
7. 外部 managed profiles、Git 不可用恢复以及现有首次配置流程无回归。
