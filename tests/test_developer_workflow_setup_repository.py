from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from src.developer_workflow.contracts import RepositoryMapping, RepositoryRole
from src.developer_workflow.setup_models import SetupValidationError
from src.developer_workflow.setup_repository import (
    RepositoryGroupDraftBuilder,
    build_repository,
)
from src.developer_workflow.setup_validation import ReadOnlyRepositoryInspector


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )
    return result.stdout.strip()


def _source_and_remote(root: Path, name: str = "app") -> tuple[Path, Path]:
    source = root / name
    remote = root / f"{name}.git"
    source.mkdir()
    _git("init", "-b", "main", cwd=source)
    _git("config", "user.name", "Setup Test", cwd=source)
    _git("config", "user.email", "setup@example.invalid", cwd=source)
    (source / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=source)
    _git("commit", "-m", "initial", cwd=source)
    _git("clone", "--bare", str(source), str(remote), cwd=root)
    _git("remote", "add", "origin", str(remote.resolve()), cwd=source)
    return source.resolve(), remote.resolve()


def _repo(key: str, *, depends_on: tuple[str, ...] = ()) -> RepositoryMapping:
    return RepositoryMapping(
        key=key, project_id="project", iteration_id="iteration",
        repo_url=f"https://example.invalid/{key}.git", repo_name=key,
        depends_on=depends_on, test_commands=(f"pytest tests/{key}",),
        allowed_paths=("src", "tests"),
    )


def test_build_repository_returns_contract_and_preserves_source(tmp_path: Path) -> None:
    source, remote = _source_and_remote(tmp_path)
    before = (
        _git("rev-parse", "HEAD", cwd=source),
        _git("status", "--porcelain=v1", "--untracked-files=all", cwd=source),
        (source / ".git" / "index").read_bytes(),
        (source / ".git" / "config").read_bytes(),
    )

    result = build_repository(
        key="app", project_id="project", iteration_id="iteration",
        repo_url=str(remote), repo_name="app", source_path=source,
        base_branch="main", role=RepositoryRole.PRIMARY, depends_on=(),
        allowed_paths=("src", "tests"), lint_commands=("ruff check src",),
        build_commands=("python -m build",), test_commands=("python -u -m pytest",),
    )

    assert type(result) is RepositoryMapping
    assert result.source_path == source
    after = (
        _git("rev-parse", "HEAD", cwd=source),
        _git("status", "--porcelain=v1", "--untracked-files=all", cwd=source),
        (source / ".git" / "index").read_bytes(),
        (source / ".git" / "config").read_bytes(),
    )
    assert after == before


@pytest.mark.parametrize(
    "command",
    (
        "curl -u alice:TOKEN-SECRET https://example.invalid",
        "curl --netrc https://example.invalid",
        "curl --cert client.pem https://example.invalid",
        "curl --key client.key https://example.invalid",
        "uv run curl -ualice https://example.invalid",
        "env API_TOKEN=secret pytest",
        "uv run env PASSWORD=secret pytest",
    ),
)
def test_secret_bearing_commands_are_rejected_without_echo(command: str) -> None:
    with pytest.raises(SetupValidationError) as captured:
        build_repository(
            key="app", project_id="project", iteration_id="iteration",
            repo_url="https://example.invalid/app.git", repo_name="app",
            test_commands=(command,),
        )
    assert "secret" not in str(captured.value).casefold()
    assert "token" not in str(captured.value).casefold()


def test_repository_rejects_unsafe_paths_and_duplicate_argv() -> None:
    for paths in (("src", "src/api"), ("src", "src"), ("../src",), ("src\\api",)):
        with pytest.raises(SetupValidationError, match="repository draft is invalid"):
            build_repository(
                key="app", project_id="project", iteration_id="iteration",
                repo_url="https://example.invalid/app.git", repo_name="app",
                allowed_paths=paths,
            )
    with pytest.raises(SetupValidationError, match="repository draft is invalid"):
        build_repository(
            key="app", project_id="project", iteration_id="iteration",
            repo_url="https://example.invalid/app.git", repo_name="app",
            lint_commands=("pytest",), test_commands=('"pytest"',),
        )


