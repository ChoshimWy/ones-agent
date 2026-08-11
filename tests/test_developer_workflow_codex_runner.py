from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.developer_workflow.codex_runner import (
    CodexExecutionError,
    CodexOutputError,
    CodexRunner,
    CodexTimeoutError,
    UnsafeCodexRunError,
)
from src.developer_workflow.contracts import (
    PreparedWorktree,
    RepositoryChangeClaim,
    RepositoryGroupMapping,
    RepositoryMapping,
    RepositoryRole,
    RepositorySnapshot,
)
from src.developer_workflow.repository import HeadChangedError
from src.developer_workflow.repository_group import PreparedRepository


OID = "a" * 40
EMPTY_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _prepared(root: Path) -> PreparedWorktree:
    worktree = root / "worktree"
    mirror = root / "mirror.git"
    worktree.mkdir(exist_ok=True)
    mirror.mkdir(exist_ok=True)
    return PreparedWorktree(
        path=worktree.resolve(), branch="ai/run-1", base_commit=OID,
        head_commit=OID, mirror_path=mirror.resolve(),
    )


def _mapping(root: Path) -> RepositoryMapping:
    return RepositoryMapping(
        key="repo", project_id="project", iteration_id="iteration",
        repo_url=str((root / "origin.git").resolve()), repo_name="repo",
        allowed_paths=("src",),
    )


def _payload(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "summary": "implemented safely",
        "changed_files": ["src/app.py"],
        "commands": [{"command": "pytest", "exit_code": 0, "summary": "passed"}],
        "evidence": ["tests pass"],
        "review_findings": [],
        "risks": [],
        "unresolved_items": [],
    }
    value.update(updates)
    return value


class FakeRepository:
    def __init__(
        self, *, changed_files: tuple[str, ...] = ("src/app.py",),
        contains_sensitive_content: bool = False,
        changed_by_mapping: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.head_checks = 0
        self.changed_files = changed_files
        self.sensitive_content = contains_sensitive_content
        self.changed_by_mapping = changed_by_mapping

    def assert_head_unchanged(self, prepared: PreparedWorktree) -> None:
        self.head_checks += 1

    def snapshot(self, prepared: PreparedWorktree, mapping: RepositoryMapping) -> RepositorySnapshot:
        changed_files = (
            self.changed_files
            if self.changed_by_mapping is None
            else self.changed_by_mapping[mapping.key]
        )
        return RepositorySnapshot(
            head_commit=OID,
            diff_sha256="b" * 64 if changed_files else EMPTY_HASH,
            changed_files=changed_files,
            patch="diff" if changed_files else "",
            is_clean=not changed_files,
        )

    def contains_sensitive_content(
        self, prepared: PreparedWorktree, mapping: RepositoryMapping,
        secrets: tuple[str, ...],
    ) -> bool:
        return self.sensitive_content


class FakeExecutor:
    def __init__(self, stdout: str | None = None, *, returncode: int = 0, error: Exception | None = None) -> None:
        self.stdout = stdout or json.dumps(_payload())
        self.returncode = returncode
        self.error = error
        self.calls: list[
            tuple[list[str], Path, dict[str, str], float, int, bytes | None]
        ] = []

    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        max_output_bytes: int,
        stdin: bytes | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, cwd, env, timeout, max_output_bytes, stdin))
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, "sensitive stderr")


def _runner(root: Path, executor: FakeExecutor, repository: FakeRepository | None = None) -> CodexRunner:
    return CodexRunner(
        run_root=(root / "runs").resolve(), repository=repository or FakeRepository(),
        command_executor=executor,
    )


def _prepared_group(root: Path) -> tuple[RepositoryGroupMapping, tuple[PreparedRepository, ...]]:
    workspace = root / "workspace"
    workspace.mkdir()
    mappings = (
        RepositoryMapping(
            key="shared-sdk", project_id="project", iteration_id="iteration",
            repo_url="https://example.invalid/shared-sdk.git", repo_name="shared-sdk",
            role=RepositoryRole.DEPENDENCY, allowed_paths=("src",),
        ),
        RepositoryMapping(
            key="desktop-app", project_id="project", iteration_id="iteration",
            repo_url="https://example.invalid/desktop-app.git", repo_name="desktop-app",
            role=RepositoryRole.PRIMARY, depends_on=("shared-sdk",),
            allowed_paths=("src",),
        ),
    )
    prepared: list[PreparedRepository] = []
    for mapping in mappings:
        worktree = workspace / mapping.key
        mirror = root / f"{mapping.key}.git"
        worktree.mkdir()
        mirror.mkdir()
        prepared.append(PreparedRepository(
            repository_key=mapping.key,
            mapping=mapping,
            prepared=PreparedWorktree(
                path=worktree.resolve(), branch=f"bugfix/DEF-1-{mapping.key}",
                base_commit=OID, head_commit=OID, mirror_path=mirror.resolve(),
            ),
        ))
    group = RepositoryGroupMapping(
        key="desktop-suite", project_id="project", iteration_id="iteration",
        primary_repository="desktop-app", repositories=mappings,
    )
    return group, tuple(prepared)


