"""增量检测状态存储 - JSON 文件"""

from __future__ import annotations

import json
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.parent
DEFAULT_PATH = _PROJECT_ROOT / "data" / "seen_defects.json"


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else DEFAULT_PATH
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"seen_ids": {}, "last_check": None}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")

    def is_seen(self, defect_id: str) -> bool:
        return defect_id in self._data["seen_ids"]

    def mark_seen(self, defect_id: str, title: str = "") -> None:
        self._data["seen_ids"][defect_id] = title
        self._save()

    def filter_new(self, defects: list[dict], id_key: str = "uuid") -> list[dict]:
        new = [d for d in defects if not self.is_seen(d.get(id_key, ""))]
        for d in new:
            self.mark_seen(d.get(id_key, ""), d.get("name", ""))
        return new

    def update_check_time(self) -> None:
        from datetime import datetime
        self._data["last_check"] = datetime.now().isoformat()
        self._save()

    @property
    def seen_count(self) -> int:
        return len(self._data["seen_ids"])

    @property
    def last_check(self) -> str | None:
        return self._data.get("last_check")

    def reset(self) -> None:
        self._data = {"seen_ids": {}, "last_check": None}
        self._save()
