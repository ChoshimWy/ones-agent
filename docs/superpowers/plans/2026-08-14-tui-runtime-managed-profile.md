# TUI Runtime Managed Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让没有预装 Codex managed profile 的用户在 TUI 运行期间明确确认、验证并启用固定安全工作区 profile，而不修改用户 Codex 配置文件。

**Architecture:** 在持久化工作流中显式记录 profile 来源；用统一的结构化 Codex argv 解析器驱动 doctor、sandbox probe 和正式 runtime；Controller 以受管异步事务验证固定内置 profile，TUI 仅在用户确认且真实能力探测通过后解锁后续步骤。外部 managed profiles 保持原有 fail-closed 发现流程，内置 profile 不伪装成已安装配置。

**Tech Stack:** Python 3.11、Pydantic v2、asyncio、Textual、pytest、Windows subprocess/ACL/reparse-point 安全边界。

---

## 文件结构与职责

- Modify: `src/developer_workflow/config.py` — 定义并持久化 profile 来源，校验名称与来源组合。
- Modify: `src/developer_workflow/setup_models.py` — 将来源字段带入可编辑 `WorkflowDraft`。
- Modify: `src/developer_workflow/codex_runner.py` — 解析安全、不可变的 Codex argv 前缀。
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

### Task 2: Resolve Codex as a safe argv prefix on Windows

**Files:**
- Modify: `src/developer_workflow/codex_runner.py`
- Test: `tests/test_developer_workflow_codex_runner.py`

- [ ] **Step 1: Write failing resolver tests**

Use a temporary fake install layout and dependency-injected `which`, identity validator and platform value. Cover direct executable, npm layout and rejection paths:

```python
def test_resolve_codex_command_uses_direct_executable(tmp_path: Path) -> None:
    executable = _safe_file(tmp_path / "codex.exe")
    command = resolve_codex_command(
        which=lambda name: str(executable) if name == "codex.exe" else None,
        platform="win32",
        path_validator=_accept_exact(executable),
    )
    assert command.prefix == (str(executable.resolve()),)


def test_resolve_codex_command_maps_standard_npm_shim_without_executing_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "node"
    shim = _safe_file(root / "codex.cmd", b"UNTRUSTED-CONTENT-NOT-EXECUTED")
    node = _safe_file(root / "node.exe")
    entry = _safe_file(root / "node_modules/@openai/codex/bin/codex.js")
    command = resolve_codex_command(
        which=lambda name: str(shim) if name == "codex.cmd" else None,
        platform="win32",
        path_validator=_accept_exact(node, entry, shim),
    )
    assert command.prefix == (str(node.resolve()), str(entry.resolve()))


@pytest.mark.parametrize("unsafe", ["reparse", "missing-node", "missing-entry", "identity-race"])
def test_resolve_codex_command_rejects_unsafe_npm_layout(unsafe: str, tmp_path: Path) -> None:
    with pytest.raises(CodexProcessStartError, match="Codex executable is unavailable") as caught:
        _resolve_unsafe_layout(tmp_path, unsafe)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
```

Also assert that `.ps1`, extensionless workspace shims and arbitrary `.cmd` locations are never returned as executable argv entries.

- [ ] **Step 2: Run and verify RED**

```powershell
uv run pytest tests/test_developer_workflow_codex_runner.py -k "resolve_codex_command" -q
```

Expected: import failure for `resolve_codex_command`/`CodexCommand`.

- [ ] **Step 3: Implement immutable command resolution**

Add:

```python
@dataclass(frozen=True, slots=True)
class CodexCommand:
    prefix: tuple[str, ...]

    def argv(self, *arguments: str) -> list[str]:
        if not self.prefix or any(not item or "\x00" in item for item in self.prefix):
            raise CodexProcessStartError("Codex executable is unavailable")
        return [*self.prefix, *arguments]


def resolve_codex_command(
    *,
    which: Callable[[str], str | None] = shutil.which,
    platform: str = sys.platform,
    path_validator: Callable[[Path], Path] = _validate_codex_component,
) -> CodexCommand:
    failed = False
    try:
        direct_name = "codex.exe" if platform == "win32" else "codex"
        direct = which(direct_name)
        if direct is not None:
            return CodexCommand((str(path_validator(Path(direct))),))
        if platform == "win32" and (shim := which("codex.cmd")) is not None:
            shim_path = path_validator(Path(shim))
            node = path_validator(shim_path.parent / "node.exe")
            entry = path_validator(
                shim_path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            )
            return CodexCommand((str(node), str(entry)))
        failed = True
    except (OSError, ValueError):
        failed = True
    if failed:
        raise CodexProcessStartError("Codex executable is unavailable") from None
    raise CodexProcessStartError("Codex executable is unavailable") from None
```

Implement `_validate_codex_component(path: Path) -> Path` with `lstat` before and after `resolve(strict=True)`, exact `(st_dev, st_ino, st_size, st_mtime_ns)` comparison, regular-file enforcement, Windows reparse rejection, and the repository's protected-owner ACL allowlist. Reject a candidate inside the current worktree. Validation failure is converted outside the raw exception handler to fixed `CodexProcessStartError("Codex executable is unavailable") from None`. Do not log candidate paths. The resolver reads no shim contents and returns neither `.cmd` nor `.ps1` in `CodexCommand.prefix`.

- [ ] **Step 4: Run resolver and process-start regressions**

```powershell
uv run pytest tests/test_developer_workflow_codex_runner.py -q
```

Expected: all pass, including existing distinction between start failure and isolation failure.

- [ ] **Step 5: Commit**

```powershell
git add src/developer_workflow/codex_runner.py tests/test_developer_workflow_codex_runner.py
git commit -m "feat(codex): resolve safe command prefixes"
```

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
    executor = SandboxCommandExecutor(
        permission_profile=BUILTIN_WORKSPACE_PROFILE,
        permission_profile_source=SandboxPermissionProfileSource.BUILTIN_WORKSPACE,
        codex_command=CodexCommand(("node.exe", "codex.js")),
        backend_executor=_record_success(calls),
    )
    executor([sys.executable, "-I", "-c", "print('ok')"], cwd=tmp_path, env={}, timeout=2, max_output_bytes=4096)
    assert calls[-1][:7] == [
        "node.exe", "codex.js", "-c", BUILTIN_WORKSPACE_OVERRIDE,
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
    return self.codex_command or resolve_codex_command()

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

Update `SubprocessDoctorRunner` to accept a resolver and build `command.argv("doctor", "--json")`. `ManagedSandboxExecutorFactory` passes `MANAGED`; add a factory method that builds the fixed `BUILTIN_WORKSPACE` executor. CLI profile validation explicitly constructs the managed form.

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

The backend must record exact argv and assert no `.cmd`, `.ps1`, `shell=True`, secret-bearing environment, user config write or ACL change occurred.

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

Expected: Profile page displays the creation button; Confirm runs the probe; success selects `ones-dev-workspace` and enables Test/Next. Capture only status/category metadata—never environment values, credentials or raw command output.

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
- [ ] Windows npm Codex uses `node.exe + codex.js`, never `.cmd`/PowerShell/shell execution.
- [ ] Git absence remains independently recoverable and does not block Profile creation.
- [ ] Cancellation, unmount, close and revision races leave no late state mutation.
- [ ] Public exceptions and UI surfaces contain no raw path, command, environment or secret data.
