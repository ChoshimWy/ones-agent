# ONES 开发工作流 CLI 运维指南

## 全屏终端界面

使用与非交互 CLI 相同的生产装配启动全屏 Textual 控制台：

```powershell
uv run ones-dev tui --config docs/examples/ones-dev.config.json
```

`--config` 指向的示例文件只用于导入，不会被改写；其中的标识符和地址仍是占位符。
首次配置或已保存配置不完整时，TUI 会进入受限配置模式，而不是要求用户先在终端中
准备完整配置。配置过程依次包含七个步骤：托管 profile、ONES、仓库与仓库组、Git
provider、Codex、私有目录和 Review。每一步只执行对应的只读连接或能力检查；Review
确认并再次显式确认“保存并激活”之前，不会构建生产 Orchestrator，也不会创建 run、
mirror、worktree、commit、push、PR 或写入 ONES。

ONES、provider、Codex 和 Git 传输凭据保存在 Windows Credential Manager；版本化配置
文件只保存非敏感策略。sandbox 与 Codex 只能选择管理员预先安装并被探测通过的托管
profile。全部检查通过后无需重启即可进入 Dashboard。Dashboard 的 `Configure runtime`
可先关闭现有 runtime，再进入重新配置；取消会恢复之前的稳定 generation。若进程在
激活中断，下一次启动会显示恢复页面；恢复旧 generation、丢弃未完成 generation 和
清理孤立凭据均要求二次确认。任何错误、通知、Rich renderable 或 TaskEvent 都只显示
固定类别，不回显凭据、认证邮箱、私有路径或外部错误文本。

配置向导支持键盘和鼠标。`Tab`/`Shift+Tab` 在字段和操作间移动，`Ctrl+Enter` 执行
当前步骤的连接测试或在 Review 请求激活确认，`Escape` 取消当前编辑并清除瞬态凭据；
普通 `Enter` 不会保存凭据、激活 runtime 或执行恢复操作。

非交互 CLI 保持原有兼容语义：继续从受控进程环境变量以及命令指定的 `--config`
装配 runtime，不读取 TUI 的 Credential Manager generation，也不会自动改写或删除
环境变量和 `.env`。环境或 `.env` 中检测到的凭据只有在配置向导中明确选择来源并确认
导入后，才会进入本次编辑事务。

发行物验证使用仓库外的新建 venv。`pip install --no-deps <wheel>` 只验证 wheel 自身
内容，不表示该 wheel 可在没有依赖的 Python 中独立运行；导入和 `ones-dev tui --help`
smoke 必须显式使用已安装的只读第三方依赖（例如新 venv 的
`--system-site-packages`，或仅把既有 venv 的 `site-packages` 加入测试进程搜索路径）。
验证脚本同时断言 `runtime_bootstrap`、setup screen、`main` 的 `__file__` 都位于新 venv
安装目录，当前工作目录位于仓库外，并从安装包读取 TUI CSS、workflow schema 与 planner
prompt，避免误用源码树掩盖缺失资源。

常用键：

- `n`：新建需求或缺陷工作流；缺陷查询的状态值必须是 ONES 工作流状态 ID，多个 ID 用英文逗号分隔，不按状态名称匹配。
- `r`：恢复当前 run；`v`：修订；`a`：打开审批确认；`x`：打开取消确认。
- `q`：仅退出界面，不取消、回滚或修改任何工作流状态。
- `j`/`k` 或方向键移动，`Enter` 打开详情，`Tab`/`Shift+Tab` 切换证据页签。
- `/` 按 run ID 或 work item ID 搜索；`f` 按状态、工作流类型、work item ID 和 ISO 8601 更新时间范围过滤。筛选界面的 `Apply` 才生效，`Clear` 恢复完整列表，`Escape` 保留原筛选。
- `?` 从 Dashboard 打开固定的安全帮助页，`Escape` 返回。帮助、搜索和筛选均只读，不读取或显示环境变量、凭据与本地路径。
- 左侧 `Defects` 直接进入缺陷向导；在点击 `Query defects` 前不会查询 ONES、创建 run 或 worktree。

不同 run 最多并行 `tui_max_concurrency` 个任务，超过上限按提交顺序排队；同一
run 的 mutation 始终 FIFO 串行，底层 operation lock 和 version CAS 仍是最终门禁。
退出不会把后台任务解释为取消；已持久化检查点保留在私有 `run_root`，再次启动后
只从 `FileRunStore` 恢复。异常退出后可先查看详情，再使用 `r` 从允许的检查点继续。

