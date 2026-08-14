# TUI Runtime Managed Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让没有预装 Codex managed profile 的用户在 TUI 运行期间明确确认、验证并启用固定安全工作区 profile，而不修改用户 Codex 配置文件。

**Architecture:** 在持久化工作流中显式记录 profile 来源；用统一的结构化 Codex argv 解析器驱动 doctor、sandbox probe 和正式 runtime；Controller 以受管异步事务验证固定内置 profile，TUI 仅在用户确认且真实能力探测通过后解锁后续步骤。外部 managed profiles 保持原有 fail-closed 发现流程，内置 profile 不伪装成已安装配置。

**Tech Stack:** Python 3.11、Pydantic v2、asyncio、Textual、pytest、Windows subprocess/ACL/reparse-point 安全边界。

---

## 文件结构与职责

- Modify: `src/developer_workflow/config.py` — 定义并持久化 profile 来源，校验名称与来源组合。
- Modify: `src/developer_workflow/setup_models.py` — 将来源字段带入可编辑 `WorkflowDraft`。
- Create: `src/developer_workflow/codex_runtime.py` — 锁定并验证 OpenAI 签名的原生来源，准备和复核私有 Codex 运行时。
- Modify: `src/developer_workflow/codex_runner.py` — 提供安全、不可变的 Codex argv 前缀与进程错误边界。
- Modify: `src/developer_workflow/requirement_flow.py` — 统一根据 profile 来源构造 sandbox 命令。
- Modify: `src/developer_workflow/setup_validation.py` — doctor 使用统一 Codex 命令，并提供内置 profile 的真实能力验证边界。
- Modify: `src/developer_workflow/setup_controller.py` — 以 operation lock、revision CAS 和取消收割管理确认事务。
- Modify: `src/developer_workflow/runtime_bootstrap.py` — preflight 与正式 runner 使用同一 profile 来源和 executor 构造。
- Modify: `src/developer_workflow/tui/setup_screens.py` — 提供显式创建/确认/重试 UI，保持 detached/cancel 生命周期安全。
- Modify: `src/developer_workflow/cli.py` — 现有非 TUI profile 验证显式使用 `managed` 来源。
- Modify tests under `tests/test_developer_workflow_{config,codex_runner,requirement,setup_validation,setup_controller,runtime_bootstrap,tui_bootstrap}.py` — 分层覆盖模型、命令、事务、UI 和端到端行为。

## 固定契约

以下常量和类型在后续任务中保持一致：

```python
class SandboxPermissionProfileSource(str, Enum):
    MANAGED = "managed"
    BUILTIN_WORKSPACE = "builtin_workspace"


BUILTIN_WORKSPACE_PROFILE = "ones-dev-workspace"
BUILTIN_WORKSPACE_OVERRIDE = (
    'permissions.ones-dev-workspace.extends=":workspace"'
)
```

旧 JSON 缺少来源字段时迁移为 `managed`，并继续要求外部目录验证；绝不自动迁移为 `builtin_workspace`。

---

### Task 1: Persist profile source without weakening legacy validation

**Files:**
- Modify: `src/developer_workflow/config.py:25-115`
- Modify: `src/developer_workflow/setup_models.py:349-380`
- Test: `tests/test_developer_workflow_config.py`
- Test: `tests/test_developer_workflow_setup_models.py`

- [ ] **Step 1: Write failing contract tests**

Add tests that express the exact persisted combinations:

```python
def test_workflow_config_defaults_legacy_profile_source_to_managed() -> None:
    raw = _valid_workflow_document()
    raw.pop("sandbox_permission_profile_source", None)
    config = DeveloperWorkflowConfig.model_validate(raw)
    assert config.sandbox_permission_profile_source is SandboxPermissionProfileSource.MANAGED


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("managed-dev", "managed"),
        (BUILTIN_WORKSPACE_PROFILE, "builtin_workspace"),
    ],
)
def test_workflow_config_accepts_exact_profile_source_pairs(name: str, source: str) -> None:
    raw = _valid_workflow_document()
    raw["sandbox_permission_profile"] = name
    raw["sandbox_permission_profile_source"] = source
    parsed = DeveloperWorkflowConfig.model_validate(raw)
    assert parsed.sandbox_permission_profile == name
    assert parsed.sandbox_permission_profile_source.value == source


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("managed-dev", "builtin_workspace"),
        (BUILTIN_WORKSPACE_PROFILE, "managed"),
        (BUILTIN_WORKSPACE_PROFILE, "unknown"),
    ],
)
def test_workflow_config_rejects_profile_source_confusion(name: str, source: str) -> None:
    raw = _valid_workflow_document()
    raw["sandbox_permission_profile"] = name
    raw["sandbox_permission_profile_source"] = source
    with pytest.raises(ValueError, match="sandbox permission profile is invalid"):
        DeveloperWorkflowConfig.model_validate(raw)
```

