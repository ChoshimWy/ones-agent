# TUI 无 Git 依赖启动实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `uv run ones-dev tui` 在 Git 不存在或不在 `PATH` 时仍进入 setup 界面，并在仓库测试步骤显示固定、脱敏、可重试的 Git 不可用结果。

**Architecture:** 把 `src.services` 从急切聚合导入改为线程安全的按名延迟导入，切断 TUI 启动到 `ExecutionService → git_ops → GitPython` 的无关依赖。仓库只读探测仍严格需要 Git；由 `ReadOnlyRepositoryInspector` 将 `git` 进程不存在转换为专用的无链异常，`SetupValidator` 再将其映射为固定结果类别供 TUI 渲染。

**Tech Stack:** Python 3.11、GitPython、Textual、Pydantic、pytest、subprocess

---

## 文件结构

- 修改 `src/services/__init__.py`：只负责公开服务符号的线程安全延迟导入。
- 修改 `src/developer_workflow/setup_validation.py`：定义 Git 可执行文件不可用的安全异常，并在仓库 subprocess 边界分类。
- 修改 `src/developer_workflow/tui/setup_screens.py`：把新的固定结果类别映射为用户可见文本。
- 新建 `tests/test_services_package.py`：验证冷进程导入和聚合导入兼容性。
- 修改 `tests/test_developer_workflow_setup_validation.py`：验证 Git 缺失只产生固定、无异常链的失败结果。
- 修改 `tests/test_developer_workflow_tui_bootstrap.py`：验证 TUI 固定提示和失败后可重试。
- 修改 `tests/test_developer_workflow_cli.py`：用冷子进程覆盖无 Git 环境下的真实 CLI bootstrap。

### Task 1: 延迟加载服务聚合模块

**Files:**
- Create: `tests/test_services_package.py`
- Modify: `src/services/__init__.py`
- Modify: `tests/test_developer_workflow_cli.py`

- [ ] **Step 1: 写冷进程失败测试**

在 `tests/test_services_package.py` 添加：

```python
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def _without_git() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("GIT_PYTHON_GIT_EXECUTABLE", None)
    environment["GIT_PYTHON_REFRESH"] = "error"
    environment["PATH"] = ""
    environment["PYTHONPATH"] = str(ROOT)
    return environment


def test_tui_package_import_does_not_require_git_executable() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import src.developer_workflow.tui; "
                "assert 'git' not in sys.modules; "
                "print('tui-import-ok')"
            ),
        ],
        cwd=ROOT,
        env=_without_git(),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "tui-import-ok"


def test_services_lazy_export_keeps_public_import_contract() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import src.services as services; "
                "assert 'src.services.execution_service' not in sys.modules; "
                "from src.services import OnesGateway; "
                "assert OnesGateway.__name__ == 'OnesGateway'; "
                "assert 'src.services.execution_service' not in sys.modules"
            ),
        ],
        cwd=ROOT,
        env=_without_git(),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
```

同时在 `tests/test_developer_workflow_cli.py` 添加真实 CLI bootstrap 冷进程测试。子进程直接使用 `sys.executable`，清空 Git 相关环境，并注入只记录调用的 `tui_runner`，避免启动交互界面：

```python
def test_tui_bootstrap_reaches_runner_without_git_executable() -> None:
    environment = dict(os.environ)
    environment.pop("GIT_PYTHON_GIT_EXECUTABLE", None)
    environment["GIT_PYTHON_REFRESH"] = "error"
    environment["PATH"] = ""
    environment["PYTHONPATH"] = str(PROJECT_ROOT)
    script = """
from src.developer_workflow.cli import main

def runner(factory, runtime_builder):
    assert callable(factory)
    print("runner-reached")

raise SystemExit(main(["tui"], tui_runner=runner))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "runner-reached"
    assert "Bad git executable" not in completed.stderr
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```powershell
uv run pytest tests/test_services_package.py tests/test_developer_workflow_cli.py -k "without_git_executable" -q -p no:cacheprovider
```

Expected: `test_tui_package_import_does_not_require_git_executable` 因 GitPython 的 `Bad git executable` 失败。

- [ ] **Step 3: 实现线程安全延迟导入**

用以下结构替换 `src/services/__init__.py` 的急切导入：

```python
"""Backward-compatible lazy exports for backend service boundaries."""

