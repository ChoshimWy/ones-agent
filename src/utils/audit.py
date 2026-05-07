"""审计日志 - 记录所有写操作"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import structlog

log = structlog.get_logger()


class AuditLog:
    """审计日志记录器

    每条记录包含: actor, action, target, result, timestamp
    同时写入 structlog 和 JSON 文件。
    """

    def __init__(self, log_dir: str = "data/audit"):
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def record(self, actor: str, action: str, target: str, result: str = "", extra: dict | None = None) -> None:
        entry = {
            "actor": actor,
            "action": action,
            "target": target,
            "result": result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            entry.update(extra)

        log.info("audit", **entry)
        self._write_file(entry)

    def _write_file(self, entry: dict) -> None:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self._log_dir / f"{date}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def query(
        self,
        level: str | None = None,
        search: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict], int]:
        entries: list[dict] = []
        if not self._log_dir.exists():
            return entries, 0
        for f in sorted(self._log_dir.glob("*.jsonl"), reverse=True):
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                    entries.append(entry)
                except json.JSONDecodeError:
                    continue
        if level:
            entries = [e for e in entries if e.get("result", "").lower() == level]
        if search:
            entries = [e for e in entries if search.lower() in json.dumps(e).lower()]
        total = len(entries)
        start = (page - 1) * page_size
        return entries[start : start + page_size], total
