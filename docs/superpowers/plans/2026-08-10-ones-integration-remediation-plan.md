# ONES 集成加固实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复局域网实测发现的 ONES 认证、缺陷状态、详情查询、分页和成员查询问题，为后续需求开发与缺陷修复 AI 工作流提供稳定且完整的 ONES 数据边界。

**Architecture:** 继续以 `OnesAsyncClient` / `OnesClient` 作为原始协议适配器，以 `OnesGateway` 作为唯一业务访问边界。状态选项必须来自项目工作流定义而非现有缺陷样本；详情查询统一接受 UUID 和 ONES key；认证、分页和错误处理在适配器层完成，FastAPI 与后续 CLI 只消费规范化结果。

**Tech Stack:** Python 3.11、httpx、requests、FastAPI、Pydantic Settings、pytest、respx、React/TypeScript。

---

## 已验证问题与实施顺序

以下问题均已在 `http://aputureones.com:8088/` 使用只读请求验证。它们必须作为 `2026-08-10-ones-ai-development-workflows-design.md` 中工作流实现的前置任务完成：

1. `ONES_DEFECT_STATUS_IDS` 写入 `.env` 后变成 Python 列表文本，重启后产生非法状态 UUID，ONES 返回 `400 InvalidParameter.Task.Status.InvalidOption`。
2. 项目缺陷工作流有 8 个状态，现有状态接口只返回有缺陷记录的 7 个，遗漏零数据状态“已拒绝”。
3. 首次并发调用异步客户端时，一个请求成功、另一个请求得到 `401 InvalidToken`。
4. Gateway 用 ONES key 查询详情会误报 NotFound；带项目/迭代范围查询只返回 24 字段列表项，而完整详情有 51 字段。
5. 缺陷列表丢弃 `pageInfo`，超过单页限制时不能完整遍历。
6. 空 UUID 列表查询团队成员返回 0 人，而项目角色成员接口返回 11 人。
7. OAuth 回调地址硬编码、Token 不刷新、GraphQL `errors` 未检查、详情日志包含业务字段、同步客户端资源未统一关闭。

---

### Task 1: 修复状态 ID 配置序列化与旧数据迁移

**Files:**
- Modify: `config/settings.py:28-31`
- Modify: `main.py:1403-1484`
- Test: `tests/test_config.py`
- Test: `tests/test_api_v1.py`

- [ ] **Step 1: 写入持久化后重新加载的失败测试**

在 `tests/test_api_v1.py` 增加测试，使用临时 `.env`，不能只断言当前进程中的 `settings.ones`：

```python
def test_update_config_round_trips_ones_status_ids(client, admin_headers, monkeypatch, tmp_path):
    import main
    from config.settings import OnesSettings

    env_file = tmp_path / ".env"
    monkeypatch.setattr(main, "_ENV_FILE", str(env_file))
    response = client.put(
        "/api/v1/config",
        headers=admin_headers,
        json={"ones": {"defectStatusIds": ["JAZYLueG", "VMxom1Jo"]}},
    )

    assert response.status_code == 200
    reloaded = OnesSettings(_env_file=env_file)
    assert reloaded.defect_status_id_list() == ["JAZYLueG", "VMxom1Jo"]
    assert "[" not in env_file.read_text(encoding="utf-8")
```

- [ ] **Step 2: 运行测试并确认当前实现失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_api_v1.py -k "round_trips_ones_status_ids" -v`

Expected: FAIL，重新加载结果包含引号或方括号。

- [ ] **Step 3: 为状态列表使用唯一序列化规则**

在 `main.py` 增加并复用辅助函数，内存值和 `.env` 值必须走同一路径：

```python
def _serialize_config_value(dot_key: str, value: object) -> str:
    if dot_key == "ones.defectStatusIds":
        return ",".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
```

`_persist_config()` 的内存更新和 `updates[env_key]` 都调用该函数，不再对列表调用 `str(value)`。

- [ ] **Step 4: 兼容读取已经损坏的旧配置**

在 `config/settings.py` 将解析集中到一个纯函数，清理历史遗留的列表括号和引号：

```python
def _parse_status_ids(raw: str) -> list[str]:
    values: list[str] = []
    for part in raw.split(","):
        status_id = part.strip().strip("[]'\"")
        if status_id and status_id not in values:
            values.append(status_id)
    return values