from __future__ import annotations

from importlib import import_module
from threading import RLock
from typing import Any


__all__ = [
    "AnalysisResultShaper",
    "CodebaseEvidenceService",
    "DefectAnalysisWorkflowService",
    "ExecutionService",
    "OnesGateway",
    "RepoResolver",
]

_EXPORTS = {
    "AnalysisResultShaper": ("src.services.analysis_result_shaper", "AnalysisResultShaper"),
    "CodebaseEvidenceService": ("src.services.codebase_evidence", "CodebaseEvidenceService"),
    "DefectAnalysisWorkflowService": (
        "src.services.defect_analysis_workflow",
        "DefectAnalysisWorkflowService",
    ),
    "ExecutionService": ("src.services.execution_service", "ExecutionService"),
    "OnesGateway": ("src.services.ones_gateway", "OnesGateway"),
    "RepoResolver": ("src.services.repo_resolver", "RepoResolver"),
}
_EXPORT_LOCK = RLock()


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None
    with _EXPORT_LOCK:
        cached = globals().get(name)
        if cached is not None:
            return cached
        value = getattr(import_module(module_name), attribute_name)
        globals()[name] = value
        return value
```

不要捕获 `import_module` 的异常；真正请求 `ExecutionService` 时，Git 缺失仍必须严格失败。

- [ ] **Step 4: 运行聚焦与兼容测试并确认 GREEN**

Run:

```powershell
uv run pytest tests/test_services_package.py tests/test_developer_workflow_cli.py tests/test_codebase_evidence_service.py tests/test_repo_resolver.py -q -p no:cacheprovider
```

Expected: PASS；冷进程输出不包含 GitPython 初始化错误。

- [ ] **Step 5: 提交 Task 1**

```powershell
git add src/services/__init__.py tests/test_services_package.py tests/test_developer_workflow_cli.py
git commit -m "fix(tui): defer git-dependent service imports"
```

### Task 2: 在仓库探测边界分类 Git 不可用

**Files:**
- Modify: `src/developer_workflow/setup_validation.py`
- Modify: `src/developer_workflow/tui/setup_screens.py`
- Modify: `tests/test_developer_workflow_setup_validation.py`
- Modify: `tests/test_developer_workflow_tui_bootstrap.py`

- [ ] **Step 1: 写 Git 缺失分类的失败测试**

在 `tests/test_developer_workflow_setup_validation.py` 添加：

```python
def test_repository_inspector_classifies_missing_git_without_exception_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inspector = ReadOnlyRepositoryInspector()

    def missing(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("SECRET-GIT-PATH")

    monkeypatch.setattr(
        "src.developer_workflow.setup_validation._bounded_subprocess", missing
    )
    with pytest.raises(GitExecutableUnavailableError) as captured:
        inspector._run(
            ["git", "--version"],
            cwd=tmp_path,
            private_root=tmp_path,
            hooks=tmp_path,
            timeout=1.0,
        )
    assert str(captured.value) == "Git executable is unavailable"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "SECRET-GIT-PATH" not in repr(captured.value)


@pytest.mark.asyncio
async def test_repository_probe_returns_git_unavailable_category(tmp_path: Path) -> None:
    class MissingGitInspector:
        def snapshot(self, path: Path, *, timeout: float) -> object:
            raise GitExecutableUnavailableError("Git executable is unavailable")

    validator = SetupValidator(repository_inspector=MissingGitInspector())
    result = await validator.probe_repository(
        RepositoryProbeInput(path=tmp_path, remote_url="https://example.invalid/repo.git")
    )
    assert result.status is ValidationStatus.FAILED
    assert result.category == "git_unavailable"
```

在 `tests/test_developer_workflow_tui_bootstrap.py` 添加对 `_RESULT_TEXT` 或真实 Wizard notice 的断言：

```python
def test_git_unavailable_result_has_fixed_recoverable_notice() -> None:
    assert _RESULT_TEXT["git_unavailable"] == "Git executable is unavailable"
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```powershell
uv run pytest tests/test_developer_workflow_setup_validation.py tests/test_developer_workflow_tui_bootstrap.py -k "git_unavailable or missing_git" -q -p no:cacheprovider
```

Expected: FAIL，原因是 `GitExecutableUnavailableError` 和 `git_unavailable` 类别尚不存在。

- [ ] **Step 3: 实现专用安全异常与固定类别**

在 `src/developer_workflow/setup_validation.py`：

```python
class GitExecutableUnavailableError(RuntimeError):
    """The repository probe cannot start the required Git executable."""


def _raise_git_unavailable() -> None:
    raise GitExecutableUnavailableError("Git executable is unavailable") from None
```

将 `_CATEGORIES` 增加 `"git_unavailable"`。在 `ReadOnlyRepositoryInspector._run` 中只包围 `_bounded_subprocess` 调用：

```python
try:
    completed = _bounded_subprocess(...)
except FileNotFoundError:
    unavailable = True
else:
    unavailable = False
if unavailable:
    _raise_git_unavailable()
```

必须在 `except` 处理器外抛出，确保公开异常的 `__cause__` 和 `__context__` 都是 `None`。不要把非零退出码、权限错误、超时或其他内部异常误分类为 Git 不存在。

在 `_failure_category` 最前面增加：

```python
if isinstance(error, GitExecutableUnavailableError):
    return "git_unavailable"
```

在 `src/developer_workflow/tui/setup_screens.py` 的 `_RESULT_TEXT` 增加：

```python
"git_unavailable": "Git executable is unavailable",
```

并把 `GitExecutableUnavailableError` 加入 `setup_validation.__all__`，供测试和边界调用者使用。

- [ ] **Step 4: 运行聚焦测试并确认 GREEN**

Run:

```powershell
uv run pytest tests/test_developer_workflow_setup_validation.py tests/test_developer_workflow_tui_bootstrap.py -k "git_unavailable or missing_git" -q -p no:cacheprovider
```

Expected: PASS；异常链为空且 UI 文本精确固定。

- [ ] **Step 5: 运行完整相邻测试**

Run:

```powershell
uv run pytest tests/test_developer_workflow_setup_validation.py tests/test_developer_workflow_setup_repository.py tests/test_developer_workflow_setup_controller.py tests/test_developer_workflow_tui_bootstrap.py -q -p no:cacheprovider
```

Expected: PASS。

- [ ] **Step 6: 提交 Task 2**

```powershell
git add src/developer_workflow/setup_validation.py src/developer_workflow/tui/setup_screens.py tests/test_developer_workflow_setup_validation.py tests/test_developer_workflow_tui_bootstrap.py
git commit -m "fix(setup): report missing git as recoverable"
```

### Task 3: 完成合并验收

**Files:**
- Verify only: no production or test file changes

- [ ] **Step 1: 运行全部相关回归**

Run:

```powershell
uv run pytest tests/test_services_package.py tests/test_developer_workflow_cli.py tests/test_developer_workflow_setup_validation.py tests/test_developer_workflow_setup_repository.py tests/test_developer_workflow_setup_controller.py tests/test_developer_workflow_tui_bootstrap.py -q -p no:cacheprovider
```

Expected: PASS，无 skip、无 GitPython 初始化警告。

- [ ] **Step 2: 执行静态检查和原命令验收**

Run:

```powershell
python -m compileall -q src tests
git diff --check HEAD~2..HEAD
Remove-Item Env:GIT_PYTHON_GIT_EXECUTABLE -ErrorAction SilentlyContinue
uv run ones-dev tui
```

Expected: compileall 和 diff-check exit 0；原命令进入 setup TUI，而不是输出 `error: command failed safely`。交互验收完成后正常退出 TUI。

## 最终检查

- [ ] 逐项核对设计目标：启动不依赖 Git、仓库验证 fail-closed、固定脱敏提示、可重试、公开服务导入兼容、不修改 PATH。
- [ ] 搜索计划实现中是否出现 Git 自动发现、注册表扫描、异常原文渲染或 Git 验证绕过；任何命中都必须删除。
- [ ] 确认 `git status --short --untracked-files=no` 无已跟踪未提交改动。
