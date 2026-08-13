from __future__ import annotations

import os
import threading
import time
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
    }
    values.update({SecretKind(key): value for key, value in updates.items()})
    return RuntimeSecrets(values)


@pytest.mark.asyncio
async def test_provider_probe_transport_uses_gitlab_private_token_header() -> None:
    from src.developer_workflow.runtime_bootstrap import RuntimeBootstrapper

    builder = RuntimeBootstrapper()
    transport = builder.build_provider_probe_transport(
        {
            "provider_host": "gitlab.example.invalid",
            "provider_api_url": "https://gitlab.example.invalid/api/v4",
            "provider": "gitlab",
        },
        RuntimeSecrets({SecretKind.PROVIDER_TOKEN: "GITLAB-SECRET"}),
    )
    try:
        assert transport.headers["PRIVATE-TOKEN"] == "GITLAB-SECRET"
        assert "Authorization" not in transport.headers
    finally:
        await transport.aclose()


def test_malicious_provider_url_is_rejected_before_secret_read() -> None:
    from src.developer_workflow.runtime_bootstrap import RuntimeBootstrapper

    class Secrets:
        reads = 0

        def require(self, kind: object) -> str:
            self.reads += 1
            return "MUST-NOT-BE-READ"

    secrets = Secrets()
    with pytest.raises(Exception):
        RuntimeBootstrapper().build_provider_probe_transport(
            {
                "provider_host": "gitlab.example.invalid",
                "provider_api_url": "https://user:pass@gitlab.example.invalid/api?leak=1",
                "provider": "gitlab",
            },
            secrets,  # type: ignore[arg-type]
        )
    assert secrets.reads == 0


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


def test_bootstrap_ones_settings_ignore_all_unspecified_ambient_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow.runtime_bootstrap import RuntimeBootstrapper

    monkeypatch.setenv("ONES_PROJECT_ID", "AMBIENT-PROJECT")
    monkeypatch.setenv("ONES_DEFECT_STATUS_IDS", "AMBIENT-STATUS")
    monkeypatch.setenv("ONES_COMMENT_TIMEOUT_SECONDS", "999")
    handle = RuntimeBootstrapper(
        private_root_preparer=lambda roots: tuple(Path(root) for root in roots),
        sandbox_profile_validator=lambda profile, environment: None,
    ).build(_active(tmp_path), _secrets())
    try:
        settings = handle.gateway.settings
        assert settings.project_id == ""
        assert settings.defect_status_ids == ""
        assert settings.comment_timeout_seconds == 30.0
    finally:
        handle.close()


@pytest.mark.parametrize("mode", ["missing", "extra"])
def test_bootstrap_requires_exact_active_credential_kind_set(
    tmp_path: Path, mode: str
) -> None:
    from src.developer_workflow.runtime_bootstrap import (
        RuntimeBootstrapError,
        RuntimeBootstrapper,
    )

    active = _active(tmp_path)
    secrets = _secrets()
    if mode == "missing":
        secrets = RuntimeSecrets(
            {
                kind: value
                for kind, value in secrets.values.items()
                if kind is not SecretKind.PROVIDER_TOKEN
            }
        )
    else:
        secrets = RuntimeSecrets(
            {**secrets.values, SecretKind.GIT_ASKPASS: "C:/trusted/askpass.exe"}
        )
    root_calls: list[object] = []
    sandbox_calls: list[object] = []
    bootstrapper = RuntimeBootstrapper(
        private_root_preparer=lambda roots: (
            root_calls.append(tuple(roots)) or tuple(roots)
        ),
        sandbox_profile_validator=lambda profile, environment: sandbox_calls.append(
            environment
        ),
    )

    with pytest.raises(
        RuntimeBootstrapError,
        match="production runtime configuration is incomplete",
    ):
        bootstrapper.build(active, secrets)
    assert root_calls == []
    assert sandbox_calls == []