def test_group_run_requires_exact_repository_qualified_claims(tmp_path: Path) -> None:
    group, prepared = _prepared_group(tmp_path)
    repository = FakeRepository(changed_by_mapping={
        "shared-sdk": ("src/shortcut.py",),
        "desktop-app": ("src/window.py",),
    })
    executor = FakeExecutor(json.dumps(_payload(
        changed_files=[],
        repository_changes=[
            {"repository_key": "shared-sdk", "path": "src/shortcut.py"},
            {"repository_key": "desktop-app", "path": "src/window.py"},
        ],
    )))

    result = _runner(tmp_path, executor, repository).run_group(
        group, prepared, run_id="group-run", prompt="fix across repositories"
    )

    assert result.repository_changes == (
        RepositoryChangeClaim(repository_key="shared-sdk", path="src/shortcut.py"),
        RepositoryChangeClaim(repository_key="desktop-app", path="src/window.py"),
    )
    assert executor.calls[0][1] == prepared[0].prepared.path.parent
    assert repository.head_checks == 6


def test_group_run_rejects_unknown_repository_and_claim_drift(tmp_path: Path) -> None:
    group, prepared = _prepared_group(tmp_path)
    repository = FakeRepository(changed_by_mapping={
        "shared-sdk": ("src/shortcut.py",), "desktop-app": (),
    })
    unknown = FakeExecutor(json.dumps(_payload(
        changed_files=[],
        repository_changes=[{"repository_key": "other", "path": "src/x.py"}],
    )))
    with pytest.raises(CodexOutputError, match="invalid structured output"):
        _runner(tmp_path / "unknown", unknown, repository).run_group(
            group, prepared, run_id="unknown", prompt="fix"
        )

    drift = FakeExecutor(json.dumps(_payload(
        changed_files=[],
        repository_changes=[
            {"repository_key": "shared-sdk", "path": "src/different.py"}
        ],
    )))
    with pytest.raises(CodexOutputError, match="invalid structured output"):
        _runner(tmp_path / "drift", drift, repository).run_group(
            group, prepared, run_id="drift", prompt="fix"
        )


def test_run_uses_noninteractive_command_safe_environment_and_persisted_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = _prepared(tmp_path)
    mapping = _mapping(tmp_path)
    executor = FakeExecutor()
    monkeypatch.setenv("ONES_TOKEN", "ones-secret-value")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret-value")
    monkeypatch.setenv("GIT_ASKPASS", "askpass")
    monkeypatch.setenv("CODEX_API_KEY", "codex-auth-value")

    result = _runner(tmp_path, executor).run(
        prepared, mapping, run_id="run-1", prompt="Implement the requested change", timeout_seconds=30,
    )

    command, cwd, env, timeout, _, stdin = executor.calls[0]
    schema = Path(command[command.index("--output-schema") + 1])
    assert command == [
        "codex", "exec", "--cd", str(prepared.path), "--sandbox", "workspace-write",
        "--output-schema", str(schema), "-",
    ]
    assert schema.is_file()
    assert cwd == prepared.path
    assert timeout == 30
    assert stdin == b"Implement the requested change"
    assert "ONES_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert "GIT_ASKPASS" not in env
    assert env["CODEX_API_KEY"] == "codex-auth-value"
    assert "CODEX_HOME" not in env
    assert (tmp_path / "runs" / "run-1" / "codex-prompt.txt").read_text(encoding="utf-8") == "Implement the requested change"
    assert result.summary == "implemented safely"
    assert result.changed_files == ("src/app.py",)


def test_long_prompt_is_streamed_on_stdin_and_never_placed_in_argv(tmp_path: Path) -> None:
    prompt = "完整 Wiki 正文" * 5000
    executor = FakeExecutor(
        json.dumps(_payload(changed_files=[], commands=[]))
    )

    _runner(tmp_path, executor).run_preflight(run_id="long-prompt", prompt=prompt)

    command, _, _, _, _, stdin = executor.calls[0]
    assert command[-1] == "-"
    assert prompt not in command
    assert all(len(argument) < 32768 for argument in command)
    assert stdin == prompt.encode("utf-8")
    assert (tmp_path / "runs" / "long-prompt" / "codex-prompt.txt").read_text(
        encoding="utf-8"
    ) == prompt


def test_run_read_only_phase_uses_read_only_sandbox(tmp_path: Path) -> None:
    executor = FakeExecutor()

    _runner(tmp_path, executor).run(
        _prepared(tmp_path),
        _mapping(tmp_path),
        run_id="run-review",
        prompt="Review only",
        allow_changes=False,
    )

    assert executor.calls[0][0][
        executor.calls[0][0].index("--sandbox") + 1
    ] == "read-only"