配置 `source_path` 时，本地 source workspace 始终只读；镜像、修改、测试、commit
和 push 只发生在隔离的 managed worktree。一个工作项可映射到有向无环的
`repository_groups`：依赖仓库按拓扑顺序处理，每个有改动的仓库分别创建一个 commit、
push 和 PR，全部 PR 确认后才向 ONES 写入一条汇总评论；工作流不会自动修改 ONES
状态。进入 `WAITING_APPROVAL` 前 commit、push、PR 和评论均为零，审批确认时会重新
加载权威 version 和签名证据，漂移时保持零远端副作用。

## 多仓库工作流

一个需求或缺陷可以选择一个 `repository_groups` 映射。组内必须且只能有一个
`primary` 仓库；依赖关系通过 `depends_on` 表达，且必须是无环拓扑。`--mapping`
仍只接收一个键，但该键可以是单仓映射，也可以是仓库组：

```powershell
uv run ones-dev defect `
  --project <ONES_PROJECT_ID> `
  --iteration <ONES_ITERATION_ID> `
  --assignee <ONES_ASSIGNEE_ID> `
  --select <DEFECT_UUID> `
  --mapping desktop-suite
```

`source_path` 可选，只用于从现有本地仓库读取对象并创建隔离 mirror/worktree；工作流
会验证源仓库的 HEAD、索引和状态在前后完全不变，绝不会把本地工作区当作发布目录。
`repo_url` 始终必填，并且仍是远端基线、push 和 PR 的权威地址。每个仓库被放在同一
运行目录下的固定同级子目录中，仓库键和相对路径都经过独立校验，不能跨仓访问。

确认映射时 CLI 按拓扑顺序显示仓库、角色、基线、本地只读源和远端 URL。所有仓库的
lint/build/test 依次通过后，才会在主仓库执行 `integration_test_commands`。人工审批
指纹一次性绑定全部仓库的基线、HEAD、diff、测试、commit message 和 PR 文案。

批准后，Publisher 先为所有有改动的仓库准备并持久化本地 commit，再按拓扑顺序执行
push 和创建 PR。每个有改动的仓库各有一个 commit 和一个 PR；全部 PR 完成后只向
ONES 写一条汇总评论，绝不自动修改 ONES 状态。若中途失败，状态为
`PARTIAL_SUCCESS`；先运行 `uv run ones-dev show <run-id>` 检查事实，再执行
`uv run ones-dev resume <run-id>`，只会继续尚未完成的仓库。已创建的 PR 不会自动回滚。

`ones-dev` 是独立于现有 FastAPI、前端和调度器的本地开发工作流。它读取 ONES 需求/Wiki 或指定迭代中的缺陷，在隔离 worktree 中调用 Codex、执行配置测试并生成人工审批包。人工批准前不会 commit、push、创建 PR 或评论 ONES；它从不自动修改 ONES 状态。

## 配置

配置文件只保存非敏感策略。以下值均为占位符：

```json
{
  "run_root": "D:/private/ones-dev/runs",
  "worktree_root": "D:/private/ones-dev/worktrees",
  "mirror_root": "D:/private/ones-dev/mirrors",
  "sandbox_permission_profile": "managed-ones-worktree",
  "max_codex_attempts": 3,
  "repositories": [
    {
      "key": "product-app",
      "project_id": "<ONES_PROJECT_ID>",
      "iteration_id": "<ONES_ITERATION_ID>",
      "repo_url": "https://git.example.invalid/team/product-app.git",
      "repo_name": "product-app",
      "base_branch": "main",
      "test_commands": ["uv run pytest -q"],
      "lint_commands": ["uv run ruff check ."],
      "build_commands": [],
      "allowed_paths": ["src", "tests"]
    }
  ],
  "publishing": {
    "provider": "github",
    "default_target_branch": "main",
    "commit_template": "{summary}",
    "pr_title_template": "{summary}",
    "pr_body_template": "{body}"
  }
}
```

`run_root`、`worktree_root` 和 `mirror_root` 必须是专用 private roots，不应指向当前源码目录、共享目录或符号链接。`sandbox_permission_profile` 必须是管理员已安装的 managed sandbox profile；工作流会在每次测试命令前验证 worktree 可写、外部目录不可写、网络不可达且敏感环境变量未进入沙箱。

凭据不得写入 JSON。生产 CLI 只从受控进程环境读取下列变量，示例值均是占位符：

```text
# ONES：生产装配必需
ONES_BASE_URL=https://ones.example.invalid
ONES_EMAIL=<service-account-email>
ONES_PASSWORD=<secret-from-vault>
ONES_TEAM_ID=<team-id>
ONES_ISSUE_TYPE_ID=<defect-type-id>
ONES_COMMENT_LIST_PATH_TEMPLATE=/project/api/project/team/{team_id}/task/{item_id}/comments

