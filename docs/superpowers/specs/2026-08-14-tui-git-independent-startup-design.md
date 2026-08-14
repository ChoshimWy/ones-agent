# TUI 无 Git 依赖启动设计

## 背景

`uv run ones-dev tui` 在当前终端找不到 `git.exe` 时，会在界面显示前退出并只输出 `error: command failed safely`。根因不是 TUI 配置内容，而是导入 `src.services.ones_gateway` 时先执行 `src.services.__init__`；该聚合模块立即导入 `ExecutionService`，继而加载 `git_ops` 和 GitPython。GitPython 在模块导入阶段检查 Git 可执行文件，因此一个尚未使用仓库功能的配置界面被 Git 环境阻断。

## 目标

- 未安装 Git、Git 不在 `PATH`、且未设置 `GIT_PYTHON_GIT_EXECUTABLE` 时，TUI 仍能进入配置界面。
- 只有用户执行仓库验证时才检查 Git 能力。
- Git 不可用时显示固定、脱敏、可恢复的验证错误；TUI 不退出，用户可以返回并重试。
- 保持 `from src.services import ...` 的公开导入兼容性。
- 不自动搜索或信任 Windows 常见安装路径，不修改进程 `PATH`。

## 设计

### 服务聚合模块延迟加载

`src.services` 保留现有 `__all__` 与公开符号，但不在包初始化时导入所有服务。模块通过显式名称到“模块 + 属性”的映射和 `__getattr__` 按需加载目标服务，并缓存成功结果。

延迟加载必须线程安全，避免并发访问同一服务符号时创建不一致的模块状态。未知名称继续抛出标准 `AttributeError`，不吞掉目标模块自身的导入错误。

这样，`runtime_bootstrap` 导入 `src.services.ones_gateway` 时不再附带加载 `ExecutionService` 和 GitPython；主 API 仍可按原方式从 `src.services` 导入所需服务。

### Git 能力错误边界

仓库配置和验证仍以 Git 为必需能力。真正进入仓库验证时，如果 GitPython 因找不到可执行文件而无法初始化，边界层把该内部异常转换为固定的验证失败，例如 `Git executable is unavailable`。

错误文本不得包含原始异常消息、环境变量值、搜索路径或异常链。该失败仅影响当前仓库步骤，不关闭 SetupController、Supervisor 或 TUI；用户修复环境后可以重新执行测试。

### 数据与控制流

1. CLI 构造私有 Setup host。
2. TUI 导入运行时和 ONES 组件；`src.services` 不加载 Git 相关服务。
3. 配置界面正常显示。
4. 用户测试仓库配置。
5. 仓库边界首次加载 Git 能力：
   - 可用：继续现有只读快照和远端验证。
   - 不可用：返回固定失败状态，界面保持可操作。

## 安全约束

- 不通过扫描磁盘、注册表或常见目录自动选择 `git.exe`。
- 不修改全局或进程级 `PATH`。
- 不将 GitPython 原始 ImportError、路径或环境内容放入 UI、日志或异常链。
- Git 缺失只延迟到需要该能力的步骤，不允许绕过仓库安全验证或把失败视为通过。

## 测试策略

- 冷进程移除 Git 的 `PATH` 与 `GIT_PYTHON_GIT_EXECUTABLE` 后，导入 TUI 和构造/启动 setup host 成功。
- 同一环境下执行仓库验证，得到固定、脱敏、可恢复的失败结果。
- 验证失败后仍可修改字段并重试，不关闭 TUI 生命周期。
- `from src.services import ExecutionService` 等原有公开导入保持兼容；真正请求 `ExecutionService` 时仍严格暴露 Git 缺失。
- Git 可用环境下现有仓库验证、TUI bootstrap、CLI 和服务测试保持通过。

## 非目标

- 自动安装 Git。
- 自动发现或选择系统上的 Git 副本。
- 允许没有 Git 的配置通过仓库验证或激活生产 runtime。
- 重构 Git 操作实现或替换 GitPython。
