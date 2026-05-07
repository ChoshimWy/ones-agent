"""工作流引擎 - 状态机 + SQLite 持久化 + 幂等执行"""

from __future__ import annotations

import enum
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()


class State(str, enum.Enum):
    PENDING = "pending"
    PARSING = "parsing"
    PLANNING = "planning"
    CODING = "coding"
    TESTING = "testing"
    PUSHING = "pushing"
    REPORTING = "reporting"
    SUCCESS = "success"
    FAILED = "failed"
    WAITING_APPROVAL = "waiting_approval"


TRANSITIONS: dict[State, set[State]] = {
    State.PENDING: {State.PARSING, State.WAITING_APPROVAL, State.FAILED},
    State.PARSING: {State.PLANNING, State.WAITING_APPROVAL, State.FAILED},
    State.PLANNING: {State.CODING, State.WAITING_APPROVAL, State.FAILED},
    State.WAITING_APPROVAL: {State.CODING, State.FAILED},
    State.CODING: {State.TESTING, State.WAITING_APPROVAL, State.FAILED},
    State.TESTING: {State.PUSHING, State.WAITING_APPROVAL, State.FAILED},
    State.PUSHING: {State.REPORTING, State.WAITING_APPROVAL, State.FAILED},
    State.REPORTING: {State.SUCCESS, State.WAITING_APPROVAL, State.FAILED},
    State.SUCCESS: set(),
    State.FAILED: {State.PENDING},
}


class WorkItemRecord:
    __slots__ = ("work_item_id", "state", "branch", "commit_hash", "plan_json", "logs", "updated_at")

    def __init__(self, **kwargs):
        for s in self.__slots__:
            setattr(self, s, kwargs.get(s, ""))

    @property
    def plan(self) -> dict:
        if self.plan_json:
            try:
                return json.loads(self.plan_json)
            except json.JSONDecodeError:
                return {}
        return {}


