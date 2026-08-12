# ONES Dev TUI Bootstrap Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `ones-dev tui` 在生产配置不完整时进入安全配置向导，并在全部配置、凭据和能力验证通过后切换到现有 Dashboard。

**Architecture:** 顶层 Textual Host 只在两种互斥状态间切换：配置状态持有 `SetupController`，运行状态持有现有 `TuiController` 与生产服务图。非敏感配置使用受保护的 generation JSON，秘密使用 Windows Credential Manager；`RuntimeBootstrapper` 接收显式运行时输入，不依赖 TUI 预先设置父进程环境变量。

**Tech Stack:** Python 3.11、Pydantic v2、Textual、ctypes/Windows Credential Manager、原子 JSON、pytest、现有 ONES/Git/Codex/sandbox 安全边界。

---

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `src/developer_workflow/setup_models.py` | 配置档案、草稿、generation、状态和显式运行时输入的严格模型 |
| `src/developer_workflow/credential_store.py` | Windows Credential Manager 读写、删除和 target 校验 |
| `src/developer_workflow/setup_store.py` | 用户配置的 nofollow/ACL/原子读取、写入、恢复和孤立 generation 检测 |
| `src/developer_workflow/setup_import.py` | `--config`、环境变量和 `.env` 的只检测、显式导入 |
| `src/developer_workflow/setup_validation.py` | ONES、Git、provider、Codex、private roots 和 sandbox 的只读验证编排 |
| `src/developer_workflow/setup_controller.py` | 七步状态机、临时秘密、generation 提交和运行时激活 |
| `src/developer_workflow/runtime_bootstrap.py` | 显式 `RuntimeInputs` 到现有生产服务图的唯一构建边界 |
| `src/developer_workflow/tui/setup_models.py` | 不含秘密的冻结配置 ViewModel |
| `src/developer_workflow/tui/setup_screens.py` | 七步配置 Screen、恢复 Screen 和安全表单控件 |
| `src/developer_workflow/tui/runtime_session.py` | Dashboard Controller/Supervisor/轮询生命周期封装 |
| `src/developer_workflow/tui/app.py` | 顶层 Host 的配置/运行互斥切换 |
| `src/developer_workflow/cli.py` | `tui` 分支在严格配置加载前启动 Host；非 TUI 分支保持现状 |

---

### Task 1: 严格配置档案与运行时输入模型

**Files:**
- Create: `src/developer_workflow/setup_models.py`
- Modify: `src/developer_workflow/__init__.py`
- Create: `tests/test_developer_workflow_setup_models.py`

- [ ] **Step 1: 写缺失模型的失败测试**

```python
def test_setup_document_never_accepts_secret_fields() -> None:
    payload = {
        "schema_version": 1,
        "profile_id": "default",
        "draft": {"ones_password": "TOKEN-SECRET"},
    }
    with pytest.raises(ValidationError):
        SetupDocument.model_validate(payload)


def test_runtime_inputs_keep_secrets_out_of_model_dump() -> None:
    inputs = RuntimeInputs(public=_public_config(), secrets=_secret_bundle())
    assert "TOKEN-SECRET" not in repr(inputs)
    assert not hasattr(inputs, "model_dump")
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_setup_models.py -q`

Expected: collection FAIL，`setup_models` 尚不存在。

- [ ] **Step 3: 实现严格模型**

```python
class SecretKind(str, Enum):
    ONES_EMAIL = "ones_email"
    ONES_PASSWORD = "ones_password"
    PROVIDER_TOKEN = "provider_token"
    CODEX_API_KEY = "codex_api_key"
    CODEX_AUTH_TOKEN = "codex_auth_token"
    GIT_ASKPASS = "git_askpass"
    GIT_SSH = "git_ssh"
    GIT_SSH_COMMAND = "git_ssh_command"
    SSH_ASKPASS = "ssh_askpass"
    SSH_AUTH_SOCK = "ssh_auth_sock"


class RuntimePublicConfig(WorkflowModel):
    ones_base_url: str
    ones_team_id: str
    ones_issue_type_id: str
    ones_comment_list_path_template: str
    provider_host: str
    provider_api_url: str
    git_author_name: str
    git_author_email: str
    codex_auth_mode: Literal["credential", "file"]
    codex_home: Path | None = None


class WorkflowDraft(WorkflowModel):
    run_root: Path | None = None
    mirror_root: Path | None = None
    worktree_root: Path | None = None
    sandbox_permission_profile: str | None = None
    max_codex_attempts: StrictInt = 3
    tui_max_concurrency: StrictInt = 3
    repositories: tuple[RepositoryMapping, ...] = ()
    repository_groups: tuple[RepositoryGroupMapping, ...] = ()
    publishing: PublishingConfig | None = None


class SetupDraft(WorkflowModel):
    runtime: RuntimePublicConfig | None = None
    workflow: WorkflowDraft = Field(default_factory=WorkflowDraft)
    detected_secret_kinds: tuple[SecretKind, ...] = ()


class ActiveSetup(WorkflowModel):
    generation: str
    runtime: RuntimePublicConfig
    workflow: DeveloperWorkflowConfig
    credential_kinds: tuple[SecretKind, ...]


class SetupDocument(WorkflowModel):
    schema_version: Literal[1] = 1
    profile_id: str
    active: ActiveSetup | None = None
    previous: ActiveSetup | None = None
    draft: SetupDraft = Field(default_factory=SetupDraft)


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeSecrets:
    values: Mapping[SecretKind, str]

    def require(self, kind: SecretKind) -> str:
        value = self.values.get(kind, "")
        if not value:
            raise SetupValidationError("runtime credential is unavailable")
        return value


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeInputs:
    public: RuntimePublicConfig
    secrets: RuntimeSecrets
```

