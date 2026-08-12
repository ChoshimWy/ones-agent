"""Explicit, fail-closed construction of the production workflow graph."""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from config.settings import OnesSettings
from src.services.ones_gateway import OnesGateway

from .approval_rebuilder import WorkflowApprovalRebuilder
from .codex_runner import CodexRunner, validate_codex_auth_source
from .config import DeveloperWorkflowConfig
from .defect_flow import DefectCandidateService, DefectFlow
from .ones_comment import OnesCommenter
from .orchestrator import DeveloperWorkflowOrchestrator
from .pr_provider import HttpPullRequestClient, parse_repository_identity
from .private_paths import prepare_private_roots
from .publisher import Publisher
from .repository import WorktreeRepository, validate_git_identity_environment
from .repository_group import RepositoryGroupWorkspace
from .requirement_flow import (
    RequirementFlow,
    SandboxCommandExecutor,
    sandbox_preflight_command,
)
from .setup_models import ActiveSetup, RuntimePublicConfig, RuntimeSecrets, SecretKind
from .state_store import FileRunStore


class RuntimeBootstrapError(RuntimeError):
    """Production runtime inputs could not be safely activated."""


class PrivateRootPreparer(Protocol):
    def __call__(self, roots: Sequence[Path]) -> tuple[Path, ...]: ...


class SandboxProfileValidator(Protocol):
    def __call__(self, profile: str, environment: Mapping[str, str]) -> None: ...


_GIT_SECRET_ENV = {
    SecretKind.GIT_ASKPASS: "GIT_ASKPASS",
    SecretKind.GIT_SSH: "GIT_SSH",
    SecretKind.GIT_SSH_COMMAND: "GIT_SSH_COMMAND",
    SecretKind.SSH_ASKPASS: "SSH_ASKPASS",
    SecretKind.SSH_AUTH_SOCK: "SSH_AUTH_SOCK",
}
_CODEX_BASE_ENV = frozenset(
    {
        "COMSPEC", "LANG", "LC_ALL", "NO_COLOR", "PATH", "PATHEXT",
        "SYSTEMROOT", "TEMP", "TERM", "TMP", "TMPDIR", "WINDIR",
        "SSL_CERT_DIR", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "OPENAI_API_VERSION",
        "OPENAI_BASE_URL", "OPENAI_ORGANIZATION", "OPENAI_ORG_ID", "OPENAI_PROJECT",
    }
)


def _validate_runtime_secret(value: str) -> str:
    if type(value) is not str or not value or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        for character in value
    ):
        raise ValueError
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        raise ValueError from None
    if len(encoded) > 2560:
        raise ValueError
    return value


def _default_sandbox_validator(
    profile: str, environment: Mapping[str, str]
) -> None:
    with tempfile.TemporaryDirectory(prefix="ones-dev-sandbox-preflight-") as raw:
        cwd = Path(raw).resolve(strict=True)
        completed = SandboxCommandExecutor(permission_profile=profile)(
            sandbox_preflight_command(),
            cwd=cwd,
            env=dict(environment),
            timeout=20,
            max_output_bytes=64 * 1024,
        )
        if completed.returncode != 0:
            raise RuntimeError("sandbox profile is unavailable")


def _run_gateway_close(gateway: OnesGateway) -> None:
    async def close() -> None:
        await gateway.close()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(close())
        return
    failure: list[BaseException] = []

    def worker() -> None:
        try:
            asyncio.run(close())
        except BaseException as error:  # pragma: no cover - defensive bridge
            failure.append(error)

    thread = threading.Thread(target=worker, name="ones-gateway-close")
    thread.start()
    thread.join()
    if failure:
        raise RuntimeBootstrapError("production runtime close failed") from None


@dataclass(slots=True)
class RuntimeHandle:
    orchestrator: DeveloperWorkflowOrchestrator
    gateway: OnesGateway
    close_callback: Callable[[], None] = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.close_callback()
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            raise RuntimeBootstrapError("production runtime close failed") from None