Add a `WorkflowDraft` round-trip test proving deep copies preserve the enum and model dumps emit the stable string.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
uv run pytest tests/test_developer_workflow_config.py tests/test_developer_workflow_setup_models.py -k "profile_source or legacy_profile" -q
```

Expected: failures because the enum and field do not exist.

- [ ] **Step 3: Implement the strict model contract**

In `config.py`, add the enum/constants and field:

```python
class SandboxPermissionProfileSource(str, Enum):
    MANAGED = "managed"
    BUILTIN_WORKSPACE = "builtin_workspace"


BUILTIN_WORKSPACE_PROFILE = "ones-dev-workspace"


class DeveloperWorkflowConfig(WorkflowModel):
    sandbox_permission_profile: str
    sandbox_permission_profile_source: SandboxPermissionProfileSource = (
        SandboxPermissionProfileSource.MANAGED
    )

    @model_validator(mode="after")
    def validate_profile_source(self) -> DeveloperWorkflowConfig:
        builtin = self.sandbox_permission_profile_source is (
            SandboxPermissionProfileSource.BUILTIN_WORKSPACE
        )
        if builtin != (self.sandbox_permission_profile == BUILTIN_WORKSPACE_PROFILE):
            raise ValueError("sandbox permission profile is invalid")
        return self
```

Mirror the field in `WorkflowDraft`, using the same enum and default. Keep existing profile-name validators; do not accept colon-prefixed built-ins or arbitrary source strings.

- [ ] **Step 4: Run focused and adjacent tests**

Run:

```powershell
uv run pytest tests/test_developer_workflow_config.py tests/test_developer_workflow_setup_models.py tests/test_developer_workflow_setup_store.py -q
```

Expected: all pass; legacy stored documents load as `managed`.

- [ ] **Step 5: Commit**

```powershell
git add src/developer_workflow/config.py src/developer_workflow/setup_models.py tests/test_developer_workflow_config.py tests/test_developer_workflow_setup_models.py
git commit -m "feat(setup): persist sandbox profile sources"
```

---

### Task 2A: Discover and lock the signed native Codex payload

**Files:**
- Create: `src/developer_workflow/codex_runtime.py`
- Modify: `src/developer_workflow/codex_runner.py`
- Create: `tests/test_developer_workflow_codex_runtime.py`
- Modify: `tests/test_developer_workflow_codex_runner.py`

- [ ] **Step 1: Write failing native-source tests**

Add dependency-injected tests for the fixed npm native layout and Windows trust boundary:

```python
def test_discover_native_source_uses_fixed_platform_payload_without_node(
    tmp_path: Path,
) -> None:
    root = tmp_path / "node"
    shim = _regular_file(root / "codex.cmd", b"NEVER-EXECUTED")
    native = _regular_file(
        root / "node_modules/@openai/codex/node_modules/@openai/"
        "codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe",
        b"SIGNED-NATIVE-PAYLOAD",
    )
    source = discover_locked_native_codex(
        which=lambda name: str(shim) if name == "codex.cmd" else None,
        opener=_fake_locked_opener(native),
        signature_verifier=_valid_openai_signature,
        platform="win32",
    )
    try:
        assert source.publisher == "OpenAI OpCo, LLC"
        assert source.size == len(b"SIGNED-NATIVE-PAYLOAD")
        assert source.read_chunk(4096) == b"SIGNED-NATIVE-PAYLOAD"
    finally:
        source.close()
```

Add negative tests for missing/fake layout, `.cmd` contents pointing elsewhere, nonregular/reparse source, repository alias, identity race, invalid signature, wrong publisher, trust API failure, and a second writer/delete handle while the source is locked. Assert no test executes Node, JavaScript, a shim, PowerShell or a shell.

- [ ] **Step 2: Run and verify RED**

```powershell
uv run pytest tests/test_developer_workflow_codex_runtime.py -k "native_source or signature or publisher" -q
```

Expected: collection fails because `codex_runtime` and its source API do not exist.

- [ ] **Step 3: Implement the locked source and WinVerifyTrust adapter**

Create these production-facing types:

```python
OPENAI_AUTHENTICODE_PUBLISHER = "OpenAI OpCo, LLC"
NATIVE_CODEX_RELATIVE_PATH = Path(
    "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/"
    "vendor/x86_64-pc-windows-msvc/bin/codex.exe"
)


@dataclass(frozen=True, slots=True)
class NativeCodexIdentity:
    volume_serial: int
    file_index: int
    size: int
    mtime_ns: int


@dataclass(slots=True, repr=False)
class LockedNativeCodex:
    descriptor: int = field(repr=False)
    identity: NativeCodexIdentity
    size: int
    publisher: str
    _closed: bool = False

    def read_chunk(self, size: int) -> bytes: ...
    def rewind(self) -> None: ...
    def close(self) -> None: ...
