from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import sys
import time
import traceback
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.developer_workflow.codex_runner as codex_runner_module
from src.developer_workflow.codex_runner import (
    CodexExecutionError,
    CodexOutputError,
    CodexRunner,
    CodexTimeoutError,
    UnsafeCodexRunError,
    validate_codex_auth_source,
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


def _which_from(mapping: dict[str, str | None]):
    return lambda name: mapping.get(name)


def _canonical_test_validator(path: Path) -> Path:
    if "missing" in str(path):
        raise FileNotFoundError
    return path.resolve(strict=False)


def test_codex_command_keeps_an_immutable_validated_prefix() -> None:
    command = codex_runner_module.CodexCommand(("C:\\Tools\\node.exe", "C:\\Tools\\codex.js"))

    assert command.argv("exec", "--version") == [
        "C:\\Tools\\node.exe", "C:\\Tools\\codex.js", "exec", "--version",
    ]
    assert command.prefix == ("C:\\Tools\\node.exe", "C:\\Tools\\codex.js")
    with pytest.raises(FrozenInstanceError):
        command.prefix = ("replacement",)  # type: ignore[misc]


@pytest.mark.parametrize("prefix", [(), ("",), ("safe", "bad\x00component")])
def test_codex_command_rejects_empty_or_nul_prefix(prefix: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        codex_runner_module.CodexCommand(prefix)


@pytest.mark.parametrize("argument", ["", "bad\x00argument"])
def test_codex_command_rejects_empty_or_nul_arguments(argument: str) -> None:
    command = codex_runner_module.CodexCommand(("codex",))
    with pytest.raises(ValueError):
        command.argv(argument)


def test_windows_resolver_prefers_a_validated_direct_executable() -> None:
    executable = str(Path("C:/Program Files/OpenAI/codex.exe"))
    command = codex_runner_module.resolve_codex_command(
        which=_which_from({"codex.exe": executable, "codex.cmd": "C:/npm/codex.cmd"}),
        platform="win32",
        path_validator=_canonical_test_validator,
    )

    assert command.prefix == (str(Path(executable).resolve(strict=False)),)


def test_windows_resolver_uses_only_node_and_js_from_the_standard_npm_layout() -> None:
    shim = Path("C:/nvm4w/nodejs/codex.cmd")
    validated: list[Path] = []

    def validator(path: Path) -> Path:
        validated.append(path)
        return path.resolve(strict=False)

    command = codex_runner_module.resolve_codex_command(
        which=_which_from({"codex.exe": None, "codex.cmd": str(shim)}),
        platform="win32",
        path_validator=validator,
    )

    root = shim.resolve(strict=False).parent
    assert command.prefix == (
        str(root / "node.exe"),
        str(root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"),
    )
    assert validated == [shim, root / "node.exe", root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"]
    assert all(not item.casefold().endswith((".cmd", ".ps1")) for item in command.prefix)


def test_windows_resolver_falls_back_from_an_unsafe_exe_to_safe_npm_layout() -> None:
    shim = Path("C:/nvm4w/nodejs/codex.cmd")

    def validator(path: Path) -> Path:
        if path.name.casefold() == "codex.exe":
            raise PermissionError("unsafe executable")
        return path.resolve(strict=False)

    command = codex_runner_module.resolve_codex_command(
        which=_which_from({"codex.exe": "C:/unsafe/codex.exe", "codex.cmd": str(shim)}),
        platform="win32",
        path_validator=validator,
    )

    assert command.prefix[0].casefold().endswith("node.exe")
    assert command.prefix[1].casefold().endswith("codex.js")


@pytest.mark.parametrize("missing_name", ["node.exe", "codex.js"])
def test_windows_resolver_rejects_incomplete_npm_layout(missing_name: str) -> None:
    shim = Path("C:/nvm4w/nodejs/codex.cmd")

    def validator(path: Path) -> Path:
        if path.name.casefold() == missing_name:
            raise FileNotFoundError("layout incomplete")
        return path.resolve(strict=False)

    with pytest.raises(
        codex_runner_module.CodexProcessStartError,
        match="^Codex executable is unavailable$",
    ):
        codex_runner_module.resolve_codex_command(
            which=_which_from({"codex.exe": None, "codex.cmd": str(shim)}),
            platform="win32",
            path_validator=validator,
        )


@pytest.mark.parametrize(
    ("query", "replacement"),
    [
        ("codex.exe", "C:/unsafe/codex.ps1"),
        ("codex.exe", "C:/unsafe/codex"),
        ("node.exe", "C:/unsafe/codex.cmd"),
        ("codex.js", "C:/unsafe/codex.ps1"),
    ],
)
def test_windows_resolver_never_returns_a_shell_shim_prefix(
    query: str, replacement: str,
) -> None:
    shim = Path("C:/nvm4w/nodejs/codex.cmd")

    def validator(path: Path) -> Path:
        if path.name.casefold() == query:
            return Path(replacement).resolve(strict=False)
        return path.resolve(strict=False)

    try:
        command = codex_runner_module.resolve_codex_command(
            which=_which_from({"codex.exe": "C:/unsafe/codex.exe", "codex.cmd": str(shim)}),
            platform="win32",
            path_validator=validator,
        )
    except codex_runner_module.CodexProcessStartError:
        return
    assert all(
        not component.casefold().endswith((".cmd", ".ps1"))
        and Path(component).suffix.casefold() in {".exe", ".js"}
        for component in command.prefix
    )


def test_posix_resolver_requires_a_validated_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    executable = Path("/opt/openai/bin/codex")
    monkeypatch.setattr(codex_runner_module.os, "access", lambda path, mode: path == executable.resolve())

    command = codex_runner_module.resolve_codex_command(
        which=_which_from({"codex": str(executable)}),
        platform="linux",
        path_validator=_canonical_test_validator,
    )

    assert command.prefix == (str(executable.resolve()),)


def test_posix_resolver_rejects_a_non_executable_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_runner_module.os, "access", lambda path, mode: False)
    with pytest.raises(codex_runner_module.CodexProcessStartError):
        codex_runner_module.resolve_codex_command(
            which=_which_from({"codex": "/opt/openai/bin/codex"}),
            platform="linux",
            path_validator=_canonical_test_validator,
        )


def test_codex_component_rejects_non_regular_file(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        codex_runner_module._validate_codex_component(tmp_path)


def test_codex_component_rejects_nul_path() -> None:
    with pytest.raises((OSError, ValueError)):
        codex_runner_module._validate_codex_component(Path("bad\x00canary"))


def test_codex_component_rejects_current_worktree_file() -> None:
    with pytest.raises(OSError):
        codex_runner_module._validate_codex_component(Path(__file__).resolve())


def test_codex_component_rejects_reparse_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "codex.exe"
    candidate.write_bytes(b"binary")
    real_lstat = Path.lstat

    def reparse_lstat(path: Path):
        metadata = real_lstat(path)
        if path == candidate:
            return SimpleNamespace(
                st_dev=metadata.st_dev, st_ino=metadata.st_ino,
                st_size=metadata.st_size, st_mtime_ns=metadata.st_mtime_ns,
                st_mode=metadata.st_mode, st_file_attributes=0x400,
            )
        return metadata

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    with pytest.raises(OSError):
        codex_runner_module._validate_codex_component(candidate)


def test_codex_component_rejects_identity_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "codex.exe"
    candidate.write_bytes(b"binary")
    real_lstat = Path.lstat
    calls = 0

    def racing_lstat(path: Path):
        nonlocal calls
        metadata = real_lstat(path)
        if path == candidate:
            calls += 1
            if calls >= 2:
                return SimpleNamespace(
                    st_dev=metadata.st_dev, st_ino=metadata.st_ino,
                    st_size=metadata.st_size + 1, st_mtime_ns=metadata.st_mtime_ns,
                    st_mode=metadata.st_mode, st_file_attributes=0,
                )
        return metadata

    monkeypatch.setattr(Path, "lstat", racing_lstat)
    with pytest.raises(OSError):
        codex_runner_module._validate_codex_component(candidate)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL boundary")
def test_codex_component_rejects_unlisted_windows_acl_principal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "codex.exe"
    candidate.write_bytes(b"binary")
    monkeypatch.setattr(codex_runner_module, "_current_user_sid", lambda: "S-1-5-21-user")
    monkeypatch.setattr(
        codex_runner_module,
        "_windows_descriptor",
        lambda path: (
            "S-1-5-21-user",
            (("S-1-5-21-unlisted", 0x001F01FF, 0, 0),),
            True,
        ),
    )
    with pytest.raises(OSError):
        codex_runner_module._validate_codex_component(candidate)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL boundary")
def test_codex_component_rejects_trusted_leaf_beneath_broadly_writable_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = (tmp_path / "bin" / "codex.exe").resolve()
    candidate.parent.mkdir()
    candidate.write_bytes(b"binary")
    user_sid = "S-1-5-21-user"
    monkeypatch.setattr(codex_runner_module, "_current_user_sid", lambda: user_sid)

    def descriptor(path: Path):
        if path == candidate.parent:
            return (
                user_sid,
                (("S-1-5-11", 0x001301BF, 0, 0),),
                True,
            )
        return user_sid, ((user_sid, 0x001F01FF, 0, 0),), True

    monkeypatch.setattr(codex_runner_module, "_windows_descriptor", descriptor)

    with pytest.raises(OSError):
        codex_runner_module._validate_codex_component(candidate)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL boundary")
def test_codex_component_accepts_fixed_trustedinstaller_and_read_only_principals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = (tmp_path / "codex.exe").resolve()
    candidate.write_bytes(b"binary")
    trusted_installer = (
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
    )
    monkeypatch.setattr(codex_runner_module, "_current_user_sid", lambda: "S-1-5-21-user")
    monkeypatch.setattr(
        codex_runner_module,
        "_windows_descriptor",
        lambda path: (
            trusted_installer,
            (
                (trusted_installer, 0x001F01FF, 0, 0),
                ("S-1-15-3-package-capability", 0x001200A9, 0, 0),
            ),
            True,
        ),
    )

    assert codex_runner_module._validate_codex_component(candidate) == candidate


def test_resolver_sanitizes_all_ordinary_discovery_and_validation_failures() -> None:
    canary = "candidate-canary"
    calls = 0

    def validator(path: Path) -> Path:
        nonlocal calls
        calls += 1
        raise RuntimeError(f"unsafe path {canary}: {path}")

    with pytest.raises(codex_runner_module.CodexProcessStartError) as caught:
        codex_runner_module.resolve_codex_command(
            which=_which_from({
                "codex.exe": f"C:/sensitive/{canary}/codex.exe",
                "codex.cmd": f"C:/sensitive/{canary}/codex.cmd",
            }),
            platform="win32",
            path_validator=validator,
        )

    assert calls == 2
    assert str(caught.value) == "Codex executable is unavailable"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    rendered = "".join(traceback.format_exception(caught.value))
    assert canary not in rendered


@pytest.mark.parametrize(
    "control_flow",
    [
        MemoryError(), KeyboardInterrupt(), SystemExit(), GeneratorExit(),
        asyncio.CancelledError(),
    ],
)
def test_resolver_preserves_memory_and_control_flow(control_flow: BaseException) -> None:
    def validator(path: Path) -> Path:
        raise control_flow

    with pytest.raises(type(control_flow)):
        codex_runner_module.resolve_codex_command(
            which=_which_from({"codex.exe": "C:/safe/codex.exe"}),
            platform="win32",
            path_validator=validator,
        )


_REAL_NPM_CODEX = Path(r"C:\nvm4w\nodejs\codex.cmd")
_REAL_NPM_NODE = Path(r"C:\nvm4w\nodejs\node.exe")
_REAL_NPM_ENTRY = Path(r"C:\nvm4w\nodejs\node_modules\@openai\codex\bin\codex.js")


@pytest.mark.skipif(
    os.name != "nt" or not all(
        path.is_file() for path in (_REAL_NPM_CODEX, _REAL_NPM_NODE, _REAL_NPM_ENTRY)
    ),
    reason="specific C:\\nvm4w\\nodejs npm Codex layout is unavailable",
)
def test_real_windows_standard_npm_layout_is_rejected_when_broadly_writable() -> None:
    with pytest.raises(
        codex_runner_module.CodexProcessStartError,
        match="^Codex executable is unavailable$",
    ):
        codex_runner_module.resolve_codex_command(
            which=_which_from({"codex.exe": None, "codex.cmd": str(_REAL_NPM_CODEX)}),
            platform="win32",
        )


def test_codex_auth_source_accepts_environment_auth_without_returning_secret() -> None:
    assert validate_codex_auth_source(
        {"CODEX_API_KEY": "runtime-only-codex-auth"}
    ) is None


def test_codex_auth_source_requires_a_regular_auth_file_in_codex_home(
    tmp_path: Path,
) -> None:
    codex_home = (tmp_path / "codex-home").resolve()
    codex_home.mkdir()

    with pytest.raises(UnsafeCodexRunError, match="authentication source"):
        validate_codex_auth_source({"CODEX_HOME": str(codex_home)})

    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    assert validate_codex_auth_source({"CODEX_HOME": str(codex_home)}) == codex_home


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
        acceptance_coverage=[{
            "criterion_id": "AC-1",
            "criterion_text": "window recreation remains safe",
            "files": [],
            "repository_files": [
                {"repository_key": "shared-sdk", "path": "src/shortcut.py"},
                {"repository_key": "desktop-app", "path": "src/window.py"},
            ],
            "tests": ["pytest"],
        }],
    )))

    result = _runner(tmp_path, executor, repository).run_group(
        group, prepared, run_id="group-run", prompt="fix across repositories"
    )

    assert result.repository_changes == (
        RepositoryChangeClaim(repository_key="shared-sdk", path="src/shortcut.py"),
        RepositoryChangeClaim(repository_key="desktop-app", path="src/window.py"),
    )
    assert result.acceptance_coverage[0].files == ()
    assert len(result.acceptance_coverage[0].repository_files) == 2
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


def test_run_parses_repository_qualified_root_cause_evidence(tmp_path: Path) -> None:
    root = {
        "file_path": "src/app.py",
        "repository_file": {"repository_key": "repo", "path": "src/app.py"},
        "location": "app:1",
        "start_line": 1,
        "end_line": 1,
        "symbol": "run",
        "mechanism": "invalid lifecycle",
        "code_excerpt": "run()",
        "call_chain": [],
        "reproduction_test": "tests/test_app.py",
        "reproduction_file": {
            "repository_key": "repo", "path": "tests/test_app.py"
        },
        "test_selector": "tests/test_app.py::test_lifecycle",
        "reproduction_command": "pytest",
        "confidence": 0.9,
        "insufficient_evidence": False,
        "impacted_files": ["src/app.py"],
        "impacted_repository_files": [
            {"repository_key": "repo", "path": "src/app.py"}
        ],
        "fix_steps": ["guard lifecycle"],
        "supporting_points": [{
            "kind": "code",
            "description": "unsafe call",
            "source": "repo",
            "file_path": "src/app.py",
            "repository_file": {"repository_key": "repo", "path": "src/app.py"},
            "snippet": "run()",
            "start_line": 1,
            "end_line": 1,
            "direct_root_cause": True,
        }],
    }
    result = _runner(
        tmp_path,
        FakeExecutor(json.dumps(_payload(root_cause_evidence=[root]))),
    ).run(_prepared(tmp_path), _mapping(tmp_path), run_id="root", prompt="analyze")

    assert result.root_cause_evidence[0].repository_file is not None
    assert result.root_cause_evidence[0].reproduction_file is not None


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


def test_process_start_file_not_found_uses_structured_sanitized_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.developer_workflow.codex_runner as module
    from src.developer_workflow.codex_runner import CodexProcessStartError

    canary = "SECRET-CODEX-EXECUTABLE-PATH"
    cause = FileNotFoundError(canary)
    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(cause),
    )

    with pytest.raises(CodexProcessStartError) as raised:
        module._start_isolated_process(["codex"], cwd=tmp_path, env={"PATH": ""})

    assert isinstance(raised.value, CodexExecutionError)
    assert str(raised.value) == "Codex process could not be started"
    assert raised.value.__cause__ is cause
    assert canary not in str(raised.value)


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
