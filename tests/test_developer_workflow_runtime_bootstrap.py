from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.developer_workflow.config import DeveloperWorkflowConfig, PublishingConfig
from src.developer_workflow.contracts import RepositoryMapping
from src.developer_workflow.setup_models import (
    ActiveSetup,
    RuntimePublicConfig,
    RuntimeSecrets,
    SecretKind,
)


def _workflow(tmp_path: Path) -> DeveloperWorkflowConfig:
    return DeveloperWorkflowConfig(
        run_root=(tmp_path / "runs").resolve(),
        mirror_root=(tmp_path / "mirrors").resolve(),
        worktree_root=(tmp_path / "worktrees").resolve(),
        sandbox_permission_profile="managed-dev",
        max_codex_attempts=2,
        repositories=(
            RepositoryMapping(
                key="repo",
                project_id="PROJ",
                iteration_id="ITER",
                repo_url="ssh://git@example.invalid/team/repo.git",
                repo_name="repo",
                base_branch="main",
                test_commands=("uv run pytest",),
            ),
        ),
        publishing=PublishingConfig(provider="github"),
    )


def _active(tmp_path: Path, *, codex_auth_mode: str = "credential") -> ActiveSetup:
    runtime = RuntimePublicConfig(
        ones_base_url="https://ones.invalid",
        ones_team_id="TEAM",
        ones_issue_type_id="BUG",
        ones_comment_list_path_template=(
            "/project/api/project/team/{team_id}/task/{item_id}/comments"
        ),
        provider_host="example.invalid",
        provider_api_url="https://example.invalid/api/v3",
        git_author_name="ONES Dev",
        git_author_email="ones-dev@example.invalid",
        codex_auth_mode=codex_auth_mode,
    )
    kinds = (
        SecretKind.ONES_EMAIL,
        SecretKind.ONES_PASSWORD,
        SecretKind.PROVIDER_TOKEN,
        SecretKind.CODEX_API_KEY,
    )
    return ActiveSetup(
        generation="a" * 32,
        runtime=runtime,
        workflow=_workflow(tmp_path),
        credential_kinds=kinds,
    )


def _secrets(**updates: str) -> RuntimeSecrets:
    values = {
        SecretKind.ONES_EMAIL: "stored@example.invalid",
        SecretKind.ONES_PASSWORD: "STORED-PASSWORD",
        SecretKind.PROVIDER_TOKEN: "STORED-PROVIDER-TOKEN",
        SecretKind.CODEX_API_KEY: "STORED-CODEX-KEY",
        SecretKind.GIT_ASKPASS: "C:/trusted/askpass.exe",
    }
    values.update({SecretKind(key): value for key, value in updates.items()})
    return RuntimeSecrets(values)


def test_bootstrap_uses_explicit_inputs_without_mutating_parent_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow.runtime_bootstrap import RuntimeBootstrapper

    monkeypatch.setenv("ONES_PASSWORD", "PARENT-PASSWORD")
    monkeypatch.setenv("ONES_DEV_PROVIDER_TOKEN", "PARENT-PROVIDER-TOKEN")
    monkeypatch.setenv("CODEX_API_KEY", "PARENT-CODEX-KEY")
    before = dict(os.environ)
    bootstrapper = RuntimeBootstrapper(
        private_root_preparer=lambda roots: tuple(Path(root) for root in roots),
        sandbox_profile_validator=lambda profile, environment: None,
    )

    handle = bootstrapper.build(_active(tmp_path), _secrets())
    try:
        assert handle.gateway.settings.password == "STORED-PASSWORD"
        assert handle.orchestrator.publisher.pr_client.token_provider() == (
            "STORED-PROVIDER-TOKEN"
        )
        codex = handle.orchestrator.requirement_flow.codex
        environment = codex.environment_provider()
        assert environment["CODEX_API_KEY"] == "STORED-CODEX-KEY"
        assert "PARENT-CODEX-KEY" not in environment.values()
        assert os.environ == before
    finally:
        handle.close()


def test_bootstrap_fails_closed_before_private_roots_when_secret_missing(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.runtime_bootstrap import (
        RuntimeBootstrapError,
        RuntimeBootstrapper,
    )

    roots: list[tuple[Path, ...]] = []
    bootstrapper = RuntimeBootstrapper(
        private_root_preparer=lambda values: roots.append(tuple(values)) or tuple(values),
        sandbox_profile_validator=lambda profile, environment: None,
    )
    incomplete = RuntimeSecrets(
        {
            SecretKind.ONES_EMAIL: "stored@example.invalid",
            SecretKind.ONES_PASSWORD: "STORED-PASSWORD",
            SecretKind.CODEX_API_KEY: "STORED-CODEX-KEY",
        }
    )

    with pytest.raises(RuntimeBootstrapError, match="production runtime configuration is incomplete") as caught:
        bootstrapper.build(_active(tmp_path), incomplete)
    assert caught.value.__cause__ is None
    assert roots == []


def test_runtime_handle_closes_gateway_exactly_once(tmp_path: Path) -> None:
    from src.developer_workflow.runtime_bootstrap import RuntimeBootstrapper

    closed: list[object] = []
    bootstrapper = RuntimeBootstrapper(
        private_root_preparer=lambda roots: tuple(Path(root) for root in roots),
        sandbox_profile_validator=lambda profile, environment: None,
        gateway_close=lambda gateway: closed.append(gateway),
    )
    handle = bootstrapper.build(_active(tmp_path), _secrets())

    handle.close()
    handle.close()

    assert closed == [handle.gateway]


def test_bootstrap_rejects_control_characters_in_explicit_transport_secrets(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.runtime_bootstrap import (
        RuntimeBootstrapError,
        RuntimeBootstrapper,
    )

    bootstrapper = RuntimeBootstrapper(
        private_root_preparer=lambda roots: tuple(Path(root) for root in roots),
        sandbox_profile_validator=lambda profile, environment: None,
    )
    unsafe = _secrets(provider_token="unsafe\nheader")

    with pytest.raises(RuntimeBootstrapError, match="production runtime configuration is incomplete"):
        bootstrapper.build(_active(tmp_path), unsafe)
