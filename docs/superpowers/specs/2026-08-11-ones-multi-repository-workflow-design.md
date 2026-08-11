# ONES 多仓库 AI 开发工作流设计

日期：2026-08-11
状态：已确认
范围：需求与缺陷开发工作流的多仓库分析、修改、测试、审批、发布和恢复

## 1. 背景与目标

当前开发工作流以单个 `RepositoryMapping`、单个隔离 worktree、单个提交和单个 PR 为核心。一项 ONES 缺陷有时横跨主项目及其引用的多个项目，需要在同一次分析中读取多个仓库，并允许修改其中多个仓库。每个被修改的仓库必须分别提交并创建 PR，同时仍保持统一证据链、统一人工审批、安全隔离和可恢复发布。

本设计目标如下：

- 一个 ONES 工作项对应一个多仓库运行。
- 一个仓库组包含一个主仓库和若干有依赖顺序的关联仓库。
- Codex 可联合分析全部仓库，并按仓库边界修改一个或多个仓库。
- 每个仓库独立执行路径、HEAD、diff 和测试门禁。
- 全部仓库通过后执行仓库组级集成测试。
- 一次人工审批绑定全部仓库的证据与发布意图。
- 每个被修改仓库独立提交、推送并创建 PR。
- 发布失败时保留已成功的 PR，进入 `PARTIAL_SUCCESS`，恢复时只继续未完成仓库。
- 可读取本地已有工作区作为代码来源，但绝不直接修改用户当前工作区。

## 2. 非目标

- 不提供多个仓库的原子远端事务；Git 托管平台无法保证跨仓库原子发布。
- 不在失败后自动关闭已经创建的 PR 或回滚远端分支。
- 不允许 Codex 动态加入配置之外的仓库。
- 不允许直接修改 `source_path` 指向的本地工作区。
- 不改变“发布前必须人工审批”的原则。
- 不自动修改 ONES 缺陷状态。

## 3. 方案选择

### 3.1 采用方案：单运行、多仓库工作区

一个 `WorkflowRun` 保存整个仓库组的配置快照、仓库状态、测试证据、审批包和逐仓库发布事实。所有仓库共享一个运行级状态机，但每个仓库拥有独立的 Git、测试与发布状态。

该方案可以可靠表达跨仓库证据、统一审批和逐仓库恢复，避免父子运行之间的状态与指纹漂移。

### 3.2 未采用方案

- 父运行编排多个单仓库子运行：单仓库逻辑复用较多，但跨仓库测试、统一审批和恢复一致性较弱。
- 多个独立运行人工关联：实现简单，但无法保证依赖顺序、统一证据链及幂等发布。

## 4. 仓库组配置

新增 `repository_groups` 配置。每个仓库组恰好包含一个主仓库，并使用 `depends_on` 描述无环依赖关系。

```json
{
  "repository_groups": [
    {
      "key": "desktop-suite",
      "primary_repository": "desktop-app",
      "repositories": [
        {
          "key": "shared-sdk",
          "role": "dependency",
          "source_path": "E:/workspace/shared-sdk",
          "repo_url": "git@github.example.com:team/shared-sdk.git",
          "base_branch": "main",
          "depends_on": [],
          "allowed_paths": ["src/**", "tests/**"],
          "lint_commands": [],
          "build_commands": [],
          "test_commands": ["uv run pytest"]
        },
        {
          "key": "desktop-app",
          "role": "primary",
          "source_path": "E:/workspace/desktop-app",
          "repo_url": "git@github.example.com:team/desktop-app.git",
          "base_branch": "main",
          "depends_on": ["shared-sdk"],
          "allowed_paths": ["src/**", "tests/**"],
          "lint_commands": [],
          "build_commands": [],
          "test_commands": ["uv run pytest"]
        }
      ],
      "integration_test_commands": [
        "uv run pytest tests/integration"
      ]
    }
  ]
}
```

### 4.1 配置约束

