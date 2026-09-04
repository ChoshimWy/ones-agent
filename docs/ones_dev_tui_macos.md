# macOS TUI 启动与验收

## 范围

TUI 使用同一套 Textual 界面；本次适配 macOS 配置路径、Keychain 凭据存储、
原生 Codex 发现/签名/私有缓存和 Git 凭据通道。Apple Silicon 使用 arm64
payload，Intel 使用 x86_64 payload；不执行 npm 的 JavaScript 启动器。

当前开发机为 Windows：模拟适配测试不能替代真实 macOS Keychain、codesign、
终端交互和 Codex 登录验收。执行下面的实机清单后才可宣称对应机器验证通过。

## 启动

1. 安装 Python 3.11+、uv、Git。确认终端中 `git --version` 和 `uv --version` 可用。
2. 按 [OpenAI Codex CLI 文档](https://learn.chatgpt.com/docs/codex/cli) 安装 Codex，
   在本机终端完成登录并确认 `codex --version` 可运行。支持原生可执行文件、
   npm 原生平台包，以及 Homebrew 链接最终指向的原生文件。
3. 将 ones-agent 放到当前用户可写的目录，在该项目根目录执行：

```sh
uv sync
uv run ones-dev tui
```

建议终端至少 120 列 × 32 行并使用支持中文的等宽字体；小窗口可滚动操作。
不需要 Windows、PowerShell 或管理员权限。应用仍会校验本机 Codex 认证来源；
现有认证契约读取 `CODEX_HOME/auth.json`（未指定时为 `~/.codex/auth.json`），
仅存在 Codex Keychain 登录、没有此文件时不能视为已经通过该契约。
不要复制其他人的凭据，也不要把认证内容填写进命令行。

## 数据与安全

| 数据 | macOS 位置 / 行为 |
| --- | --- |
| 非敏感配置 | `~/Library/Application Support/ones-dev/config.json` |
| ONES 等凭据 | 系统 Keychain，服务 `ones-dev.credentials`，按 profile / generation 分隔 |
| 原生程序缓存 | `~/Library/Application Support/ones-dev/codex-runtime/<SHA-256>/codex` |
| 默认工作流数据 | 项目所在目录的上一级 `.ones-dev-runtime`，以界面显示的配置为准 |

配置目录由系统账号信息定位，不信任任务传入的 HOME；私有目录 0700、配置文件
0600、缓存可执行文件 0700。Keychain 不可用或访问被拒绝时不会回退成明文文件。
不要将 Windows 配置中的盘符路径直接复制到 Mac；仓库与节点路径应在 Mac 重新配置。

原生程序须为 Mach-O，且通过 Apple 信任链和 OpenAI 组织签名校验；缓存继续绑定
内容 SHA-256、文件身份、manifest，并在执行前重验。不支持无签名/临时签名的
自编译 Codex，不以关闭 Gatekeeper、重签名或跳过校验作为解决办法。安装路径不能
位于任务仓库内，也不能允许其他用户写入。已有 code-mode companion 同样须验签。

Git HTTPS 使用 Git 的 `osxkeychain` helper，保留已存储凭据；不会继承任意全局
helper 或 hooks。SSH 只使用本机账号已有的 `known_hosts`、标准命名密钥，保持
严格主机校验，不加载 `~/.ssh/config`、代理命令或任意 agent。使用自定义主机别名、
非标准密钥或交互式认证的仓库，需要在现有凭据配置通道显式配置；不会自动接受指纹。

## 实机验收清单

- `uv sync` 成功，`uv run ones-dev tui` 打开配置页，不出现 Windows API 错误。
- 配置 ONES 后保存，退出并重新打开，配置可恢复且密码不回显；Keychain 拒绝访问时提示失败。
- 创建工作区、查询缺陷、浏览 Configuration 各 tab，中文和滚动正常。
- 本机已登录 Codex 的情况下，启动一次只读分析，确认原生缓存、签名检查和退出清理成功。
- 在专用测试仓库验证 Git HTTPS/SSH 读取；不要为了平台验收自动推送生产仓库。
- 在 Apple Silicon / Intel 各自执行平台测试：

```sh
uv run pytest -q tests/test_developer_workflow_macos.py
```

人工/实机验证待完成仍可按既有规则交接 Draft PR；这不代表验证已通过，也不授予合并/发布权限。