所有字符串 validator 必须复用现有 UTF-8、Cc/Cf/Cs/Zl/Zp、URL、Git email 和路径校验，不接受 Pydantic 类型强转。

- [ ] **Step 4: 运行模型与配置回归**

Run: `uv run pytest tests/test_developer_workflow_setup_models.py tests/test_developer_workflow_config.py tests/test_developer_workflow_contracts.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/developer_workflow/setup_models.py src/developer_workflow/__init__.py tests/test_developer_workflow_setup_models.py
git commit -m "feat(setup): add bootstrap configuration contracts"
```

---

### Task 2: Windows Credential Manager 安全适配器

**Files:**
- Create: `src/developer_workflow/credential_store.py`
- Create: `tests/test_developer_workflow_credential_store.py`

- [ ] **Step 1: 写 target、读写、删除和脱敏失败测试**

```python
def test_credential_target_is_derived_only_from_validated_ids() -> None:
    assert credential_target("profile-1", "a" * 32, SecretKind.ONES_PASSWORD) == (
        "ones-dev/profile-1/" + "a" * 32 + "/ones_password"
    )
    with pytest.raises(CredentialStoreError):
        credential_target("../escape", "a" * 32, SecretKind.ONES_PASSWORD)


def test_store_never_exposes_backend_error_or_secret(fake_wincred) -> None:
    fake_wincred.fail_with(RuntimeError("TOKEN-SECRET"))
    with pytest.raises(CredentialStoreError, match="credential operation failed") as error:
        WindowsCredentialStore(fake_wincred).write(
            "profile-1", "a" * 32, SecretKind.ONES_PASSWORD, "TOKEN-SECRET"
        )
    assert error.value.__cause__ is None
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_credential_store.py -q`

Expected: collection FAIL。

- [ ] **Step 3: 实现 ctypes Win32 边界**

```python
class CredentialStore(Protocol):
    def write(self, profile_id: str, generation: str, kind: SecretKind, value: str) -> None:
        raise NotImplementedError

    def read(self, profile_id: str, generation: str, kind: SecretKind) -> str:
        raise NotImplementedError

    def delete(self, profile_id: str, generation: str, kind: SecretKind) -> None:
        raise NotImplementedError

    def write_generation(self, profile_id: str, generation: str, secrets: RuntimeSecrets) -> None:
        raise NotImplementedError

    def read_generation(
        self, profile_id: str, generation: str, kinds: tuple[SecretKind, ...]
    ) -> RuntimeSecrets:
        raise NotImplementedError

    def delete_generation(self, profile_id: str, generation: str) -> None:
        raise NotImplementedError

    def list_generations(self, profile_id: str) -> tuple[str, ...]:
        raise NotImplementedError


class WindowsCredentialStore:
    def write(self, profile_id: str, generation: str, kind: SecretKind, value: str) -> None:
        target = credential_target(profile_id, generation, kind)
        raw = bytearray(validate_secret_value(value).encode("utf-8", errors="strict"))
        try:
            self._backend.write_generic(target, raw, persist="local_machine")
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            raise CredentialStoreError("credential operation failed") from None
        finally:
            for index in range(len(raw)):
                raw[index] = 0
```

Win32 backend 必须使用 `CredWriteW`、`CredReadW`、`CredDeleteW`、`CredFree`，检查 blob 长度上限、严格 UTF-8、普通 generic credential 类型，并在所有 handle/blob 路径释放资源。非 Windows 平台固定 fail closed。

- [ ] **Step 4: 运行 fake 与真实 Windows roundtrip**

Run: `uv run pytest tests/test_developer_workflow_credential_store.py -q`

Expected: PASS；真实测试使用随机 target，`finally` 删除，不打印值。

- [ ] **Step 5: 提交**

```bash
git add src/developer_workflow/credential_store.py tests/test_developer_workflow_credential_store.py
git commit -m "feat(setup): store workflow secrets in Windows credentials"
```

---

### Task 3: 私有用户配置与 generation 事务

**Files:**
- Create: `src/developer_workflow/setup_store.py`
- Modify: `src/developer_workflow/private_paths.py`
- Create: `tests/test_developer_workflow_setup_store.py`

- [ ] **Step 1: 写 nofollow、ACL、原子切换和崩溃恢复失败测试**

```python
def test_commit_switches_active_generation_atomically(store, credentials) -> None:
    first = store.commit("profile-1", _candidate("a" * 32), credentials)
    second = store.commit("profile-1", _candidate("b" * 32), credentials)
    assert store.load().active == second.active
    assert store.load().previous == first.active


def test_crash_before_json_replace_keeps_old_generation(store, fault_after_credentials) -> None:
    before = store.load()
    with pytest.raises(SetupStoreError):
        store.commit("profile-1", _candidate("c" * 32), _secrets())
    assert store.load() == before
    assert store.orphan_generations() == ("c" * 32,)
```