- 仓库组 `key` 全局唯一。
- 仓库 `key` 在组内唯一，并且必须是安全、稳定的目录名。
- `primary_repository` 必须精确引用组内一个仓库。
- 必须恰好有一个 `role=primary`，并与 `primary_repository` 一致。
- `depends_on` 只能引用组内仓库，不能自引用，整个图必须无环。
- 发布、准备和逐仓库测试使用稳定的拓扑顺序；相同拓扑层按配置顺序稳定排序。
- 每个仓库独立配置基线、允许路径、测试命令和远端身份。
- `source_path` 可选且必须是可信的本地 Git 仓库绝对路径。
- `repo_url` 必填，是远端基线、推送和 PR 的权威身份。
- `source_path` 与 `repo_url` 必须解析为同一仓库身份，否则拒绝。
- 单仓库映射与仓库组不能使用相同的 key。

### 4.2 本地工作区双地址模型

- `source_path` 仅作为建立 bare mirror 的本地读取来源。
- 所有修改发生在运行私有目录中的隔离 worktree。
- 系统不得在 `source_path` 中 checkout、写文件、修改索引、创建分支或提交。
- 未提供 `source_path` 时，直接从 `repo_url` 获取代码。
- 发布前仍以 `repo_url` 的远端事实为准，本地源不能替代远端基线验证。

## 5. 运行时数据模型

新运行统一使用版本化的多仓库模型。主要结构包括：

- `RepositoryGroupSnapshot`
  - 仓库组 key、主仓库 key、拓扑顺序和集成测试命令。
  - 完整、不可变的仓库配置快照。
- `RepositoryRunContext`
  - 仓库 key、角色和依赖关系。
  - mirror、worktree、base commit、branch 和 HEAD。
  - 当前快照、测试快照和审批快照。
  - lint、build、test 结果。
  - 修改声明、风险、覆盖关系和 unresolved 项。
- `MultiRepositoryTestEvidence`
  - 各仓库测试证据。
  - 仓库组级集成测试证据。
- `RepositoryPublicationResult`
  - 每个仓库不可变的 publication intent。
  - commit、push 和 PR 的单向事实。
- `MultiRepositoryPublicationResult`
  - 拓扑发布顺序。
  - 逐仓库发布结果。
  - ONES 汇总评论事实。

`WorkflowRun` 顶层仍只有一个主状态；仓库子状态不能绕过主状态机或独立批准。

## 6. 隔离工作区

每个运行使用一个私有的仓库组工作区：

```text
run_root/<run_id>/workspace/
├── shared-sdk/
└── desktop-app/
```

安全约束：

- 每个目录都是独立、受控的 Git worktree。
- 共同父目录及所有子目录必须通过现有私有目录、symlink/reparse、身份和所有权门禁。
- Codex 沙箱唯一可写范围是已配置的 worktree 和受控临时目录。
- 禁止写入共同父目录之外、未配置的兄弟目录、mirror、`.git` 元数据或本地 `source_path`。
- 所有仓库在运行前后分别执行 worktree identity、HEAD、branch、common-dir 和 snapshot 校验。
- 固定兄弟目录布局允许主仓库的配置集成测试引用关联仓库，但不能扩大允许写路径。

## 7. 多仓库分析和修改

Codex 可读取仓库组内全部仓库。输出中的文件必须使用仓库限定路径：

```text
shared-sdk:src/lifecycle/shortcut.py
desktop-app:src/windows/main_window.py
```

系统不得仅凭字符串前缀分派路径，而应将仓库 key 与已固化的仓库上下文精确关联，然后在对应 worktree 内执行路径规范化、containment、nofollow 和 allowed-path 校验。

每个阶段必须分别验证：

- Codex 声明的仓库 key 存在于固化仓库组中。
- 声明文件属于对应仓库并在允许路径内。
- 每个仓库实际 `changed_files` 与该仓库声明完全一致。
- 所有仓库声明的并集与整个 Codex 结果一致。
- 未修改仓库可以作为只读分析依赖，但不得产生提交或 PR。
- Codex 不能自行添加测试命令、仓库、远端或发布目标。