```

增加覆盖普通 CSV、JSON/Python 列表残留、空值和重复 ID 的参数化测试。

- [ ] **Step 5: 运行配置与 API 回归测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_config.py tests/test_api_v1.py -k "config or status_ids" -v`

Expected: PASS，且重新加载后的每个 ID 都是纯 UUID 字符串。

- [ ] **Step 6: 提交配置修复**

```bash
git add config/settings.py main.py tests/test_config.py tests/test_api_v1.py
git commit -m "fix: preserve ONES defect status ids across restart"
```

---

### Task 2: 增加项目工作流状态读取与规范化契约

**Files:**
- Modify: `src/contracts.py`
- Modify: `src/integrations/ones_api.py`
- Modify: `src/integrations/ones.py`
- Modify: `src/services/ones_gateway.py`
- Test: `tests/test_phase2.py`
- Test: `tests/test_ones.py`
- Test: `tests/test_ones_gateway.py`

- [ ] **Step 1: 写入原始状态接口契约测试**

测试以下只读请求：

```text
POST /project/api/project/team/{team_id}/task_statuses
Body: {"project_uuids": ["proj-1"]}

GET /project/api/project/team/{team_id}/task_statuses
```

断言 POST 结果读取 `task_status_configs`，GET 结果读取 `task_statuses`；空响应返回空列表，非 2xx 抛出 HTTP 异常。

- [ ] **Step 2: 运行适配器测试并确认方法尚不存在**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_phase2.py tests/test_ones.py -k "task_status" -v`

Expected: FAIL with missing `fetch_task_status_configs` / `fetch_task_status_definitions`。

- [ ] **Step 3: 在同步和异步客户端增加相同语义的方法**

```python
async def fetch_task_status_configs(self, project_ids: list[str]) -> list[dict[str, Any]]:
    client = await self._get_client()
    response = await client.post(
        f"{self._base_url}/project/api/project/team/{self._team_id}/task_statuses",
        json={"project_uuids": project_ids},
    )
    response.raise_for_status()
    return list(response.json().get("task_status_configs", []))

async def fetch_task_status_definitions(self) -> list[dict[str, Any]]:
    client = await self._get_client()
    response = await client.get(
        f"{self._base_url}/project/api/project/team/{self._team_id}/task_statuses",
    )
    response.raise_for_status()
    return list(response.json().get("task_statuses", []))
```

同步客户端使用相同 URL、字段和返回语义，不创建第二套状态模型。

- [ ] **Step 4: 增加完整工作流状态契约**

在 `src/contracts.py` 增加：

```python
@dataclass(slots=True)
class WorkflowStatusRef:
    id: str = ""
    name: str = ""
    category: str = ""
    position: int = 0
    default: bool = False
    built_in: bool = False
    detail_type: str = ""
    name_pinyin: str = ""