def test_preflight_runs_without_worktree_in_read_only_sandbox(tmp_path: Path) -> None:
    executor = FakeExecutor(
        json.dumps(_payload(changed_files=[], commands=[], evidence=["sources checked"]))
    )
    runner = _runner(tmp_path, executor)

    result = runner.run_preflight(
        run_id="preflight-1", prompt="Check sources only", timeout_seconds=30
    )

    command, cwd, _, timeout, _, _ = executor.calls[0]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert cwd == (tmp_path / "runs" / "preflight-1").resolve()
    assert timeout == 30
    assert result.changed_files == ()


def test_preflight_rejects_claimed_repository_changes(tmp_path: Path) -> None:
    runner = _runner(tmp_path, FakeExecutor())

    with pytest.raises(CodexOutputError):
        runner.run_preflight(run_id="preflight-2", prompt="Check sources only")


def test_run_parses_optional_strict_acceptance_coverage_and_review_flag(tmp_path: Path) -> None:
    payload = _payload(
        acceptance_coverage=[
            {
                "criterion_id": "AC-1",
                "criterion_text": "works",
                "files": ["src/app.py"],
                "tests": ["pytest"],
            }
        ],
        unrelated_changes_checked=True,
    )

    result = _runner(tmp_path, FakeExecutor(json.dumps(payload))).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="coverage", prompt="implement"
    )

    assert result.acceptance_coverage[0].criterion_id == "AC-1"
    assert result.unrelated_changes_checked is True


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        json.dumps({key: value for key, value in _payload().items() if key != "summary"}),
        json.dumps(_payload(commit="deadbeef")),
        json.dumps(_payload(commands=[{"command": "pytest", "exit_code": 0}])),
        json.dumps(_payload(changed_files=["../escape.py"])),
        json.dumps(_payload(changed_files=["src//escape.py"])),
        json.dumps(_payload(changed_files=["src/escape.py/"])),
        json.dumps(_payload(changed_files=["docs/outside.md"])),
    ],
)
def test_run_rejects_invalid_or_unsafe_structured_output(tmp_path: Path, stdout: str) -> None:
    with pytest.raises(CodexOutputError, match="invalid structured output"):
        _runner(tmp_path, FakeExecutor(stdout)).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )


def test_run_rejects_claimed_files_that_do_not_match_repository_snapshot(tmp_path: Path) -> None:
    repository = FakeRepository(changed_files=("src/other.py",))
    with pytest.raises(CodexOutputError, match="invalid structured output"):
        _runner(tmp_path, FakeExecutor(), repository).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )


def test_run_rejects_nonzero_exit_without_leaking_stderr(tmp_path: Path) -> None:
    with pytest.raises(CodexExecutionError) as caught:
        _runner(tmp_path, FakeExecutor(returncode=2)).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )
    assert "sensitive stderr" not in str(caught.value)


def test_run_maps_timeout_to_safe_error(tmp_path: Path) -> None:
    with pytest.raises(CodexTimeoutError):
        _runner(tmp_path, FakeExecutor(error=subprocess.TimeoutExpired("codex", 2))).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )


@pytest.mark.parametrize("timeout", [math.nan, math.inf, -math.inf])
def test_run_rejects_non_finite_timeout_before_executor(
    tmp_path: Path, timeout: float,
) -> None:
    executor = FakeExecutor()
    with pytest.raises(UnsafeCodexRunError, match="timeout"):
        _runner(tmp_path, executor).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1",
            prompt="safe prompt", timeout_seconds=timeout,
        )
    assert not executor.calls


@pytest.mark.parametrize("timeout", [math.nan, math.inf, -math.inf, 0.0])
def test_default_executor_rejects_invalid_timeout_before_process_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timeout: float,
) -> None:
    import src.developer_workflow.codex_runner as module

    monkeypatch.setattr(
        module, "_start_isolated_process",
        lambda *args, **kwargs: pytest.fail("invalid timeout started a process"),
    )
    with pytest.raises(ValueError, match="timeout"):
        module._bounded_subprocess(
            [sys.executable, "--version"], cwd=tmp_path, env={},
            timeout=timeout, max_output_bytes=1024,
        )


def test_run_wraps_unexpected_executor_error_without_leaking_details(tmp_path: Path) -> None:
    with pytest.raises(CodexExecutionError) as caught:
        _runner(tmp_path, FakeExecutor(error=RuntimeError("credential-in-error"))).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )
    assert "credential-in-error" not in str(caught.value)


@pytest.mark.parametrize(
    "executor_error",
    [
        subprocess.TimeoutExpired("codex", 1),
        CodexOutputError("output limit"),
        CodexExecutionError("isolation failed"),
        RuntimeError("unexpected"),
    ],
)
def test_repository_boundary_failure_wins_over_executor_failure(
    tmp_path: Path, executor_error: Exception,
) -> None:
    repository = FakeRepository()

    def guard(prepared: PreparedWorktree) -> None:
        repository.head_checks += 1
        if repository.head_checks == 2:
            raise HeadChangedError("changed during failed execution")

    repository.assert_head_unchanged = guard  # type: ignore[method-assign]
    with pytest.raises(HeadChangedError, match="changed during failed execution"):
        _runner(tmp_path, FakeExecutor(error=executor_error), repository).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1",
            prompt="safe prompt",
        )
    assert repository.head_checks == 2


