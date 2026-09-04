# 动态验证节点（首版）

## 行为

验证不固定为 macOS。Review 根据实际改动输出验证需求、能力标签和具体验收标准，例如 `os:macos`、`os:windows`、`arch:arm64`、`device:camera`、`gpu:opengl`。应用从配置中选择能力匹配的启用节点及验证脚本，模型不能创建执行命令。

默认主流程：本地测试 → Review → 待人工审批 → Draft PR → PR 人工验证。仅未执行的外部验证可以随清单交付，代码问题仍回修，实际验证失败或节点错误仍阻断。配置 `publishing.defer_external_verification_to_pr=false` 可保留严格的“环境验证 → 待人工审批”路径；下文节点执行说明适用于该路径。详见 [Draft PR 交接与仓库门禁](developer_workflow_pr_verification_handoff.md)。

Configuration 按模块分为“ONES 配置”“验证节点”“运行信息”三个 Tab。验证节点 Tab 直接展示节点列表和“添加节点”按钮，点击节点或选中后按 Enter 进入详情，使用表单编辑，无需填写 JSON。高级用户仍可编辑工作流 JSON 的 `verification_nodes`；现有配置导入/保存会保留此字段。提供 TUI 验证确认、人工证据录入和 CLI 节点探测；尚不包含远程桌面控制或自动部署节点。

## 表单配置

1. 在“验证节点”Tab 点击“添加节点”，填写唯一节点标识，选择本机或 SSH；新节点默认禁用。SSH 节点额外填写已配置的连接别名、远端 Python 与 worker 脚本路径，不填写密码。
2. 选择操作系统、架构；相机、GPU 等其他能力用逗号分隔。支持自定义标签，不限于 macOS 或 Windows，编辑现有节点时保留自定义平台标签。
3. 在节点中“新增脚本”，选择仓库标识，声明脚本实际覆盖的能力，填写程序、参数和超时时间。参数每行一项，不加外层引号；带空格的路径作为一项完整保留。没有配置仓库列表时可填写仓库 key。
4. 脚本编辑点击“应用到节点草稿”，最后在详情点击“保存节点”。成功后返回并刷新列表；取消不会落盘。移除节点需在详情中二次确认，仅删除配置，不删除远端文件。重复标识、无效能力或超时等在对应表单显示中文错误提示。
5. 保存只更新配置，不连接节点、不运行命令；执行仍需逐项授权。已打开的任务重新打开后匹配新配置。保存失败保留当前输入；若编辑期间配置被其他操作修改，保存会拒绝覆盖，需取消后重新打开详情。列表加载失败时禁止添加，刷新成功后恢复，避免误覆盖原配置。

## 配置示例

将以下字段合并到已有工作流配置，保留原有仓库、认证等配置。`repository_key` 必须是该任务仓库的实际配置 key，示例不能直接代替真实脚本。

```json
{
  "verification_nodes": [
    {
      "key": "mac-lab",
      "enabled": false,
      "transport": "ssh",
      "ssh_alias": "mac-validation",
      "worker_argv": ["/usr/bin/python3", "/Users/tester/ones-verification/verification_worker.py"],
      "capabilities": ["os:macos", "arch:arm64", "device:camera", "gpu:opengl"],
      "recipes": [
        {
          "key": "camera-gpu-regression",
          "repository_key": "camera-sdk",
          "capabilities": ["os:macos", "arch:arm64", "device:camera", "gpu:opengl"],
          "argv": ["/Users/tester/test-env/bin/python", "tests/verify_camera_gpu.py"],
          "timeout_seconds": 300
        }
      ]
    },
    {
      "key": "windows-local",
      "enabled": false,
      "transport": "local",
      "capabilities": ["os:windows", "arch:x86_64"],
      "recipes": [
        {
          "key": "windows-regression",
          "repository_key": "app",
          "capabilities": ["os:windows", "arch:x86_64"],
          "argv": ["C:/test-env/Scripts/python.exe", "-m", "pytest", "tests/test_windows.py"],
          "timeout_seconds": 300
        }
      ]
    }
  ]
}
```

Linux 或其他环境使用同样结构。能力标签是管理员声明，不是自动认证；平台/架构在执行前会实测校验，GPU、相机、桌面会话等必须由所选脚本真正验证。能力匹配只是候选筛选，不证明脚本覆盖所有验收标准：确认执行前必须核对检查项与脚本，建议配置具体的 `check:*` 能力标签以区分同平台上的不同检查。

多个节点匹配时，首版按配置顺序选择第一个；每个检查项单独确认，不自动并行或故障转移。缺少依赖时不会擅自安装软件。

## 节点准备

