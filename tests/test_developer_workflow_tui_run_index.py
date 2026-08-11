from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.developer_workflow import state_store as state_store_module
from src.developer_workflow.contracts import WorkflowRun, WorkflowState, WorkflowType
from src.developer_workflow.state_store import FileRunStore, UnsafeRunPathError
from src.developer_workflow.tui.models import RunActivity, RunFilter
from src.developer_workflow.tui.run_index import RunIndex


def _run(
    run_id: str,
    *,
    workflow_type: str = "requirement",
    work_item_id: str = "REQ-1",
) -> WorkflowRun:
    if workflow_type == "defect":
        return WorkflowRun.new_defect(
            "project", "iteration", "assignee", work_item_id
        ).validated_update(run_id=run_id)
    return WorkflowRun.new(workflow_type, work_item_id).validated_update(run_id=run_id)


def _set_updated_at(root: Path, run_id: str, updated_at: datetime) -> None:
    run_file = root / run_id / "run.json"
    payload = json.loads(run_file.read_text(encoding="utf-8"))
    payload["updated_at"] = updated_at.isoformat().replace("+00:00", "Z")
    run_file.write_text(json.dumps(payload), encoding="utf-8")


def _stored_payload(run: WorkflowRun) -> bytes:
    return json.dumps(
        run.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _tree_snapshot(root: Path) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        sorted(
            (
                str(path.relative_to(root)),
                path.lstat().st_mtime_ns,
                path.lstat().st_size,
            )
            for path in root.rglob("*")
        )
    )


