from __future__ import annotations

import json
from pathlib import Path
import subprocess
from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from src.developer_workflow.setup_models import SetupValidationError
from src.developer_workflow.setup_validation import (
    CodexProbeInput,
    ConnectionTestResult,
    ManagedProfileCatalog,
    OnesProbeInput,
    PrivatePathsProbeInput,
    ProviderProbeInput,
    RepositoryProbeInput,
    SetupStep,
    SetupValidator,
    ValidationStatus,
)


def _doctor(config: Path):
    def run(argv, **kwargs):
        assert argv == ["codex", "doctor", "--json"]
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "schemaVersion": 1,
                    "generatedAt": "now",
                    "overallStatus": "ok",
                    "codexVersion": "0.147.0",
                    "checks": {
                        "config.load": {
                            "id": "config.load",
                            "category": "config",
                            "status": "ok",
                            "summary": "config loaded",
                            "details": {
                                "CODEX_HOME": str(config.parent),
                                "config.toml": str(config),
                                "config.toml parse": "ok",
                            },
                            "remediation": None,
                            "durationMs": 0,
                        }
                    },
                }
            ),
            "",
        )

    return run


class _CapabilityExecutor:
    calls: list[tuple[tuple[str, ...], Path]]

    def __init__(self, profile: str):
        self.profile = profile
        self.calls = []

    def __call__(self, command, *, cwd, **kwargs):
        self.calls.append((tuple(command), cwd))
        return subprocess.CompletedProcess(command, 0, "probe-ok\n", "")


def test_result_is_strict_frozen_and_has_no_detail_channel() -> None:
    result = ConnectionTestResult(
        step=SetupStep.PROFILE, status=ValidationStatus.PASSED, category="ok"
    )
    assert result.model_dump() == {
        "step": "profile",
        "status": "passed",
        "category": "ok",
    }
    with pytest.raises((ValidationError, FrozenInstanceError)):
        result.category = "sandbox"
    with pytest.raises(ValidationError):
        ConnectionTestResult(
            step=SetupStep.PROFILE,
            status=ValidationStatus.FAILED,
            category="other",
        )
    with pytest.raises(ValidationError):
        ConnectionTestResult(
            step=SetupStep.PROFILE,
            status=ValidationStatus.FAILED,
            category="sandbox",
            detail="C:/secret TOKEN",
        )


def test_profile_catalog_uses_exact_permissions_table_and_rechecks_selection(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[permissions."managed-b"]\nextends = ":workspace-write"\n'
        '[permissions."managed-a"]\nextends = ":workspace-write"\n',
        encoding="utf-8",
    )
    executors: list[_CapabilityExecutor] = []

    def factory(profile: str):
        executor = _CapabilityExecutor(profile)
        executors.append(executor)
        return executor

    catalog = ManagedProfileCatalog(
        _doctor(config),
        trusted_admin_catalog=None,
        probe_worktree=tmp_path,
        executor_factory=factory,
        file_security=lambda path, admin: True,
    )
    assert catalog.list_profiles() == ("managed-a", "managed-b")
    assert all(executor.calls == [(('git', 'status', '--short'), tmp_path)] for executor in executors)
    assert catalog.require_selected("managed-a") == "managed-a"
    config.write_text('[permissions."managed-b"]\nextends = ":workspace-write"\n', encoding="utf-8")
    with pytest.raises(SetupValidationError, match="unavailable"):
        catalog.require_selected("managed-a")
    with pytest.raises(SetupValidationError, match="invalid"):
        catalog.require_selected("invented/profile")


@pytest.mark.parametrize(
    "document",
    [
        "[profiles.managed]\nsandbox_mode='workspace-write'\n",
        "permissions = ['managed']\n",
        "[permissions]\nmanaged = 'workspace-write'\n",
        "[permissions.bad/name]\nextends=':workspace-write'\n",
    ],
)
def test_profile_catalog_fails_closed_on_wrong_schema(tmp_path: Path, document: str) -> None:
    config = tmp_path / "config.toml"
    config.write_text(document, encoding="utf-8")
    catalog = ManagedProfileCatalog(
        _doctor(config),
        trusted_admin_catalog=None,
        probe_worktree=tmp_path,
        executor_factory=_CapabilityExecutor,
        file_security=lambda path, admin: True,
    )
    with pytest.raises(SetupValidationError):
        catalog.list_profiles()