```

- [ ] **Step 5: 在 Gateway 合并配置与定义**

新增 `list_defect_statuses(project_id, issue_type_id)`：按 `project_uuid + issue_type_uuid` 选择配置，使用 `status_uuid` 关联定义，按 `position` 排序。定义缺失时抛出 `OnesGatewayPayloadError`，不能静默丢状态。

```python
configs = await self._call_async("fetch_task_status_configs", [project_id])
definitions = await self._call_async("fetch_task_status_definitions")
by_id = {item["uuid"]: item for item in definitions}
selected = [
    item for item in configs
    if item.get("project_uuid") == project_id
    and item.get("issue_type_uuid") == issue_type_id
]
return [self._normalize_workflow_status(item, by_id) for item in sorted(selected, key=lambda x: x["position"])]
```

- [ ] **Step 6: 验证零缺陷状态仍会返回**

在 Gateway 测试中构造 8 个配置但只给 7 个缺陷样本，断言 `list_defect_statuses()` 仍返回全部 8 个，并保留 `position/default/category`。

- [ ] **Step 7: 提交状态契约能力**

```bash
git add src/contracts.py src/integrations/ones_api.py src/integrations/ones.py src/services/ones_gateway.py tests/test_phase2.py tests/test_ones.py tests/test_ones_gateway.py
git commit -m "feat: read complete ONES defect workflow statuses"
```

---

### Task 3: 让缺陷状态 API 使用工作流定义

**Files:**
- Modify: `main.py:1943-1975`
- Modify: `agent-gui/src/api/types.ts:268-272`
- Test: `tests/test_api_v1.py`

- [ ] **Step 1: 将现有样本推导测试改为工作流定义测试**

Fake Gateway 提供 `list_defect_statuses()`，返回包含一个零缺陷状态的完整列表。断言 API 不再调用 `_fetch_filtered_defects()`，并保持 ONES 工作流顺序。

```python
assert response.json() == [
    {"id": "todo", "name": "待处理", "category": "to_do", "position": 0, "default": True},
    {"id": "rejected", "name": "已拒绝", "category": "done", "position": 4, "default": False},
]
```

- [ ] **Step 2: 运行测试并确认现有接口遗漏零数据状态**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_api_v1.py -k "fetch_ones_defect_statuses" -v`

Expected: FAIL，因为当前实现扫描最多 1000 条缺陷并按名称排序。

- [ ] **Step 3: 替换 API 数据来源**

`fetch_ones_defect_statuses()` 必须调用：

```python
statuses = await gateway.list_defect_statuses(
    project_id=projectId.strip() or settings.ones.project_id,
    issue_type_id=settings.ones.issue_type_id,
)
```

响应保留 `id/name/category/position/default`；不再依赖缺陷数量，也不再按名称重新排序。

- [ ] **Step 4: 扩展前端类型但保持现有选择器兼容**

```typescript
export interface OnesStatusOption {
  id: string;
  name: string;
  category?: string;
  position: number;
  default: boolean;
}
```

- [ ] **Step 5: 运行后端与前端验证**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_api_v1.py -k "fetch_ones_defect_statuses" -v`

Expected: PASS。

Run: `npm run build` in `agent-gui`

Expected: PASS，无 TypeScript 类型错误。

- [ ] **Step 6: 提交状态 API 修复**

```bash
git add main.py agent-gui/src/api/types.ts tests/test_api_v1.py
git commit -m "fix: return all ONES workflow statuses"
```

---

### Task 4: 修复认证并发、动态回调地址与 Token 续期

**Files:**
- Modify: `src/integrations/ones_api.py:58, 746-895`
- Modify: `src/integrations/ones.py:50, 728-838`
- Test: `tests/test_ones.py`
- Test: `tests/test_phase2.py`

- [ ] **Step 1: 写入首次并发登录测试**

使用两个并发 `fetch_projects()`，模拟登录完成前业务请求会得到 401，断言登录端点只调用一次且两个业务请求都在登录完成后发送。

```python
results = await asyncio.gather(client.fetch_projects(), client.fetch_projects())
assert results == [projects, projects]
assert login_calls == 1
```

- [ ] **Step 2: 写入 Token 过期后仅重登一次的测试**

模拟两个并发 GraphQL 请求同时返回 401，断言共享刷新锁只触发一次重新登录，每个请求最多重放一次，第二次 401 原样抛出。

- [ ] **Step 3: 运行认证测试并确认失败**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ones.py tests/test_phase2.py -k "concurrent or token or redirect" -v`

Expected: FAIL，当前首次并发会使用尚未设置 Authorization 的客户端。

- [ ] **Step 4: 增加初始化锁和认证版本号**

```python
self._init_lock = asyncio.Lock()
self._auth_generation = 0

async def _get_client(self) -> httpx.AsyncClient:
    if self._client is None:
        async with self._init_lock:
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=30.0)
                if self._settings.email and self._settings.password:
                    await self._login_with_client(self._client)
    return self._client
```

`_login_with_client()` 不得再次调用 `_get_client()`。401 重试必须比较认证版本，避免并发请求重复登录。

- [ ] **Step 5: 动态生成 OAuth redirect URI 并校验中间响应**