Windows symlink/reparse、开放 DACL、继承未保护、replace identity 竞态和非法 JSON 都必须有测试。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_setup_store.py -q`

Expected: collection FAIL。

- [ ] **Step 3: 抽取单目录安全准备并实现 SetupStore**

```python
def prepare_private_directory(path: Path) -> Path:
    """Create or verify one private directory with existing strict ACL rules."""
    return _prepare_private_directories((path,))[0]


class SetupStore:
    def commit(self, profile_id: str, candidate: ActiveSetup, secrets: RuntimeSecrets) -> SetupDocument:
        current = self.load_or_empty(profile_id=profile_id)
        self._credentials.write_generation(profile_id, candidate.generation, secrets)
        document = current.validated_update(active=candidate, previous=current.active)
        try:
            self._atomic_write(document)
        except BaseException:
            self._credentials.delete_generation(profile_id, candidate.generation)
            raise SetupStoreError("configuration save failed") from None
        return self.load()

    def read_active_secrets(self, document: SetupDocument) -> RuntimeSecrets:
        active = document.active
        if active is None:
            raise SetupStoreError("active configuration is unavailable")
        return self._credentials.read_generation(
            document.profile_id, active.generation, active.credential_kinds
        )

    def restore_previous(self, profile_id: str) -> SetupDocument:
        current = self.load()
        failed = current.active
        restored = current.validated_update(active=current.previous, previous=None)
        self._atomic_write(restored)
        if failed is not None:
            self._credentials.delete_generation(profile_id, failed.generation)
        return self.load()

    def finalize_activation(self, profile_id: str) -> SetupDocument:
        current = self.load()
        obsolete = current.previous
        finalized = current.validated_update(previous=None)
        self._atomic_write(finalized)
        if obsolete is not None:
            try:
                self._credentials.delete_generation(profile_id, obsolete.generation)
            except CredentialStoreError:
                logger.warning("setup credential cleanup deferred")
        return self.load()