def test_profile_catalog_rejects_doctor_path_outside_reported_home(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[permissions.managed]\nextends=":workspace-write"\n', encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    runner = _doctor(config)

    def attacked(argv, **kwargs):
        completed = runner(argv, **kwargs)
        report = json.loads(completed.stdout)
        report["checks"]["config.load"]["details"]["CODEX_HOME"] = str(other)
        return subprocess.CompletedProcess(argv, 0, json.dumps(report), "")

    catalog = ManagedProfileCatalog(
        attacked,
        trusted_admin_catalog=None,
        probe_worktree=tmp_path,
        executor_factory=_CapabilityExecutor,
        file_security=lambda path, admin: True,
    )
    with pytest.raises(SetupValidationError, match="unavailable"):
        catalog.list_profiles()


def test_profile_catalog_rejects_admin_duplicates_conflicts_and_untrusted_acl(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[permissions.managed]\nextends=":workspace-write"\n', encoding="utf-8")
    admin = tmp_path / "managed-sandbox-profiles.json"
    admin.write_text(
        json.dumps({"schema_version": 1, "profiles": ["admin", "admin"]}),
        encoding="utf-8",
    )
    catalog = ManagedProfileCatalog(
        _doctor(config),
        trusted_admin_catalog=admin,
        probe_worktree=tmp_path,
        executor_factory=_CapabilityExecutor,
        file_security=lambda path, is_admin: True,
    )
    with pytest.raises(SetupValidationError, match="invalid"):
        catalog.list_profiles()

    admin.write_text(
        json.dumps({"schema_version": 1, "profiles": ["managed"]}), encoding="utf-8"
    )
    with pytest.raises(SetupValidationError, match="conflicts"):
        catalog.list_profiles()

    untrusted = ManagedProfileCatalog(
        _doctor(config),
        trusted_admin_catalog=admin,
        probe_worktree=tmp_path,
        executor_factory=_CapabilityExecutor,
        file_security=lambda path, is_admin: not is_admin,
    )
    with pytest.raises(SetupValidationError, match="unsafe"):
        untrusted.list_profiles()


def test_profile_catalog_excludes_candidates_that_fail_full_executor_probe(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[permissions.good]\nextends=":workspace-write"\n'
        '[permissions.bad]\nextends=":workspace-write"\n',
        encoding="utf-8",
    )

    class MatrixExecutor(_CapabilityExecutor):
        def __call__(self, command, **kwargs):
            if self.profile == "bad":
                raise RuntimeError("outside write was allowed: TOKEN-SECRET C:/private")
            return super().__call__(command, **kwargs)

    catalog = ManagedProfileCatalog(
        _doctor(config),
        trusted_admin_catalog=None,
        probe_worktree=tmp_path,
        executor_factory=MatrixExecutor,
        file_security=lambda path, admin: True,
    )
    assert catalog.list_profiles() == ("good",)


class _OnesGateway:
    def __init__(self):
        self.calls = []

    async def authenticate(self): self.calls.append(("authenticate",))
    async def get_team(self, value): self.calls.append(("get_team", value))
    async def get_project(self, value): self.calls.append(("get_project", value))
    async def get_status(self, value): self.calls.append(("get_status", value))
    async def list_comments(self, value, *, page_size):
        self.calls.append(("list_comments", value, page_size))


class _Provider:
    def __init__(self): self.calls = []
    async def get(self, url, *, timeout): self.calls.append(("GET", url, timeout)); return 200


class _Git:
    def __init__(self): self.calls = []
    async def run(self, argv, *, cwd, timeout):
        self.calls.append(tuple(argv))
        if argv[-2:] == ("rev-parse", "HEAD"):
            return "a" * 40
        return ""


class _Auth:
    def metadata(self): return {"configured": True, "mode": "file"}


@pytest.mark.asyncio
async def test_setup_validator_probes_are_read_only_and_private_paths_are_not_created(
    tmp_path: Path,
) -> None:
    gateway, provider, git = _OnesGateway(), _Provider(), _Git()
    validator = SetupValidator(
        ones_gateway=gateway,
        provider_transport=provider,
        git_runner=git,
        codex_auth_metadata=_Auth(),
        profile_catalog=object(),
    )
    ones = await validator.probe_ones(
        OnesProbeInput(team_id="T", project_id="P", status_id="S", item_id="I")
    )
    assert ones.category == "ok"
    assert [call[0] for call in gateway.calls] == [
        "authenticate", "get_team", "get_project", "get_status", "list_comments"
    ]
    provider_result = await validator.probe_provider(
        ProviderProbeInput(host="git.example.invalid", api_url="https://git.example.invalid/api")
    )
    assert provider_result.status is ValidationStatus.PASSED
    assert provider.calls == [("GET", "https://git.example.invalid/api", 10.0)]

    repo = tmp_path / "repo"
    repo.mkdir()
    result = await validator.probe_repository(
        RepositoryProbeInput(path=repo, remote_url="https://git.example.invalid/o/r.git")
    )
    assert result.category == "ok"
    assert all("clone" not in call and "fetch" not in call for call in git.calls)

    missing = tmp_path / "future" / "runs"
    private = await validator.probe_private_paths(
        PrivatePathsProbeInput(paths=(missing, tmp_path / "mirrors", tmp_path / "worktrees"))
    )
    assert private.category == "ok"
    assert not missing.exists()


@pytest.mark.asyncio
async def test_probe_errors_are_fixed_and_sanitized() -> None:
    secret = "TOKEN-VERY-SECRET C:/private/path"

    class Broken:
        async def authenticate(self): raise RuntimeError(secret)

    validator = SetupValidator(ones_gateway=Broken())
    result = await validator.probe_ones(
        OnesProbeInput(team_id="T", project_id="P", status_id="S", item_id="I")
    )
    assert result == ConnectionTestResult(
        step=SetupStep.ONES,
        status=ValidationStatus.FAILED,
        category="unreachable",
    )
    assert secret not in repr(result)
