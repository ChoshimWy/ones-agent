from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.developer_workflow import cli
from src.developer_workflow.config import DeveloperWorkflowConfig


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
    tmp_path: Path,
) -> None:
    orchestrator = _Orchestrator()
    seen: list[object] = []

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