def test_run_checks_repository_identity_and_head_before_and_after(tmp_path: Path) -> None:
    repository = FakeRepository()

    def changed_head(prepared: PreparedWorktree) -> None:
        repository.head_checks += 1
        if repository.head_checks == 2:
            raise HeadChangedError("changed")

    repository.assert_head_unchanged = changed_head  # type: ignore[method-assign]
    with pytest.raises(HeadChangedError):
        _runner(tmp_path, FakeExecutor(), repository).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )
    assert repository.head_checks == 2


@pytest.mark.parametrize("run_id", ["../escape", "bad/name", ".", "run id"])
def test_run_rejects_unsafe_run_id(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(UnsafeCodexRunError):
        _runner(tmp_path, FakeExecutor()).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id=run_id, prompt="safe prompt",
        )


def test_run_rejects_prompt_containing_removed_credential_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONES_TOKEN", "a-real-secret-value")
    with pytest.raises(UnsafeCodexRunError, match="credential"):
        _runner(tmp_path, FakeExecutor()).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1",
            prompt="Use a-real-secret-value while working",
        )


def test_run_removes_all_git_process_control_and_ssh_agent_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executor = FakeExecutor()
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "evil-helper")
    monkeypatch.setenv("SSH_AUTH_SOCK", "agent-socket")
    _runner(tmp_path, executor).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )
    environment = executor.calls[0][2]
    assert not any(
        key in environment
        for key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")
    )
    assert {
        key for key in environment if key.casefold().startswith("git_")
    } == {
        "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_TERMINAL_PROMPT",
    }
    assert "SSH_AUTH_SOCK" not in environment


def test_run_isolates_home_and_git_credentials_from_child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_home = tmp_path / "parent-home"
    parent_appdata = tmp_path / "parent-appdata"
    parent_local = tmp_path / "parent-local"
    parent_home.mkdir()
    parent_appdata.mkdir()
    parent_local.mkdir()
    (parent_home / ".gitconfig").write_text(
        "[credential]\n\thelper = parent-secret-helper\n", encoding="utf-8",
    )
    (parent_home / ".git-credentials").write_text(
        "https://parent-secret@example.invalid\n", encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(parent_home))
    monkeypatch.setenv("USERPROFILE", str(parent_home))
    monkeypatch.setenv("APPDATA", str(parent_appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(parent_local))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(parent_home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(parent_home / ".gitconfig"))
    monkeypatch.setenv("GIT_ASKPASS", "parent-askpass")
    monkeypatch.setenv("SSH_ASKPASS", "parent-ssh-askpass")
    monkeypatch.setenv("SSH_AUTH_SOCK", "parent-agent")

    executor = FakeExecutor()
    _runner(tmp_path, executor).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )
    environment = executor.calls[0][2]
    run_directory = (tmp_path / "runs" / "run-1").resolve()
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        value = Path(environment[name]).resolve()
        assert value != parent_home.resolve()
        assert value != parent_appdata.resolve()
        assert value != parent_local.resolve()
        assert value.is_relative_to(run_directory)
    for name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        config = Path(environment[name]).resolve()
        assert config.is_relative_to(run_directory)
        assert config.read_bytes() == b""
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GCM_INTERACTIVE"] == "Never"
    assert not any(
        name in environment
        for name in ("GIT_ASKPASS", "SSH_ASKPASS", "SSH_AUTH_SOCK")
    )


def test_run_uses_minimal_environment_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executor = FakeExecutor()
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-sensitive-value")
    monkeypatch.setenv("NPM_TOKEN", "npm-sensitive-value")
    monkeypatch.setenv("CI_JOB_TOKEN", "ci-sensitive-value")
    monkeypatch.setenv("RANDOM_PARENT_VALUE", "not-needed")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-auth-value")
    codex_home = (tmp_path / "codex-home").resolve()
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    _runner(tmp_path, executor).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )

    environment = executor.calls[0][2]
    assert environment["OPENAI_API_KEY"] == "openai-auth-value"
    assert environment["CODEX_HOME"] == str((tmp_path / "codex-home").resolve())
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "NPM_TOKEN" not in environment
    assert "CI_JOB_TOKEN" not in environment
    assert "RANDOM_PARENT_VALUE" not in environment