```

The real opener uses `CreateFileW` with `FILE_SHARE_READ` only, `OPEN_EXISTING`, `FILE_FLAG_OPEN_REPARSE_POINT`, and no write/delete sharing. Validate the fixed path lexically and by final handle path, require a regular non-reparse file, bind Windows volume serial/file index/size/mtime, and reject a source inside any repository/worktree by stable identity.

Implement a `WinVerifyTrust` ctypes adapter using `WINTRUST_ACTION_GENERIC_VERIFY_V2`; close trust state on every exit. After trust succeeds, read the signing certificate and require the exact publisher organization `OpenAI OpCo, LLC`. Do not call PowerShell or trust a certificate thumbprint. Catch only ordinary OS/trust-format errors; propagate `MemoryError`, `asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit`, and `GeneratorExit` unchanged. Raise the fixed public `CodexProcessStartError("Codex executable is unavailable")` outside raw handlers with empty cause/context and scrubbed project-frame locals.

- [ ] **Step 4: Remove the unsafe Node/JS resolver path**

Keep `CodexCommand` in `codex_runner.py`, but delete production behavior that returns `(node.exe, codex.js)`. `CodexCommand.argv()` continues to reject empty/NUL prefix and arguments. No production API may return `.cmd`, `.ps1`, `.js`, `node.exe`, or an external npm-native path as an executable prefix.

- [ ] **Step 5: Run focused tests and commit**

```powershell
uv run pytest tests/test_developer_workflow_codex_runtime.py tests/test_developer_workflow_codex_runner.py -q
uv run python -m py_compile src/developer_workflow/codex_runtime.py src/developer_workflow/codex_runner.py
git diff --check
git add src/developer_workflow/codex_runtime.py src/developer_workflow/codex_runner.py tests/test_developer_workflow_codex_runtime.py tests/test_developer_workflow_codex_runner.py
git commit -m "feat(codex): verify native runtime payloads"
```

Expected: all focused tests pass; the source handle is closed exactly once on every path.

---

### Task 2B: Stage and reuse a private Codex runtime

**Files:**
- Modify: `src/developer_workflow/codex_runtime.py`
- Modify: `src/developer_workflow/codex_runner.py`
- Modify: `tests/test_developer_workflow_codex_runtime.py`

- [ ] **Step 1: Write failing private-staging tests**

```python
def test_preparer_copies_locked_payload_to_private_hash_directory(tmp_path: Path) -> None:
    source = _locked_source(b"SIGNED-CODEX", publisher=OPENAI_AUTHENTICODE_PUBLISHER)
    runner = RecordingRunner(returncode=0, stdout="codex-cli 0.147.0\n")
    preparer = CodexRuntimePreparer(
        cache_root=tmp_path / "private-runtime",
        source_factory=lambda: source,
        signature_verifier=_valid_openai_signature,
        directory_preparer=_prepare_test_private_directory,
        process_runner=runner,
    )
    command = preparer.prepare()
    digest = hashlib.sha256(b"SIGNED-CODEX").hexdigest()
    assert command.prefix == (str((tmp_path / "private-runtime" / digest / "codex.exe").resolve()),)
    assert runner.argv == [*command.prefix, "--version"]
    assert source.closed is True
```

Add RED tests for source replacement/truncation, partial read, SHA mismatch, destination reparse, unsafe ACL, destination signature/publisher mismatch, disk-full/write/fsync/atomic-replace failure, manifest corruption/path injection, cancellation, exact temp cleanup, and old trusted cache preservation. Add reuse tests proving unchanged source avoids copy, a changed source prepares a new hash directory, and a valid cached runtime works when the npm source is absent.

- [ ] **Step 2: Run and verify RED**

```powershell
uv run pytest tests/test_developer_workflow_codex_runtime.py -k "preparer or private_runtime or cache" -q
```

Expected: failures because `CodexRuntimePreparer` is absent.

- [ ] **Step 3: Implement private staging and immutable manifest**

```python
@dataclass(slots=True)
class CodexRuntimePreparer:
    cache_root: Path
    source_factory: Callable[[], LockedNativeCodex]
    signature_verifier: SignatureVerifier
    directory_preparer: Callable[[Path], Path]
    process_runner: CommandExecutor
    copy_buffer_bytes: int = 1024 * 1024

    @classmethod
    def production(cls) -> CodexRuntimePreparer:
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise CodexProcessStartError("Codex executable is unavailable")
        return cls(
            cache_root=Path(local) / "ones-dev" / "codex-runtime",
            source_factory=discover_locked_native_codex,
            signature_verifier=WindowsAuthenticodeVerifier(),
            directory_preparer=prepare_private_directory,
            process_runner=_bounded_subprocess,
        )

    def prepare(self) -> CodexCommand: ...
