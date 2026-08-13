# TUI 不安全 `.env` 启动降级 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 当当前目录的可选 `.env` 未通过私有文件安全校验时，`ones-dev tui` 跳过该来源并正常进入配置向导。

**Architecture:** 保留 `setup_import.parse_dotenv()` 的严格 fail-closed 语义，只在 `build_production_tui_host()` 的隐式可选来源探测边界捕获脱敏的 `SetupImportError`。失败时重新构造仅含环境变量元数据的检测结果，并把 import context 的 `dotenv_path` 置为 `None`，确保后续无法重读被拒文件；显式模板错误继续阻断启动。

**Tech Stack:** Python 3.11、pytest、Pydantic、Textual、现有 `developer_workflow.setup_import` 安全导入边界。

---

## 文件结构

- Modify: `src/developer_workflow/cli.py` — 在生产 TUI host 中隔离隐式 `.env` 探测失败。
- Modify: `tests/test_developer_workflow_cli.py` — 覆盖真实宽松权限 `.env` 的启动降级与 canary 脱敏。

### Task 1: 可选不安全 `.env` 不阻断 TUI host

**Files:**
- Modify: `tests/test_developer_workflow_cli.py:313`
- Modify: `src/developer_workflow/cli.py:608-648`

- [ ] **Step 1: 写入失败回归测试**

在 `test_tui_missing_optional_template_still_builds_setup_host` 后加入：

```python
def test_tui_unsafe_optional_dotenv_is_ignored_without_exposing_canary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow.cli import build_production_tui_host

    canary = "UNSAFE-DOTENV-CANARY"
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"ONES_PASSWORD={canary}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    factory, runtime = build_production_tui_host(tmp_path / "missing.json")
    context = factory.import_context  # type: ignore[attr-defined]

    assert context.detection.dotenv == ()
    assert context.dotenv_path is None
    assert canary not in repr(context)
    assert runtime is not None
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```powershell
uv run pytest tests/test_developer_workflow_cli.py::test_tui_unsafe_optional_dotenv_is_ignored_without_exposing_canary -q -p no:cacheprovider
```

Expected: FAIL，`build_production_tui_host()` 抛出固定 `SetupImportError("dotenv path is unsafe")`；不得因语法或 fixture 错误失败。

- [ ] **Step 3: 实现最小 host 降级**

在 `build_production_tui_host()` 的局部导入中加入 `SetupImportError`，并只包围 `.env` 检测：

```python
from .setup_import import (
    ImportDetection,
    SetupImportError,
    detect_import_sources,
    load_template_workflow,
)

dotenv_path: Path | None = Path.cwd() / ".env"
try:
    detected = detect_import_sources(
        template_config_path=None,
        dotenv_path=dotenv_path,
        environment=environment,
    )
except SetupImportError:
    dotenv_path = None
    detected = detect_import_sources(
        template_config_path=None,
        dotenv_path=None,
        environment=environment,
    )
```

不要捕获控制流异常，不要修改 `parse_dotenv()`，不要放宽 ACL，也不要捕获随后 `load_template_workflow()` 的错误。

- [ ] **Step 4: 运行定向测试并确认 GREEN**

Run:

```powershell
uv run pytest tests/test_developer_workflow_cli.py::test_tui_unsafe_optional_dotenv_is_ignored_without_exposing_canary tests/test_developer_workflow_cli.py::test_tui_missing_optional_template_still_builds_setup_host tests/test_developer_workflow_cli.py::test_tui_unsafe_template_reports_only_fixed_failure -q -p no:cacheprovider
```

Expected: `3 passed`。

- [ ] **Step 5: 运行相邻安全回归**

Run:

```powershell
uv run pytest tests/test_developer_workflow_setup_import.py tests/test_developer_workflow_cli.py tests/test_developer_workflow_tui_bootstrap.py -q -p no:cacheprovider
```

Expected: 全部通过；若 Windows Textual 时序测试发生已知等待抖动，隔离复现后原样重跑一次并记录两次结果。

- [ ] **Step 6: 验证真实启动边界**

Run:

```powershell
uv run python -c "from pathlib import Path; from src.developer_workflow.cli import build_production_tui_host; factory, runtime = build_production_tui_host(Path('ones-dev.config.json')); context = factory.import_context; assert context.detection.dotenv == (); assert context.dotenv_path is None; print('host-ok')"
uv run ones-dev tui --help
```

Expected: 第一条输出 `host-ok`，第二条 exit 0 并显示 `--config`。不要用自动超时启动交互式 Textual 进程作为唯一成功证据。

- [ ] **Step 7: 静态检查并提交**

Run:

```powershell
python -m compileall -q src/developer_workflow tests
git diff --check
git status --short
```

Expected: compileall 与 diff-check exit 0；只出现本任务的 `cli.py`、CLI 测试和计划文档相关提交状态。

Commit:

```powershell
git add src/developer_workflow/cli.py tests/test_developer_workflow_cli.py
git commit -m "fix(tui): ignore unsafe optional dotenv source"
```