def test_run_maps_default_userprofile_codex_login_without_restoring_user_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_profile = (tmp_path / "parent-profile").resolve()
    parent_home = (tmp_path / "parent-home").resolve()
    codex_home = parent_profile / ".codex"
    codex_home.mkdir(parents=True)
    parent_home.mkdir()
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(parent_profile))
    monkeypatch.setenv("HOME", str(parent_home))
    for name in ("CODEX_HOME", "CODEX_API_KEY", "CODEX_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    executor = FakeExecutor()
    _runner(tmp_path, executor).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )
    environment = executor.calls[0][2]
    assert Path(environment["CODEX_HOME"]) == codex_home
    assert Path(environment["HOME"]).is_relative_to(tmp_path / "runs" / "run-1")
    assert Path(environment["USERPROFILE"]).is_relative_to(
        tmp_path / "runs" / "run-1"
    )
    assert Path(environment["HOME"]) != parent_home
    assert Path(environment["USERPROFILE"]) != parent_profile


def test_run_does_not_invent_codex_home_when_default_login_directory_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_profile = (tmp_path / "empty-profile").resolve()
    parent_profile.mkdir()
    monkeypatch.setenv("USERPROFILE", str(parent_profile))
    monkeypatch.setenv("HOME", str(parent_profile))
    for name in ("CODEX_HOME", "CODEX_API_KEY", "CODEX_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    executor = FakeExecutor()
    _runner(tmp_path, executor).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )
    assert "CODEX_HOME" not in executor.calls[0][2]


def test_explicit_codex_home_takes_priority_over_default_user_login_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_profile = (tmp_path / "parent-profile").resolve()
    default_home = parent_profile / ".codex"
    explicit_home = (tmp_path / "explicit-codex-home").resolve()
    default_home.mkdir(parents=True)
    explicit_home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(parent_profile))
    monkeypatch.setenv("HOME", str(parent_profile))
    monkeypatch.setenv("CODEX_HOME", str(explicit_home))

    executor = FakeExecutor()
    _runner(tmp_path, executor).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )
    assert Path(executor.calls[0][2]["CODEX_HOME"]) == explicit_home


def test_explicit_unsafe_codex_home_is_rejected_without_path_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_path = "relative-parent-secret-codex-home"
    monkeypatch.setenv("CODEX_HOME", secret_path)
    with pytest.raises(UnsafeCodexRunError) as caught:
        _runner(tmp_path, FakeExecutor()).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )
    assert secret_path not in str(caught.value)


def test_reparse_codex_home_is_resolved_without_restoring_parent_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.developer_workflow.codex_runner as module

    codex_home = (tmp_path / "reparse-codex-home").resolve()
    codex_home.mkdir()
    original = module._is_reparse_or_link
    monkeypatch.setattr(
        module, "_is_reparse_or_link",
        lambda path: Path(path) == codex_home or original(path),
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    executor = FakeExecutor()
    _runner(tmp_path, executor).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )
    environment = executor.calls[0][2]
    assert Path(environment["CODEX_HOME"]) == codex_home.resolve()
    assert Path(environment["HOME"]) != codex_home.parent


def test_unreadable_codex_home_is_rejected_without_os_error_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.developer_workflow.codex_runner as module

    codex_home = (tmp_path / "unreadable-codex-home").resolve()
    codex_home.mkdir()
    original_scandir = module.os.scandir

    def guarded_scandir(path: object) -> object:
        if Path(path) == codex_home:
            raise PermissionError("parent-secret-permission-detail")
        return original_scandir(path)

    monkeypatch.setattr(module.os, "scandir", guarded_scandir)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    with pytest.raises(UnsafeCodexRunError) as caught:
        _runner(tmp_path, FakeExecutor()).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )
    assert "parent-secret-permission-detail" not in str(caught.value)


@pytest.mark.parametrize("environment_name", ["HTTPS_PROXY", "OPENAI_BASE_URL"])
def test_auth_url_userinfo_is_removed_and_treated_as_sensitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment_name: str
) -> None:
    executor = FakeExecutor()
    monkeypatch.setenv(environment_name, "https://user:proxy-secret-value@proxy.local:8443")
    with pytest.raises(UnsafeCodexRunError, match="credential"):
        _runner(tmp_path, executor).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1",
            prompt="Do not copy proxy-secret-value",
        )
    assert not executor.calls


def test_prompt_and_output_cannot_echo_retained_codex_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_API_KEY", "codex-sensitive-value")
    with pytest.raises(UnsafeCodexRunError, match="credential"):
        _runner(tmp_path, FakeExecutor()).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1",
            prompt="Never copy codex-sensitive-value",
        )
    executor = FakeExecutor(json.dumps(_payload(summary="codex-sensitive-value")))
    with pytest.raises(CodexOutputError, match="invalid structured output"):
        _runner(tmp_path, executor).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-2", prompt="safe prompt",
        )


