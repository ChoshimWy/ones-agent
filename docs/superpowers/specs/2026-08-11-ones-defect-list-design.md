# ONES 缺陷列表命令设计

## 目标

新增纯只读命令 `ones-dev defects list`，按项目、迭代和负责人列出 ONES 中的开放缺陷。该命令不得创建 workflow run、worktree，不调用 Codex，也不要求 Git、PR 或 ONES 评论配置。

## 命令契约

```text
ones-dev defects list --project <id> --iteration <id> --assignee <id>
  [--status <id>[,<id>...]]
  [--format table|json] [--limit <1..5000>] [--page-size <1..200>]
```

- 默认输出安全表格；`--format json` 输出 JSON 数组。
- 只输出 `uuid`、`key`、`number`、`title`、`priority`、`status`、`updated_at`。
- 不输出候选快照令牌、原始 ONES payload、描述或认证信息。
- `limit` 与 `page-size` 在发起网络请求前校验，且 `page-size <= limit`。
- `--status` 是逗号分隔的 ONES 工作流状态 ID；每个 ID 必须是当前项目、缺陷类型下已配置的开放状态。空项、重复项、名称和未知 ID 均拒绝。
- 未提供 `--status` 时保持现有行为，查询全部开放状态；提供后只把已验证的 ID 放入 GraphQL `status_in`，绝不按显示名称匹配。
- 表格和 JSON 均增加 `status_id`，便于后续使用精确 ID。
- 空列表正常返回退出码 0；读取或数据校验失败返回脱敏错误和非零退出码。

## 架构

CLI 为该命令使用独立的只读候选服务工厂。生产工厂只验证 ONES URL、team、issue type、邮箱和密码，随后构造 `OnesGateway` 与 `DefectCandidateService`；不加载开发工作流配置，也不创建私有目录或发布组件。候选服务继续复用现有开放状态解析和严格范围校验，并接受本次调用的分页边界与状态 ID。Gateway 始终先读取项目工作流状态定义，再验证请求 ID 是开放状态集合的子集。

## 测试

覆盖命令帮助、表格与 JSON 白名单输出、空列表、分页参数透传、状态 ID 逗号解析、未知/关闭状态拒绝、精确 `status_in` 透传、非法边界在工厂前拒绝、读取异常脱敏，以及只读生产工厂不依赖发布/Git 配置。相邻回归覆盖既有 `defect` 启动命令、候选服务和 ONES gateway。