@dataclass(slots=True)
class RuntimeBootstrapper:
    private_root_preparer: PrivateRootPreparer = prepare_private_roots
    sandbox_profile_validator: SandboxProfileValidator = _default_sandbox_validator
    gateway_close: Callable[[OnesGateway], None] = _run_gateway_close
    ambient_environment: Callable[[], Mapping[str, str]] = lambda: os.environ

    def build(self, active: ActiveSetup, secrets: RuntimeSecrets) -> RuntimeHandle:
        gateway: OnesGateway | None = None
        pr_client: HttpPullRequestClient | None = None
        try:
            if type(active) is not ActiveSetup or type(secrets) is not RuntimeSecrets:
                raise ValueError
            public = active.runtime
            workflow = active.workflow
            validated_secrets = {
                kind: _validate_runtime_secret(value)
                for kind, value in secrets.values.items()
            }
            settings = OnesSettings(
                _env_file=None,
                base_url=public.ones_base_url,
                team_id=public.ones_team_id,
                issue_type_id=public.ones_issue_type_id,
                comment_list_path_template=public.ones_comment_list_path_template,
                email=validated_secrets[SecretKind.ONES_EMAIL],
                password=validated_secrets[SecretKind.ONES_PASSWORD],
                api_token="",
            )
            provider_token = validated_secrets[SecretKind.PROVIDER_TOKEN]
            codex_environment = self._codex_environment(public, secrets)
            git_credentials = {
                name: value
                for kind, name in _GIT_SECRET_ENV.items()
                if (value := secrets.values.get(kind, ""))
            }
            identity_values = {
                "GIT_AUTHOR_NAME": public.git_author_name,
                "GIT_AUTHOR_EMAIL": public.git_author_email,
                "GIT_COMMITTER_NAME": public.git_author_name,
                "GIT_COMMITTER_EMAIL": public.git_author_email,
            }
            validate_git_identity_environment(identity_values)
            validate_codex_auth_source(codex_environment)
            for mapping in (
                *workflow.repositories,
                *(repo for group in workflow.repository_groups for repo in group.repositories),
            ):
                parse_repository_identity(mapping.repo_url, public.provider_host)
            self.sandbox_profile_validator(
                workflow.sandbox_permission_profile, codex_environment
            )
            run_root, mirror_root, worktree_root = self.private_root_preparer(
                (workflow.run_root, workflow.mirror_root, workflow.worktree_root)
            )

            store = FileRunStore(run_root)
            repository = WorktreeRepository(
                mirror_root,
                worktree_root,
                credential_env_provider=lambda: dict(git_credentials),
                identity_env_provider=lambda: dict(identity_values),
            )
            gateway = OnesGateway(settings=settings)
            codex = CodexRunner(
                run_root,
                repository,
                environment_provider=lambda: dict(codex_environment),
            )
            test_runner = SandboxCommandExecutor(
                permission_profile=workflow.sandbox_permission_profile
            )
            group_workspace = RepositoryGroupWorkspace(repository)
            requirement_flow = RequirementFlow(
                store, gateway, workflow, repository, codex, test_runner,
                group_workspace=group_workspace,
            )
            defect_flow = DefectFlow(
                store, workflow, repository, codex, test_runner,
                group_workspace=group_workspace,
            )
            candidates = DefectCandidateService(gateway, settings.issue_type_id)
            pr_client = HttpPullRequestClient(
                provider=workflow.publishing.provider.value,
                provider_host=public.provider_host,
                api_base_url=public.provider_api_url,
                token_provider=lambda: provider_token,
            )
            publisher = Publisher(
                store,
                repository,
                WorkflowApprovalRebuilder(gateway, repository),
                pr_client,
                OnesCommenter(gateway, store),
                workflow.publishing.provider.value,
                public.provider_host,
            )
            orchestrator = DeveloperWorkflowOrchestrator(
                store, requirement_flow, defect_flow, publisher, workflow, candidates
            )

            def close() -> None:
                assert pr_client is not None and gateway is not None
                try:
                    pr_client.client.close()
                finally:
                    self.gateway_close(gateway)

            return RuntimeHandle(orchestrator, gateway, close)
        except BaseException as error:
            if pr_client is not None:
                try:
                    pr_client.client.close()
                except BaseException:
                    pass
            if gateway is not None:
                try:
                    self.gateway_close(gateway)
                except BaseException:
                    pass
            if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            if isinstance(error, RuntimeBootstrapError):
                raise
            raise RuntimeBootstrapError(
                "production runtime configuration is incomplete"
            ) from None

    def _codex_environment(
        self, public: RuntimePublicConfig, secrets: RuntimeSecrets
    ) -> dict[str, str]:
        ambient = self.ambient_environment()
        environment = {
            key: value
            for key, value in ambient.items()
            if type(key) is str
            and type(value) is str
            and key.upper() in _CODEX_BASE_ENV
        }
        if public.codex_auth_mode == "credential":
            api_key = secrets.values.get(SecretKind.CODEX_API_KEY, "")
            auth_token = secrets.values.get(SecretKind.CODEX_AUTH_TOKEN, "")
            if bool(api_key) == bool(auth_token):
                raise ValueError
            environment[
                "CODEX_API_KEY" if api_key else "CODEX_AUTH_TOKEN"
            ] = api_key or auth_token
        elif public.codex_home is not None:
            environment["CODEX_HOME"] = str(public.codex_home)
        else:
            raise ValueError
        return environment