def test_sandbox_preflight_never_receives_credentials_or_userinfo_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow.runtime_bootstrap import RuntimeBootstrapper

    monkeypatch.setenv("HTTPS_PROXY", "https://proxy-user:proxy-secret@proxy.invalid")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api-user:api-secret@api.invalid/v1")
    seen: list[dict[str, str]] = []
    handle = RuntimeBootstrapper(
        private_root_preparer=lambda roots: tuple(Path(root) for root in roots),
        sandbox_profile_validator=lambda profile, environment: seen.append(
            dict(environment)
        ),
    ).build(_active(tmp_path), _secrets())
    try:
        assert len(seen) == 1
        assert "HTTPS_PROXY" not in seen[0]
        assert "OPENAI_BASE_URL" not in seen[0]
        assert not (
            {"CODEX_API_KEY", "CODEX_AUTH_TOKEN", "OPENAI_API_KEY"}
            & seen[0].keys()
        )
        assert "proxy-secret" not in repr(seen)
        assert "api-secret" not in repr(seen)
    finally:
        handle.close()


def test_legacy_sandbox_adapter_forwards_only_sanitized_preflight_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow.cli import build_production_orchestrator

    from tests.test_developer_workflow_cli import (
        _config_file,
        _set_complete_non_ones_runtime,
    )

    _set_complete_non_ones_runtime(monkeypatch)
    monkeypatch.setenv("ONES_EMAIL", "legacy@example.invalid")
    monkeypatch.setenv("ONES_PASSWORD", "LEGACY-PASSWORD")
    monkeypatch.setenv(
        "ONES_COMMENT_LIST_PATH_TEMPLATE",
        "/project/api/project/team/{team_id}/task/{item_id}/comments",
    )
    monkeypatch.setenv("HTTPS_PROXY", "https://user:secret@proxy.invalid")
    seen: list[dict[str, str]] = []

    orchestrator = build_production_orchestrator(
        DeveloperWorkflowConfig.load(_config_file(tmp_path)),
        sandbox_profile_validator=lambda profile, environment: seen.append(
            dict(environment)
        ),
    )

    assert len(seen) == 1
    assert "ONES_PASSWORD" not in seen[0]
    assert "HTTPS_PROXY" not in seen[0]
    assert "LEGACY-PASSWORD" not in repr(seen)
    orchestrator.publisher.pr_client.client.close()


@pytest.mark.parametrize(
    ("mode", "codex_home", "codex_kinds"),
    [
        ("credential", "present", (SecretKind.CODEX_API_KEY,)),
        ("credential", None, (SecretKind.CODEX_API_KEY, SecretKind.CODEX_AUTH_TOKEN)),
        ("file", None, ()),
        ("file", "present", (SecretKind.CODEX_API_KEY,)),
    ],
)
def test_codex_auth_mode_contract_rejects_conflicting_or_unused_inputs_before_roots(
    tmp_path: Path,
    mode: str,
    codex_home: str | None,
    codex_kinds: tuple[SecretKind, ...],
) -> None:
    from src.developer_workflow.runtime_bootstrap import (
        RuntimeBootstrapError,
        RuntimeBootstrapper,
    )

    home = (tmp_path / "codex-home").resolve() if codex_home else None
    if home is not None:
        home.mkdir()
        (home / "auth.json").write_text("{}", encoding="utf-8")
    active = _active(tmp_path).validated_update(
        runtime=_active(tmp_path).runtime.validated_update(
            codex_auth_mode=mode, codex_home=home
        ),
        credential_kinds=(
            SecretKind.ONES_EMAIL,
            SecretKind.ONES_PASSWORD,
            SecretKind.PROVIDER_TOKEN,
            *codex_kinds,
        ),
    )
    values = {
        SecretKind.ONES_EMAIL: "stored@example.invalid",
        SecretKind.ONES_PASSWORD: "STORED-PASSWORD",
        SecretKind.PROVIDER_TOKEN: "STORED-PROVIDER-TOKEN",
    }
    for kind in codex_kinds:
        values[kind] = f"STORED-{kind.value}"
    root_calls: list[object] = []

    with pytest.raises(RuntimeBootstrapError):
        RuntimeBootstrapper(
            private_root_preparer=lambda roots: root_calls.append(roots) or tuple(roots),
            sandbox_profile_validator=lambda profile, environment: None,
        ).build(active, RuntimeSecrets(values))
    assert root_calls == []


