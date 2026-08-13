# TUI 不安全 `.env` 启动降级设计

## 背景

`ones-dev tui` 启动时会探测当前目录的可选 `.env` 导入来源。Windows 上若该文件继承了宽松 ACL，安全导入器会正确拒绝读取；当前生产 host 却把这一可选来源的拒绝升级为整个命令失败，并只输出 `error: command failed safely`。这违反了 TUI 在没有预配置或可用导入源时仍应进入配置向导的要求。

## 目标

- 不安全、不可读取或竞态变化的可选 `.env` 不得阻止 TUI 启动。
- 对被拒绝的 `.env` 保持 fail-closed：不读取内容、不保留路径供后续导入、不显示为可选来源。
- 环境变量和公开模板的安全检测语义保持不变。
- 显式选择 `.env` 后的即时重读仍执行现有严格 ACL、nofollow、identity 与大小校验。
- 显式提供的无效公开模板继续使启动安全失败；本变更只处理隐式、可选 `.env` 探测。

## 设计

生产 TUI host 在探测导入来源时分别处理环境变量、可选 `.env` 和公开模板：

1. 环境变量继续只检测允许列表中的凭据种类，不复制完整环境到长期上下文。
2. 对当前目录 `.env` 执行现有安全探测。
3. 若 `.env` 探测抛出 `SetupImportError`，host 将该来源降级为不可用：检测结果中的 `dotenv` 为空，`SetupImportContext.dotenv_path` 为 `None`。
4. 不捕获 `KeyboardInterrupt`、`SystemExit` 等控制流异常。
5. 公开模板仍按现有规则加载；显式模板的路径或内容不安全时继续返回固定安全错误。

该设计不会放宽文件权限要求，也不会吞掉用户明确选择导入后的错误。它只把“隐式可选来源不可用”从 host 致命错误转换为向导可继续的缺省状态。

## 错误与安全边界

- 被拒绝 `.env` 的内容、路径细节和底层异常链不得进入终端、日志、Rich widget 或 import context。
- 不安全 `.env` 不得出现在导入对话框的可选来源计数中。
- 后续不得通过旧路径重复读取该文件。
- 其他 host 初始化失败仍统一输出固定的 `error: command failed safely`。

## 测试

采用 TDD 增加生产 host 回归：

1. 创建存在但 ACL 不满足私有要求的 `.env`，确认旧实现先失败。
2. 修复后确认 host 构建成功，`detection.dotenv == ()` 且 `dotenv_path is None`。
3. 确认 `.env` 中的 canary 不出现在公开对象或异常表示中。
4. 保留现有安全 `.env` 检测与导入测试，证明安全来源仍可选择。
5. 运行 CLI、setup import、TUI bootstrap 聚焦回归，并实际启动 `uv run ones-dev tui` 验证不再立即返回错误。

