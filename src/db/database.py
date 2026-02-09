"""SQLite database layer using aiosqlite."""

import json
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from .models import (
    Item,
    ItemStatus,
    ItemType,
    PipelineRun,
    PipelineStatus,
    StepLog,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('ARTICLE', 'TOPIC_SEED', 'CONTEXT_NOTE')),
    raw_content TEXT NOT NULL,
    source_url TEXT,
    extracted_text TEXT,
    summary TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    language TEXT NOT NULL DEFAULT 'ru',
    week_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'COLLECTED'
        CHECK(status IN ('COLLECTED', 'CLUSTERED', 'PUBLISHED'))
);

CREATE INDEX IF NOT EXISTS idx_items_week_id ON items(week_id);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id TEXT PRIMARY KEY,
    week_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'RUNNING'
        CHECK(status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS step_logs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    llm_model TEXT NOT NULL,
    details TEXT DEFAULT '',
    error TEXT,
    FOREIGN KEY (run_id) REFERENCES pipeline_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_step_logs_run_id ON step_logs(run_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _current_week_id() -> str:
    now = datetime.now()
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _dt_to_str(dt: datetime) -> str:
    return dt.isoformat()


def _str_to_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA_SQL)
            await db.commit()

    # ── Items ──

    async def save_item(self, item: Item) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO items
                   (id, created_at, type, raw_content, source_url, extracted_text,
                    summary, tags, language, week_id, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.id,
                    _dt_to_str(item.created_at),
                    item.type.value,
                    item.raw_content,
                    item.source_url,
                    item.extracted_text,
                    item.summary,
                    json.dumps(item.tags, ensure_ascii=False),
                    item.language,
                    item.week_id,
                    item.status.value,
                ),
            )
            await db.commit()

    async def get_items_by_week(
        self,
        week_id: str | None = None,
        status: ItemStatus | None = None,
    ) -> list[Item]:
        if week_id is None:
            week_id = _current_week_id()
        query = "SELECT * FROM items WHERE week_id = ?"
        params: list = [week_id]
        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        query += " ORDER BY created_at ASC"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_item(row) for row in rows]

    async def get_item(self, item_id: str) -> Item | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM items WHERE id = ?", (item_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._row_to_item(row) if row else None

    async def find_item_by_short_id(self, short_id: str) -> Item | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM items WHERE id LIKE ?", (f"{short_id}%",)
            ) as cursor:
                row = await cursor.fetchone()
                return self._row_to_item(row) if row else None

    async def delete_item(self, item_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM items WHERE id = ?", (item_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def update_items_status(
        self, item_ids: list[str], status: ItemStatus
    ) -> None:
        if not item_ids:
            return
        placeholders = ",".join("?" for _ in item_ids)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE items SET status = ? WHERE id IN ({placeholders})",
                [status.value] + item_ids,
            )
            await db.commit()

    async def count_items_by_week(self, week_id: str | None = None) -> int:
        if week_id is None:
            week_id = _current_week_id()
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM items WHERE week_id = ?", (week_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    def _row_to_item(self, row: aiosqlite.Row) -> Item:
        return Item(
            id=row["id"],
            created_at=_str_to_dt(row["created_at"]),
            type=ItemType(row["type"]),
            raw_content=row["raw_content"],
            source_url=row["source_url"],
            extracted_text=row["extracted_text"],
            summary=row["summary"],
            tags=json.loads(row["tags"]),
            language=row["language"],
            week_id=row["week_id"],
            status=ItemStatus(row["status"]),
        )

    # ── Pipeline Runs ──

    async def save_pipeline_run(self, run: PipelineRun) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO pipeline_runs
                   (id, week_id, started_at, finished_at, status,
                    total_input_tokens, total_output_tokens, estimated_cost_usd)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run.id,
                    run.week_id,
                    _dt_to_str(run.started_at),
                    _dt_to_str(run.finished_at) if run.finished_at else None,
                    run.status.value,
                    run.total_input_tokens,
                    run.total_output_tokens,
                    run.estimated_cost_usd,
                ),
            )
            await db.commit()

    async def update_pipeline_run(
        self,
        run_id: str,
        status: PipelineStatus,
        total_input_tokens: int = 0,
        total_output_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE pipeline_runs
                   SET finished_at = ?, status = ?,
                       total_input_tokens = ?, total_output_tokens = ?,
                       estimated_cost_usd = ?
                   WHERE id = ?""",
                (
                    _dt_to_str(datetime.now()),
                    status.value,
                    total_input_tokens,
                    total_output_tokens,
                    estimated_cost_usd,
                    run_id,
                ),
            )
            await db.commit()

    async def get_last_run(self, week_id: str | None = None) -> PipelineRun | None:
        query = "SELECT * FROM pipeline_runs"
        params: list = []
        if week_id:
            query += " WHERE week_id = ?"
            params.append(week_id)
        query += " ORDER BY started_at DESC LIMIT 1"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                steps = await self._get_steps_for_run(db, row["id"])
                return PipelineRun(
                    id=row["id"],
                    week_id=row["week_id"],
                    started_at=_str_to_dt(row["started_at"]),
                    finished_at=_str_to_dt(row["finished_at"]) if row["finished_at"] else None,
                    status=PipelineStatus(row["status"]),
                    total_input_tokens=row["total_input_tokens"],
                    total_output_tokens=row["total_output_tokens"],
                    estimated_cost_usd=row["estimated_cost_usd"],
                    steps=steps,
                )

    # ── Step Logs ──

    async def save_step_log(self, step: StepLog) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO step_logs
                   (id, run_id, agent, started_at, finished_at, status,
                    input_tokens, output_tokens, llm_model, details, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    step.id,
                    step.run_id,
                    step.agent,
                    _dt_to_str(step.started_at),
                    _dt_to_str(step.finished_at) if step.finished_at else None,
                    step.status,
                    step.input_tokens,
                    step.output_tokens,
                    step.llm_model,
                    step.details,
                    step.error,
                ),
            )
            await db.commit()

    async def _get_steps_for_run(
        self, db: aiosqlite.Connection, run_id: str
    ) -> list[StepLog]:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM step_logs WHERE run_id = ? ORDER BY started_at ASC",
            (run_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                StepLog(
                    id=r["id"],
                    run_id=r["run_id"],
                    agent=r["agent"],
                    started_at=_str_to_dt(r["started_at"]),
                    finished_at=_str_to_dt(r["finished_at"]) if r["finished_at"] else None,
                    status=r["status"],
                    input_tokens=r["input_tokens"],
                    output_tokens=r["output_tokens"],
                    llm_model=r["llm_model"],
                    details=r["details"],
                    error=r["error"],
                )
                for r in rows
            ]

    # ── Settings ──

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
            await db.commit()

    # ── Utilities ──

    @staticmethod
    def current_week_id() -> str:
        return _current_week_id()