def test_codex_probe_checker_uses_only_explicit_runtime_auth_and_clears_it(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.runtime_bootstrap import RuntimeBootstrapper

    public = _active(tmp_path).runtime.validated_update(
        codex_auth_mode="credential", codex_home=None
    )
    checker = RuntimeBootstrapper(
        ambient_environment=lambda: {"CODEX_AUTH_TOKEN": "AMBIENT-MUST-NOT-WIN"}
    ).build_codex_probe_auth_checker(
        public,
        RuntimeSecrets({SecretKind.CODEX_API_KEY: "EXPLICIT-PROBE-KEY"}),
    )
    assert checker.metadata() == {"configured": True, "mode": "credential"}
    assert "EXPLICIT-PROBE-KEY" not in repr(checker)
    checker.close()
    with pytest.raises(Exception):
        checker.metadata()


def test_runtime_handle_close_is_single_flight_for_concurrent_callers() -> None:
    from src.developer_workflow.runtime_bootstrap import RuntimeHandle

    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def cleanup() -> None:
        calls.append(1)
        entered.set()
        assert release.wait(5)

    handle = RuntimeHandle(object(), object(), cleanup)
    finished: list[int] = []
    threads = [
        threading.Thread(target=lambda index=index: (handle.close(), finished.append(index)))
        for index in range(2)
    ]
    threads[0].start()
    assert entered.wait(5)
    threads[1].start()
    time.sleep(0.05)
    assert finished == []
    release.set()
    for thread in threads:
        thread.join(5)
    assert calls == [1]
    assert sorted(finished) == [0, 1]


def test_runtime_handle_close_replays_one_sanitized_failure_to_all_waiters() -> None:
    from src.developer_workflow.runtime_bootstrap import (
        RuntimeBootstrapError,
        RuntimeHandle,
    )

    entered = threading.Event()
    release = threading.Event()

    def cleanup() -> None:
        entered.set()
        assert release.wait(5)
        raise RuntimeError("STORED-PASSWORD")

    handle = RuntimeHandle(object(), object(), cleanup)
    errors: list[BaseException] = []

    def close() -> None:
        try:
            handle.close()
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=close) for _ in range(2)]
    threads[0].start()
    assert entered.wait(5)
    threads[1].start()
    release.set()
    for thread in threads:
        thread.join(5)
    with pytest.raises(RuntimeBootstrapError) as later:
        handle.close()
    assert len(errors) == 2
    assert all(type(error) is RuntimeBootstrapError for error in errors)
    assert all(str(error) == "production runtime close failed" for error in errors)
    assert str(later.value) == "production runtime close failed"
    assert all(error.__cause__ is None for error in (*errors, later.value))


def test_runtime_handle_and_reachable_runtime_repr_never_expose_secrets(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.runtime_bootstrap import RuntimeBootstrapper

    secrets = RuntimeSecrets(
        {
            **_secrets().values,
            SecretKind.GIT_ASKPASS: "C:/trusted/STORED-GIT-ASKPASS.exe",
        }
    )
    active = _active(tmp_path).validated_update(
        credential_kinds=(
            *_active(tmp_path).credential_kinds,
            SecretKind.GIT_ASKPASS,
        )
    )
    handle = RuntimeBootstrapper(
        private_root_preparer=lambda roots: tuple(Path(root) for root in roots),
        sandbox_profile_validator=lambda profile, environment: None,
    ).build(active, secrets)
    try:
        rendered = "\n".join(
            (
                repr(handle),
                str(handle),
                repr(handle.gateway),
                repr(handle.gateway.settings),
                repr(handle.orchestrator),
                repr(handle.orchestrator.publisher.pr_client),
            )
        )
        assert not any(value in rendered for value in secrets.values.values())
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