def test_run_index_sorts_filters_and_applies_known_activity(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    older_id = "1" * 32
    newer_id = "2" * 32
    store.create(_run(older_id, work_item_id="REQ-older"))
    store.create(_run(newer_id, workflow_type="defect", work_item_id="BUG-newer"))
    _set_updated_at(tmp_path, older_id, datetime(2026, 8, 10, tzinfo=UTC))
    _set_updated_at(tmp_path, newer_id, datetime(2026, 8, 11, tzinfo=UTC))
    index = RunIndex(store)

    listed = index.list(
        RunFilter(),
        activities={newer_id: RunActivity.RUNNING, "f" * 32: RunActivity.QUEUED},
    )

    assert tuple(item.run_id for item in listed) == (newer_id, older_id)
    assert tuple(item.activity for item in listed) == (
        RunActivity.RUNNING,
        RunActivity.IDLE,
    )
    assert tuple(
        item.run_id
        for item in index.list(RunFilter(workflow_types=(WorkflowType.DEFECT,)))
    ) == (newer_id,)
    assert tuple(
        item.run_id
        for item in index.list(RunFilter(states=(WorkflowState.CREATED,)))
    ) == (newer_id, older_id)
    assert tuple(
        item.run_id for item in index.list(RunFilter(query="older"))
    ) == (older_id,)


def test_equal_timestamps_sort_by_run_id(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    first_id = "a" * 32
    second_id = "b" * 32
    timestamp = datetime(2026, 8, 11, tzinfo=UTC)
    for run_id in (second_id, first_id):
        store.create(_run(run_id, work_item_id=run_id))
        _set_updated_at(tmp_path, run_id, timestamp)

    assert tuple(item.run_id for item in RunIndex(store).list(RunFilter())) == (
        first_id,
        second_id,
    )


def test_corrupted_run_is_fixed_safe_entry_after_valid_runs(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    valid_id = "a" * 32
    corrupt_id = "b" * 32
    store.create(_run(valid_id, work_item_id="REQ-safe"))
    store.create(_run(corrupt_id, work_item_id="REQ-secret"))
    secret = "TOP-SECRET-run-payload"
    (tmp_path / corrupt_id / "run.json").write_text(
        f'{{"secret":"{secret}"', encoding="utf-8"
    )

    listed = RunIndex(store).list(RunFilter())

    assert tuple(item.run_id for item in listed) == (valid_id, corrupt_id)
    assert listed[1].corrupted is True
    assert listed[1].work_item_id == "storage-corrupted"
    assert secret not in repr(listed)


@pytest.mark.parametrize(
    "unsafe_work_item",
    (
        "SECRET-newline\nforged",
        "SECRET-control\x01forged",
        "SECRET-overlong-" + "x" * 300,
    ),
)
def test_run_index_isolates_legacy_run_with_unsafe_display_work_item(
    unsafe_work_item: str, tmp_path: Path
) -> None:
    store = FileRunStore(tmp_path)
    valid_id = "a" * 32
    unsafe_id = "b" * 32
    store.create(_run(valid_id, work_item_id="REQ-safe"))
    store.create(_run(unsafe_id, work_item_id=unsafe_work_item))

    listed = RunIndex(store).list(RunFilter())

    assert tuple(item.run_id for item in listed) == (valid_id, unsafe_id)
    assert listed[0].corrupted is False
    assert listed[1] == listed[1].corrupted_entry(unsafe_id)
    assert "SECRET" not in repr(listed)


def test_list_run_ids_ignores_noncanonical_files_and_is_sorted(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    for run_id in ("b" * 32, "a" * 32):
        store.create(_run(run_id))
    (tmp_path / ("c" * 32)).write_text("not a directory", encoding="utf-8")
    (tmp_path / ("D" * 32)).mkdir()
    (tmp_path / "not-a-run").mkdir()

    assert store.list_run_ids() == ("a" * 32, "b" * 32)


def test_list_run_ids_ignores_directory_symlink_without_reading_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    escaped_id = "e" * 32
    (outside / "run.json").write_text("EXTERNAL-SECRET", encoding="utf-8")
    link = root / escaped_id
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    store = FileRunStore(root)
    loaded: list[str] = []
    monkeypatch.setattr(store, "load", lambda run_id: loaded.append(run_id))

    assert store.list_run_ids() == ()
    assert RunIndex(store).list(RunFilter()) == ()
    assert loaded == []


def test_list_run_ids_rejects_first_seen_directory_replaced_after_entry_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = FileRunStore(tmp_path)
    run_id = "a" * 32
    writer.create(_run(run_id, work_item_id="REQ-original"))
    store = FileRunStore(tmp_path)
    run_dir = tmp_path / run_id
    displaced = tmp_path / "displaced"

    class RacingEntry:
        name = run_id

        def stat(self, *, follow_symlinks: bool) -> os.stat_result:
            assert follow_symlinks is False
            enumerated = run_dir.stat(follow_symlinks=False)
            run_dir.rename(displaced)
            run_dir.mkdir()
            (run_dir / "run.json").write_text(
                "REPLACEMENT-SECRET", encoding="utf-8"
            )
            return enumerated

    class RacingScan:
        def __enter__(self) -> object:
            return iter((RacingEntry(),))

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(os, "scandir", lambda _path: RacingScan())

    assert store.list_run_ids() == ()


def test_list_run_ids_rejects_windows_zero_identity_entry_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = FileRunStore(tmp_path)
    run_id = "a" * 32
    writer.create(_run(run_id))
    store = FileRunStore(tmp_path)
    run_dir = tmp_path / run_id
    displaced = tmp_path / "displaced"

    class ZeroIdentityRacingEntry:
        name = run_id

        def stat(self, *, follow_symlinks: bool) -> object:
            assert follow_symlinks is False
            mode = run_dir.stat(follow_symlinks=False).st_mode
            run_dir.rename(displaced)
            run_dir.mkdir()
            return SimpleNamespace(st_mode=mode, st_dev=0, st_ino=0)

    class RacingScan:
        def __enter__(self) -> object:
            return iter((ZeroIdentityRacingEntry(),))

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(os, "scandir", lambda _path: RacingScan())

    assert store.list_run_ids() == ()


def test_run_index_does_not_load_first_seen_replacement_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = FileRunStore(tmp_path)
    run_id = "a" * 32
    writer.create(_run(run_id))
    store = FileRunStore(tmp_path)
    run_dir = tmp_path / run_id
    displaced = tmp_path / "displaced"

    class RacingEntry:
        name = run_id

        def stat(self, *, follow_symlinks: bool) -> os.stat_result:
            assert follow_symlinks is False
            enumerated = run_dir.stat(follow_symlinks=False)
            run_dir.rename(displaced)
            run_dir.mkdir()
            (run_dir / "run.json").write_text(
                "REPLACEMENT-SECRET", encoding="utf-8"
            )
            return enumerated

    class RacingScan:
        def __enter__(self) -> object:
            return iter((RacingEntry(),))

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(os, "scandir", lambda _path: RacingScan())
    loaded: list[str] = []

    def forbidden_load(candidate: str, **_kwargs: object) -> WorkflowRun:
        loaded.append(candidate)
        raise AssertionError("replacement directory content was read")

    monkeypatch.setattr(store, "load", forbidden_load)

    assert RunIndex(store).list(RunFilter()) == ()
    assert loaded == []


def test_list_run_ids_fails_closed_when_root_identity_changes(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    store = FileRunStore(root)
    displaced = tmp_path / "displaced"
    root.rename(displaced)
    root.mkdir()

    with pytest.raises(UnsafeRunPathError, match="^workflow run root identity changed$"):
        store.list_run_ids()


def test_list_run_ids_fails_closed_on_enumeration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path)

    def denied(_path: object) -> object:
        raise PermissionError("private operating-system detail")

    monkeypatch.setattr(os, "scandir", denied)
    with pytest.raises(UnsafeRunPathError) as raised:
        store.list_run_ids()
    assert str(raised.value) == "workflow run root cannot be safely enumerated"
    assert raised.value.__cause__ is not None


def test_list_run_ids_checks_root_identity_after_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path)
    original = store._validate_root_identity
    checks = 0

    def replace_during_enumeration() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise UnsafeRunPathError("workflow run root identity changed")
        original()

    monkeypatch.setattr(store, "_validate_root_identity", replace_during_enumeration)

    with pytest.raises(UnsafeRunPathError, match="^workflow run root identity changed$"):
        store.list_run_ids()
    assert checks == 2


def test_run_index_is_read_only_and_creates_no_locks(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run_id = "a" * 32
    store.create(_run(run_id))
    before = _tree_snapshot(tmp_path)

    assert len(RunIndex(store).list(RunFilter())) == 1

    assert _tree_snapshot(tmp_path) == before
    assert not tuple(tmp_path.rglob(".operation-*.lock"))


def test_run_index_does_not_recreate_a_missing_run_lock(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run_id = "a" * 32
    store.create(_run(run_id))
    (tmp_path / run_id / ".lock").unlink()
    before = _tree_snapshot(tmp_path)

    assert len(RunIndex(store).list(RunFilter())) == 1

    assert _tree_snapshot(tmp_path) == before
    assert not (tmp_path / run_id / ".lock").exists()


def test_run_index_does_not_modify_an_empty_existing_lock(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run_id = "a" * 32
    store.create(_run(run_id))
    lock_path = tmp_path / run_id / ".lock"
    lock_path.write_bytes(b"")
    before = _tree_snapshot(tmp_path)

    assert len(RunIndex(store).list(RunFilter())) == 1

    assert _tree_snapshot(tmp_path) == before
    assert lock_path.read_bytes() == b""


def test_run_index_does_not_wait_for_an_existing_run_lock(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path, lock_timeout=0)
    run_id = "a" * 32
    store.create(_run(run_id))

    with store._locked(run_id):
        assert len(RunIndex(store).list(RunFilter())) == 1


def test_read_only_load_rejects_internal_run_file_symlink_before_reading_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path)
    run_id = "a" * 32
    store.create(_run(run_id))
    run_file = tmp_path / run_id / "run.json"
    target = tmp_path / run_id / "target.json"
    target.write_bytes(run_file.read_bytes())
    run_file.unlink()
    try:
        run_file.symlink_to(target)
    except OSError:
        pytest.skip("file symlinks unavailable")

    def forbidden_read(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("symlink target content was read")

    monkeypatch.setattr(os, "read", forbidden_read)
    with pytest.raises(UnsafeRunPathError):
        store.load(run_id, read_only=True)


def test_run_index_rejects_external_run_file_symlink_as_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path / "runs")
    run_id = "a" * 32
    store.create(_run(run_id))
    run_file = tmp_path / "runs" / run_id / "run.json"
    target = tmp_path / "outside.json"
    target.write_bytes(run_file.read_bytes())
    run_file.unlink()
    try:
        run_file.symlink_to(target)
    except OSError:
        pytest.skip("file symlinks unavailable")

    def forbidden_read(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("external symlink target content was read")

    monkeypatch.setattr(os, "read", forbidden_read)
    with pytest.raises(UnsafeRunPathError):
        RunIndex(store).list(RunFilter())


def test_read_only_load_rejects_directory_reparse_at_run_file(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path / "runs")
    run_id = "a" * 32
    store.create(_run(run_id))
    run_file = tmp_path / "runs" / run_id / "run.json"
    target = tmp_path / "outside-directory"
    target.mkdir()
    (target / "secret.json").write_text("EXTERNAL-SECRET", encoding="utf-8")
    run_file.unlink()
    try:
        run_file.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")

    with pytest.raises(UnsafeRunPathError):
        store.load(run_id, read_only=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
def test_read_only_load_rejects_windows_junction_at_run_file(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "runs")
    run_id = "a" * 32
    store.create(_run(run_id))
    run_file = tmp_path / "runs" / run_id / "run.json"
    target = tmp_path / "junction-target"
    target.mkdir()
    (target / "secret.json").write_text("EXTERNAL-SECRET", encoding="utf-8")
    run_file.unlink()
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(run_file), str(target)],
        capture_output=True,
        check=False,
    )
    if created.returncode != 0:
        pytest.skip("directory junctions unavailable")
    assert run_file.is_symlink() is False

    with pytest.raises(UnsafeRunPathError):
        store.load(run_id, read_only=True)


def test_read_only_load_gives_path_swap_precedence_over_read_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path)
    run_id = "a" * 32
    store.create(_run(run_id))
    run_file = tmp_path / run_id / "run.json"
    attempts = 0

    def failing_read(_descriptor: int, _size: int) -> bytes:
        nonlocal attempts
        attempts += 1
        displaced = tmp_path / f"displaced-run-{attempts}.json"
        run_file.rename(displaced)
        run_file.write_text("REPLACEMENT-SECRET", encoding="utf-8")
        raise OSError("simulated read failure")

    monkeypatch.setattr(os, "read", failing_read)
    with pytest.raises(UnsafeRunPathError):
        store.load(run_id, read_only=True)
    assert attempts == 3


def test_read_only_load_classifies_disappearance_after_lstat_as_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path)
    run_id = "a" * 32
    store.create(_run(run_id))
    run_file = tmp_path / run_id / "run.json"
    path_type = type(run_file)
    real_stat = path_type.stat
    observations = 0

    def disappearing_stat(
        path: Path, *, follow_symlinks: bool = True
    ) -> os.stat_result:
        nonlocal observations
        if path == run_file and follow_symlinks is False:
            observations += 1
            if observations == 2:
                run_file.unlink()
                raise FileNotFoundError("simulated post-lstat disappearance")
        return real_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(path_type, "stat", disappearing_stat)
    with pytest.raises(UnsafeRunPathError):
        store.load(run_id, read_only=True)
    assert observations == 2


def test_read_only_load_preserves_unsafe_when_run_directory_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path)
    run_id = "a" * 32
    store.create(_run(run_id))
    run_dir = tmp_path / run_id
    displaced = tmp_path / "displaced-run"

    def unsafe_read(_path: Path) -> str:
        run_dir.rename(displaced)
        raise UnsafeRunPathError("workflow run file identity changed")

    monkeypatch.setattr(state_store_module, "_read_run_file_nofollow", unsafe_read)
    with pytest.raises(UnsafeRunPathError):
        store.load(run_id, read_only=True)


@pytest.mark.parametrize("replacement_count", (1, 2))
def test_read_only_load_retries_bounded_atomic_replaces_to_complete_json(
    replacement_count: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileRunStore(tmp_path)
    run_id = "a" * 32
    original = store.create(_run(run_id))
    updated = original.validated_update(work_item_id="REQ-updated")
    run_file = tmp_path / run_id / "run.json"
    payloads = (_stored_payload(updated), _stored_payload(original))
    real_validate = state_store_module._validate_run_file_identity
    replacements = 0

    def replacing_validate(path: Path, expected_identity: tuple[int, int]) -> None:
        nonlocal replacements
        if replacements < replacement_count:
            replacements += 1
            temporary = run_file.with_name(f".replacement-{replacements}.tmp")
            temporary.write_bytes(payloads[replacements - 1])
            os.replace(temporary, run_file)
        real_validate(path, expected_identity)

    monkeypatch.setattr(
        state_store_module,
        "_validate_run_file_identity",
        replacing_validate,
    )

    expected = updated if replacement_count == 1 else original
    assert store.load(run_id, read_only=True) == expected
    assert replacements == replacement_count


@pytest.mark.skipif(os.name != "nt", reason="Windows pre-open race probe")
def test_read_only_load_retries_atomic_replace_before_descriptor_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path)
    run_id = "a" * 32
    original = store.create(_run(run_id))
    updated = original.validated_update(work_item_id="REQ-new-complete")
    run_file = tmp_path / run_id / "run.json"
    real_open = state_store_module._open_windows_run_file
    replaced = False

    def replacing_open(path: Path) -> int:
        nonlocal replaced
        if not replaced:
            replaced = True
            temporary = run_file.with_name(".pre-open-replacement.tmp")
            temporary.write_bytes(_stored_payload(updated))
            os.replace(temporary, run_file)
        return real_open(path)

    monkeypatch.setattr(state_store_module, "_open_windows_run_file", replacing_open)

    assert store.load(run_id, read_only=True) == updated
    assert replaced is True


def test_read_only_load_fails_closed_after_atomic_replace_retry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path)
    run_id = "a" * 32
    store.create(_run(run_id))
    run_file = tmp_path / run_id / "run.json"
    payload = run_file.read_bytes()
    real_close = os.close
    real_validate = state_store_module._validate_run_file_identity
    replacements = 0
    closed: list[int] = []

    def replacing_validate(path: Path, expected_identity: tuple[int, int]) -> None:
        nonlocal replacements
        replacements += 1
        temporary = run_file.with_name(f".replacement-{replacements}.tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, run_file)
        real_validate(path, expected_identity)

    def recording_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(os, "close", recording_close)
    monkeypatch.setattr(
        state_store_module,
        "_validate_run_file_identity",
        replacing_validate,
    )

    with pytest.raises(UnsafeRunPathError):
        store.load(run_id, read_only=True)
    assert replacements == 3
    assert len(closed) == 3


def test_read_only_load_rejects_final_descriptor_identity_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path)
    run_id = "a" * 32
    store.create(_run(run_id))
    real_fstat = os.fstat
    calls = 0

    def changing_fstat(descriptor: int) -> object:
        nonlocal calls
        calls += 1
        info = real_fstat(descriptor)
        if calls == 2:
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_dev=info.st_dev,
                st_ino=info.st_ino + 1,
                st_size=info.st_size,
                st_mtime_ns=info.st_mtime_ns,
                st_file_attributes=getattr(info, "st_file_attributes", 0),
            )
        return info

    monkeypatch.setattr(os, "fstat", changing_fstat)
    with pytest.raises(UnsafeRunPathError):
        store.load(run_id, read_only=True)
    assert calls == 2