```python
redirect_uri = f"{self._base_url}/auth/authorize/callback"
```

对 authorize、auth_request、org_users、finalize 和 callback 每一步调用 `raise_for_status()`；使用 URL 解析器读取 `Location` 和授权码，不再使用字符串 `split()`。

- [ ] **Step 6: 按 team/org 关联选择组织用户**

不要固定使用 `org_users[0]`。如果响应无法把配置的 team 映射到唯一 org，则抛出明确的认证配置错误，并在错误中只记录 team ID，不记录凭据或 Token。

- [ ] **Step 7: 运行认证回归测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ones.py tests/test_phase2.py -k "login or token or redirect or concurrent" -v`

Expected: PASS；首次并发和过期并发均只登录一次。

- [ ] **Step 8: 提交认证加固**

```bash
git add src/integrations/ones_api.py src/integrations/ones.py tests/test_ones.py tests/test_phase2.py
git commit -m "fix: make ONES authentication concurrency safe"
```

---

### Task 5: 统一 UUID/key 详情解析并保证返回完整详情

**Files:**
- Modify: `src/integrations/ones_api.py:954-986`
- Modify: `src/integrations/ones.py:881-913`
- Modify: `src/services/ones_gateway.py:145-171, 657-689`
- Test: `tests/test_ones.py`
- Test: `tests/test_ones_gateway.py`

- [ ] **Step 1: 写入 UUID 和 key 等价测试**

```python
by_uuid = await gateway.get_defect_detail("uuid-1")
by_key = await gateway.get_defect_detail("task-key-1")
assert by_uuid["uuid"] == by_key["uuid"] == "uuid-1"
assert by_uuid["description"] == by_key["description"] == "full detail"
```

增加带 `project_id/sprint_id/mine` 条件的测试，断言范围条件只用于确认目标属于集合，最终仍调用 `fetch_issue_detail(key)` 获取完整详情。

- [ ] **Step 2: 运行测试并确认 key 被误判、范围查询不完整**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ones.py tests/test_ones_gateway.py -k "detail or key or scoped" -v`

Expected: FAIL，当前 Gateway 只接受 `uuid == issue_id`，范围查询直接返回列表项。

- [ ] **Step 3: 让身份匹配同时接受 UUID 和 key**

```python
@staticmethod
def _matches_issue(defect: dict, issue_id: str) -> bool:
    return issue_id in {str(defect.get("uuid") or ""), str(defect.get("key") or "")}
```

`_select_issue()`、`_resolve_issue_detail()` 和 `_validate_issue_payload()` 统一使用该规则。

- [ ] **Step 4: 范围查询命中后再次读取完整详情**

范围列表命中后取 `matched["key"]`，调用原始详情接口；随后验证返回详情与命中项 UUID/key 一致。不能把 24 字段列表项当作详情返回。

- [ ] **Step 5: 收紧 UUID 回退查询范围**

`fetch_issue_detail()` 不再隐式扫描默认项目的前 1000 个缺陷。Gateway 已有范围时使用范围查找；无范围且输入不是合法 ONES key 时，返回明确的 NotFound/InvalidIdentifier，避免跨项目误命中。

- [ ] **Step 6: 运行详情回归测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ones.py tests/test_ones_gateway.py -k "detail or key or scoped" -v`

Expected: PASS，UUID/key 均可查到完整详情。

- [ ] **Step 7: 提交详情修复**

```bash
git add src/integrations/ones_api.py src/integrations/ones.py src/services/ones_gateway.py tests/test_ones.py tests/test_ones_gateway.py
git commit -m "fix: resolve complete ONES issue details by uuid or key"
```

---

### Task 6: 实现真正分页并检查 GraphQL 业务错误

**Files:**
- Modify: `src/integrations/ones_api.py:61-100, 850-952`
- Modify: `src/integrations/ones.py:54-93, 812-879`
- Modify: `src/services/ones_gateway.py:421-471`
- Test: `tests/test_ones.py`
- Test: `tests/test_ones_gateway.py`

- [ ] **Step 1: 写入两页缺陷与 HTTP 200 GraphQL errors 测试**

第一页返回 `hasNextPage=true/endCursor=cursor-1`，第二页返回 false，断言结果包含两页且没有重复 UUID。另一个响应返回：

```json
{"data": null, "errors": [{"message": "invalid filter"}]}
```

断言适配器抛出专用 GraphQL 错误，而不是返回空数据。

- [ ] **Step 2: 运行测试并确认 pageInfo 被丢弃**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ones.py tests/test_ones_gateway.py -k "pagination or graphql_errors" -v`