1. 在目标机器创建专用测试账号，安装 Python 3.11+、项目测试依赖和必要设备驱动。GUI/相机权限、桌面登录会话由管理员准备；SSH 登录本身不保证 GUI 或 GPU 可用。
2. 将 `src/developer_workflow/verification_worker.py` 复制到目标机器可信目录；脚本只依赖 Python 标准库，不需要把整个 ONES agent 安装到节点上。
3. 在控制机 SSH 配置中设置 alias、HostName、User、IdentityFile 和端口。核验主机指纹并写入 known_hosts，使用密钥认证。程序始终开启 `BatchMode=yes`、`StrictHostKeyChecking=yes`，不接受自动信任陌生主机。
4. 配置 `worker_argv`。SSH 远端启动路径暂不支持空格或 shell 语法；Windows 可用简单绝对路径。真正测试命令使用 argv 数组、`shell=False`，支持参数内空格。
5. 检查脚本与能力声明后在表单勾选“启用此节点”并保存；直接编辑配置文件时通过现有配置导入/重启载入。不要在配置、命令参数或人工证据中填写密码/令牌。

节点是受信任的执行边界，控制账号可提交测试命令。源码复制目录不是系统沙箱，脚本拥有测试账号的权限，可能访问网络、设备和该账号的其他文件。因此不要在持有生产凭据的账号下执行不可信测试，也不要对不可信控制端开放该 worker。

## 操作

```powershell
uv run ones-dev verification-nodes --config ones-dev.config.json
uv run ones-dev probe-node mac-lab --config ones-dev.config.json
uv run ones-dev show RUN_ID --config ones-dev.config.json
uv run ones-dev verify RUN_ID --task TASK_KEY --actor tester --version 42 --recipe-digest RECIPE_DIGEST --config ones-dev.config.json
```

`show` 显示当前版本、验证项 ID、节点、状态、验收标准及 `recipe_digest`。`verify` 是本次执行的授权，不是预览。版本或脚本配置摘要变化时必须重新查看后确认，避免配置被改后执行未经确认的新脚本。探测仅报告 Python、操作系统与架构，不能代表硬件测试通过。

TUI 的 Review 显示“环境验证计划”和最近验证记录；原来的 Continue review 在等待环境时改为“环境验证”。点击后选检查项、核对节点及脚本，填写操作人并确认权限。执行在后台进行，不会占住弹窗事件处理。

旧任务只有自由文本外部验证说明时，点击“重新规划验证需求（旧任务）”，让模型在当前任务上下文中补充能力与验收标准。CLI 等价入口：

```powershell
uv run ones-dev plan-verification RUN_ID --version 42 --config ones-dev.config.json
```

确实需要人工操作的验证，可以选择“记录人工通过/失败证据”，填写设备、实际结果及日志/截图位置。人工记录是操作者对当前快照的明确确认，不等同于程序自动测试。CLI 等价入口：

```powershell
uv run ones-dev verify RUN_ID --task TASK_KEY --actor tester --version 42 --manual-evidence "Mac lab A，当前快照验证通过，日志 /results/001.txt" --config ones-dev.config.json
```

失败时增加 `--failed`。不能将“没有环境”“仅源码测试通过”登记为实机通过。

## 证据与限制

- 验证包包含各仓库 Git 跟踪文件与非忽略新增文件，保留实际未提交改动；删除文件不导出。多仓库分别放在各自 repository key 目录下，脚本工作目录为指定仓库，可按需访问同包其他仓库。
- 不同步 `.git`、被忽略的虚拟环境、SSH/云凭据目录；发现受保护环境文件、私钥或链接则拒绝整次导出，不悄悄遗漏可能影响结果的源码。包上限 64 MiB / 20,000 文件；大型产物验证需另行准备专用脚本或人工验证，当前版本不自动下载发布包。
- 每次节点执行创建全新临时目录，不覆盖既有工作区。测试可生成文件，但受测源文件改变后不会记录通过。控制机源码快照也在执行前后复核。
- 测试进程使用最小环境变量集合，不继承 ONES/OpenAI 等 API 凭据。脚本必须以前台方式等待验证完成，不能启动后立即退出并把后台任务当作通过。目录隔离和环境变量过滤不等于操作系统权限隔离。
- 留存节点、脚本配置摘要、输入包摘要、代码快照摘要、退出码、输出摘要、日志尾部和节点日志目录。完整日志留在节点目录，需按测试账号的数据保留策略清理；当前没有自动下载视频/截图或远程目录清理功能。
- 记录只对相同需求与受测快照有效；代码或验收标准改变需要重新验证。自动记录还绑定节点/脚本配置，修改配置后旧记录不可自动复用。验证证据进入审批指纹，不能借旧审批发布新代码。
- 普通复审不会因为模型漏写某一项就丢弃已有验证需求；确需调整验证范围时，通过“重新规划验证需求”明确重评。
- 本地“仅验证现有代码、不发布”模式仍遵循原有语义；它完成不代表已通过发布实机验收。此动态环境验证门禁接在需要发布的正常修复/实现流程上。
