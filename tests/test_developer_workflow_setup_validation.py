from __future__ import annotations

import json
from pathlib import Path
import subprocess
from dataclasses import FrozenInstanceError
import asyncio
import threading
import time
import sys

import pytest
from pydantic import ValidationError

from src.developer_workflow.setup_models import SetupValidationError
from src.developer_workflow.requirement_flow import sandbox_preflight_command
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
    RuntimeBootstrapper,
    SubprocessDoctorRunner,
    CodexAuthSourceChecker,
    ManagedSandboxExecutorFactory,
    ReadOnlyRepositoryInspector,
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
                                "cwd": str(config.parent),
                                "enabled feature flags": "<redacted>",
                                "feature flag overrides": "none",
                                "feature flags enabled": "0",
                                "log dir": str(config.parent / "log"),
                                "mcp servers": "0",
                                "model": "gpt-test",
                                "model provider": "openai",
                                "sqlite home": str(config.parent),
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
        '[permissions."managed-b"]\nextends = ":workspace"\n'
        '[permissions."managed-a"]\nextends = ":workspace"\n',
        encoding="utf-8",
    )
    executors: list[_CapabilityExecutor] = []

    def factory(profile: str):
        executor = _CapabilityExecutor(profile)
        executors.append(executor)
        return executor

    catalog = ManagedProfileCatalog._testing(
        _doctor(config),
        trusted_admin_catalog=None,
        probe_worktree=tmp_path,
        executor_factory=factory,
        file_security=lambda path, admin: True,
    )
    assert catalog.list_profiles() == ("managed-a", "managed-b")
    probe_roots = [executor.calls[0][1] for executor in executors]
    assert all(executor.calls[0][0] == tuple(sandbox_preflight_command()) for executor in executors)
    assert all(root != tmp_path and not root.exists() for root in probe_roots)
    assert catalog.require_selected("managed-a") == "managed-a"
    config.write_text('[permissions."managed-b"]\nextends = ":workspace"\n', encoding="utf-8")
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
    catalog = ManagedProfileCatalog._testing(
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
    config.write_text('[permissions.managed]\nextends=":workspace"\n', encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    runner = _doctor(config)

    def attacked(argv, **kwargs):
        completed = runner(argv, **kwargs)
        report = json.loads(completed.stdout)
        report["checks"]["config.load"]["details"]["CODEX_HOME"] = str(other)
        return subprocess.CompletedProcess(argv, 0, json.dumps(report), "")

    catalog = ManagedProfileCatalog._testing(
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
    config.write_text('[permissions.managed]\nextends=":workspace"\n', encoding="utf-8")
    admin = tmp_path / "managed-sandbox-profiles.json"
    admin.write_text(
        json.dumps({"schema_version": 1, "profiles": ["admin", "admin"]}),
        encoding="utf-8",
    )
    catalog = ManagedProfileCatalog._testing(
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

    untrusted = ManagedProfileCatalog._testing(
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
        '[permissions.good]\nextends=":workspace"\n'
        '[permissions.bad]\nextends=":workspace"\n',
        encoding="utf-8",
    )

    class MatrixExecutor(_CapabilityExecutor):
        def __call__(self, command, **kwargs):
            if self.profile == "bad":
                raise RuntimeError("outside write was allowed: TOKEN-SECRET C:/private")
            return super().__call__(command, **kwargs)

    catalog = ManagedProfileCatalog._testing(
        _doctor(config),
        trusted_admin_catalog=None,
        probe_worktree=tmp_path,
        executor_factory=MatrixExecutor,
        file_security=lambda path, admin: True,
    )
    assert catalog.list_profiles() == ("good",)


def test_profile_catalog_final_child_succeeds_without_git_repository(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[permissions.managed]\nextends=":workspace"\n', encoding="utf-8")
    executed: list[tuple[str, ...]] = []

    class RealChildExecutor(_CapabilityExecutor):
        def __call__(self, command, *, cwd, env, timeout, **kwargs):
            executed.append(tuple(command))
            if command[:2] == ["git", "status"]:
                return subprocess.CompletedProcess(command, 128, "", "not a repository")
            completed = subprocess.run(
                command, cwd=cwd, env=env, capture_output=True, text=True,
                timeout=timeout, check=False,
            )
            return completed

    catalog = ManagedProfileCatalog._testing(
        _doctor(config), trusted_admin_catalog=None, probe_worktree=tmp_path,
        executor_factory=RealChildExecutor, file_security=lambda path, admin: True,
    )
    assert catalog.list_profiles() == ("managed",)
    assert len(executed) == 1
    assert executed[0][0] == sys.executable
    assert executed[0][1:3] == ("-I", "-c")


def test_profile_probe_uses_private_directory_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.developer_workflow.setup_validation as validation_module

    config = tmp_path / "config.toml"
    config.write_text('[permissions.managed]\nextends=":workspace"\n', encoding="utf-8")
    prepared: list[Path] = []

    def prepare(path: Path) -> Path:
        path.mkdir()
        prepared.append(path)
        return path.resolve(strict=True)

    monkeypatch.setattr(validation_module, "prepare_private_directory", prepare)
    catalog = ManagedProfileCatalog._testing(
        _doctor(config), trusted_admin_catalog=None, probe_worktree=tmp_path,
        executor_factory=_CapabilityExecutor, file_security=lambda path, admin: True,
    )
    assert catalog.list_profiles() == ("managed",)
    assert len(prepared) == 1
    assert not prepared[0].exists()


@pytest.mark.parametrize(
    "profile",
    [
        'extends = ":workspace-write"',
        'extends = [":workspace"]',
        'unknown = true',
        '[permissions.managed.workspace_roots]\n"." = "true"',
        '[permissions.managed.filesystem]\nglob_scan_max_depth = 0',
        '[permissions.managed.filesystem]\n"**/.env" = "execute"',
        '[permissions.managed.filesystem]\nroot = { child = "execute" }',
        '[permissions.managed.network]\nmode = "unknown"',
        '[permissions.managed.network]\nenabled = "yes"',
        '[permissions.managed.network.domains]\n"example.com" = "maybe"',
        '[permissions.managed.network]\nextra = false',
    ],
)
def test_permissions_profile_exact_0147_schema_rejects_invalid_nested_values(
    tmp_path: Path, profile: str
) -> None:
    config = tmp_path / "config.toml"
    prefix = "[permissions.managed]\n" if not profile.startswith("[permissions") else ""
    config.write_text(prefix + profile + "\n", encoding="utf-8")
    catalog = ManagedProfileCatalog._testing(
        _doctor(config),
        trusted_admin_catalog=None,
        probe_worktree=tmp_path,
        executor_factory=_CapabilityExecutor,
        file_security=lambda path, admin: True,
    )
    with pytest.raises(SetupValidationError, match="permissions table"):
        catalog.list_profiles()


def test_permissions_profile_accepts_exact_0147_nested_schema(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[permissions.managed]\ndescription = "ONES managed"\nextends = ":workspace"\n'
        '[permissions.managed.workspace_roots]\n"." = true\n'
        '[permissions.managed.filesystem]\nglob_scan_max_depth = 4\n'
        '"**/.env" = "deny"\nroot = { child = "read" }\n'
        '[permissions.managed.network]\nenabled = false\nmode = "limited"\n'
        '[permissions.managed.network.domains]\n"example.invalid" = "deny"\n',
        encoding="utf-8",
    )
    catalog = ManagedProfileCatalog._testing(
        _doctor(config), trusted_admin_catalog=None, probe_worktree=tmp_path,
        executor_factory=_CapabilityExecutor, file_security=lambda path, admin: True,
    )
    assert catalog.list_profiles() == ("managed",)


def test_permissions_profile_inheritance_cycle_fails_closed(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[permissions.a]\nextends="b"\n[permissions.b]\nextends="a"\n',
        encoding="utf-8",
    )
    catalog = ManagedProfileCatalog._testing(
        _doctor(config), trusted_admin_catalog=None, probe_worktree=tmp_path,
        executor_factory=_CapabilityExecutor, file_security=lambda path, admin: True,
    )
    with pytest.raises(SetupValidationError, match="permissions table"):
        catalog.list_profiles()


def test_doctor_config_load_schema_is_exact_for_0147(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[permissions.managed]\nextends=":workspace"\n', encoding="utf-8")
    base = _doctor(config)

    def attacked(argv, **kwargs):
        completed = base(argv, **kwargs)
        report = json.loads(completed.stdout)
        report["checks"]["config.load"]["details"]["unexpected"] = "value"
        return subprocess.CompletedProcess(argv, 0, json.dumps(report), "")

    catalog = ManagedProfileCatalog._testing(
        attacked, trusted_admin_catalog=None, probe_worktree=tmp_path,
        executor_factory=_CapabilityExecutor, file_security=lambda path, admin: True,
    )
    with pytest.raises(SetupValidationError, match="unavailable"):
        catalog.list_profiles()


def test_doctor_rejects_unknown_fields_in_any_check(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[permissions.managed]\nextends=":workspace"\n', encoding="utf-8")
    base = _doctor(config)

    def attacked(argv, **kwargs):
        completed = base(argv, **kwargs)
        report = json.loads(completed.stdout)
        report["checks"]["other"] = {
            "id": "other", "category": "system", "status": "ok", "summary": "ok",
            "details": {}, "remediation": None, "durationMs": 0, "unexpected": True,
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(report), "")

    catalog = ManagedProfileCatalog._testing(
        attacked, trusted_admin_catalog=None, probe_worktree=tmp_path,
        executor_factory=_CapabilityExecutor, file_security=lambda path, admin: True,
    )
    with pytest.raises(SetupValidationError, match="unavailable"):
        catalog.list_profiles()


def test_profile_catalog_has_one_total_capability_budget(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[permissions.a]\nextends=":workspace"\n[permissions.b]\nextends=":workspace"\n',
        encoding="utf-8",
    )
    observed: list[float] = []

    class SlowExecutor(_CapabilityExecutor):
        def __call__(self, command, *, timeout, **kwargs):
            observed.append(timeout)
            time.sleep(min(timeout, 0.04))
            return subprocess.CompletedProcess(command, 1, "", "")

    catalog = ManagedProfileCatalog._testing(
        _doctor(config), trusted_admin_catalog=None, probe_worktree=tmp_path,
        executor_factory=SlowExecutor, file_security=lambda path, admin: True,
        timeout_seconds=0.05,
    )
    started = time.monotonic()
    assert catalog.list_profiles() == ()
    # Windows protected-DACL preparation has fixed overhead outside the command budget.
    assert time.monotonic() - started < 0.20
    assert sum(observed) <= 0.07


def test_production_runtime_wires_only_real_policy_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.developer_workflow.setup_validation as validation_module

    config = tmp_path / "config.toml"
    config.write_text('[permissions.managed]\nextends=":workspace"\n', encoding="utf-8")
    calls: list[tuple[list[str], Path]] = []

    def os_command(command, *, cwd, env, timeout, max_output_bytes, stdin=None):
        calls.append((command, cwd))
        if command == ["codex", "doctor", "--json"]:
            return _doctor(config)(command, cwd=cwd, env=env, timeout=timeout,
                                   max_output_bytes=max_output_bytes, shell=False)
        child = command[command.index("--") + 1 :]
        if any("outside-write.txt" in item for item in child):
            return subprocess.CompletedProcess(command, 13, "", "denied")
        if any("s.connect" in item for item in child):
            return subprocess.CompletedProcess(command, 23, "", "denied")
        if any("inside-write.txt" in item for item in child):
            completed = subprocess.run(child, cwd=cwd, env=env, capture_output=True, check=False)
            return subprocess.CompletedProcess(
                command, completed.returncode, completed.stdout.decode(), completed.stderr.decode()
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(validation_module, "_bounded_subprocess", os_command)
    monkeypatch.setattr(validation_module, "_default_file_security", lambda path, admin: True)
    runtime = RuntimeBootstrapper.production(probe_parent=tmp_path)
    assert isinstance(runtime.catalog.codex_doctor, SubprocessDoctorRunner)
    assert isinstance(runtime.catalog.executor_factory, ManagedSandboxExecutorFactory)
    assert isinstance(runtime.validator.codex_auth_metadata, CodexAuthSourceChecker)
    assert isinstance(runtime.validator.repository_inspector, ReadOnlyRepositoryInspector)
    assert runtime.validator.profile_catalog is runtime.catalog
    assert runtime.catalog.require_selected("managed") == "managed"
    assert any(call[0][:2] == ["codex", "sandbox"] for call in calls)
    assert all(cwd != tmp_path for command, cwd in calls if command[:2] == ["codex", "sandbox"])


def test_read_only_repository_inspector_preserves_source_index_and_status(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=Test", "-c",
         "user.email=test@example.invalid", "commit", "-qm", "initial"], check=True,
    )
    (source / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    status_before = subprocess.run(
        ["git", "-C", str(source), "status", "--porcelain=v1"],
        capture_output=True, check=True,
    ).stdout
    index = source / ".git" / "index"
    index_before = index.stat()

    inspector = ReadOnlyRepositoryInspector()
    before = inspector.snapshot(source, timeout=10)
    after = inspector.snapshot(source, timeout=10)

    index_after = index.stat()
    status_after = subprocess.run(
        ["git", "-C", str(source), "status", "--porcelain=v1"],
        capture_output=True, check=True,
    ).stdout
    assert before == after
    assert status_before == status_after
    assert (index_before.st_size, index_before.st_mtime_ns) == (
        index_after.st_size, index_after.st_mtime_ns
    )


def test_repository_inspector_sanitizes_missing_git_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.developer_workflow.setup_validation as validation_module
    from src.developer_workflow.setup_validation import GitExecutableUnavailableError

    canary = "SECRET-GIT-PATH"
    monkeypatch.setattr(
        validation_module,
        "_bounded_subprocess",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError(canary)),
    )

    with pytest.raises(GitExecutableUnavailableError) as raised:
        ReadOnlyRepositoryInspector()._run(
            ["git", "status"],
            cwd=Path.cwd(),
            private_root=Path.cwd(),
            hooks=Path.cwd(),
            timeout=1,
        )

    assert str(raised.value) == "Git executable is unavailable"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert canary not in str(raised.value)
    assert canary not in repr(raised.value)


def test_repository_inspector_sanitizes_real_wrapped_missing_git_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.developer_workflow.setup_validation import GitExecutableUnavailableError

    private_root = tmp_path / "private"
    hooks = private_root / "hooks"
    hooks.mkdir(parents=True)
    inspector = ReadOnlyRepositoryInspector()
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("GIT_PYTHON_GIT_EXECUTABLE", raising=False)
    environment = inspector._environment(private_root, hooks)
    for key in tuple(environment):
        if key.casefold() in {"path", "git_python_git_executable"}:
            environment.pop(key)
    environment["PATH"] = ""
    monkeypatch.setattr(
        ReadOnlyRepositoryInspector,
        "_environment",
        lambda self, private_root, hooks: dict(environment),
    )

    with pytest.raises(GitExecutableUnavailableError) as raised:
        inspector._run(
            ["git", "--version"],
            cwd=private_root,
            private_root=private_root,
            hooks=hooks,
            timeout=2,
        )

    assert str(raised.value) == "Git executable is unavailable"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def _raise_wrapped_start_error(cause: OSError) -> None:
    from src.developer_workflow.codex_runner import CodexProcessStartError

    try:
        raise cause
    except OSError as error:
        raise CodexProcessStartError("wrapped process start failure") from error


def test_repository_inspector_preserves_wrapped_non_file_not_found_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.developer_workflow.setup_validation as validation_module
    from src.developer_workflow.codex_runner import CodexExecutionError
    from src.developer_workflow.setup_validation import GitExecutableUnavailableError

    cause = PermissionError("permission denied")
    monkeypatch.setattr(
        validation_module,
        "_bounded_subprocess",
        lambda *args, **kwargs: _raise_wrapped_start_error(cause),
    )

    with pytest.raises(CodexExecutionError) as raised:
        ReadOnlyRepositoryInspector()._run(
            ["git", "--version"],
            cwd=Path.cwd(),
            private_root=Path.cwd(),
            hooks=Path.cwd(),
            timeout=1,
        )

    assert raised.value.__cause__ is cause
    assert not isinstance(raised.value, GitExecutableUnavailableError)


def test_repository_inspector_preserves_isolation_file_not_found_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.developer_workflow.setup_validation as validation_module
    from src.developer_workflow.codex_runner import CodexExecutionError
    from src.developer_workflow.setup_validation import GitExecutableUnavailableError

    cause = FileNotFoundError("missing isolation resource")
    isolation_error = CodexExecutionError("Codex process could not be isolated")

    def fail_isolation(*args: object, **kwargs: object) -> None:
        try:
            raise cause
        except FileNotFoundError as error:
            raise isolation_error from error

    monkeypatch.setattr(validation_module, "_bounded_subprocess", fail_isolation)

    with pytest.raises(CodexExecutionError) as raised:
        ReadOnlyRepositoryInspector()._run(
            ["git", "--version"],
            cwd=Path.cwd(),
            private_root=Path.cwd(),
            hooks=Path.cwd(),
            timeout=1,
        )

    assert type(raised.value) is CodexExecutionError
    assert raised.value is isolation_error
    assert raised.value.__cause__ is cause
    assert not isinstance(raised.value, GitExecutableUnavailableError)


def test_repository_inspector_preserves_wrapped_missing_non_git_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.developer_workflow.setup_validation as validation_module
    from src.developer_workflow.codex_runner import CodexExecutionError
    from src.developer_workflow.setup_validation import GitExecutableUnavailableError

    cause = FileNotFoundError("missing executable")
    monkeypatch.setattr(
        validation_module,
        "_bounded_subprocess",
        lambda *args, **kwargs: _raise_wrapped_start_error(cause),
    )

    with pytest.raises(CodexExecutionError) as raised:
        ReadOnlyRepositoryInspector()._run(
            ["not-git", "--version"],
            cwd=Path.cwd(),
            private_root=Path.cwd(),
            hooks=Path.cwd(),
            timeout=1,
        )

    assert raised.value.__cause__ is cause
    assert not isinstance(raised.value, GitExecutableUnavailableError)


@pytest.mark.parametrize("invalid_cwd", ["missing", "file"])
def test_repository_inspector_preserves_wrapped_missing_error_for_invalid_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_cwd: str,
) -> None:
    import src.developer_workflow.setup_validation as validation_module
    from src.developer_workflow.codex_runner import CodexExecutionError
    from src.developer_workflow.setup_validation import GitExecutableUnavailableError

    cwd = tmp_path / invalid_cwd
    if invalid_cwd == "file":
        cwd.write_text("not a directory", encoding="utf-8")
    cause = FileNotFoundError("missing path")
    monkeypatch.setattr(
        validation_module,
        "_bounded_subprocess",
        lambda *args, **kwargs: _raise_wrapped_start_error(cause),
    )

    with pytest.raises(CodexExecutionError) as raised:
        ReadOnlyRepositoryInspector()._run(
            ["git", "--version"],
            cwd=cwd,
            private_root=tmp_path,
            hooks=tmp_path,
            timeout=1,
        )

    assert raised.value.__cause__ is cause
    assert not isinstance(raised.value, GitExecutableUnavailableError)


@pytest.mark.parametrize(
    "error",
    [
        PermissionError("permission denied"),
        subprocess.TimeoutExpired(["git"], 1),
        OSError(24, "too many files"),
        RuntimeError("probe failed"),
    ],
)
def test_repository_inspector_does_not_misclassify_other_failures(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    import src.developer_workflow.setup_validation as validation_module
    from src.developer_workflow.setup_validation import GitExecutableUnavailableError

    monkeypatch.setattr(
        validation_module,
        "_bounded_subprocess",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(type(error)) as raised:
        ReadOnlyRepositoryInspector()._run(
            ["git", "status"],
            cwd=Path.cwd(),
            private_root=Path.cwd(),
            hooks=Path.cwd(),
            timeout=1,
        )

    assert raised.value is error
    assert not isinstance(raised.value, GitExecutableUnavailableError)


@pytest.mark.parametrize("fatal_type", [KeyboardInterrupt, SystemExit])
def test_repository_inspector_preserves_control_flow_failures(
    monkeypatch: pytest.MonkeyPatch,
    fatal_type: type[BaseException],
) -> None:
    import src.developer_workflow.setup_validation as validation_module

    fatal = fatal_type("control flow")
    monkeypatch.setattr(
        validation_module,
        "_bounded_subprocess",
        lambda *args, **kwargs: (_ for _ in ()).throw(fatal),
    )

    with pytest.raises(fatal_type) as raised:
        ReadOnlyRepositoryInspector()._run(
            ["git", "status"],
            cwd=Path.cwd(),
            private_root=Path.cwd(),
            hooks=Path.cwd(),
            timeout=1,
        )

    assert raised.value is fatal


def test_repository_inspector_never_executes_repo_local_programs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    tracked = source / "tracked.txt"
    attributes = source / ".gitattributes"
    tracked.write_text("before\n", encoding="utf-8")
    attributes.write_text("*.txt diff=hostile\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=Test", "-c",
         "user.email=test@example.invalid", "commit", "-qm", "initial"], check=True,
    )
    tracked.write_text("after\n", encoding="utf-8")

    markers = {name: tmp_path / f"{name}.marker" for name in (
        "ssh", "textconv", "fsmonitor", "include", "credential"
    )}

    def marker_command(marker: Path) -> str:
        program = (
            "from pathlib import Path; import sys; "
            "Path(sys.argv[1]).write_text('executed', encoding='utf-8'); raise SystemExit(1)"
        )
        return subprocess.list2cmdline([sys.executable, "-I", "-c", program, str(marker)])

    included = tmp_path / "included.gitconfig"
    included.write_text(
        '[core]\n\tsshCommand = "{}"\n'.format(
            marker_command(markers["include"]).replace("\\", "\\\\").replace('"', '\\"')
        ),
        encoding="utf-8",
    )
    settings = {
        "core.sshCommand": marker_command(markers["ssh"]),
        "diff.hostile.textconv": marker_command(markers["textconv"]),
        "core.fsmonitor": marker_command(markers["fsmonitor"]),
        "include.path": str(included),
        "credential.helper": "!" + marker_command(markers["credential"]),
    }
    for key, value in settings.items():
        subprocess.run(["git", "-C", str(source), "config", key, value], check=True)

    config = source / ".git" / "config"
    config_before = (config.stat().st_dev, config.stat().st_ino, config.read_bytes())
    inspector = ReadOnlyRepositoryInspector()
    inspector.snapshot(source, timeout=10)
    with pytest.raises(RuntimeError, match="Git probe"):
        inspector.ls_remote(source, "ssh://example.invalid/repository.git", timeout=2)
    config_after = (config.stat().st_dev, config.stat().st_ino, config.read_bytes())

    assert config_before == config_after
    assert all(not marker.exists() for marker in markers.values())


def test_repository_snapshot_never_runs_content_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    attributes = source / ".gitattributes"
    attributes.write_text(
        "clean.txt filter=clean\nsmudge.txt filter=smudge\nprocess.txt filter=process\n",
        encoding="utf-8",
    )
    files = [source / name for name in ("clean.txt", "smudge.txt", "process.txt")]
    for candidate in files:
        candidate.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git", "-C", str(source), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-qm", "initial",
        ],
        check=True,
    )

    markers = {name: tmp_path / f"{name}.marker" for name in ("clean", "smudge", "process")}

    def marker_command(marker: Path) -> str:
        program = (
            "from pathlib import Path; import sys; "
            "Path(sys.argv[1]).write_text('executed', encoding='utf-8'); raise SystemExit(1)"
        )
        return subprocess.list2cmdline([sys.executable, "-I", "-c", program, str(marker)])

    settings = {
        "filter.clean.clean": marker_command(markers["clean"]),
        "filter.smudge.smudge": marker_command(markers["smudge"]),
        "filter.process.process": marker_command(markers["process"]),
    }
    for key, value in settings.items():
        subprocess.run(["git", "-C", str(source), "config", key, value], check=True)
    for candidate in files:
        candidate.write_text("after\n", encoding="utf-8")

    commands: list[tuple[str, ...]] = []
    original_run = ReadOnlyRepositoryInspector._run

    def recording_run(self, argv, **kwargs):
        commands.append(tuple(argv))
        return original_run(self, argv, **kwargs)

    monkeypatch.setattr(ReadOnlyRepositoryInspector, "_run", recording_run)
    inspector = ReadOnlyRepositoryInspector()
    before = inspector.snapshot(source, timeout=10)
    files[0].write_text("changed-again\n", encoding="utf-8")
    after = inspector.snapshot(source, timeout=10)

    assert before != after
    assert all(not marker.exists() for marker in markers.values())
    assert all("diff" not in command for command in commands)


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


class _Inspector:
    def __init__(self): self.calls = []; self.value = ("snapshot", "a" * 40)
    def snapshot(self, path, *, timeout):
        self.calls.append(("snapshot", path, timeout)); return self.value
    def ls_remote(self, path, url, *, timeout):
        self.calls.append(("ls_remote", path, url, timeout)); return None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["snapshot", "ls_remote"])
async def test_repository_probe_reports_missing_git_without_original_details(
    failure_stage: str,
) -> None:
    from src.developer_workflow.setup_validation import GitExecutableUnavailableError

    canary = "SECRET-GIT-PATH"

    class MissingGitInspector(_Inspector):
        def snapshot(self, path, *, timeout):
            if failure_stage == "snapshot":
                raise GitExecutableUnavailableError("Git executable is unavailable")
            return super().snapshot(path, timeout=timeout)

        def ls_remote(self, path, url, *, timeout):
            if failure_stage == "ls_remote":
                raise GitExecutableUnavailableError("Git executable is unavailable")
            return super().ls_remote(path, url, timeout=timeout)

    repository = Path.cwd()
    validator = SetupValidator._testing(repository_inspector=MissingGitInspector())

    result = await validator.probe_repository(
        RepositoryProbeInput(
            path=repository,
            remote_url="https://git.example.invalid/team/repository.git",
        )
    )

    assert result == ConnectionTestResult(
        step=SetupStep.REPOSITORIES,
        status=ValidationStatus.FAILED,
        category="git_unavailable",
    )
    assert canary not in repr(result)


class _Auth:
    def metadata(self): return {"configured": True, "mode": "file"}


@pytest.mark.asyncio
async def test_setup_validator_probes_are_read_only_and_private_paths_are_not_created(
    tmp_path: Path,
) -> None:
    gateway, provider, inspector = _OnesGateway(), _Provider(), _Inspector()
    validator = SetupValidator._testing(
        ones_gateway=gateway,
        provider_transport=provider,
        repository_inspector=inspector,
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
    assert [call[0] for call in inspector.calls] == ["snapshot", "ls_remote", "snapshot"]

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

    validator = SetupValidator._testing(ones_gateway=Broken())
    result = await validator.probe_ones(
        OnesProbeInput(team_id="T", project_id="P", status_id="S", item_id="I")
    )
    assert result == ConnectionTestResult(
        step=SetupStep.ONES,
        status=ValidationStatus.FAILED,
        category="unreachable",
    )
    assert secret not in repr(result)


@pytest.mark.asyncio
async def test_sync_codex_probe_does_not_block_event_loop_and_times_out_cleanly(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    finished = threading.Event()

    class BlockingCatalog:
        def require_selected(self, selected):
            started.set()
            time.sleep(0.12)
            finished.set()
            return selected

    validator = SetupValidator._testing(
        codex_auth_metadata=_Auth(), profile_catalog=BlockingCatalog(), timeout_seconds=0.04
    )
    ticks = 0

    async def ticker():
        nonlocal ticks
        while not finished.is_set():
            ticks += 1
            await asyncio.sleep(0.005)

    ticker_task = asyncio.create_task(ticker())
    result = await validator.probe_codex(CodexProbeInput(profile="managed", worktree=tmp_path))
    assert started.is_set()
    assert result.category == "timeout"
    assert ticks >= 3
    assert finished.is_set()
    await ticker_task


@pytest.mark.asyncio
async def test_codex_probe_never_runs_capability_executor_in_source(tmp_path: Path) -> None:
    class Catalog:
        def require_selected(self, selected): return selected

    def reject_source_execution(*args, **kwargs):
        raise AssertionError("catalog already performed the capability probe in a temp root")

    validator = SetupValidator._testing(
        codex_auth_metadata=_Auth(),
        profile_catalog=Catalog(),
        command_executor=reject_source_execution,
    )
    result = await validator.probe_codex(
        CodexProbeInput(profile="managed", worktree=tmp_path)
    )
    assert result.category == "ok"