def test_repository_url_must_match_local_origin(tmp_path: Path) -> None:
    source, _ = _source_and_remote(tmp_path)
    with pytest.raises(SetupValidationError, match="repository source is invalid"):
        build_repository(
            key="app", project_id="project", iteration_id="iteration",
            repo_url=str((tmp_path / "other.git").resolve()), repo_name="app",
            source_path=source,
        )


def test_source_symlink_is_rejected_without_mutating_target(tmp_path: Path) -> None:
    source, remote = _source_and_remote(tmp_path)
    link = tmp_path / "source-link"
    try:
        link.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    before = (source / "tracked.txt").read_bytes()
    try:
        with pytest.raises(SetupValidationError, match="repository source is invalid"):
            build_repository(
                key="app", project_id="project", iteration_id="iteration",
                repo_url=str(remote), repo_name="app", source_path=link,
            )
        assert (source / "tracked.txt").read_bytes() == before
    finally:
        link.unlink(missing_ok=True)


def test_source_race_between_snapshots_fails_closed(tmp_path: Path) -> None:
    source, remote = _source_and_remote(tmp_path)

    class RacingInspector(ReadOnlyRepositoryInspector):
        snapshots = 0

        def snapshot(self, path: Path, *, timeout: float = 10.0):
            self.snapshots += 1
            if self.snapshots == 2:
                (path / "tracked.txt").write_text("raced\n", encoding="utf-8")
            return super().snapshot(path, timeout=timeout)

    with pytest.raises(SetupValidationError, match="repository source is invalid"):
        build_repository(
            key="app", project_id="project", iteration_id="iteration",
            repo_url=str(remote), repo_name="app", source_path=source,
            repository_inspector=RacingInspector(),
        )


def test_source_replacement_before_first_snapshot_fails_closed(tmp_path: Path) -> None:
    source, remote = _source_and_remote(tmp_path, "source")
    replacement, _ = _source_and_remote(tmp_path, "replacement")
    _git("remote", "set-url", "origin", str(remote), cwd=replacement)
    displaced = tmp_path / "displaced"

    class ReplacingInspector(ReadOnlyRepositoryInspector):
        replaced = False

        def snapshot(self, path: Path, *, timeout: float = 10.0):
            if not self.replaced:
                self.replaced = True
                source.rename(displaced)
                replacement.rename(source)
            return super().snapshot(path, timeout=timeout)

    try:
        with pytest.raises(SetupValidationError, match="repository source is invalid"):
            build_repository(
                key="app", project_id="project", iteration_id="iteration",
                repo_url=str(remote), repo_name="app", source_path=source,
                repository_inspector=ReplacingInspector(),
            )
    finally:
        if source.exists():
            source.rename(replacement)
        if displaced.exists():
            displaced.rename(source)


def test_source_replacement_after_second_snapshot_fails_closed(tmp_path: Path) -> None:
    source, remote = _source_and_remote(tmp_path, "source")
    replacement, _ = _source_and_remote(tmp_path, "replacement")
    _git("remote", "set-url", "origin", str(remote), cwd=replacement)
    displaced = tmp_path / "displaced"

    class LateReplacingInspector(ReadOnlyRepositoryInspector):
        snapshots = 0

        def snapshot(self, path: Path, *, timeout: float = 10.0):
            result = super().snapshot(path, timeout=timeout)
            self.snapshots += 1
            if self.snapshots == 2:
                source.rename(displaced)
                replacement.rename(source)
            return result

    try:
        with pytest.raises(SetupValidationError, match="repository source is invalid"):
            build_repository(
                key="app", project_id="project", iteration_id="iteration",
                repo_url=str(remote), repo_name="app", source_path=source,
                repository_inspector=LateReplacingInspector(),
            )
    finally:
        if source.exists():
            source.rename(replacement)
        if displaced.exists():
            displaced.rename(source)