def test_repository_snapshot_cannot_contain_parent_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-file-secret-value")
    repository = FakeRepository()

    def sensitive_snapshot(prepared: PreparedWorktree, mapping: RepositoryMapping) -> RepositorySnapshot:
        return RepositorySnapshot(
            head_commit=OID,
            diff_sha256="b" * 64,
            changed_files=("src/app.py",),
            patch="+aws-file-secret-value",
            is_clean=False,
        )

    repository.snapshot = sensitive_snapshot  # type: ignore[method-assign]
    executor = FakeExecutor(json.dumps(_payload(changed_files=["src/app.py"])))
    with pytest.raises(CodexOutputError, match="invalid structured output") as caught:
        _runner(tmp_path, executor, repository).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )
    assert "aws-file-secret-value" not in str(caught.value)


def test_changed_file_content_cannot_contain_parent_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_API_KEY", "codex-auth-written-to-file")
    repository = FakeRepository(contains_sensitive_content=True)
    with pytest.raises(CodexOutputError, match="invalid structured output") as caught:
        _runner(tmp_path, FakeExecutor(), repository).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )
    assert "codex-auth-written-to-file" not in str(caught.value)
    assert repository.head_checks == 3

def test_default_executor_enforces_timeout_and_output_limit(tmp_path: Path) -> None:
    from src.developer_workflow.codex_runner import _bounded_subprocess

    with pytest.raises(subprocess.TimeoutExpired):
        _bounded_subprocess(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=tmp_path, env=dict(os.environ), timeout=0.05, max_output_bytes=1024,
        )
    with pytest.raises(CodexOutputError, match="output limit"):
        _bounded_subprocess(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000)"],
            cwd=tmp_path, env=dict(os.environ), timeout=5, max_output_bytes=1024,
        )


def test_default_executor_streams_stdin_larger_than_windows_command_line_limit(
    tmp_path: Path,
) -> None:
    from src.developer_workflow.codex_runner import _bounded_subprocess

    content = ("完整 Wiki 正文" * 5000).encode("utf-8")
    completed = _bounded_subprocess(
        [
            sys.executable,
            "-c",
            "import sys; data=sys.stdin.buffer.read(); print(len(data))",
        ],
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=10,
        max_output_bytes=1024,
        stdin=content,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == str(len(content))


def test_default_executor_timeout_kills_child_blocking_stdin_writer(tmp_path: Path) -> None:
    from src.developer_workflow.codex_runner import _bounded_subprocess

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _bounded_subprocess(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=tmp_path,
            env=dict(os.environ),
            timeout=0.05,
            max_output_bytes=1024,
            stdin=b"x" * (10 * 1024 * 1024),
        )

    assert time.monotonic() - started < 3


def test_default_executor_reaps_descendant_holding_output_pipe(tmp_path: Path) -> None:
    from src.developer_workflow.codex_runner import _bounded_subprocess

    child = "import time; time.sleep(10)"
    parent = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "sys.stdout.write('done'); sys.stdout.flush()"
    )
    started = time.monotonic()
    completed = _bounded_subprocess(
        [sys.executable, "-c", parent], cwd=tmp_path, env=dict(os.environ),
        timeout=5, max_output_bytes=1024,
    )
    elapsed = time.monotonic() - started
    assert completed.returncode == 0
    assert completed.stdout == "done"
    assert elapsed < 2


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended launcher invariant")
def test_windows_launcher_assigns_job_before_resuming_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.developer_workflow.codex_runner as module

    events: list[str] = []

    class FakeProcess:
        _handle = 42

    process = FakeProcess()

    def fake_popen(*args: object, **kwargs: object) -> FakeProcess:
        flags = int(kwargs["creationflags"])
        assert flags & 0x00000004  # CREATE_SUSPENDED
        events.append("spawn-suspended")
        return process

    class FakeGuard:
        def __init__(self, actual: object) -> None:
            assert actual is process
            events.append("assigned-job")

    def fake_resume(actual: object) -> None:
        assert actual is process
        assert events == ["spawn-suspended", "assigned-job"]
        events.append("resumed")

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(module, "_ProcessTreeGuard", FakeGuard)
    monkeypatch.setattr(module, "_resume_suspended_process", fake_resume)

    actual_process, guard = module._start_isolated_process(
        ["codex", "exec"], cwd=tmp_path, env={"PATH": os.environ.get("PATH", "")},
    )
    assert actual_process is process
    assert isinstance(guard, FakeGuard)
    assert events == ["spawn-suspended", "assigned-job", "resumed"]


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended launcher invariant")
def test_windows_launcher_failure_kills_suspended_process_before_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.developer_workflow.codex_runner as module

    events: list[str] = []

    class FakeProcess:
        _handle = 42

        def kill(self) -> None:
            events.append("kill-suspended")

        def wait(self, timeout: float) -> int:
            events.append("wait")
            return 1

    process = FakeProcess()

    def fake_popen(*args: object, **kwargs: object) -> FakeProcess:
        assert int(kwargs["creationflags"]) & 0x00000004
        events.append("spawn-suspended")
        return process

    class FailingGuard:
        def __init__(self, actual: object) -> None:
            events.append("job-failed")
            raise OSError("job unavailable")

    def must_not_resume(actual: object) -> None:
        raise AssertionError("an unassigned process must never resume")

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(module, "_ProcessTreeGuard", FailingGuard)
    monkeypatch.setattr(module, "_resume_suspended_process", must_not_resume)

    with pytest.raises(CodexExecutionError, match="isolated"):
        module._start_isolated_process(
            ["codex", "exec"], cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
        )
    assert events == ["spawn-suspended", "job-failed", "kill-suspended", "wait"]