# ONES：网关默认筛选或容量限制，可选
ONES_PROJECT_ID=<project-id>
ONES_DEFECT_STATUS_IDS=<status-id-1>,<status-id-2>
ONES_COMMENT_TIMEOUT_SECONDS=30
ONES_COMMENT_MAX_PAGES=50
ONES_COMMENT_MAX_COMMENTS=10000
ONES_COMMENT_MAX_PAYLOAD_BYTES=10485760

# PR provider：生产装配必需；provider 本身来自 JSON publishing.provider
ONES_DEV_PROVIDER_TOKEN=<secret-from-vault>
ONES_DEV_PROVIDER_HOST=git.example.invalid
ONES_DEV_PROVIDER_API_URL=https://git.example.invalid/api/v3

# Git 提交身份：生产装配必需
ONES_DEV_GIT_AUTHOR_NAME=<automation-name>
ONES_DEV_GIT_AUTHOR_EMAIL=<automation@example.invalid>

# Git credential transport：按部署方式选择，只有这五个名称会被接受
ONES_DEV_GIT_ASKPASS=<absolute-helper-path>
ONES_DEV_GIT_SSH=<absolute-ssh-path>
ONES_DEV_GIT_SSH_COMMAND=<bounded-ssh-command>
ONES_DEV_SSH_ASKPASS=<absolute-helper-path>
ONES_DEV_SSH_AUTH_SOCK=<agent-socket-path>

# Codex 认证：由运行环境选择一种；不要放入工作流 JSON
CODEX_HOME=<absolute-private-codex-home>
CODEX_API_KEY=<secret-from-vault>
CODEX_AUTH_TOKEN=<secret-from-vault>
OPENAI_API_KEY=<secret-from-vault>
```

`ONES_BASE_URL` 可以是 `http` 或 `https`，但不得含 userinfo、query 或 fragment；生产环境应优先使用 HTTPS。`ONES_DEV_PROVIDER_API_URL` 必须是 HTTPS，且主机必须与小写规范化后的 `ONES_DEV_PROVIDER_HOST` 完全一致；每个仓库的 HTTPS `repo_url` 也必须属于该主机。`ONES_COMMENT_LIST_PATH_TEMPLATE` 必须是显式的相对路径模板并仅含 `{team_id}`、`{item_id}`；未配置时评论去重 fail closed，不能猜测接口。评论超时、分页、数量和响应字节上限都必须是正数。

Git author 与 committer 使用同一组 `ONES_DEV_GIT_AUTHOR_NAME/EMAIL` 生成四个受控身份变量；凭据传输只能来自上述五项 allowlist，其他 Git 凭据变量会被丢弃或拒绝。Codex 可使用绝对、私有且可读的 `CODEX_HOME`，或显式环境 token；认证状态不会传入测试命令，也不会赋予 Codex ONES、Git 远端或 PR 发布权限。所有 secret 应来自服务管理器/凭据库注入，日志和 CLI 输出不会回显其值。

## 命令

```text
ones-dev requirement <requirement-id> [--mapping <key>] [--config <path>]
ones-dev defect --project <id> --iteration <id> --assignee <id> [--select <uuid>] [--mapping <key>] [--config <path>]
ones-dev defects list --project <id> --iteration <id> --assignee <id> [--status <id>[,<id>...]] [--format table|json] [--limit <1..5000>] [--page-size <1..200>]
ones-dev show <run-id> [--config <path>]
ones-dev resume <run-id> [--config <path>]
ones-dev revise <run-id> --feedback "..." [--scope implementation|repair] [--config <path>]
ones-dev approve <run-id> --actor <identity> [--config <path>]
ones-dev cancel <run-id> --actor <identity> [--config <path>]
```

`defects list` 是独立的只读查询，不创建 run 或 worktree，也不调用 Codex、Git、PR 和 ONES 评论接口。它只需要 `ONES_BASE_URL`、`ONES_TEAM_ID`、`ONES_ISSUE_TYPE_ID`、`ONES_EMAIL` 和 `ONES_PASSWORD`；默认输出开放缺陷表格，`--format json` 输出白名单字段 `uuid/key/number/title/priority/status/status_id/updated_at`。`limit` 最大 5000，`page-size` 最大 200 且不得大于 `limit`。若要选中缺陷并启动修复流程，仍使用单数命令 `defect`。

`--status` 只接受 ONES 工作流状态 ID，多个 ID 以英文逗号分隔。CLI 不按状态名称匹配；Gateway 会先验证每个 ID 都属于当前项目和缺陷类型的开放状态，再把 ID 原样传入 GraphQL `status_in`。例如当前授权项目中的“待处理”和“修复中”：

```powershell
uv run ones-dev defects list `
  --project <ONES_PROJECT_ID> `
  --iteration <ONES_ITERATION_ID> `
  --assignee <ONES_ASSIGNEE_ID> `
  --status <PENDING_STATUS_ID>,<FIXING_STATUS_ID>