Expected: FAIL。

- [ ] **Step 3: 让 `_graphql()` 验证响应信封**

```python
payload = response.json()
if not isinstance(payload, dict):
    raise OnesGraphQLPayloadError("GraphQL response must be an object")
if payload.get("errors"):
    raise OnesGraphQLResponseError.from_errors(payload["errors"])
return payload.get("data", payload)
```

日志只记录错误码、operation 和字段路径，不记录完整响应正文或业务内容。

- [ ] **Step 4: 保留并消费分页信息**

原始客户端返回包含 `items` 和 `page_info` 的内部页对象，Gateway 根据 `hasNextPage/endCursor` 继续请求，直到达到用户 limit 或没有下一页。每一页必须使用服务端返回的 cursor，不能用本地 offset 猜测。

- [ ] **Step 5: 运行分页与现有筛选测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_ones.py tests/test_ones_gateway.py -k "defect or pagination or status" -v`

Expected: PASS；迭代、经办人、状态和项目过滤在后续页继续生效。

- [ ] **Step 6: 提交分页与错误处理**

```bash
git add src/integrations/ones_api.py src/integrations/ones.py src/services/ones_gateway.py tests/test_ones.py tests/test_ones_gateway.py
git commit -m "fix: paginate ONES defects and surface GraphQL errors"
```

---

### Task 7: 修复成员语义、资源关闭和日志脱敏

**Files:**
- Modify: `src/integrations/ones_api.py:30-45, 1023-1075`
- Modify: `src/integrations/ones.py`
- Modify: `src/services/ones_gateway.py:177-181, 317-326`
- Test: `tests/test_phase2.py`
- Test: `tests/test_ones_gateway.py`

- [ ] **Step 1: 写入成员查询语义测试**

明确区分：

```python
await client.fetch_team_members(uuids=["u1", "u2"])  # 精确查询
await gateway.list_project_members("proj-1")           # 项目可选成员全集
```

禁止用 `{"uuids": []}` 表示“全部用户”，因为局域网实测其含义是“匹配空集合”。项目/迭代缺陷工作流使用 `role_members` 获取项目成员。

- [ ] **Step 2: 运行成员测试并确认空 UUID 查询错误**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_phase2.py tests/test_ones_gateway.py -k "member" -v`

Expected: FAIL，现有 `fetch_team_members()` 无参数时发送空 UUID 数组。

- [ ] **Step 3: 收紧团队成员方法参数并提供项目成员方法**

`fetch_team_members()` 要求非空 UUID；空列表直接返回空列表。Gateway 的“供用户选择项目成员”入口统一调用 `fetch_role_members(project_id)`，并对 UUID 去重。

- [ ] **Step 4: 关闭同步与异步资源**

为 `OnesClient` 增加 `close()` 关闭 `requests.Session`；`OnesGateway.close()` 同时关闭自身创建的同步与异步客户端，但不关闭由调用方注入的客户端。

- [ ] **Step 5: 删除详情业务字段日志**

`_defect_detail_summary()` 只允许输出：

```python
{
    "has_uuid": bool(detail.get("uuid")),
    "has_key": bool(detail.get("key")),
    "field_count": len(detail),
}
```

不得记录标题、描述、负责人、所有者、项目名、Wiki 内容或 Token。GraphQL 失败正文只保留结构化错误码和截断后的非业务诊断信息。