def test_run_rejects_snapshot_head_race_and_guards_after_snapshot(tmp_path: Path) -> None:
    class RacingRepository(FakeRepository):
        def snapshot(self, prepared: PreparedWorktree, mapping: RepositoryMapping) -> RepositorySnapshot:
            return RepositorySnapshot(
                head_commit="b" * 40, diff_sha256="b" * 64,
                changed_files=("src/app.py",), patch="diff", is_clean=False,
            )

    repository = RacingRepository()
    with pytest.raises(HeadChangedError):
        _runner(tmp_path, FakeExecutor(), repository).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )
    assert repository.head_checks == 2


def test_run_performs_full_repository_guard_after_snapshot(tmp_path: Path) -> None:
    repository = FakeRepository()

    def guard(prepared: PreparedWorktree) -> None:
        repository.head_checks += 1
        if repository.head_checks == 3:
            raise HeadChangedError("raced after snapshot")

    repository.assert_head_unchanged = guard  # type: ignore[method-assign]
    with pytest.raises(HeadChangedError):
        _runner(tmp_path, FakeExecutor(), repository).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )
    assert repository.head_checks == 3


def test_run_rejects_snapshot_content_race_after_sensitive_file_scan(
    tmp_path: Path,
) -> None:
    class RacingRepository(FakeRepository):
        def __init__(self) -> None:
            super().__init__()
            self.snapshot_calls = 0

        def snapshot(
            self, prepared: PreparedWorktree, mapping: RepositoryMapping,
        ) -> RepositorySnapshot:
            self.snapshot_calls += 1
            return RepositorySnapshot(
                head_commit=OID,
                diff_sha256=("b" if self.snapshot_calls == 1 else "c") * 64,
                changed_files=("src/app.py",), patch="diff", is_clean=False,
            )

    repository = RacingRepository()
    with pytest.raises(CodexOutputError, match="invalid structured output"):
        _runner(tmp_path, FakeExecutor(), repository).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1",
            prompt="safe prompt",
        )
    assert repository.snapshot_calls == 2
    assert repository.head_checks == 3


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m generated",
        "git -c user.name=AI push origin branch",
        "gh pr create --title generated",
        "gh --repo org/repo pr create --title generated",
        "curl -X PATCH http://aputureones.local/api/tasks/1",
        "curl --request=DELETE http://aputureones.local/api/tasks/1",
        "curl https://aputureones.local/api/tasks -d '{\"status\":\"done\"}'",
        "curl --form file=@report.txt https://example.invalid/upload",
        "curl -dstatus=done https://aputureones.local/api/tasks",
        "curl -Ffile=@report.txt https://example.invalid/upload",
        "curl -Treport.txt https://example.invalid/upload",
        "gh api --method PATCH repos/org/repo/pulls/1",
        "gh api repos/org/repo/pulls -f title=generated",
        "git send-pack origin refs/heads/main",
        "git-remote-http origin https://example.invalid/repo.git",
        "python ones_client.py --method PATCH --url http://aputureones.local/task/1",
        "ones --method patch task task-1",
        "ones task update task-1",
    ],
)
def test_run_rejects_self_reported_publication_commands(tmp_path: Path, command: str) -> None:
    executor = FakeExecutor(json.dumps(_payload(commands=[{
        "command": command, "exit_code": 0, "summary": "done",
    }])))
    with pytest.raises(CodexOutputError, match="invalid structured output"):
        _runner(tmp_path, executor).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )


def test_run_accepts_read_only_and_verification_commands(tmp_path: Path) -> None:
    commands = [
        {"command": "git diff --check", "exit_code": 0, "summary": "clean"},
        {"command": "git remote -v", "exit_code": 0, "summary": "listed"},
        {"command": "git diff -- src/remote/client.py", "exit_code": 0, "summary": "read"},
        {"command": "git diff -- 'src/remote client.py' | rg update", "exit_code": 0, "summary": "read"},
        {"command": "'C:/Program Files/Git/bin/git.exe' log --grep='commit update'", "exit_code": 0, "summary": "read"},
        {"command": "git log --grep=commit", "exit_code": 0, "summary": "read"},
        {"command": "gh pr view 12", "exit_code": 0, "summary": "read"},
        {"command": "gh api --method GET repos/org/repo", "exit_code": 0, "summary": "read"},
        {"command": "curl -fsS https://example.invalid/status", "exit_code": 0, "summary": "read"},
        {"command": "ones task get task-1", "exit_code": 0, "summary": "read"},
        {"command": "bash -lc 'git status && pytest -q'", "exit_code": 0, "summary": "read"},
        {"command": "( git status )", "exit_code": 0, "summary": "read"},
        {"command": "timeout 10 pytest -q", "exit_code": 0, "summary": "passed"},
        {"command": "python -m pytest -q", "exit_code": 0, "summary": "passed"},
        {"command": "pytest -q", "exit_code": 0, "summary": "passed"},
        {"command": "pytest -k update", "exit_code": 0, "summary": "passed"},
        {"command": "rg update src", "exit_code": 0, "summary": "searched"},
        {"command": "rg 'update; commit' src", "exit_code": 0, "summary": "searched"},
    ]
    result = _runner(tmp_path, FakeExecutor(json.dumps(_payload(commands=commands)))).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )
    assert tuple(item.command for item in result.commands) == tuple(
        item["command"] for item in commands
    )