```

旧 generation 的删除发生在新 JSON 指针稳定之后；删除失败只留下可枚举的 orphan，使用固定日志并在恢复页请求用户二次确认清理，不能反向破坏已经验证成功的新 active generation。

配置文件固定为 `%LOCALAPPDATA%\ones-dev\config.json`。打开和读取必须与 `FileRunStore` 同等级 nofollow/reparse/final-path/identity 检查；原子写使用同目录临时文件、flush、fsync 和 `os.replace`。配置目录使用 `prepare_private_directory`。

- [ ] **Step 4: 运行存储与 private path 回归**

Run: `uv run pytest tests/test_developer_workflow_setup_store.py tests/test_developer_workflow_cli.py -k "private or setup" -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/developer_workflow/setup_store.py src/developer_workflow/private_paths.py tests/test_developer_workflow_setup_store.py
git commit -m "feat(setup): persist versioned private configuration"
```

---

### Task 4: 模板、环境变量与 `.env` 显式迁移

**Files:**
- Create: `src/developer_workflow/setup_import.py`
- Create: `tests/test_developer_workflow_setup_import.py`

- [ ] **Step 1: 写只检测、不显示值、明确确认后导入的失败测试**

```python
def test_detection_returns_names_only(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ONES_PASSWORD", "TOKEN-SECRET")
    result = detect_import_sources(tmp_path / ".env", os.environ)
    assert result.environment == (SecretKind.ONES_PASSWORD,)
    assert "TOKEN-SECRET" not in repr(result)


def test_import_requires_explicit_selected_kinds(monkeypatch) -> None:
    monkeypatch.setenv("ONES_PASSWORD", "TOKEN-SECRET")
    imported = import_selected(os.environ, selected=())
    assert imported.values == {}
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_setup_import.py -q`

Expected: collection FAIL。

- [ ] **Step 3: 实现有界解析与导入**

```python
@dataclass(frozen=True, slots=True)
class ImportDetection:
    environment: tuple[SecretKind, ...]
    dotenv: tuple[SecretKind, ...]
    template_available: bool


def import_selected(
    environment: Mapping[str, str],
    dotenv_values: Mapping[str, str],
    selected: tuple[SecretKind, ...],
) -> RuntimeSecrets:
    values: dict[SecretKind, str] = {}
    for kind in selected:
        env_name = ENV_BY_SECRET_KIND[kind]
        raw = environment.get(env_name) or dotenv_values.get(env_name) or ""
        values[kind] = validate_secret_value(raw)
    return RuntimeSecrets(MappingProxyType(values))
```

`.env` 只接受有界的 `NAME=value` 行，不执行变量展开、命令替换、include 或 shell 语义。原文件绝不自动修改或删除。

- [ ] **Step 4: 运行导入安全矩阵**

Run: `uv run pytest tests/test_developer_workflow_setup_import.py tests/test_developer_workflow_security.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/developer_workflow/setup_import.py tests/test_developer_workflow_setup_import.py
git commit -m "feat(setup): import detected credentials explicitly"
```

---

### Task 5: Managed profile 发现与只读连接验证

**Files:**
- Create: `src/developer_workflow/setup_validation.py`
- Create: `tests/test_developer_workflow_setup_validation.py`

- [ ] **Step 1: 写 allowlist、profile 发现和零业务写失败测试**

```python
def test_profile_catalog_never_accepts_free_text(codex_doctor, config_toml) -> None:
    catalog = ManagedProfileCatalog(codex_doctor, trusted_admin_catalog=None)
    assert catalog.list_profiles() == ("managed-ones-worktree",)
    with pytest.raises(SetupValidationError):
        catalog.require_selected("invented-profile")


async def test_ones_probe_is_read_only(audited_transport) -> None:
    validator = SetupValidator(
        ones_transport=audited_transport,
        provider_transport=RejectingProviderTransport(),
        command_executor=RejectingCommandExecutor(),
    )
    result = await validator.probe_ones(_ones_probe_input())
    assert result.status is ValidationStatus.PASSED
    assert audited_transport.business_writes == []
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_setup_validation.py -q`

Expected: collection FAIL。

- [ ] **Step 3: 实现 profile catalog 和固定验证结果**

```python
class ValidationStatus(str, Enum):
    NOT_CONFIGURED = "not_configured"
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class ConnectionTestResult(WorkflowModel):
    step: SetupStep
    status: ValidationStatus
    category: Literal[
        "ok", "authentication", "unreachable", "tls", "timeout",
        "incompatible", "unsafe_path", "sandbox", "invalid_field",
    ]


class ManagedProfileCatalog:
    def list_profiles(self) -> tuple[str, ...]:
        doctor = self._load_redacted_doctor_json()
        config_path = self._validated_config_path(doctor)
        user_profiles = self._read_exact_permissions_table(config_path)
        admin_profiles = self._read_acl_verified_admin_catalog()
        candidates = sorted(set(user_profiles) | set(admin_profiles))
        return tuple(name for name in candidates if PROFILE_RE.fullmatch(name))
```

当前 Codex CLI 不提供 profile 列表命令。实现必须从 `codex doctor --json` 返回的受信 `config.toml` 路径读取精确 `permissions` 表，并可选读取 `%PROGRAMDATA%\ones-dev\managed-sandbox-profiles.json`；管理员目录必须验证 owner/DACL。候选仍必须通过现有 `SandboxCommandExecutor` capability probe，不能仅凭名称认为可用。

ONES、provider 和 Git probe 使用显式客户端和只读 allowlist；sandbox probe 使用专用临时 probe root，不创建 run/mirror/worktree roots。

- [ ] **Step 4: 运行连接与 sandbox 相邻回归**

Run: `uv run pytest tests/test_developer_workflow_setup_validation.py tests/test_developer_workflow_requirement.py -k "sandbox or configured" -q`

Expected: PASS；无 managed profile 时真实能力测试条件 skip，fake 能力矩阵必须全绿。

- [ ] **Step 5: 提交**

```bash
git add src/developer_workflow/setup_validation.py tests/test_developer_workflow_setup_validation.py
git commit -m "feat(setup): validate read-only runtime capabilities"
```

---

### Task 6: 仓库与仓库组草稿构建器

**Files:**
- Create: `src/developer_workflow/setup_repository.py`
- Create: `tests/test_developer_workflow_setup_repository.py`

- [ ] **Step 1: 写单仓、DAG、命令凭据和 source 不变测试**

```python
def test_group_builder_rejects_cycles() -> None:
    builder = RepositoryGroupDraftBuilder()
    builder.add(_repo("app", depends_on=("sdk",)))
    builder.add(_repo("sdk", depends_on=("app",)))
    with pytest.raises(SetupValidationError, match="repository group is invalid"):
        builder.build(primary="app")


def test_secret_bearing_command_is_rejected() -> None:
    with pytest.raises(SetupValidationError):
        build_repository(test_commands=("curl -u alice:TOKEN-SECRET https://x",))
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_setup_repository.py -q`

Expected: collection FAIL。

- [ ] **Step 3: 实现 typed builder**

```python
class RepositoryGroupDraftBuilder:
    def build(self, *, primary: str) -> RepositoryGroupMapping:
        repositories = tuple(self._repositories.values())
        group = RepositoryGroupMapping(
            key=self._key,
            project_id=self._project_id,
            iteration_id=self._iteration_id,
            primary_repository=primary,
            repositories=repositories,
            integration_test_commands=self._integration_commands,
        )
        return group
```

命令必须复用 `command_utils.parse_command_argv` 和现有 credential-argument 门禁。source probe 前后比较 HEAD、index、status 和目标路径 identity；远端 probe 只执行隔离环境中的 `ls-remote --refs`。

- [ ] **Step 4: 运行仓库草稿与现有契约测试**

Run: `uv run pytest tests/test_developer_workflow_setup_repository.py tests/test_developer_workflow_multi_repository.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/developer_workflow/setup_repository.py tests/test_developer_workflow_setup_repository.py
git commit -m "feat(setup): configure repository groups safely"
```

---

### Task 7: SetupController 七步状态机

**Files:**
- Create: `src/developer_workflow/setup_controller.py`
- Create: `tests/test_developer_workflow_setup_controller.py`

- [ ] **Step 1: 写步骤、临时秘密清理和生产构建门禁失败测试**

```python
async def test_incomplete_setup_never_builds_runtime(controller, runtime_builder) -> None:
    await controller.test_step(SetupStep.ONES)
    with pytest.raises(SetupActionError, match="configuration is incomplete"):
        await controller.save_and_activate()
    assert runtime_builder.calls == []


async def test_cancel_clears_transient_secrets(controller) -> None:
    controller.set_secret(SecretKind.ONES_PASSWORD, "TOKEN-SECRET")
    controller.cancel_edit()
    assert controller.secret_presence(SecretKind.ONES_PASSWORD) is False
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_setup_controller.py -q`

Expected: collection FAIL。

- [ ] **Step 3: 实现固定状态机和脱敏 ViewModel 输出**

```python
class RuntimeBuilder(Protocol):
    def build(self, active: ActiveSetup, secrets: RuntimeSecrets) -> object:
        raise NotImplementedError


class SetupController:
    STEPS = (
        SetupStep.PROFILE,
        SetupStep.ONES,
        SetupStep.REPOSITORIES,
        SetupStep.PROVIDER,
        SetupStep.CODEX,
        SetupStep.PRIVATE_PATHS,
        SetupStep.REVIEW,
    )

    async def activate_existing(self) -> object | None:
        try:
            document = self._store.load_or_empty(profile_id=self._profile_id)
            if document.active is None:
                return None
            secrets = self._store.read_active_secrets(document)
            return await asyncio.to_thread(self._runtime_builder.build, document.active, secrets)
        except (SetupValidationError, CredentialStoreError, SetupStoreError):
            return None

    async def save_and_activate(self) -> object:
        if any(self._results.get(step) is None or not self._results[step].passed for step in self.STEPS[:-1]):
            raise SetupActionError("configuration is incomplete")
        candidate, secrets = self._build_candidate()
        document = self._store.commit(self._profile_id, candidate, secrets)
        handle: object | None = None
        try:
            active = document.active
            if active is None:
                raise SetupStoreError("active configuration is unavailable")
            persisted_secrets = self._store.read_active_secrets(document)
            handle = await asyncio.to_thread(self._runtime_builder.build, active, persisted_secrets)
            self._store.finalize_activation(document.profile_id)
            return handle
        except Exception:
            if handle is not None:
                close = getattr(handle, "close", None)
                if callable(close):
                    close()
            self._store.restore_previous(document.profile_id)
            raise SetupActionError("runtime validation failed") from None
        finally:
            self._clear_transient_secrets()
```

Controller 必须串行化 test/save 操作；错误消息只使用固定 category。所有公开 ViewModel 为 frozen/slots 且不包含 credential target、路径全文或原始异常。

- [ ] **Step 4: 运行 Controller 并发与崩溃回归**

Run: `uv run pytest tests/test_developer_workflow_setup_controller.py tests/test_developer_workflow_setup_store.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/developer_workflow/setup_controller.py tests/test_developer_workflow_setup_controller.py
git commit -m "feat(setup): orchestrate configuration validation"
```

---

### Task 8: 显式 RuntimeBootstrapper 与旧 CLI 兼容

**Files:**
- Create: `src/developer_workflow/runtime_bootstrap.py`
- Modify: `src/developer_workflow/codex_runner.py`
- Modify: `src/developer_workflow/cli.py`
- Modify: `tests/test_developer_workflow_cli.py`
- Create: `tests/test_developer_workflow_runtime_bootstrap.py`

- [ ] **Step 1: 写显式凭据和环境兼容失败测试**

```python
def test_bootstrap_uses_explicit_inputs_not_parent_environment(monkeypatch) -> None:
    monkeypatch.setenv("ONES_PASSWORD", "PARENT-SECRET")
    bootstrapper = RuntimeBootstrapper(
        service_graph_factory=recording_service_graph_factory,
        private_root_preparer=fake_private_root_preparer,
    )
    handle = bootstrapper.build(_active_setup(), _runtime_secrets(password="STORED-SECRET"))
    assert handle.gateway.settings.password == "STORED-SECRET"
    assert "PARENT-SECRET" not in handle.audit_values


def test_non_tui_factory_still_reads_existing_environment(monkeypatch) -> None:
    _set_legacy_runtime_env(monkeypatch)
    assert build_production_orchestrator(_config()) is not None
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_runtime_bootstrap.py tests/test_developer_workflow_cli.py -k "bootstrap or production" -q`

Expected: FAIL，现有工厂直接读取 `os.environ`。

- [ ] **Step 3: 抽取唯一显式构建边界**

```python
class RuntimeBootstrapper:
    def build(self, active: ActiveSetup, secrets: RuntimeSecrets) -> RuntimeHandle:
        settings = OnesSettings(
            base_url=active.runtime.ones_base_url,
            team_id=active.runtime.ones_team_id,
            issue_type_id=active.runtime.ones_issue_type_id,
            comment_list_path_template=active.runtime.ones_comment_list_path_template,
            email=secrets.require(SecretKind.ONES_EMAIL),
            password=secrets.require(SecretKind.ONES_PASSWORD),
        )
        return _build_service_graph(active.workflow, active.runtime, secrets, settings)


@dataclass(slots=True)
class RuntimeHandle:
    orchestrator: DeveloperWorkflowOrchestrator
    gateway: OnesGateway
    close_callback: Callable[[], None]

    def close(self) -> None:
        self.close_callback()


def build_production_orchestrator(config: DeveloperWorkflowConfig) -> DeveloperWorkflowOrchestrator:
    public, secrets = runtime_inputs_from_environment(config, os.environ)
    active = active_setup_from_legacy_config(config, public)
    return RuntimeBootstrapper().build(active, secrets).orchestrator
```

`CodexRunner` 增加 `environment_provider: Callable[[], Mapping[str, str]]`，默认返回 `os.environ`；TUI runtime 使用只含允许 Codex 凭据的映射。Provider token、Git credential 和身份也必须从显式 `RuntimeSecrets/RuntimePublicConfig` 注入。

- [ ] **Step 4: 运行生产服务图相邻回归**

Run: `uv run pytest tests/test_developer_workflow_runtime_bootstrap.py tests/test_developer_workflow_cli.py tests/test_developer_workflow_codex_runner.py tests/test_developer_workflow_repository.py -q`

Expected: PASS；真实 Git 慢组可拆分但必须覆盖全部 collected tests。

- [ ] **Step 5: 提交**

```bash
git add src/developer_workflow/runtime_bootstrap.py src/developer_workflow/codex_runner.py src/developer_workflow/cli.py tests/test_developer_workflow_cli.py tests/test_developer_workflow_runtime_bootstrap.py
git commit -m "refactor(setup): inject production runtime credentials"
```

---

### Task 9: TUI RuntimeSession 与双阶段 Host

**Files:**
- Create: `src/developer_workflow/tui/runtime_session.py`
- Create: `src/developer_workflow/tui/setup_screens.py`
- Modify: `src/developer_workflow/tui/app.py`
- Modify: `src/developer_workflow/tui/__init__.py`
- Modify: `src/developer_workflow/cli.py`
- Create: `tests/test_developer_workflow_tui_bootstrap.py`

- [ ] **Step 1: 写空配置启动和互斥 Controller 失败测试**

```python
async def test_incomplete_configuration_opens_setup_without_runtime(host_factory) -> None:
    app, runtime_builder = host_factory(document=SetupDocument(profile_id="default"))
    async with app.run_test() as pilot:
        assert isinstance(app.screen, SetupRootScreen)
        assert runtime_builder.calls == []
        assert not app.query("#run-list")


async def test_activation_closes_setup_before_dashboard(app_factory) -> None:
    async with app_factory().run_test() as pilot:
        await complete_setup(pilot)
        assert app.setup_controller.closed
        assert app.runtime_session is not None
        assert not app.query(".setup-secret-input")
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_tui_bootstrap.py -q`

Expected: FAIL，App 仍要求预先构造 `TuiController`。

- [ ] **Step 3: 实现 RuntimeSession 和 Host 切换**

```python
class TuiRuntimeSession:
    def __init__(self, controller: TuiController, max_concurrency: int, sink: Callable[[TaskEvent], None]):
        self.controller = controller
        self.supervisor = RunTaskSupervisor(max_concurrency, sink)

    async def close(self) -> None:
        await self.supervisor.close()
        self.controller.close()


class SetupRootScreen(Screen[RuntimeHandle | None]):
    """Minimal setup host; Task 10 replaces the body with the seven-step wizard."""

    def __init__(self, controller: SetupController) -> None:
        super().__init__(id="setup-root")
        self.controller = controller

    def compose(self) -> ComposeResult:
        yield Static("Runtime configuration is required", id="setup-required")


class DeveloperWorkflowTuiApp(App[None]):
    def __init__(self, setup_controller: SetupController, runtime_bootstrapper: RuntimeBootstrapper):
        super().__init__()
        self.setup_controller = setup_controller
        self.runtime_session: TuiRuntimeSession | None = None

    async def on_mount(self) -> None:
        handle = await self.setup_controller.activate_existing()
        if handle is None:
            await self.push_screen(SetupRootScreen(self.setup_controller), self._setup_done)
            return
        await self._replace_with_runtime(handle)
```

CLI 的 `tui` 分支必须在 `DeveloperWorkflowConfig.load(args.config)` 之前分发到 Host；`--help` 仍零配置/网络副作用。非 TUI 分支保持严格配置加载。

- [ ] **Step 4: 运行 Host、旧 Dashboard 和 CLI 回归**

Run: `uv run pytest tests/test_developer_workflow_tui_bootstrap.py tests/test_developer_workflow_tui_app.py tests/test_developer_workflow_cli.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/developer_workflow/tui/runtime_session.py src/developer_workflow/tui/setup_screens.py src/developer_workflow/tui/app.py src/developer_workflow/tui/__init__.py src/developer_workflow/cli.py tests/test_developer_workflow_tui_bootstrap.py
git commit -m "feat(tui): bootstrap into setup mode"
```

---

### Task 10: 七步配置 Screen 与安全 ViewModel

**Files:**
- Create: `src/developer_workflow/tui/setup_models.py`
- Modify: `src/developer_workflow/tui/setup_screens.py`
- Modify: `src/developer_workflow/tui/app.py`
- Modify: `src/developer_workflow/tui/tui.tcss`
- Modify: `tests/test_developer_workflow_tui_bootstrap.py`

- [ ] **Step 1: 写七步导航、密码控件和窄屏失败测试**

```python
@pytest.mark.parametrize("width,layout", [(60, "one"), (99, "two"), (120, "three")])
async def test_setup_layout_preserves_all_actions(app_factory, width, layout) -> None:
    async with app_factory().run_test(size=(width, 32)) as pilot:
        assert app.screen.has_class(layout)
        assert app.query_one("#test-connection")
        assert app.query_one("#next-step")


async def test_plain_enter_never_saves_secret(app_factory) -> None:
    async with app_factory().run_test() as pilot:
        await pilot.click("#ones-password")
        await pilot.press("enter")
        assert app.setup_controller.save_calls == []
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_tui_bootstrap.py -k "setup_layout or enter or step" -q`

Expected: FAIL，配置 Screen 尚不存在。

- [ ] **Step 3: 实现冻结 ViewModel 和七步 Screen**

```python
@dataclass(frozen=True, slots=True)
class SetupStepView:
    step: SetupStep
    label: str
    status: ValidationStatus
    summary: tuple[str, ...]
    can_test: bool
    can_continue: bool


class SetupWizardScreen(Screen[RuntimeHandle | None]):
    BINDINGS = [Binding("escape", "cancel_edit", "Cancel")]

    async def action_test_connection(self) -> None:
        self._clear_notice()
        result = await self.controller.test_step(self.current_step, self._read_transient_fields())
        self._render_fixed_result(result)

    def _read_transient_fields(self) -> Mapping[str, str]:
        return MappingProxyType({
            widget.id: widget.value
            for widget in self.query(Input)
            if widget.id is not None
        })

    def _clear_notice(self) -> None:
        self.query_one("#setup-notice", Static).update("")

    def _render_fixed_result(self, result: ConnectionTestResult) -> None:
        self.query_one("#setup-notice", Static).update(FIXED_RESULT_TEXT[result.category])
```

七个步骤必须分别有独立容器和明确按钮。密码/token 使用 `Input(password=True)`，离开步骤、取消、保存和 unmount 时调用 Controller 清理。ViewModel 只能包含固定 label、状态和脱敏摘要。

同时把 `DeveloperWorkflowTuiApp.on_mount()` 的配置分支从 `SetupRootScreen` 替换为 `SetupWizardScreen`；`SetupRootScreen` 仅保留为加载失败前的最小 fallback，不再作为正常配置入口。

- [ ] **Step 4: 运行完整配置交互测试**

Run: `uv run pytest tests/test_developer_workflow_tui_bootstrap.py -q`

Expected: PASS，覆盖键盘、鼠标、60/99/100/120 列边界、返回、取消和错误定位。

- [ ] **Step 5: 提交**

```bash
git add src/developer_workflow/tui/setup_models.py src/developer_workflow/tui/setup_screens.py src/developer_workflow/tui/app.py src/developer_workflow/tui/tui.tcss tests/test_developer_workflow_tui_bootstrap.py
git commit -m "feat(tui): add secure runtime setup wizard"
```

---

### Task 11: 保存启用、重新配置与崩溃恢复

**Files:**
- Modify: `src/developer_workflow/tui/setup_screens.py`
- Modify: `src/developer_workflow/tui/app.py`
- Modify: `src/developer_workflow/setup_controller.py`
- Create: `tests/test_developer_workflow_tui_setup_recovery.py`

- [ ] **Step 1: 写 generation 回滚、重配互斥和 orphan 清理失败测试**

```python
async def test_failed_activation_rolls_back_and_stays_in_setup(app_factory) -> None:
    app.runtime_builder.fail_next()
    async with app.run_test() as pilot:
        await complete_and_save(pilot)
        assert isinstance(app.screen, SetupWizardScreen)
        assert app.screen.current_step is SetupStep.REVIEW
        assert app.setup_store.load().active == OLD_ACTIVE
        assert app.runtime_session is None


async def test_reconfigure_closes_runtime_before_showing_secrets(app_factory) -> None:
    async with app_factory(active=True).run_test() as pilot:
        await pilot.click("#configure-runtime")
        assert app.old_runtime_session.closed
        assert app.runtime_session is None
        assert isinstance(app.screen, SetupWizardScreen)
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `uv run pytest tests/test_developer_workflow_tui_setup_recovery.py -q`

Expected: FAIL。

- [ ] **Step 3: 实现激活事务和恢复页**

```python
async def activate_candidate(self) -> None:
    try:
        handle = await self.controller.save_and_activate()
    except SetupActionError:
        self.show_setup_error("runtime validation failed")
        return
    await self._replace_with_runtime(handle)
```

恢复页只显示 previous 是否可用、orphan 数量和固定错误类别。清理 orphan 必须二次确认；普通 Enter 不执行。重新配置必须先关闭 runtime session，再载入不含秘密的草稿。

- [ ] **Step 4: 运行恢复、现有轮询和退出回归**

Run: `uv run pytest tests/test_developer_workflow_tui_setup_recovery.py tests/test_developer_workflow_tui_recovery.py tests/test_developer_workflow_tui_supervisor.py -q`

Expected: PASS，无 coroutine/thread/temp 残留。

- [ ] **Step 5: 提交**

```bash
git add src/developer_workflow/tui/setup_screens.py src/developer_workflow/tui/app.py src/developer_workflow/setup_controller.py tests/test_developer_workflow_tui_setup_recovery.py
git commit -m "feat(tui): activate and recover runtime configuration"
```

---

### Task 12: 安全 E2E、文档与发行物

**Files:**
- Create: `tests/test_developer_workflow_tui_setup_security.py`
- Modify: `tests/test_developer_workflow_tui_integration.py`
- Modify: `tests/test_developer_workflow_cli.py`
- Modify: `docs/ones_dev_cli.md`
- Modify: `MANIFEST.in`
- Modify: `pyproject.toml`

- [ ] **Step 1: 写空配置到 Dashboard 的完整失败测试**

```python
async def test_empty_setup_reaches_dashboard_without_pre_activation_effects(e2e_host) -> None:
    async with e2e_host.run_test() as pilot:
        await configure_all_steps(pilot)
        assert e2e_host.effects == []
        assert e2e_host.created_runs == ()
        assert e2e_host.created_worktrees == ()
        await save_and_activate(pilot)
        assert isinstance(e2e_host.screen, DashboardScreen)
        assert e2e_host.runtime_builder.calls == 1


async def test_every_setup_surface_contains_no_secret(secret_matrix_host) -> None:
    async with secret_matrix_host.run_test() as pilot:
        for surface in await visit_all_setup_surfaces(pilot):
            rendered = collect_raw_and_rich_widget_data(surface)
            for secret in secret_matrix_host.secrets:
                assert secret not in rendered


async def test_configured_group_runs_existing_requirement_flow_to_completion(
    empty_group_setup_host,
) -> None:
    async with empty_group_setup_host.run_test() as pilot:
        await configure_all_steps(pilot, repositories=("sdk", "app"), primary="app")
        await save_and_activate(pilot)
        await start_requirement_from_dashboard(pilot, "REQ-SETUP-E2E", mapping="product")
        await wait_for_state(pilot, WorkflowState.WAITING_APPROVAL)
        assert empty_group_setup_host.effects == []
        await approve_current_run(pilot, actor="operator")
        await wait_for_state(pilot, WorkflowState.COMPLETED)
        assert empty_group_setup_host.repository_order == ("sdk", "app")
        assert empty_group_setup_host.commit_counts == {"sdk": 1, "app": 1}
        assert empty_group_setup_host.push_counts == {"sdk": 1, "app": 1}
        assert empty_group_setup_host.pr_counts == {"sdk": 1, "app": 1}
        assert empty_group_setup_host.comment_count == 1
        assert empty_group_setup_host.ones_status_updates == 0
```

- [ ] **Step 2: 运行测试并确认 RED 或现有基线**

Run: `uv run pytest tests/test_developer_workflow_tui_setup_security.py tests/test_developer_workflow_tui_integration.py -q`

Expected: 新 E2E 先因缺少完整 fixture 或泄漏面失败；不得接受未访问全部 Screen 的假阳性。

- [ ] **Step 3: 补齐文档与 package data**

文档必须提供以下实际流程：

```powershell
uv run ones-dev tui --config docs/examples/ones-dev.config.json
```

并明确：示例文件只导入、不改写；首次进入配置模式；凭据存入 Windows Credential Manager；profile 只能选择管理员已安装项；全部验证后进入 Dashboard；非交互 CLI 仍使用环境变量。

`MANIFEST.in` 和 package-data 必须包含 `tui/setup_screens.py` 所需的 `.tcss`/资源，不包含 `%LOCALAPPDATA%` 配置、Credential target、`.env`、tests、临时目录或 data。

- [ ] **Step 4: 运行完整回归和发行物审计**

Run:

```powershell
uv run pytest tests/test_developer_workflow_setup_*.py tests/test_developer_workflow_tui_*.py tests/test_developer_workflow_cli.py -q
uv run pytest tests/test_developer_workflow_repository.py tests/test_ones.py tests/test_ones_gateway.py tests/test_developer_workflow_security.py -q
uv lock --check
uv run python -m compileall -q src/developer_workflow tests
git diff --check
uv run ones-dev tui --help
uv build --offline
```

Expected: 全部 exit 0；未显式授权时 LAN smoke 不运行。审计 wheel/sdist 必须包含 TUI setup 模块、`tui.tcss`、schema、`server.py`、`main.py`，排除 secrets、tests 和临时目录。隔离 venv `--no-deps` 安装后从仓库外执行 help/import/resource smoke。

- [ ] **Step 5: 提交**

```bash
git add tests/test_developer_workflow_tui_setup_security.py tests/test_developer_workflow_tui_integration.py tests/test_developer_workflow_cli.py docs/ones_dev_cli.md MANIFEST.in pyproject.toml
git commit -m "test(tui): verify secure bootstrap configuration"
```

---

## 最终审查清单

- [ ] 配置不完整时成功进入配置模式，而不是统一安全退出。
- [ ] 配置阶段没有生产 Orchestrator、run、mirror、worktree 或业务远端写。
- [ ] JSON、日志、通知、错误和 widget 不含秘密。
- [ ] Windows Credential target 和配置 generation 均不可由自由文本逃逸。
- [ ] profile 来自受信 catalog，且通过真实 capability probe。
- [ ] 单仓和仓库组配置覆盖全部现有契约字段。
- [ ] 保存失败和构建失败均回滚到旧 generation。
- [ ] 配置与 Dashboard Controller 不同时活动。
- [ ] 非交互 CLI 和既有完整工作流回归不变。
- [ ] wheel/sdist 和隔离安装包含全部运行资源且不含用户数据。