def test_read_only_load_closes_descriptor_when_initial_fstat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path)
    run_id = "a" * 32
    store.create(_run(run_id))
    real_close = os.close
    closed: list[int] = []

    def failing_fstat(_descriptor: int) -> object:
        raise OSError("simulated fstat failure")

    def recording_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(os, "fstat", failing_fstat)
    monkeypatch.setattr(os, "close", recording_close)
    with pytest.raises(UnsafeRunPathError):
        store.load(run_id, read_only=True)
    assert len(closed) == 1


def test_normal_load_preserves_transient_open_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path)
    run_id = "a" * 32
    expected = store.create(_run(run_id))
    real_open = os.open
    attempts = 0

    def transient_open(
        path: object, flags: int, mode: int = 0o777
    ) -> int:
        nonlocal attempts
        if Path(path).name == "run.json":
            attempts += 1
            if attempts == 1:
                raise PermissionError("simulated replace window")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", transient_open)

    assert store.load(run_id) == expected
    assert attempts == 2


def test_replaced_run_directory_is_not_loaded_as_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileRunStore(tmp_path)
    run_id = "a" * 32
    store.create(_run(run_id))
    listed = store.list_run_ids()
    displaced = tmp_path / "displaced"
    (tmp_path / run_id).rename(displaced)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "run.json").write_text("EXTERNAL-SECRET", encoding="utf-8")
    try:
        (tmp_path / run_id).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    monkeypatch.setattr(store, "list_run_ids", lambda: listed)

    with pytest.raises(UnsafeRunPathError):
        RunIndex(store).list(RunFilter())