- [ ] **Step 6: 运行资源与日志测试**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_phase2.py tests/test_ones_gateway.py -k "member or close or log" -v`

Expected: PASS，注入客户端不被关闭，托管客户端恰好关闭一次。

- [ ] **Step 7: 提交成员和资源修复**

```bash
git add src/integrations/ones_api.py src/integrations/ones.py src/services/ones_gateway.py tests/test_phase2.py tests/test_ones_gateway.py
git commit -m "fix: clarify ONES member queries and close managed clients"
```

---

### Task 8: 建立局域网只读 smoke 验收和完整回归门禁

**Files:**
- Create: `tests/test_ones_lan_smoke.py`
- Modify: `pyproject.toml`
- Modify: `docs/superpowers/specs/2026-08-10-ones-ai-development-workflows-design.md`

- [ ] **Step 1: 注册显式 smoke marker**

在 `pyproject.toml` 注册：

```toml
markers = [
  "ones_lan: read-only tests against the authorized LAN ONES deployment",
]
```

测试必须在 `RUN_ONES_LAN_SMOKE=1` 时才运行，不得默认访问局域网。

- [ ] **Step 2: 增加只读 smoke 测试**

覆盖以下验收：

```python
@pytest.mark.ones_lan
async def test_defect_workflow_statuses_are_complete():
    statuses = await gateway.list_defect_statuses(project_id, issue_type_id)
    assert statuses
    assert [status.position for status in statuses] == sorted(status.position for status in statuses)
    assert all(status.id and status.name and status.category for status in statuses)

@pytest.mark.ones_lan
async def test_each_workflow_status_is_accepted_as_filter():
    for status in await gateway.list_defect_statuses(project_id, issue_type_id):
        await gateway.list_defects(project_id=project_id, issue_type_id=issue_type_id, status_ids=[status.id], limit=1)
```

另覆盖并发首次认证、UUID/key 完整详情一致性、项目成员、分页信封和 Wiki 只读接口。测试不得添加评论、修改状态或调用任何写接口。

- [ ] **Step 3: 运行离线 ONES 回归套件**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_ones.py tests/test_phase2.py tests/test_ones_gateway.py tests/test_config.py tests/test_api_v1.py -k "ones or defect_status or status_ids"`

Expected: PASS；不得保留当前 `42 passed, 4 failed` 的失败状态。

- [ ] **Step 4: 显式运行局域网只读 smoke**

Run: `$env:RUN_ONES_LAN_SMOKE='1'; .\.venv\Scripts\python.exe -m pytest tests/test_ones_lan_smoke.py -m ones_lan -v`

Expected: PASS；8 个工作流状态全部返回且逐项筛选成功，“已拒绝”即使当前为零数据也存在于状态列表中。

- [ ] **Step 5: 将本计划登记为 AI 工作流前置门禁**

在设计文档“实施顺序建议”之前增加明确约束：Task 1-8 全部通过后，才开始 Wiki、CLI、Codex Runner、需求流和缺陷流的实现；否则工作流进入 `BLOCKED`，不得通过缺陷样本推断状态或绕过 Gateway。

- [ ] **Step 6: 运行完整后端测试与前端构建**

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Expected: PASS。

Run: `npm run build` in `agent-gui`

Expected: PASS。

- [ ] **Step 7: 提交 smoke 和门禁文档**

```bash
git add tests/test_ones_lan_smoke.py pyproject.toml docs/superpowers/specs/2026-08-10-ones-ai-development-workflows-design.md
git commit -m "test: gate developer workflows on ONES integration smoke checks"
```

---

## 完成定义

- 配置保存并重启后，状态 UUID 与保存前逐项相等，不能包含引号、方括号或空值。
- 状态接口由工作流配置驱动，返回全部 8 个状态以及 `id/name/category/position/default`，不依赖当前缺陷数量。
- 首次并发访问和 Token 过期并发访问都只能触发一次登录或刷新，不出现未认证业务请求。
- UUID、ONES key、带项目/迭代范围的详情查询都返回同一份完整详情。
- 缺陷列表能依据 `hasNextPage/endCursor` 遍历多页，所有筛选条件在每页保持一致。
- 项目成员入口不再把空 UUID 数组误当作“全部成员”。
- GraphQL 业务错误不会被当作成功；日志不包含缺陷标题、描述、人员、Wiki 内容或凭据。
- 离线回归、前端构建和显式局域网只读 smoke 全部通过后，才允许开始 AI 工作流实现。
