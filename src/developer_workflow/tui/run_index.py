"""Read-only reconstruction of terminal UI run summaries."""

from __future__ import annotations

from collections.abc import Mapping

from ..state_store import FileRunStore, RunCorruptedError, RunNotFoundError
from .models import RunActivity, RunFilter, RunSummary, TuiDisplayError


class RunIndex:
    """Build display summaries from the authoritative file-backed store."""

    def __init__(self, store: FileRunStore) -> None:
        self._store = store

    def list(
        self,
        filters: RunFilter,
        activities: Mapping[str, RunActivity] | None = None,
    ) -> tuple[RunSummary, ...]:
        activity_by_id = activities or {}
        valid: list[RunSummary] = []
        corrupted: list[RunSummary] = []
        for run_id in self._store.list_run_ids():
            try:
                run = self._store.load(run_id, read_only=True)
            except RunCorruptedError:
                item = RunSummary.corrupted_entry(run_id)
                if filters.matches(item):
                    corrupted.append(item)
                continue
            except RunNotFoundError:
                continue
            try:
                item = RunSummary.from_run(
                    run,
                    activity=activity_by_id.get(run_id, RunActivity.IDLE),
                )
            except TuiDisplayError:
                item = RunSummary.corrupted_entry(run_id)
                if filters.matches(item):
                    corrupted.append(item)
                continue
            if filters.matches(item):
                valid.append(item)

        valid.sort(key=lambda item: item.run_id)
        valid.sort(key=lambda item: item.updated_at, reverse=True)
        corrupted.sort(key=lambda item: item.run_id)
        return (*valid, *corrupted)