```

TTY 下，`requirement` 可交互确认唯一仓库映射，`defect` 用候选序号完成单选。非 TTY 下必须显式提供 `--mapping`，缺陷还必须提供当前快照中的 `--select` UUID；不允许根据名称或模糊匹配自动选择。`revise` 的 scope 固定为：需求 `implementation`、缺陷 `repair`。缺陷若需要推翻既有根因或复现证据，应新建运行，而不是扩大 revision scope。

## 状态与恢复

- `READING_ONES`：固化需求、Wiki 或缺陷来源。
- `VALIDATING`：等待人工确认持久化的仓库候选。
- `PREPARING_REPO`：创建或恢复独立 worktree。
- `IMPLEMENTING`、`TESTING`、`AI_REVIEW`：生成修改、运行真实命令并审查证据。
- `WAITING_APPROVAL`：审批包完整；仍然没有远端写操作。
- `PUBLISHING`：已验证签名审批，Publisher 正按幂等检查点发布。
- `BLOCKED`：安全条件无法证明；`resume` 只从记录的安全检查点继续。
- `PARTIAL_SUCCESS`：commit、push 和 PR 已成功，但 ONES 评论未确认；`resume` 只重试评论，不重复 commit、push 或 PR。
- `COMPLETED`：PR URL 和 ONES 评论稳定标识均已持久化。
- `CANCELLED`：终止且不可发布。

中断后先运行 `show`，再运行 `resume`。映射缺失、来源不可读、测试无法运行或证据不足都会阻塞，不得绕过校验。取消不会清理审计记录；worktree 清理由受控运维流程执行。

## 审批及失效

`approve` 会在任何远端副作用前重新读取并比较全部证据。下列任一变化都会使旧审批失效：ONES 需求/缺陷关键内容；Wiki version、更新时间或内容哈希；远端基线 commit；worktree `HEAD`；diff 内容/文件集合；测试命令、argv、退出码、outcome、输出摘要或测试快照；风险、未解决事项、review；commit message、PR 标题或 PR 正文。失效后保持零新副作用，需重新验证并形成新审批。

Codex 只拥有隔离 worktree 的受管写权限，无 ONES、Git 远端或 PR 凭据，不能 commit/push/建 PR/评论。Publisher 是审批后唯一允许 commit、push 和创建 PR 的组件；`OnesCommenter` 仅在已确认 PR URL 后评论，并且永不更新 ONES 状态。

## 局域网只读 smoke

默认跳过。仅在获授权的局域网环境中显式设置以下变量；不要从项目 `.env` 自动推断筛选值，也不要把变量内容贴入日志：

```text
RUN_ONES_LAN_SMOKE=1
ONES_LAN_PROJECT_ID=<authorized-project>
ONES_LAN_ITERATION_ID=<authorized-iteration>
ONES_LAN_ASSIGNEE_ID=<authorized-account>
ONES_LAN_ISSUE_TYPE_ID=<authorized-defect-type>
ONES_LAN_WIKI_SPACE_ID=<authorized-space>
ONES_LAN_WIKI_PAGE_ID=<authorized-page>
```

然后运行 `uv run pytest tests/test_ones_lan_smoke.py -m ones_lan -v`。测试仅查询这组精确过滤条件及指定 Wiki，进程内审计所有 HTTP method/path/GraphQL operation：只允许 GET、只读 GraphQL query、必要的认证握手，以及 ONES 现有接口要求的精确 `/task_statuses` 只读元数据 POST（请求体只能包含 `project_uuids`）；出现 mutation、comment、status update 或其他业务写路径立即失败。测试不会遍历其他项目，不会打印密码或 token。

## 故障恢复

认证/权限、404、来源漂移、基线漂移、`HEAD` 漂移和沙箱能力不足均 fail closed。push 结果不确定时先只读检查远端 ref；PR 创建前先按运行标识查找已有 PR；评论前先查稳定 marker。PR 创建失败时绝不评论。PR 成功但评论失败进入 `PARTIAL_SUCCESS`，恢复时只能补评论。跨进程 operation lease 和版本 CAS 保证同一运行不会并发重复发布；若 lease 或持久化状态损坏，应保留 run/worktree 取证，修复存储后从最后一个已持久化事实恢复。