```

`prepare()` first validates existing private manifests without using manifest paths as filesystem targets. A valid cache requires protected non-reparse ancestors, exact schema/keys/types, target basename `codex.exe`, recomputed SHA-256 equal to directory name and manifest, valid OpenAI signature, stable file identity, and a bounded `--version` smoke with `shell=False` and cleaned environment.

When staging, create a random directory only beneath the validated private root, stream from the locked descriptor into a random file, update SHA-256, flush/fsync, recheck source identity, set the protected destination ACL, verify destination hash/signature/publisher, and atomically rename within the same private root. Write the manifest through a separate random file plus fsync/atomic replace. Publish the manifest last; a directory without a valid manifest is never executable. Attempt every owned-temp cleanup on failure, but never delete an existing trusted version.

- [ ] **Step 4: Verify the real installed signature without copying 299 MB in the unit suite**

Add a Windows-only test that locates the fixed installed native payload, opens it with the production locked opener, verifies `publisher == OPENAI_AUTHENTICODE_PUBLISHER`, and closes it. Skip only when that exact npm layout is absent. Do not run the external source directly. Keep the 299 MB copy/smoke as an explicitly marked acceptance test executed once in Task 7.

- [ ] **Step 5: Run tests and commit**

```powershell
uv run pytest tests/test_developer_workflow_codex_runtime.py tests/test_developer_workflow_codex_runner.py -q
uv run python -m py_compile src/developer_workflow/codex_runtime.py src/developer_workflow/codex_runner.py
git diff --check
git add src/developer_workflow/codex_runtime.py src/developer_workflow/codex_runner.py tests/test_developer_workflow_codex_runtime.py
git commit -m "feat(codex): stage private signed runtimes"
```

Expected: all focused tests pass; production source verification runs on this Windows host without executing npm/Node/JS/shims.

---

### Task 3: Make doctor and sandbox execution share command/profile construction

**Files:**
- Modify: `src/developer_workflow/requirement_flow.py:286-430`
- Modify: `src/developer_workflow/setup_validation.py:240-640`
- Modify: `src/developer_workflow/cli.py:500-535`
- Test: `tests/test_developer_workflow_requirement.py`
- Test: `tests/test_developer_workflow_setup_validation.py`
- Test: `tests/test_developer_workflow_cli.py`

- [ ] **Step 1: Write failing exact-argv tests**

Add tests using a fake backend that records argv:

```python
def test_builtin_workspace_executor_injects_only_fixed_profile_override(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    private_codex = _safe_private_codex(tmp_path)
    executor = SandboxCommandExecutor(
        permission_profile=BUILTIN_WORKSPACE_PROFILE,
        permission_profile_source=SandboxPermissionProfileSource.BUILTIN_WORKSPACE,
        codex_command=CodexCommand((str(private_codex),)),
        backend_executor=_record_success(calls),
    )
    executor([sys.executable, "-I", "-c", "print('ok')"], cwd=tmp_path, env={}, timeout=2, max_output_bytes=4096)
    assert calls[-1][:6] == [
        str(private_codex), "-c", BUILTIN_WORKSPACE_OVERRIDE,
        "sandbox", "--permission-profile", BUILTIN_WORKSPACE_PROFILE,
    ]
    assert all("shell" not in call for call in calls)


def test_managed_executor_does_not_inject_builtin_override(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    executor = SandboxCommandExecutor(
        permission_profile="managed-dev",
        permission_profile_source=SandboxPermissionProfileSource.MANAGED,
        codex_command=CodexCommand(("codex.exe",)),
        backend_executor=_record_success(calls),
    )
    executor(sandbox_preflight_command(), cwd=tmp_path, env={}, timeout=2, max_output_bytes=4096)
    assert BUILTIN_WORKSPACE_OVERRIDE not in calls[-1]
```

Add negative tests for source/name mismatch, user-supplied override, empty command prefix, and doctor receiving exactly `[*prefix, "doctor", "--json"]` with `shell=False`.

- [ ] **Step 2: Run and verify RED**

```powershell
uv run pytest tests/test_developer_workflow_requirement.py tests/test_developer_workflow_setup_validation.py -k "builtin_workspace or command_prefix or doctor_argv" -q
```

Expected: constructors lack source/command fields and doctor still calls bare `codex`.

- [ ] **Step 3: Implement one command builder**

Change `SandboxCommandExecutor` to:

```python
permission_profile: str | None = None
permission_profile_source: SandboxPermissionProfileSource = SandboxPermissionProfileSource.MANAGED
codex_command: CodexCommand | None = None

def _command(self) -> CodexCommand:
    return self.codex_command or CodexRuntimePreparer.production().prepare()

def _sandbox_prefix(self, canonical_cwd: Path) -> list[str]:
    prefix = list(self._command().prefix)
    if self.permission_profile_source is SandboxPermissionProfileSource.BUILTIN_WORKSPACE:
        prefix.extend(["-c", BUILTIN_WORKSPACE_OVERRIDE])
    prefix.extend([
        "sandbox", "--permission-profile", self.permission_profile,
        "--include-managed-config", "-C", str(canonical_cwd),
    ])
    return prefix
```

Validate source/name pairs in `__post_init__`. Preserve the state-provider branch and all existing outside-write/network/environment probes.

Update `SubprocessDoctorRunner` to accept a preparer callable and build `command.argv("doctor", "--json")`. Production injects one shared `CodexRuntimePreparer` instance so doctor, capability probe and later runtime reuse the same verified private command. `ManagedSandboxExecutorFactory` passes `MANAGED`; add a factory method that builds the fixed `BUILTIN_WORKSPACE` executor. CLI profile validation explicitly constructs the managed form.

- [ ] **Step 4: Run focused regressions**

```powershell
uv run pytest tests/test_developer_workflow_requirement.py tests/test_developer_workflow_setup_validation.py tests/test_developer_workflow_cli.py -q
```

Expected: all pass; no test observes `.cmd` execution or a shell invocation.

- [ ] **Step 5: Commit**

```powershell
git add src/developer_workflow/requirement_flow.py src/developer_workflow/setup_validation.py src/developer_workflow/cli.py tests/test_developer_workflow_requirement.py tests/test_developer_workflow_setup_validation.py tests/test_developer_workflow_cli.py
git commit -m "feat(setup): support fixed workspace sandbox profiles"
```

---

### Task 4: Add an atomic Controller operation for builtin profile confirmation

**Files:**
- Modify: `src/developer_workflow/setup_validation.py:537-640`
- Modify: `src/developer_workflow/setup_controller.py:240-370,1180-1250`
- Test: `tests/test_developer_workflow_setup_validation.py`
- Test: `tests/test_developer_workflow_setup_controller.py`

- [ ] **Step 1: Write failing verification and transaction tests**

Add a catalog method test that proves it uses the fixed descriptor and full existing executor probe. Then add Controller tests:

```python
@pytest.mark.asyncio
async def test_confirm_builtin_profile_commits_only_after_probe() -> None:
    gate = threading.Event()
    catalog = BlockingBuiltinCatalog(gate)
    controller = _controller(profile_catalog=catalog)
    before = controller.draft.model_copy(deep=True)
    task = asyncio.create_task(controller.confirm_builtin_workspace_profile())
    await catalog.started.wait()
    assert controller.draft == before
    gate.set()
    await task
    assert controller.draft.workflow.sandbox_permission_profile == BUILTIN_WORKSPACE_PROFILE
    assert controller.draft.workflow.sandbox_permission_profile_source is SandboxPermissionProfileSource.BUILTIN_WORKSPACE


@pytest.mark.asyncio
async def test_confirm_builtin_profile_rejects_stale_revision_without_mutation() -> None:
    controller, catalog = _blocking_builtin_controller()
    task = asyncio.create_task(controller.confirm_builtin_workspace_profile())
    await catalog.started.wait()
    controller.apply_workflow(_different_workflow(), changed_step=SetupStep.PRIVATE_PATHS)
    catalog.release.set()
    with pytest.raises(SetupActionError, match="configuration changed during validation"):
        await task
    assert controller.draft.workflow.sandbox_permission_profile != BUILTIN_WORKSPACE_PROFILE
```

Also cover probe failure, cancellation, `close()` during probe, repeated confirmation, downstream result invalidation, and `cause/context` sanitization.

- [ ] **Step 2: Run and verify RED**

```powershell
uv run pytest tests/test_developer_workflow_setup_validation.py tests/test_developer_workflow_setup_controller.py -k "builtin_workspace" -q
```

Expected: missing catalog/controller methods.

- [ ] **Step 3: Implement verification and atomic commit**

In `ManagedProfileCatalog` add:

```python
def verify_builtin_workspace_profile(self, *, timeout_seconds: float | None = None) -> str:
    # Prepare the same private capability root as list_profiles, construct the
    # BUILTIN_WORKSPACE executor, run sandbox_preflight_command, and return only
    # the fixed name when returncode is zero. All failures become a fixed
    # SetupValidationError outside raw exception handlers.
```

In `SetupController` add an async operation that:

```python
async def confirm_builtin_workspace_profile(self) -> None:
    async with self._operation_lock:
        self._ensure_mutable()
        owner = asyncio.current_task()
        starting_revision = self._revision
        task = asyncio.create_task(asyncio.to_thread(
            self._profile_catalog.verify_builtin_workspace_profile
        ))
        # shield, register late task on cancellation, and clear ownership using
        # the same lifecycle rules as list_managed_profiles.
        await asyncio.shield(task)
        if self._revision != starting_revision:
            raise SetupActionError("configuration changed during validation")
        workflow = self._draft.workflow.model_copy(deep=True)
        workflow.sandbox_permission_profile = BUILTIN_WORKSPACE_PROFILE
        workflow.sandbox_permission_profile_source = (
            SandboxPermissionProfileSource.BUILTIN_WORKSPACE
        )
        self._apply_workflow_locked(workflow, changed_step=SetupStep.PROFILE)
```

Factor only the minimum private helper needed so public `apply_workflow` and the locked operation share identical validation/invalidation semantics.

- [ ] **Step 4: Run Controller and catalog regressions**

```powershell
uv run pytest tests/test_developer_workflow_setup_validation.py tests/test_developer_workflow_setup_controller.py -q
```

Expected: all pass; cancellation and close tests leave no late draft mutation.

- [ ] **Step 5: Commit**

```powershell
git add src/developer_workflow/setup_validation.py src/developer_workflow/setup_controller.py tests/test_developer_workflow_setup_validation.py tests/test_developer_workflow_setup_controller.py
git commit -m "feat(setup): confirm builtin workspace profiles atomically"
```

---

### Task 5: Expose explicit create/confirm/retry interaction in the TUI

**Files:**
- Modify: `src/developer_workflow/tui/setup_screens.py:350-500,620-700`
- Test: `tests/test_developer_workflow_tui_bootstrap.py`

- [ ] **Step 1: Write failing Textual interaction tests**

Add tests using a controller whose managed list is empty and whose builtin operation is observable:

```python
@pytest.mark.asyncio
async def test_empty_catalog_offers_explicit_safe_profile_creation() -> None:
    controller = EmptyCatalogController()
    async with _mounted_wizard(controller) as (pilot, wizard):
        button = wizard.query_one("#create-workspace-profile", Button)
        assert button.disabled is False
        assert wizard.query_one("#next", Button).disabled is True
        await pilot.click("#create-workspace-profile")
        assert wizard.app.screen.query_one("#confirm-workspace-profile", Button)
        assert controller.confirm_calls == 0


@pytest.mark.asyncio
async def test_confirm_success_selects_profile_and_unlocks_test() -> None:
    controller = SuccessfulBuiltinController()
    async with _mounted_wizard(controller) as (pilot, wizard):
        await pilot.click("#create-workspace-profile")
        await pilot.click("#confirm-workspace-profile")
        await _wait_until(lambda: controller.confirm_calls == 1)
        assert wizard.query_one("#sandbox-profile", Select).value == BUILTIN_WORKSPACE_PROFILE
        assert wizard.query_one("#test-connection", Button).disabled is False
```

Add tests for Cancel (zero calls), ordinary Enter (zero calls), failure then Retry, duplicate clicks, Escape during probe, unmount during probe, fixed notices and canary-free rendered/Rich surfaces.

- [ ] **Step 2: Run and verify RED**

```powershell
uv run pytest tests/test_developer_workflow_tui_bootstrap.py -k "workspace_profile or empty_catalog" -q
```

Expected: create/confirm widgets are absent and the page remains blocked.

- [ ] **Step 3: Implement the confirmation screen and wizard state**

Add a `ModalScreen[bool]` with fixed copy and explicit buttons:

```python
class BuiltinWorkspaceProfileConfirmation(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        yield Static(
            "Enable the fixed ones-dev workspace profile. "
            "It must pass inside-write, outside-deny, network-deny, and environment isolation checks."
        )
        yield Button("Confirm", id="confirm-workspace-profile", variant="warning")
        yield Button("Back", id="cancel-workspace-profile")

    @on(Button.Pressed, "#confirm-workspace-profile")
    def confirm(self) -> None:
        self.dismiss(True)
```

In the Profile container add `Button("Create safe workspace profile", id="create-workspace-profile")`. When the managed list is empty, keep both Select widgets disabled but enable this button. On explicit modal success, launch a screen-owned task that awaits `controller.confirm_builtin_workspace_profile()`. Reuse generation/attached checks; on success add the fixed option to both Selects, select it and render. On failure show only `Safe workspace profile could not be verified` and change the button label to `Retry safe workspace profile`.

Do not bind plain Enter to confirmation. Escape cancels the pending screen task through the existing supervisor/cancel lifecycle without clearing previously accepted secrets from unrelated steps.

- [ ] **Step 4: Run the full TUI bootstrap file**

```powershell
uv run pytest tests/test_developer_workflow_tui_bootstrap.py -q
```

Expected: all pass, including existing managed Select synchronization and close/cancellation races.

- [ ] **Step 5: Commit**

```powershell
git add src/developer_workflow/tui/setup_screens.py tests/test_developer_workflow_tui_bootstrap.py
git commit -m "feat(tui): create safe workspace profiles at runtime"
```

---

### Task 6: Carry the profile source through runtime preflight and execution

**Files:**
- Modify: `src/developer_workflow/runtime_bootstrap.py:140-170,390-465`
- Modify: `src/developer_workflow/setup_controller.py:1218-1232`
- Test: `tests/test_developer_workflow_runtime_bootstrap.py`
- Test: `tests/test_developer_workflow_setup_controller.py`

- [ ] **Step 1: Write failing handoff tests**

```python
def test_runtime_uses_builtin_source_for_preflight_and_test_runner() -> None:
    workflow = _workflow(
        sandbox_permission_profile=BUILTIN_WORKSPACE_PROFILE,
        sandbox_permission_profile_source=SandboxPermissionProfileSource.BUILTIN_WORKSPACE,
    )
    factory = RecordingSandboxFactory()
    handle = _runtime_bootstrap(sandbox_factory=factory).build(_active_setup(workflow), _secrets())
    try:
        assert factory.calls == [
            (BUILTIN_WORKSPACE_PROFILE, SandboxPermissionProfileSource.BUILTIN_WORKSPACE),
            (BUILTIN_WORKSPACE_PROFILE, SandboxPermissionProfileSource.BUILTIN_WORKSPACE),
        ]
    finally:
        handle.close()


@pytest.mark.asyncio
async def test_profile_retest_uses_selected_source() -> None:
    controller = _controller_with_builtin_profile()
    result = await controller.test_step(SetupStep.PROFILE, MappingProxyType({}))
    assert result.status is ValidationStatus.PASSED
    assert controller.catalog.required == [
        (BUILTIN_WORKSPACE_PROFILE, SandboxPermissionProfileSource.BUILTIN_WORKSPACE)
    ]
```

Add a mismatch test proving runtime rejects builtin name + managed source before private root creation, credentials read or executor construction.

- [ ] **Step 2: Run and verify RED**

```powershell
uv run pytest tests/test_developer_workflow_runtime_bootstrap.py tests/test_developer_workflow_setup_controller.py -k "builtin_source or selected_source" -q
```

Expected: factory/call signatures only accept the name and lose the source.

- [ ] **Step 3: Update runtime and profile retest wiring**

Change the sandbox factory protocol and production construction to accept both values:

```python
class SandboxFactory(Protocol):
    def __call__(
        self,
        profile: str,
        source: SandboxPermissionProfileSource,
    ) -> SandboxCommandExecutor: ...
```

Pass `workflow.sandbox_permission_profile_source` to `_validate_sandbox_capability` and the final `test_runner`. In Controller `_probe(PROFILE)`, call a source-aware `require_selected`; it verifies catalog membership only for `managed`, and invokes the fixed builtin verifier for `builtin_workspace`. Never infer source from the profile name at runtime.

- [ ] **Step 4: Run runtime/controller/store regressions**

```powershell
uv run pytest tests/test_developer_workflow_runtime_bootstrap.py tests/test_developer_workflow_setup_controller.py tests/test_developer_workflow_setup_store.py -q
```

Expected: all pass; serialized source survives store load and reaches both runtime executors.

- [ ] **Step 5: Commit**

```powershell
git add src/developer_workflow/runtime_bootstrap.py src/developer_workflow/setup_controller.py tests/test_developer_workflow_runtime_bootstrap.py tests/test_developer_workflow_setup_controller.py
git commit -m "fix(runtime): preserve sandbox profile provenance"
```

---

### Task 7: Prove the no-preconfiguration path end to end

**Files:**
- Modify: `tests/test_developer_workflow_tui_setup_security.py`
- Modify: `tests/test_developer_workflow_tui_integration.py`
- Modify: `docs/ones_dev_cli.md`

- [ ] **Step 1: Add the failing cold-start acceptance test**

Build a production-shaped host with:

- no user/admin profiles;
- a fake safe Codex argv prefix backend that enforces the real executor’s inside/outside/network/environment probes;
- no Git executable;
- a real `SetupController`, `SetupStore`, transaction path and Textual wizard.

The test must drive only user-visible actions:

```python
@pytest.mark.asyncio
async def test_tui_creates_builtin_profile_without_preconfiguration(tmp_path: Path) -> None:
    before = _codex_config_facts(tmp_path)
    async with _production_shaped_tui(tmp_path, managed_profiles=()) as (pilot, app):
        wizard = app.screen
        await pilot.click("#create-workspace-profile")
        await pilot.click("#confirm-workspace-profile")
        await _wait_until(lambda: wizard.query_one("#sandbox-profile", Select).value == BUILTIN_WORKSPACE_PROFILE)
        await pilot.click("#test-connection")
        await _wait_until(lambda: wizard.view_model.current_status == "passed")
        assert wizard.query_one("#next", Button).disabled is False
    assert _codex_config_facts(tmp_path) == before
```

The backend must record exact argv and assert no Node, JavaScript, `.cmd`, `.ps1`, `shell=True`, secret-bearing environment, user config write or ACL change occurred. The accepted argv prefix must be the private hash-addressed native executable. Snapshot the npm source bytes/identity/ACL and prove they are unchanged after preparation.

- [ ] **Step 2: Run and verify RED**

```powershell
uv run pytest tests/test_developer_workflow_tui_setup_security.py tests/test_developer_workflow_tui_integration.py -k "without_preconfiguration" -q
```

Expected: the current wizard has no creation path.

- [ ] **Step 3: Add restart and managed-profile compatibility coverage**

Extend the acceptance test to save the setup, reload it and assert the runtime factory receives `BUILTIN_WORKSPACE`. Add a second test proving an existing catalog-backed managed profile still produces no `-c` override and does not show a forced confirmation.

- [ ] **Step 4: Update user documentation**

In `docs/ones_dev_cli.md`, document:

```markdown
首次启动不要求预先安装 Codex permission profile。Profile 页面没有可用配置时，
选择 **Create safe workspace profile**，阅读固定权限摘要并显式确认。ones-dev 会验证
工作区内写入、工作区外拒绝、网络拒绝和环境隔离；全部通过后才允许继续。

该操作不会修改 `~/.codex/config.toml` 或其 ACL。内置配置固定为
`ones-dev-workspace`，不能在 TUI 中扩大权限。

Windows 首次确认会验证已安装原生 Codex 的 OpenAI 数字签名，并准备约 299 MB
的私有副本。ones-dev 不会执行或修改 npm/NVM 中的 Node、JavaScript 或 shim；
相同版本后续直接复用经过哈希、签名和 ACL 复核的私有副本。
```

- [ ] **Step 5: Run focused security and integration tests**

```powershell
uv run pytest tests/test_developer_workflow_tui_setup_security.py -q
uv run pytest tests/test_developer_workflow_tui_integration.py -q
```

Expected: all pass. If the integration file exceeds the project’s established time bound, stop only that pytest process and run the new node plus existing bootstrap/security files separately; do not claim the interrupted group passed.

- [ ] **Step 6: Run the complete affected regression set**

```powershell
uv run pytest tests/test_developer_workflow_config.py tests/test_developer_workflow_setup_models.py tests/test_developer_workflow_codex_runner.py tests/test_developer_workflow_requirement.py tests/test_developer_workflow_setup_validation.py tests/test_developer_workflow_setup_controller.py tests/test_developer_workflow_runtime_bootstrap.py tests/test_developer_workflow_tui_bootstrap.py tests/test_developer_workflow_cli.py -q
uv run python -m compileall -q src/developer_workflow tests
git diff --check
```

Expected: pytest passes with only documented platform skips; compileall and diff-check exit 0.

- [ ] **Step 7: Manual smoke test**

From a process environment where `git` is absent and no `[permissions.*]` profile exists, run:

```powershell
uv run ones-dev tui
```

Expected: Profile page displays the creation button and the private-copy size notice. Confirm verifies the real installed OpenAI signature, copies the native executable once into the private hash directory, runs private `codex.exe --version`, then runs the sandbox probe. Success selects `ones-dev-workspace` and enables Test/Next. Confirm from a second fresh process reuses the same private digest without rewriting the 299 MB file. Capture only status/category metadata—never paths, environment values, credentials or raw command output.

- [ ] **Step 8: Commit**

```powershell
git add tests/test_developer_workflow_tui_setup_security.py tests/test_developer_workflow_tui_integration.py docs/ones_dev_cli.md
git commit -m "test(tui): verify runtime workspace profile setup"
```

---

## Final verification checklist

- [ ] No preinstalled profile: TUI remains usable and offers an explicit creation action.
- [ ] No confirmation: no draft, store, filesystem, environment or ACL mutation.
- [ ] Failed probe: no profile accepted and Next remains disabled.
- [ ] Successful probe: exact builtin name/source persist and reach runtime preflight plus test runner.
- [ ] Existing managed profiles retain catalog-only behavior and receive no builtin override.
- [ ] User `config.toml` bytes, identity, mtime and ACL remain unchanged.
- [ ] Windows npm source is never executed directly; the final prefix is only the signed, hashed, ACL-protected private native `codex.exe`.
- [ ] Git absence remains independently recoverable and does not block Profile creation.
- [ ] Cancellation, unmount, close and revision races leave no late state mutation.
- [ ] Public exceptions and UI surfaces contain no raw path, command, environment or secret data.
