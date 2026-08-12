from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from src.developer_workflow.config import DeveloperWorkflowConfig, PublishingConfig
from src.developer_workflow.contracts import RepositoryMapping
from src.developer_workflow.credential_store import CredentialStoreError
from src.developer_workflow.private_paths import PrivatePathError, prepare_private_directory
from src.developer_workflow.setup_models import (
    ActiveSetup,
    RuntimePublicConfig,
    RuntimeSecrets,
    SecretKind,
)
from src.developer_workflow.setup_store import SetupStore, SetupStoreError


class FakeCredentials:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], RuntimeSecrets] = {}
        self.fail_delete = False

    def write_generation(
        self, profile_id: str, generation: str, secrets: RuntimeSecrets
    ) -> None:
        key = (profile_id, generation)
        if key in self.data:
            raise CredentialStoreError("credential operation failed")
        self.data[key] = secrets

    def read_generation(
        self, profile_id: str, generation: str, kinds: tuple[SecretKind, ...]
    ) -> RuntimeSecrets:
        try:
            stored = self.data[(profile_id, generation)]
            return RuntimeSecrets({kind: stored.require(kind) for kind in kinds})
        except (KeyError, ValueError):
            raise CredentialStoreError("credential operation failed") from None

    def delete_generation(self, profile_id: str, generation: str) -> None:
        if self.fail_delete:
            raise CredentialStoreError("credential operation failed")
        self.data.pop((profile_id, generation), None)

    def list_generations(self, profile_id: str) -> tuple[str, ...]:
        return tuple(sorted(g for p, g in self.data if p == profile_id))


def _candidate(tmp_path: Path, generation: str) -> ActiveSetup:
    return ActiveSetup(
        generation=generation,
        runtime=RuntimePublicConfig(
            ones_base_url="https://ones.example.invalid",
            ones_team_id="team",
            ones_issue_type_id="type",
            ones_comment_list_path_template=(
                "/project/api/project/team/{team_id}/task/{item_id}/comment"
            ),
            provider_host="github.example.invalid",
            provider_api_url="https://github.example.invalid/api/v3",
            git_author_name="ONES Agent",
            git_author_email="agent@example.invalid",
            codex_auth_mode="credential",
        ),
        workflow=DeveloperWorkflowConfig(
            run_root=(tmp_path / "runs").resolve(),
            mirror_root=(tmp_path / "mirrors").resolve(),
            worktree_root=(tmp_path / "worktrees").resolve(),
            sandbox_permission_profile="tests",
            max_codex_attempts=3,
            repositories=(
                RepositoryMapping(
                    key="repo",
                    project_id="project",
                    iteration_id="iteration",
                    repo_url="https://example.invalid/repo.git",
                    repo_name="repo",
                ),
            ),
            publishing=PublishingConfig(provider="local_fake"),
        ),
        credential_kinds=(SecretKind.ONES_PASSWORD,),
    )


def _secrets(value: str = "TOKEN-SECRET") -> RuntimeSecrets:
    return RuntimeSecrets({SecretKind.ONES_PASSWORD: value})


def _commit_process(
    config_path: str, root: str, generation: str, ready: object, start: object
) -> None:
    credentials = FakeCredentials()
    setup = SetupStore(credentials, config_path=Path(config_path))
    ready.put(True)  # type: ignore[attr-defined]
    start.wait(10)  # type: ignore[attr-defined]
    setup.commit(
        "profile-1", _candidate(Path(root), generation), _secrets(generation)
    )


@pytest.fixture
def store(tmp_path: Path) -> tuple[SetupStore, FakeCredentials, Path]:
    credentials = FakeCredentials()
    path = tmp_path / "local" / "ones-dev" / "config.json"
    return SetupStore(credentials, config_path=path), credentials, path


def test_default_path_uses_localappdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    configured = SetupStore(FakeCredentials())

    assert configured.config_path == tmp_path / "ones-dev" / "config.json"


