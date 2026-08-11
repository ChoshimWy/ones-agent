from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