@pytest.mark.parametrize(
    "command",
    [
        "git diff --check && git push origin branch",
        "git remote -v; gh api --method DELETE repos/org/repo/git/refs/heads/x",
        "sh -c 'git -C repo -c user.name=AI commit -m generated'",
        "cmd /c git --git-dir repo/.git push origin branch",
        "powershell -Command \"curl -dstatus=done https://aputureones.local/api/tasks\"",
        "eval 'ones task update task-1'",
        "sudo git push origin branch",
        "sudo -u bot git commit -m generated",
        "command -- git push origin branch",
        "nohup git send-pack origin refs/heads/main",
        "nice -n 5 git push origin branch",
        "git push 'unterminated",
        "git -c alias.ship=push ship origin branch",
        "git --config-env alias.ship=GIT_ALIAS ship origin branch",
        "git $ACTION origin branch",
        "curl $CURL_ARGS https://aputureones.local/api/tasks",
        "gh $SUBCOMMAND create",
        "bash -lc 'git push origin branch'",
        "( git push origin branch )",
        "{ gh pr comment 1 --body generated; }",
        "timeout 10 git push origin branch",
        "python -c \"import subprocess; subprocess.run(['git','push'])\"",
        "node -e \"require('child_process').execSync('git push')\"",
        "gh repo create generated --private",
        "gh pr comment 1 --body generated",
        "curl --config request.conf https://example.invalid/status",
        "git add src/app.py",
        "if true; then git push origin branch; fi",
        "while false; do gh pr comment 1 --body generated; done",
        "powershell -EncodedCommand Z2ggcHIgY3JlYXRl",
        "powershell -File publish.ps1",
        "python tools/publish.py",
        "node tools/publish.js",
        "http https://aputureones.local/api/tasks status=done",
        "ones task synchronize task-1",
        "make publish",
    ],
)
def test_run_rejects_write_command_hidden_by_shell_or_global_options(
    tmp_path: Path, command: str
) -> None:
    executor = FakeExecutor(json.dumps(_payload(commands=[{
        "command": command, "exit_code": 0, "summary": "done",
    }])))
    with pytest.raises(CodexOutputError, match="invalid structured output"):
        _runner(tmp_path, executor).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
        )


@pytest.mark.parametrize(
    "command",
    [
        "python -m unittest -q",
        "python -m compileall -q src",
        "uv run pytest -q",
        "make test",
        "ruff check src",
    ],
)
def test_run_accepts_explicit_local_test_and_read_only_commands(
    tmp_path: Path, command: str,
) -> None:
    result = _runner(
        tmp_path,
        FakeExecutor(json.dumps(_payload(commands=[{
            "command": command, "exit_code": 0, "summary": "passed",
        }]))),
    ).run(
        _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe prompt",
    )
    assert result.commands[0].command == command


def test_run_rejects_oversized_prompt_and_output(tmp_path: Path) -> None:
    runner = CodexRunner(
        run_root=(tmp_path / "runs").resolve(), repository=FakeRepository(),
        command_executor=FakeExecutor(), max_prompt_bytes=8,
    )
    with pytest.raises(UnsafeCodexRunError, match="prompt"):
        runner.run(_prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="too long prompt")

    executor = FakeExecutor(json.dumps(_payload(summary="x" * 500)))
    runner = CodexRunner(
        run_root=(tmp_path / "other-runs").resolve(), repository=FakeRepository(),
        command_executor=executor, max_output_bytes=100,
    )
    with pytest.raises(CodexOutputError, match="output limit"):
        runner.run(_prepared(tmp_path), _mapping(tmp_path), run_id="run-2", prompt="safe")


def test_existing_symlinked_run_directory_is_rejected(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("creating a symlink requires privileges on Windows")
    outside = tmp_path / "outside"
    outside.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "run-1").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafeCodexRunError):
        _runner(tmp_path, FakeExecutor()).run(
            _prepared(tmp_path), _mapping(tmp_path), run_id="run-1", prompt="safe",
        )