## 8. 测试顺序和证据绑定

修改完成后按拓扑顺序执行：

1. 每个仓库自己的 lint。
2. 每个仓库自己的 build。
3. 每个仓库自己的 test。
4. 全部仓库通过后，在主仓库 cwd 中执行仓库组 `integration_test_commands`。

所有命令继续使用受验证的沙箱执行器、结构化 argv、禁网策略、凭据隔离、超时和有界输出。集成测试可以读取其他兄弟 worktree，但写权限仍必须受仓库允许路径约束。

测试完成后重新生成所有仓库的完整快照。进入 AI review 和审批前后，所有仓库快照必须与测试快照一致。任一仓库内容变化都使整个测试证据失效。

## 9. 统一审批

一次审批覆盖整个仓库组。审批包至少包含：

- ONES 缺陷、需求、Wiki 和其他来源版本。
- 完整仓库组配置快照和拓扑顺序。
- 每个仓库的 repo URL、base branch、base commit、最终 HEAD、diff 和 changed files。
- 每个仓库 lint、build、test 结果。
- 仓库组集成测试结果。
- 跨仓库根因、行为前后、影响范围、风险和验收覆盖。
- 每个待发布仓库的提交信息、分支、PR 标题和 PR 正文。
- unresolved 项以及人工发布字段。

统一指纹覆盖以上全部内容。任一仓库的基线、最终内容、测试、配置、依赖顺序、提交信息或 PR 内容发生变化，审批均失效，必须重新确认。

## 10. 逐仓库发布

发布前必须重新读取 ONES、Wiki、所有远端基线和所有仓库最终快照，重建审批包并验证外部保存的审批指纹。

发布步骤：

1. 为全部待发布仓库生成不可变 publication intent。
2. 重新验证全部仓库远端基线和本地快照。
3. 为全部待发布仓库创建批准的本地提交；任一失败时不开始远端发布。
4. 按拓扑顺序对每个仓库执行：
   - 再次校验该仓库远端目标分支基线；
   - 推送精确 commit 到精确远端分支；
   - 按稳定 marker 查找或创建 PR；
   - 持久化 commit、push 和 PR 事实。
5. 全部 PR 完成后，向 ONES 写入一条稳定 marker 的汇总评论。

汇总评论列出每个仓库的提交和 PR，不修改缺陷状态。

## 11. 部分成功和恢复

跨仓库远端发布不具备原子性。采用保留已成功结果的恢复策略：

- 已推送分支或已创建 PR 不自动撤销。
- 任一仓库发布失败后，运行进入 `PARTIAL_SUCCESS`。
- 每个仓库独立维护 `PENDING`、`COMMITTED`、`PUSHED`、`PR_CREATED` 事实。
- 恢复时获取运行级发布操作锁，重新读取权威状态，只继续未完成仓库。
- 已完成仓库通过远端 branch、commit 和 PR marker 重新验证，不重复写操作。
- 外部写结果不确定时先只读查询；不能证明未执行时不得盲目重试。
- 已完成事实与远端冲突时安全阻塞并要求人工处理。
- ONES 汇总评论失败只重试评论，不重复 Git 或 PR 操作。

示例：

```text
shared-sdk   PR_CREATED
desktop-app  PUSHED
tools        PENDING
```

恢复时只为 `desktop-app` 查找或创建 PR，并继续 `tools` 的本地提交和后续发布。

## 12. CLI 交互

CLI 只接收一个仓库组 mapping key：

```powershell
uv run ones-dev defects start `
  --project XjJ3QvWeJyNQWgwu `
  --iteration JkYR4hqe `
  --assignee Q6kE8A2m `
  --select EmmYk6yxljnUZ55E `
  --mapping desktop-suite