__all__ = ["RuntimeBootstrapError", "RuntimeBootstrapper", "RuntimeHandle"]


def legacy_runtime_inputs(
    workflow: DeveloperWorkflowConfig,
    environment: Mapping[str, str],
    *,
    ones_settings: OnesSettings | None = None,
) -> tuple[ActiveSetup, RuntimeSecrets]:
    """Translate the established non-interactive environment contract once."""

    try:
        settings = ones_settings or OnesSettings(_env_file=None, **{
            name: environment.get(env_name, default)
            for name, env_name, default in (
                ("base_url", "ONES_BASE_URL", "http://aputureones.com:8088"),
                ("email", "ONES_EMAIL", ""),
                ("password", "ONES_PASSWORD", ""),
                ("team_id", "ONES_TEAM_ID", ""),
                ("issue_type_id", "ONES_ISSUE_TYPE_ID", ""),
                ("comment_list_path_template", "ONES_COMMENT_LIST_PATH_TEMPLATE", None),
            )
        })
        if environment.get("ONES_API_TOKEN", ""):
            raise ValueError
        codex_home = validate_codex_auth_source(environment)
        codex_secret_kind: SecretKind | None = None
        if environment.get("CODEX_API_KEY", ""):
            codex_secret_kind = SecretKind.CODEX_API_KEY
        elif environment.get("CODEX_AUTH_TOKEN", ""):
            codex_secret_kind = SecretKind.CODEX_AUTH_TOKEN
        elif environment.get("OPENAI_API_KEY", ""):
            codex_secret_kind = SecretKind.CODEX_API_KEY
        public = RuntimePublicConfig(
            ones_base_url=settings.base_url,
            ones_team_id=settings.team_id,
            ones_issue_type_id=settings.issue_type_id,
            ones_comment_list_path_template=settings.comment_list_path_template,
            provider_host=environment.get("ONES_DEV_PROVIDER_HOST", "").casefold(),
            provider_api_url=environment.get("ONES_DEV_PROVIDER_API_URL", ""),
            git_author_name=environment.get("ONES_DEV_GIT_AUTHOR_NAME", ""),
            git_author_email=environment.get("ONES_DEV_GIT_AUTHOR_EMAIL", ""),
            codex_auth_mode="credential" if codex_secret_kind is not None else "file",
            codex_home=codex_home,
        )
        values: dict[SecretKind, str] = {
            SecretKind.ONES_EMAIL: settings.email,
            SecretKind.ONES_PASSWORD: settings.password,
            SecretKind.PROVIDER_TOKEN: environment.get("ONES_DEV_PROVIDER_TOKEN", ""),
        }
        if codex_secret_kind is not None:
            values[codex_secret_kind] = (
                environment.get("OPENAI_API_KEY", "")
                if codex_secret_kind is SecretKind.CODEX_API_KEY
                and not environment.get("CODEX_API_KEY", "")
                else environment.get(
                    "CODEX_API_KEY"
                    if codex_secret_kind is SecretKind.CODEX_API_KEY
                    else "CODEX_AUTH_TOKEN",
                    "",
                )
            )
        for kind, name in _GIT_SECRET_ENV.items():
            value = environment.get(f"ONES_DEV_{name}", "")
            if value:
                values[kind] = value
        secrets = RuntimeSecrets(values)
        active = ActiveSetup(
            generation="0" * 32,
            runtime=public,
            workflow=workflow,
            credential_kinds=tuple(values),
        )
        return active, secrets
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        raise RuntimeBootstrapError(
            "production runtime configuration is incomplete"
        ) from None


__all__.append("legacy_runtime_inputs")
