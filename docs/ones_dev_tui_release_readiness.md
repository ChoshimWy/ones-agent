# ONES Dev TUI 上线检查

## 功能矩阵

| 能力 | 当前状态 | 入口 |
| --- | --- | --- |
| ONES 地址、团队、账号密码 | 已完成 | 配置向导的 ONES 步骤可独立打开；项目/状态/Work item 仅为可选只读探测 |
| Git provider、仓库和 Codex | 已完成 | Provider、Repositories、Private paths 可独立配置；Codex 直接使用本机 CLI，不再进入配置向导 |
| 多文件夹工作区 | 已完成 | Workspace folders：新增、选择编辑、移除、仓库组 |
| 获取缺陷 | 已完成 | Defects 列表按项目、迭代、负责人和状态查询；列表项直接启动分析 |
| 获取需求 | 已完成 | Requirements 列表按项目、迭代、负责人、状态和需求类型查询；列表项直接启动分析，仍保留 ID 入口 |
| 分析、实现、修复 | 已完成 | 列表启动后进入隔离工作流；分析完成后使用 `Revise → implementation/repair` 进入实现或修复 |
| 审核、发布和恢复 | 已完成 | Dashboard 的显式确认、批准、恢复和取消动作 |

## 发布前验证

```powershell
$tuiTests = Get-ChildItem .\tests -File -Filter 'test_developer_workflow_tui*.py' |
    ForEach-Object { $_.FullName }
uv run pytest $tuiTests -q --basetemp="$PWD\.pytest-tmp"
uv build --wheel --sdist --out-dir .tmp/dist-check
uv run ones-dev --help
```

发行包必须包含 `src/developer_workflow/tui/tui.tcss`，不得包含 `.env`、用户凭据、`data/`、测试文件或内部 `AGENTS.md` 指引。

## 环境要求

生产 Windows 环境必须满足：

- 用户配置目录具备受保护 ACL；
- Windows Credential Manager 可用；
- Codex CLI、managed sandbox profile 和完整能力探测可用；可先运行 `codex doctor --summary --no-color`，再用内置 profile 执行一次受限命令：`codex -c 'permissions.ones-dev-workspace.extends=":workspace"' sandbox -P ones-dev-workspace --include-managed-config -C <worktree> -- C:\\Windows\\System32\\cmd.exe /c echo ok`；
- ONES 与 Git provider 使用受信 TLS 端点；
- 运行根、镜像根和 worktree 根均为私有目录。

沙箱能力探测或私有目录 ACL 失败时，TUI 必须停留在受限配置/恢复界面，不能降级为未隔离运行。

## ONES 连接排查

- Base URL 填站点根地址，不要填写 `/identity/api` 或 `/project/api` 等接口路径。
- Team ID 和缺陷类型 ID 必须来自当前 ONES 团队；账号必须具备读取项目元数据的权限。
- Project、Status、Work item ID 只用于可选只读探测，可以全部留空。
- 认证失败检查账号密码；主机不可达检查网络/DNS；TLS 失败检查证书；响应不兼容检查站点版本和 Base URL。