class Engine:
    """工作流引擎

    用法:
        engine = Engine(db_path="data/agent.db")
        engine.start_work("item-123", State.PARSING)
        engine.transition("item-123", State.PLANNING)
        ...
        engine.transition("item-123", State.SUCCESS)
    """

    def __init__(self, db_path: str = "data/agent.db"):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS work_items (
                    work_item_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL DEFAULT 'pending',
                    branch TEXT DEFAULT '',
                    commit_hash TEXT DEFAULT '',
                    plan_json TEXT DEFAULT '',
                    logs TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS project_repos (
                    project_id TEXT NOT NULL,
                    project_name TEXT DEFAULT '',
                    repo_url TEXT NOT NULL,
                    branch TEXT DEFAULT 'main',
                    PRIMARY KEY (project_id, repo_url)
                )
            """)
            self._ensure_project_repo_columns(conn)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    cron_expr TEXT NOT NULL,
                    project_id TEXT DEFAULT '',
                    assignee_id TEXT DEFAULT '',
                    assignee_name TEXT DEFAULT '',
                    item_type TEXT DEFAULT 'all',
                    action TEXT NOT NULL DEFAULT 'plan',
                    notify_emails TEXT DEFAULT '',
                    notify_wechat INTEGER NOT NULL DEFAULT 0,
                    last_run_at TEXT DEFAULT '',
                    last_run_count INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            self._ensure_scheduled_task_columns(conn)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_task_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'success',
                    item_count INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    finished_at TEXT DEFAULT '',
                    error_message TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_task_run_items (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    item_uuid TEXT DEFAULT '',
                    item_name TEXT DEFAULT '',
                    item_type TEXT DEFAULT '',
                    project_id TEXT DEFAULT '',
                    project_name TEXT DEFAULT '',
                    assignee TEXT DEFAULT '',
                    status_name TEXT DEFAULT '',
                    priority_name TEXT DEFAULT '',
                    action TEXT DEFAULT '',
                    plan_summary TEXT DEFAULT '',
                    plan_steps_json TEXT DEFAULT '',
                    risk_level TEXT DEFAULT '',
                    branch_name TEXT DEFAULT '',
                    requires_human_approval INTEGER NOT NULL DEFAULT 0,
                    analysis_markdown TEXT DEFAULT '',
                    with_codebase INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT DEFAULT '',
                    item_snapshot_json TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS execution_records (
                    id TEXT PRIMARY KEY,
                    request_key TEXT NOT NULL UNIQUE,
                    defect_id TEXT NOT NULL,
                    project_id TEXT DEFAULT '',
                    project_name TEXT DEFAULT '',
                    request_type TEXT NOT NULL,
                    repo_url TEXT NOT NULL,
                    base_branch TEXT NOT NULL,
                    proposed_branch_name TEXT DEFAULT '',
                    branch_name TEXT DEFAULT '',
                    repo_dir TEXT DEFAULT '',
                    requested_by TEXT DEFAULT '',
                    reason TEXT DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    source TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    operations_json TEXT DEFAULT '[]',
                    metadata_json TEXT DEFAULT '{}',
                    request_count INTEGER NOT NULL DEFAULT 1,
                    error_message TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_requested_at TEXT NOT NULL
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _ensure_project_repo_columns(conn: sqlite3.Connection) -> None:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(project_repos)").fetchall()}
        migrations = {
            "iteration_id": "ALTER TABLE project_repos ADD COLUMN iteration_id TEXT DEFAULT ''",
            "iteration_name": "ALTER TABLE project_repos ADD COLUMN iteration_name TEXT DEFAULT ''",
            "iteration_key": "ALTER TABLE project_repos ADD COLUMN iteration_key TEXT DEFAULT ''",
        }
        for column, statement in migrations.items():
            if column not in existing:
                conn.execute(statement)

    @staticmethod
    def _ensure_scheduled_task_columns(conn: sqlite3.Connection) -> None:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()}
        migrations = {
            "assignee_id": "ALTER TABLE scheduled_tasks ADD COLUMN assignee_id TEXT DEFAULT ''",
            "assignee_name": "ALTER TABLE scheduled_tasks ADD COLUMN assignee_name TEXT DEFAULT ''",
        }
        for column, statement in migrations.items():
            if column not in existing:
                conn.execute(statement)

    # ── 状态查询 ──────────────────────────────────────────

    def get(self, work_item_id: str) -> WorkItemRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM work_items WHERE work_item_id = ?", (work_item_id,)
            ).fetchone()
        if not row:
            return None
        return WorkItemRecord(
            work_item_id=row["work_item_id"],
            state=row["state"],
            branch=row["branch"],
            commit_hash=row["commit_hash"],
            plan_json=row["plan_json"],
            logs=row["logs"],
            updated_at=row["updated_at"],
        )

    def get_by_state(self, state: State) -> list[WorkItemRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM work_items WHERE state = ?", (state.value,)
            ).fetchall()
        return [WorkItemRecord(**dict(r)) for r in rows]

    # ── 状态转移 ──────────────────────────────────────────

    def start_work(self, work_item_id: str, initial_state: State = State.PENDING) -> None:
        existing = self.get(work_item_id)
        if existing:
            log.info("engine_idempotent_skip", work_item_id=work_item_id, current_state=existing.state)
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO work_items (work_item_id, state, updated_at) VALUES (?, ?, ?)",
                (work_item_id, initial_state.value, now),
            )
        log.info("engine_start_work", work_item_id=work_item_id, state=initial_state.value)

    def transition(self, work_item_id: str, new_state: State, **kwargs) -> None:
        record = self.get(work_item_id)
        if not record:
            log.error("engine_transition_not_found", work_item_id=work_item_id)
            return

        current = State(record.state)
        if new_state not in TRANSITIONS.get(current, set()):
            log.error("engine_invalid_transition", work_item_id=work_item_id,
                      current=current.value, target=new_state.value)
            raise ValueError(f"Invalid transition: {current.value} → {new_state.value}")

        now = datetime.now(timezone.utc).isoformat()
        updates = {"state": new_state.value, "updated_at": now}
        for k in ("branch", "commit_hash", "plan_json", "logs"):
            if k in kwargs:
                updates[k] = kwargs[k]

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [work_item_id]

        with self._connect() as conn:
            conn.execute(
                f"UPDATE work_items SET {set_clause} WHERE work_item_id = ?", values
            )
        log.info("engine_transition", work_item_id=work_item_id,
                 from_state=current.value, to_state=new_state.value)

    # ── 辅助 ──────────────────────────────────────────────

    def can_proceed(self, work_item_id: str) -> bool:
        record = self.get(work_item_id)
        if not record:
            return False
        state = State(record.state)
        return bool(TRANSITIONS.get(state, set()))

    def is_terminal(self, work_item_id: str) -> bool:
        record = self.get(work_item_id)
        if not record:
            return False
        return not TRANSITIONS.get(State(record.state), set())

    def reset(self, work_item_id: str) -> None:
        self.transition(work_item_id, State.PENDING)

    def delete(self, work_item_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM work_items WHERE work_item_id = ?", (work_item_id,))

    def list_items(
        self,
        state: str | None = None,
        search: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[WorkItemRecord], int]:
        with self._connect() as conn:
            clauses: list[str] = []
            params: list[Any] = []
            if state:
                clauses.append("state = ?")
                params.append(state)
            if search:
                clauses.append("(work_item_id LIKE ? OR plan_json LIKE ?)")
                params.extend([f"%{search}%", f"%{search}%"])
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            total = conn.execute(f"SELECT COUNT(*) FROM work_items{where}", params).fetchone()[0]
            offset = (page - 1) * page_size
            rows = conn.execute(
                f"SELECT * FROM work_items{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                params + [page_size, offset],
            ).fetchall()
        return [WorkItemRecord(**dict(r)) for r in rows], total

    # ── 项目-仓库映射 ────────────────────────────────────────

    def list_project_repos(self, project_id: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if project_id:
                rows = conn.execute(
                    "SELECT * FROM project_repos WHERE project_id = ? ORDER BY project_id",
                    (project_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM project_repos ORDER BY project_id"
                ).fetchall()
        return [{
            "projectId": r["project_id"],
            "projectName": r["project_name"],
            "repoUrl": r["repo_url"],
            "branch": r["branch"],
            "iterationId": r["iteration_id"] or "",
            "iterationName": r["iteration_name"] or "",
            "iterationKey": r["iteration_key"] or "",
        } for r in rows]

    def add_project_repo(
        self,
        project_id: str,
        project_name: str,
        repo_url: str,
        branch: str = "main",
        iteration_id: str = "",
        iteration_name: str = "",
        iteration_key: str = "",
    ) -> dict:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO project_repos (project_id, project_name, repo_url, branch, iteration_id, iteration_name, iteration_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project_id, project_name, repo_url, branch, iteration_id, iteration_name, iteration_key),
            )
        log.info("engine_add_project_repo", project_id=project_id, repo_url=repo_url)
        return {
            "projectId": project_id,
            "projectName": project_name,
            "repoUrl": repo_url,
            "branch": branch,
            "iterationId": iteration_id,
            "iterationName": iteration_name,
            "iterationKey": iteration_key,
        }

    def remove_project_repo(self, project_id: str, repo_url: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM project_repos WHERE project_id = ? AND repo_url = ?",
                (project_id, repo_url),
            )
        log.info("engine_remove_project_repo", project_id=project_id, repo_url=repo_url)

    def get_repo_for_project(self, project_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM project_repos WHERE project_id = ? LIMIT 1",
                (project_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "projectId": row["project_id"],
            "projectName": row["project_name"],
            "repoUrl": row["repo_url"],
            "branch": row["branch"],
            "iterationId": row["iteration_id"] or "",
            "iterationName": row["iteration_name"] or "",
            "iterationKey": row["iteration_key"] or "",
        }

    # ── 执行记录 ────────────────────────────────────────────

    def create_execution_record(
        self,
        request_key: str,
        defect_id: str,
        request_type: str,
        repo_url: str,
        base_branch: str,
        **kwargs,
    ) -> dict | None:
        now = datetime.now(timezone.utc).isoformat()
        execution_id = f"exec-{uuid.uuid4().hex[:12]}"
        fields = {
            "project_id": kwargs.get("project_id", ""),
            "project_name": kwargs.get("project_name", ""),
            "proposed_branch_name": kwargs.get("proposed_branch_name", ""),
            "branch_name": kwargs.get("branch_name", ""),
            "repo_dir": kwargs.get("repo_dir", ""),
            "requested_by": kwargs.get("requested_by", ""),
            "reason": kwargs.get("reason", ""),
            "confidence": kwargs.get("confidence", 0.0),
            "source": kwargs.get("source", ""),
            "status": kwargs.get("status", "pending"),
            "operations_json": json.dumps(kwargs.get("operations", []), ensure_ascii=False),
            "metadata_json": json.dumps(kwargs.get("metadata", {}), ensure_ascii=False),
            "request_count": int(kwargs.get("request_count", 1) or 1),
            "error_message": kwargs.get("error_message", ""),
        }
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO execution_records "
                "(id, request_key, defect_id, project_id, project_name, request_type, repo_url, base_branch, "
                "proposed_branch_name, branch_name, repo_dir, requested_by, reason, confidence, source, status, "
                "operations_json, metadata_json, request_count, error_message, created_at, updated_at, last_requested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    execution_id,
                    request_key,
                    defect_id,
                    fields["project_id"],
                    fields["project_name"],
                    request_type,
                    repo_url,
                    base_branch,
                    fields["proposed_branch_name"],
                    fields["branch_name"],
                    fields["repo_dir"],
                    fields["requested_by"],
                    fields["reason"],
                    float(fields["confidence"]),
                    fields["source"],
                    fields["status"],
                    fields["operations_json"],
                    fields["metadata_json"],
                    fields["request_count"],
                    fields["error_message"],
                    now,
                    now,
                    now,
                ),
            )
        return self.get_execution_record(execution_id)

    def get_execution_record(self, execution_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM execution_records WHERE id = ?",
                (execution_id,),
            ).fetchone()
        return _execution_record_to_dict(row) if row else None

    def get_execution_record_by_request_key(self, request_key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM execution_records WHERE request_key = ?",
                (request_key,),
            ).fetchone()
        return _execution_record_to_dict(row) if row else None

    def list_execution_records(self, defect_id: str | None = None, status: str | None = None) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if defect_id:
            clauses.append("defect_id = ?")
            params.append(defect_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM execution_records{where} ORDER BY updated_at DESC",
                params,
            ).fetchall()
        return [_execution_record_to_dict(r) for r in rows]

    def note_execution_request(self, execution_id: str) -> dict | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE execution_records SET request_count = request_count + 1, updated_at = ?, last_requested_at = ? "
                "WHERE id = ?",
                (now, now, execution_id),
            )
        return self.get_execution_record(execution_id)

    def update_execution_record(self, execution_id: str, **kwargs) -> dict | None:
        allowed = {
            "status",
            "branch_name",
            "repo_dir",
            "operations",
            "error_message",
            "proposed_branch_name",
            "requested_by",
            "reason",
            "confidence",
            "source",
            "metadata",
            "base_branch",
            "repo_url",
            "request_type",
            "project_id",
            "project_name",
        }
        updates: dict[str, Any] = {"updated_at": datetime.now(timezone.utc).isoformat()}
        for key in allowed:
            if key not in kwargs:
                continue
            value = kwargs[key]
            if key == "operations":
                updates["operations_json"] = json.dumps(value or [], ensure_ascii=False)
            elif key == "metadata":
                updates["metadata_json"] = json.dumps(value or {}, ensure_ascii=False)
            else:
                updates[key] = value

        set_clause = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [execution_id]
        with self._connect() as conn:
            conn.execute(
                f"UPDATE execution_records SET {set_clause} WHERE id = ?",
                values,
            )
        return self.get_execution_record(execution_id)

    # ── 定时任务 ────────────────────────────────────────────

    def list_scheduled_tasks(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_tasks ORDER BY created_at DESC"
            ).fetchall()
        return [_scheduled_task_to_dict(r) for r in rows]

    def get_scheduled_task(self, task_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return _scheduled_task_to_dict(row) if row else None

    def add_scheduled_task(self, task_id: str, name: str, cron_expr: str,
                           project_id: str = "", assignee_id: str = "", assignee_name: str = "", item_type: str = "all",
                           action: str = "plan", notify_emails: str = "",
                           notify_wechat: bool = False, enabled: bool = True) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO scheduled_tasks "
                "(id, name, enabled, cron_expr, project_id, assignee_id, assignee_name, item_type, action, "
                "notify_emails, notify_wechat, last_run_at, last_run_count, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, ?, ?)",
                (task_id, name, int(enabled), cron_expr, project_id, assignee_id, assignee_name, item_type,
                 action, notify_emails, int(notify_wechat), now, now),
            )
        log.info("engine_add_scheduled_task", id=task_id, name=name)
        return self.get_scheduled_task(task_id)  # type: ignore[return-value]

    def update_scheduled_task(self, task_id: str, **kwargs) -> dict | None:
        allowed = {"name", "enabled", "cron_expr", "project_id", "assignee_id", "assignee_name", "item_type",
                    "action", "notify_emails", "notify_wechat"}
        updates = {"updated_at": datetime.now(timezone.utc).isoformat()}
        for k in allowed:
            if k in kwargs:
                v = kwargs[k]
                updates[k] = int(v) if k in ("enabled", "notify_wechat") else v
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [task_id]
        with self._connect() as conn:
            conn.execute(
                f"UPDATE scheduled_tasks SET {set_clause} WHERE id = ?", values
            )
        return self.get_scheduled_task(task_id)

    def delete_scheduled_task(self, task_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        log.info("engine_delete_scheduled_task", id=task_id)

    def update_scheduled_task_run(self, task_id: str, count: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE scheduled_tasks SET last_run_at = ?, last_run_count = ?, updated_at = ? "
                "WHERE id = ?", (now, count, now, task_id)
            )

    def list_enabled_scheduled_tasks(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE enabled = 1 ORDER BY created_at"
            ).fetchall()
        return [_scheduled_task_to_dict(r) for r in rows]

    def create_scheduled_task_run(self, task_id: str, status: str = "running") -> dict:
        now = datetime.now(timezone.utc).isoformat()
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO scheduled_task_runs "
                "(id, task_id, status, item_count, started_at, finished_at, error_message, created_at) "
                "VALUES (?, ?, ?, 0, ?, '', '', ?)",
                (run_id, task_id, status, now, now),
            )
        return self.get_scheduled_task_run(run_id)  # type: ignore[return-value]

    def finish_scheduled_task_run(self, run_id: str, status: str, item_count: int, error_message: str = "") -> dict | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE scheduled_task_runs SET status = ?, item_count = ?, finished_at = ?, error_message = ? WHERE id = ?",
                (status, item_count, now, error_message, run_id),
            )
        return self.get_scheduled_task_run(run_id)

    def get_scheduled_task_run(self, run_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM scheduled_task_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        return _scheduled_task_run_to_dict(row) if row else None

    def list_scheduled_task_runs(self, task_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_task_runs WHERE task_id = ? ORDER BY created_at DESC",
                (task_id,),
            ).fetchall()
        return [_scheduled_task_run_to_dict(r) for r in rows]

    def list_all_scheduled_task_runs(
        self,
        task_id: str | None = None,
        status: str | None = None,
        search: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_id:
            clauses.append("r.task_id = ?")
            params.append(task_id)
        if status:
            clauses.append("r.status = ?")
            params.append(status)
        if search:
            clauses.append(
                "(" 
                "LOWER(r.task_id) LIKE ? OR "
                "LOWER(COALESCE(t.name, '')) LIKE ? OR "
                "EXISTS ("
                "SELECT 1 FROM scheduled_task_run_items i "
                "WHERE i.run_id = r.id AND ("
                "LOWER(COALESCE(i.item_uuid, '')) LIKE ? OR "
                "LOWER(COALESCE(i.item_name, '')) LIKE ? OR "
                "LOWER(COALESCE(i.plan_summary, '')) LIKE ? OR "
                "LOWER(COALESCE(i.analysis_markdown, '')) LIKE ?"
                ")"
                ")"
                ")"
            )
            like = f"%{search.lower()}%"
            params.extend([like, like, like, like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        base_from = (
            " FROM scheduled_task_runs r "
            "LEFT JOIN scheduled_tasks t ON t.id = r.task_id "
        )
        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*){base_from}{where}",
                params,
            ).fetchone()[0]
            offset = (page - 1) * page_size
            rows = conn.execute(
                "SELECT r.*, t.name AS task_name, t.cron_expr AS task_cron_expr, "
                "t.action AS task_action, t.project_id AS task_project_id "
                f"{base_from}{where} ORDER BY r.created_at DESC LIMIT ? OFFSET ?",
                params + [page_size, offset],
            ).fetchall()
        return [_scheduled_task_run_to_dict(r) for r in rows], total

    def add_scheduled_task_run_item(self, run_id: str, task_id: str, **kwargs) -> dict | None:
        now = datetime.now(timezone.utc).isoformat()
        item_id = f"run-item-{uuid.uuid4().hex[:12]}"
        fields = {
            "item_uuid": kwargs.get("item_uuid", ""),
            "item_name": kwargs.get("item_name", ""),
            "item_type": kwargs.get("item_type", ""),
            "project_id": kwargs.get("project_id", ""),
            "project_name": kwargs.get("project_name", ""),
            "assignee": kwargs.get("assignee", ""),
            "status_name": kwargs.get("status_name", ""),
            "priority_name": kwargs.get("priority_name", ""),
            "action": kwargs.get("action", ""),
            "plan_summary": kwargs.get("plan_summary", ""),
            "plan_steps_json": json.dumps(kwargs.get("plan_steps", []), ensure_ascii=False),
            "risk_level": kwargs.get("risk_level", ""),
            "branch_name": kwargs.get("branch_name", ""),
            "requires_human_approval": int(bool(kwargs.get("requires_human_approval", False))),
            "analysis_markdown": kwargs.get("analysis_markdown", ""),
            "with_codebase": int(bool(kwargs.get("with_codebase", False))),
            "error_message": kwargs.get("error_message", ""),
            "item_snapshot_json": json.dumps(kwargs.get("item_snapshot", {}), ensure_ascii=False),
        }
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO scheduled_task_run_items "
                "(id, run_id, task_id, item_uuid, item_name, item_type, project_id, project_name, assignee, "
                "status_name, priority_name, action, plan_summary, plan_steps_json, risk_level, branch_name, "
                "requires_human_approval, analysis_markdown, with_codebase, error_message, item_snapshot_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id,
                    run_id,
                    task_id,
                    fields["item_uuid"],
                    fields["item_name"],
                    fields["item_type"],
                    fields["project_id"],
                    fields["project_name"],
                    fields["assignee"],
                    fields["status_name"],
                    fields["priority_name"],
                    fields["action"],
                    fields["plan_summary"],
                    fields["plan_steps_json"],
                    fields["risk_level"],
                    fields["branch_name"],
                    fields["requires_human_approval"],
                    fields["analysis_markdown"],
                    fields["with_codebase"],
                    fields["error_message"],
                    fields["item_snapshot_json"],
                    now,
                ),
            )
        return self.get_scheduled_task_run_item(item_id)

    def get_scheduled_task_run_item(self, item_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM scheduled_task_run_items WHERE id = ?",
                (item_id,),
            ).fetchone()
        return _scheduled_task_run_item_to_dict(row) if row else None

    def list_scheduled_task_run_items(self, run_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_task_run_items WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
        return [_scheduled_task_run_item_to_dict(r) for r in rows]


def _scheduled_task_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "cronExpr": row["cron_expr"],
        "projectId": row["project_id"] or "",
        "assigneeId": row["assignee_id"] or "",
        "assigneeName": row["assignee_name"] or "",
        "itemType": row["item_type"],
        "action": row["action"],
        "notifyEmails": row["notify_emails"] or "",
        "notifyWechat": bool(row["notify_wechat"]),
        "lastRunAt": row["last_run_at"] or "",
        "lastRunCount": row["last_run_count"] or 0,
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _scheduled_task_run_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "taskId": row["task_id"],
        "taskName": row["task_name"] if "task_name" in row.keys() else "",
        "taskCronExpr": row["task_cron_expr"] if "task_cron_expr" in row.keys() else "",
        "taskAction": row["task_action"] if "task_action" in row.keys() else "",
        "taskProjectId": row["task_project_id"] if "task_project_id" in row.keys() else "",
        "status": row["status"],
        "itemCount": row["item_count"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"] or "",
        "errorMessage": row["error_message"] or "",
        "createdAt": row["created_at"],
    }


def _scheduled_task_run_item_to_dict(row: sqlite3.Row) -> dict:
    plan_steps_json = row["plan_steps_json"] or "[]"
    item_snapshot_json = row["item_snapshot_json"] or "{}"
    try:
        plan_steps = json.loads(plan_steps_json)
    except json.JSONDecodeError:
        plan_steps = []
    try:
        item_snapshot = json.loads(item_snapshot_json)
    except json.JSONDecodeError:
        item_snapshot = {}
    return {
        "id": row["id"],
        "runId": row["run_id"],
        "taskId": row["task_id"],
        "itemUuid": row["item_uuid"] or "",
        "itemName": row["item_name"] or "",
        "itemType": row["item_type"] or "",
        "projectId": row["project_id"] or "",
        "projectName": row["project_name"] or "",
        "assignee": row["assignee"] or "",
        "statusName": row["status_name"] or "",
        "priorityName": row["priority_name"] or "",
        "action": row["action"] or "",
        "planSummary": row["plan_summary"] or "",
        "planSteps": plan_steps,
        "riskLevel": row["risk_level"] or "",
        "branchName": row["branch_name"] or "",
        "requiresHumanApproval": bool(row["requires_human_approval"]),
        "analysisMarkdown": row["analysis_markdown"] or "",
        "withCodebase": bool(row["with_codebase"]),
        "errorMessage": row["error_message"] or "",
        "itemSnapshot": item_snapshot,
        "createdAt": row["created_at"],
    }


def _execution_record_to_dict(row: sqlite3.Row) -> dict:
    operations_json = row["operations_json"] or "[]"
    metadata_json = row["metadata_json"] or "{}"
    try:
        operations = json.loads(operations_json)
    except json.JSONDecodeError:
        operations = []
    try:
        metadata = json.loads(metadata_json)
    except json.JSONDecodeError:
        metadata = {}
    return {
        "id": row["id"],
        "requestKey": row["request_key"],
        "defectId": row["defect_id"],
        "projectId": row["project_id"] or "",
        "projectName": row["project_name"] or "",
        "requestType": row["request_type"],
        "repoUrl": row["repo_url"],
        "baseBranch": row["base_branch"],
        "proposedBranchName": row["proposed_branch_name"] or "",
        "branchName": row["branch_name"] or "",
        "repoDir": row["repo_dir"] or "",
        "requestedBy": row["requested_by"] or "",
        "reason": row["reason"] or "",
        "confidence": row["confidence"],
        "source": row["source"] or "",
        "status": row["status"],
        "operations": tuple(operations) if isinstance(operations, list) else tuple(),
        "metadata": metadata if isinstance(metadata, dict) else {},
        "requestCount": row["request_count"],
        "errorMessage": row["error_message"] or "",
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "lastRequestedAt": row["last_requested_at"],
    }