def test_prepare_private_directory_is_private_and_rejects_symlink(
    tmp_path: Path,
) -> None:
    private = prepare_private_directory(tmp_path / "private")
    assert private.is_dir()
    if os.name != "nt":
        assert private.stat().st_mode & 0o077 == 0
    link = tmp_path / "link"
    try:
        link.symlink_to(private, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(PrivatePathError, match="^private workflow root is unsafe$"):
        prepare_private_directory(link)


def test_commit_switches_generation_and_reads_only_active_kinds(
    store: tuple[SetupStore, FakeCredentials, Path], tmp_path: Path
) -> None:
    setup, _, _ = store
    first = setup.commit("profile-1", _candidate(tmp_path, "a" * 32), _secrets("old"))
    second = setup.commit("profile-1", _candidate(tmp_path, "b" * 32), _secrets("new"))

    assert setup.load() == second
    assert second.previous == first.active
    assert setup.read_active_secrets(second).require(SecretKind.ONES_PASSWORD) == "new"


def test_json_failure_rolls_back_new_credentials_and_preserves_old_document(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup, credentials, _ = store
    before = setup.commit("profile-1", _candidate(tmp_path, "a" * 32), _secrets("old"))

    def fail_write(*args: object, **kwargs: object) -> None:
        raise OSError("TOKEN-SECRET target path")

    monkeypatch.setattr(setup, "_atomic_write", fail_write)
    with pytest.raises(SetupStoreError, match="^configuration save failed$") as captured:
        setup.commit("profile-1", _candidate(tmp_path, "c" * 32), _secrets())

    assert "TOKEN-SECRET" not in repr(captured.value)
    assert setup.load() == before
    assert ("profile-1", "c" * 32) not in credentials.data


def test_reload_failure_after_replace_leaves_recoverable_new_active(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup, credentials, path = store
    original = setup._load_unlocked
    calls = 0

    def fail_second_load():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SetupStoreError("configuration load failed")
        return original()

    monkeypatch.setattr(setup, "_load_unlocked", fail_second_load)
    with pytest.raises(SetupStoreError, match="^configuration save failed$"):
        setup.commit("profile-1", _candidate(tmp_path, "d" * 32), _secrets("new"))

    recovered = SetupStore(credentials, config_path=path).load()
    assert recovered.active is not None
    assert recovered.active.generation == "d" * 32
    assert ("profile-1", "d" * 32) in credentials.data


def test_exception_after_replace_keeps_new_credentials_recoverable(
    store: tuple[SetupStore, FakeCredentials, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup, credentials, path = store
    original = os.replace

    def replace_then_fail(source: Path, destination: Path) -> None:
        original(source, destination)
        raise OSError("TOKEN-SECRET path")

    monkeypatch.setattr("src.developer_workflow.setup_store.os.replace", replace_then_fail)
    with pytest.raises(SetupStoreError, match="^configuration save failed$"):
        setup.commit("profile-1", _candidate(tmp_path, "e" * 32), _secrets("new"))

    recovered = SetupStore(credentials, config_path=path).load()
    assert recovered.active is not None and recovered.active.generation == "e" * 32
    assert ("profile-1", "e" * 32) in credentials.data


def test_two_process_commits_are_serialized_without_mixed_document(
    store: tuple[SetupStore, FakeCredentials, Path], tmp_path: Path
) -> None:
    setup, _, path = store
    setup.load_or_empty(profile_id="profile-1")
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    generations = ("a" * 32, "b" * 32)
    processes = [
        context.Process(
            target=_commit_process,
            args=(str(path), str(tmp_path), generation, ready, start),
        )
        for generation in generations
    ]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=10) is True
        start.set()
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
        document = setup.load()
        assert document.active is not None and document.previous is not None
        assert {
            document.active.generation,
            document.previous.generation,
        } == set(generations)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)


def test_restore_finalize_and_orphan_cleanup_are_pointer_safe(
    store: tuple[SetupStore, FakeCredentials, Path], tmp_path: Path
) -> None:
    setup, credentials, _ = store
    setup.commit("profile-1", _candidate(tmp_path, "a" * 32), _secrets("old"))
    setup.commit("profile-1", _candidate(tmp_path, "b" * 32), _secrets("new"))
    credentials.data[("profile-1", "c" * 32)] = _secrets("orphan")

    assert setup.orphan_generations() == ("c" * 32,)
    with pytest.raises(SetupStoreError, match="^credential cleanup refused$"):
        setup.cleanup_orphan_generations(("a" * 32, "c" * 32))
    setup.cleanup_orphan_generations(("c" * 32,))
    restored = setup.restore_previous("profile-1")
    assert restored.active is not None and restored.active.generation == "a" * 32
    assert ("profile-1", "b" * 32) not in credentials.data


def test_finalize_keeps_new_active_when_old_credential_delete_fails(
    store: tuple[SetupStore, FakeCredentials, Path], tmp_path: Path
) -> None:
    setup, credentials, _ = store
    setup.commit("profile-1", _candidate(tmp_path, "a" * 32), _secrets("old"))
    setup.commit("profile-1", _candidate(tmp_path, "b" * 32), _secrets("new"))
    credentials.fail_delete = True

    finalized = setup.finalize_activation("profile-1")

    assert finalized.previous is None
    assert finalized.active is not None and finalized.active.generation == "b" * 32
    assert setup.orphan_generations() == ("a" * 32,)


@pytest.mark.parametrize(
    "raw",
    [b"{broken", b'\xff', b"[]", b"x" * (1024 * 1024 + 1)],
    ids=("invalid-json", "invalid-utf8", "wrong-shape", "oversize"),
)
def test_load_rejects_corrupt_unsafe_or_oversize_data_without_leaks(
    store: tuple[SetupStore, FakeCredentials, Path], raw: bytes
) -> None:
    setup, _, path = store
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)

    with pytest.raises(SetupStoreError, match="^stored configuration is corrupted$") as captured:
        setup.load()
    assert str(path) not in repr(captured.value)


def test_load_rejects_final_symlink_without_disclosing_target(
    store: tuple[SetupStore, FakeCredentials, Path], tmp_path: Path
) -> None:
    setup, _, path = store
    path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "TOKEN-SECRET-target.json"
    target.write_text(json.dumps({"schema_version": 1, "profile_id": "profile-1"}))
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    with pytest.raises(SetupStoreError, match="^configuration path is unsafe$") as captured:
        setup.load()
    assert "TOKEN-SECRET" not in repr(captured.value)