def test_group_builder_rejects_cycle_missing_self_and_duplicate_keys() -> None:
    cases = (
        (_repo("app", depends_on=("sdk",)), _repo("sdk", depends_on=("app",))),
        (_repo("app", depends_on=("missing",)),),
        (_repo("app", depends_on=("app",)),),
    )
    for repositories in cases:
        builder = RepositoryGroupDraftBuilder(key="suite")
        for repository in repositories:
            builder.add(repository)
        with pytest.raises(SetupValidationError, match="repository group is invalid"):
            builder.build(primary="app")
    duplicate = RepositoryGroupDraftBuilder(key="suite")
    duplicate.add(_repo("app"))
    with pytest.raises(SetupValidationError, match="repository group is invalid"):
        duplicate.add(_repo("app"))


def test_group_build_is_deterministic_deep_copied_and_orders_integration_last() -> None:
    builder = RepositoryGroupDraftBuilder(
        key="suite", project_id="project", iteration_id="iteration",
        integration_test_commands=("pytest tests/integration",),
    )
    app = _repo("app", depends_on=("sdk",))
    builder.add(app)
    builder.add(_repo("docs"))
    builder.add(_repo("sdk"))
    app.test_commands = ("pytest changed",)

    first = builder.build(primary="app")
    first.repositories[0].test_commands = ("pytest mutated",)
    second = builder.build(primary="app")

    assert second.topological_keys() == ("docs", "sdk", "app")
    assert second.primary_repository == "app"
    assert tuple(item.role for item in second.repositories) == (
        RepositoryRole.PRIMARY, RepositoryRole.DEPENDENCY, RepositoryRole.DEPENDENCY,
    )
    assert second.repositories[0].test_commands == ("pytest tests/app",)
    assert second.integration_test_commands == ("pytest tests/integration",)


def test_group_builder_defaults_to_primary_key_and_normalizes_draft_roles() -> None:
    builder = RepositoryGroupDraftBuilder()
    builder.add(_repo("sdk"))
    builder.add(_repo("app", depends_on=("sdk",)))

    result = builder.build(primary="app")

    assert result.key == "app"
    assert result.primary_repository == "app"
    assert tuple((item.key, item.role) for item in result.repositories) == (
        ("sdk", RepositoryRole.DEPENDENCY),
        ("app", RepositoryRole.PRIMARY),
    )


def test_group_key_may_explicitly_equal_repository_key() -> None:
    builder = RepositoryGroupDraftBuilder(
        key="app", project_id="project", iteration_id="iteration"
    )
    builder.add(_repo("app"))

    assert builder.build(primary="app").key == "app"


def test_group_revalidates_earlier_sources_after_all_repository_probes(
    tmp_path: Path,
) -> None:
    sdk, sdk_remote = _source_and_remote(tmp_path, "sdk")
    app, app_remote = _source_and_remote(tmp_path, "app")

    class MutatingLaterInspector(ReadOnlyRepositoryInspector):
        mutated = False

        def snapshot(self, path: Path, *, timeout: float = 10.0):
            if path == app and not self.mutated:
                self.mutated = True
                (sdk / "tracked.txt").write_text("changed-later\n", encoding="utf-8")
            return super().snapshot(path, timeout=timeout)

    builder = RepositoryGroupDraftBuilder(
        key="suite", repository_inspector=MutatingLaterInspector()
    )
    builder.add(RepositoryMapping(
        key="sdk", project_id="project", iteration_id="iteration",
        repo_url=str(sdk_remote), repo_name="sdk", source_path=sdk,
        role=RepositoryRole.DEPENDENCY,
    ))
    builder.add(RepositoryMapping(
        key="app", project_id="project", iteration_id="iteration",
        repo_url=str(app_remote), repo_name="app", source_path=app,
        role=RepositoryRole.PRIMARY, depends_on=("sdk",),
    ))

    with pytest.raises(SetupValidationError, match="repository source is invalid"):
        builder.build(primary="app")


def test_inputs_are_strict_and_builder_copies_mutable_sequences() -> None:
    commands = ["pytest"]
    with pytest.raises(SetupValidationError, match="repository draft is invalid"):
        build_repository(
            key="app", project_id="project", iteration_id="iteration",
            repo_url="https://example.invalid/app.git", repo_name="app",
            test_commands=commands,  # type: ignore[arg-type]
        )
