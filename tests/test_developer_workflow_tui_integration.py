from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from src.developer_workflow import cli
from src.developer_workflow.config import DeveloperWorkflowConfig
from src.developer_workflow.tui.controller import TuiController


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "run_root": str(tmp_path / "runs"),
                "worktree_root": str(tmp_path / "worktrees"),
                "mirror_root": str(tmp_path / "mirrors"),
                "sandbox_permission_profile": "managed-dev",
                "max_codex_attempts": 2,
                "repositories": [
                    {
                        "key": "repo",
                        "project_id": "PROJ",
                        "iteration_id": "ITER",
                        "repo_url": "ssh://git@example.invalid/team/repo.git",
                        "repo_name": "repo",
                        "base_branch": "main",
                        "test_commands": ["uv run pytest"],
                        "lint_commands": [],
                    }
                ],
                "publishing": {"provider": "github"},
            }
        ),
        encoding="utf-8",
    )
    return path


class _Store:
    def list_run_ids(self) -> tuple[str, ...]:
        return ()


class _Orchestrator:
    def __init__(self) -> None:
        self.store = _Store()


def test_tui_command_reuses_factory_orchestrator_store_and_configured_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _Orchestrator()
    seen: list[object] = []
    closed: list[object] = []
    original_close = TuiController.close

    def close(controller: TuiController) -> None:
        closed.append(controller)
        original_close(controller)

    monkeypatch.setattr(TuiController, "close", close)

    def factory(config: DeveloperWorkflowConfig) -> _Orchestrator:
        seen.append(config)
        return orchestrator

    def runner(controller: object, max_concurrency: int) -> None:
        seen.append((controller, max_concurrency))

    assert cli.main(
        ["tui", "--config", str(_config_file(tmp_path))],
        factory=factory,  # type: ignore[arg-type]
        tui_runner=runner,
    ) == 0

    config = seen[0]
    controller, limit = seen[1]  # type: ignore[misc]
    assert isinstance(config, DeveloperWorkflowConfig)
    assert limit == 3
    assert controller._orchestrator is orchestrator  # type: ignore[attr-defined]
    assert controller._run_index._store is orchestrator.store  # type: ignore[attr-defined]
    assert closed == [controller]


class _RunnerFailure(RuntimeError):
    pass


def test_execute_tui_preserves_runner_failure_when_controller_close_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DeveloperWorkflowConfig.load(_config_file(tmp_path))
    orchestrator = _Orchestrator()
    closes: list[object] = []

    def close(controller: TuiController) -> None:
        closes.append(controller)
        raise RuntimeError("close failed")

    monkeypatch.setattr(TuiController, "close", close)

    with pytest.raises(_RunnerFailure, match="runner failed"):
        cli._execute_tui(
            config,
            lambda loaded: orchestrator,  # type: ignore[arg-type]
            lambda controller, limit: (_ for _ in ()).throw(
                _RunnerFailure("runner failed")
            ),
        )
    assert len(closes) == 1


def test_tui_keyboard_interrupt_is_not_obscured_by_controller_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _Orchestrator()
    closes: list[object] = []

    def close(controller: TuiController) -> None:
        closes.append(controller)
        raise RuntimeError("close failed")

    monkeypatch.setattr(TuiController, "close", close)

    code = cli.main(
        ["tui", "--config", str(_config_file(tmp_path))],
        factory=lambda loaded: orchestrator,  # type: ignore[arg-type]
        tui_runner=lambda controller, limit: (_ for _ in ()).throw(
            KeyboardInterrupt()
        ),
    )

    assert code == 130
    assert len(closes) == 1


def test_tui_controller_close_failure_after_normal_runner_fails_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _Orchestrator()
    monkeypatch.setattr(
        TuiController,
        "close",
        lambda controller: (_ for _ in ()).throw(RuntimeError("close failed")),
    )
    error = io.StringIO()

    code = cli.main(
        ["tui", "--config", str(_config_file(tmp_path))],
        factory=lambda loaded: orchestrator,  # type: ignore[arg-type]
        tui_runner=lambda controller, limit: None,
        stderr=error,
    )

    assert code == 1
    assert error.getvalue() == "error: command failed safely\n"


def test_incomplete_production_runtime_fails_before_tui_runner_and_root_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "ONES_EMAIL",
        "ONES_PASSWORD",
        "ONES_API_TOKEN",
        "ONES_TEAM_ID",
        "ONES_ISSUE_TYPE_ID",
        "ONES_DEV_PROVIDER_TOKEN",
        "ONES_DEV_PROVIDER_HOST",
        "ONES_DEV_PROVIDER_API_URL",
        "ONES_DEV_GIT_AUTHOR_NAME",
        "ONES_DEV_GIT_AUTHOR_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    config_path = _config_file(tmp_path)
    calls: list[object] = []

    code = cli.main(
        ["tui", "--config", str(config_path)],
        factory=cli.build_production_orchestrator,
        tui_runner=lambda controller, limit: calls.append((controller, limit)),
    )

    config = DeveloperWorkflowConfig.load(config_path)
    assert code == 1
    assert calls == []
    assert not config.run_root.exists()
    assert not config.mirror_root.exists()
    assert not config.worktree_root.exists()


def test_run_tui_constructs_and_runs_the_production_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.developer_workflow.tui as tui

    calls: list[object] = []

    class App:
        def __init__(self, controller: object, max_concurrency: int) -> None:
            calls.append((controller, max_concurrency))

        def run(self) -> None:
            calls.append("run")

    controller = object()
    monkeypatch.setattr(tui, "DeveloperWorkflowTuiApp", App)

    tui.run_tui(controller, 4)  # type: ignore[arg-type]

    assert calls == [(controller, 4), "run"]