```

确认界面必须展示主仓库、完整仓库列表、依赖和发布顺序、本地代码来源、远端身份及“本地源码只读”的提示。

现有命令保持不变：

```powershell
uv run ones-dev approve <run-id> --actor "名字"
uv run ones-dev resume <run-id>
uv run ones-dev show <run-id>
```

`show` 按仓库展示基线、修改、测试、commit、push、PR、错误和恢复进度，并显示统一审批指纹及整体状态。

## 13. 向后兼容

- 保留现有 `repository_mappings`。
- 新建单仓库运行时，将单仓库映射规范化为只包含一个主仓库的仓库组。
- 新配置使用 `repository_groups`。
- 已持久化的单仓库运行继续按原模型读取和恢复，不强制改写历史状态文件。
- 新建运行统一写入版本化的多仓库模型。
- 单仓库路径必须继续通过现有全部安全、审批和发布回归。

## 14. 错误处理

- 配置错误在创建 worktree 前拒绝。
- 任一仓库身份、路径、HEAD、基线、diff 或声明校验失败时安全阻塞。
- 任一仓库测试失败时不生成审批包。
- 集成测试失败时不生成审批包。
- 审批重建不一致时不得创建本地提交或产生远端副作用。
- 错误消息必须脱敏，不能回显凭据、环境变量、私有路径内容或外部响应正文。
- CAS 冲突不得覆盖另一个执行者推进的状态。

## 15. 测试策略

### 15.1 配置和契约

- 单主仓库、仓库 key 唯一、引用存在和依赖无环。
- 稳定拓扑排序。
- `source_path` 与 `repo_url` 身份一致。
- 单仓库配置规范化和历史 JSON 兼容。

### 15.2 工作区和安全

- 本地 source 仓库保持逐字节和 Git 状态不变。
- 多个 worktree 使用固定、隔离的兄弟目录。
- 仓库限定路径不能跨仓库、逃逸或使用混合分隔符伪造。
- symlink、reparse、`.git` 和中间目录竞态安全失败。
- Codex 和测试沙箱不能写入未允许仓库或共同父目录之外。

### 15.3 工作流

- 全部仓库只读分析。
- 仅修改主仓库。
- 仅修改依赖仓库。
- 同时修改多个仓库。
- 未修改仓库不产生提交或 PR。
- 每个仓库测试按拓扑顺序执行，集成测试最后执行。
- 测试后任一仓库变化都会阻止 review 或审批。

### 15.4 审批和发布

- 任一仓库基线、diff、测试、提交或 PR 内容漂移使指纹失效。
- 所有本地提交准备完成后才开始远端发布。
- 每个仓库 commit、push 和 PR 恰好一次。
- 中途失败进入 `PARTIAL_SUCCESS`，已有 PR 保留。
- 恢复只继续未完成仓库，不重复已确认副作用。
- 两个进程同时恢复时，运行级 OS 发布锁保证外部写操作不重复。
- ONES 汇总评论恰好一次，且不会触发状态更新。

### 15.5 真实本地 Git E2E

使用多个本地 bare remote、多个真实 mirror/worktree 和受控测试命令，验证：

- 本地 `source_path` 只读。
- 多仓库独立分支和提交。
- 拓扑顺序 push。
- 每个仓库分别创建 Fake PR。
- 统一审批重建。
- 中断和恢复。

E2E 不访问真实 ONES 写接口、不推送真实远端、不创建真实 PR。

## 16. 验收标准

- 一个缺陷运行可安全读取并修改配置内多个仓库。
- 用户本地工作区不会被直接修改。
- 每个仓库修改、测试和发布证据可独立审计。
- 仓库组集成测试在所有逐仓库测试之后执行。
- 一次审批精确绑定所有仓库最终事实。
- 每个修改仓库分别产生一个提交和一个 PR。
- 依赖仓库先于依赖它的仓库发布。
- 部分发布失败后可恢复，且不会重复已完成外部副作用。
- 现有单仓库需求、缺陷、审批、发布和 CLI 行为保持兼容。
