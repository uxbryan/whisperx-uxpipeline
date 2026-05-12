"""
core/db.py — SQLite job store.

Default DB path: ./data/jobs.db (overridable via DB_PATH env or config.json).
Single `jobs` table; status drives the pipeline state machine and is shared
between the web UI and the worker.

States:
    pending → transcribing → aligning → diarizing
           → classifying → polishing → done
    (any step can fall through to → failed)
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import ulid


STATUSES = {
    "pending", "transcribing", "aligning", "diarizing",
    "classifying", "polishing", "done", "failed",
}


def _default_db_path() -> Path:
    return Path(os.environ.get("DB_PATH", "./data/jobs.db")).expanduser()


@contextmanager
def _connect(db_path: Optional[Path] = None):
    p = (db_path or _default_db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Optional[Path] = None) -> None:
    """建表（idempotent）。Server 啟動時呼叫一次。"""
    with _connect(db_path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id              TEXT PRIMARY KEY,
                user_email      TEXT NOT NULL,
                original_name   TEXT NOT NULL,
                size_bytes      INTEGER,
                duration_sec    INTEGER,
                status          TEXT NOT NULL DEFAULT 'pending',
                progress_pct    INTEGER NOT NULL DEFAULT 0,
                error_msg       TEXT,
                nas_input_path  TEXT,
                nas_output_path TEXT,
                meeting_type    TEXT,
                options_json    TEXT,
                cost_ntd        REAL NOT NULL DEFAULT 0,
                created_at      DATETIME NOT NULL,
                updated_at      DATETIME NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_email, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        """)
        # Migration: 加 lifecycle 欄位（idempotent）
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "notified_at" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN notified_at DATETIME")
        if "archived_at" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN archived_at DATETIME")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_lifecycle "
                "ON jobs(status, created_at, archived_at)"
            )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_job(
    user_email: str,
    original_name: str,
    *,
    size_bytes: Optional[int] = None,
    duration_sec: Optional[int] = None,
    nas_input_path: Optional[str] = None,
    meeting_type: str = "other",
    options: Optional[dict] = None,
    db_path: Optional[Path] = None,
) -> str:
    """建立新任務，回傳 job_id (ULID)。"""
    job_id = str(ulid.new())
    now = _now_iso()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                id, user_email, original_name, size_bytes, duration_sec,
                status, progress_pct, nas_input_path, meeting_type,
                options_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?)
            """,
            (
                job_id, user_email, original_name, size_bytes, duration_sec,
                nas_input_path, meeting_type,
                json.dumps(options or {}, ensure_ascii=False),
                now, now,
            ),
        )
    return job_id


def update_status(
    job_id: str,
    status: str,
    *,
    progress_pct: Optional[int] = None,
    error_msg: Optional[str] = None,
    nas_output_path: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> None:
    """更新任務狀態。Pipeline 各 stage 進度時呼叫。"""
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status}")

    fields = ["status = ?", "updated_at = ?"]
    values: list = [status, _now_iso()]
    if progress_pct is not None:
        fields.append("progress_pct = ?")
        values.append(progress_pct)
    if error_msg is not None:
        fields.append("error_msg = ?")
        values.append(error_msg)
    if nas_output_path is not None:
        fields.append("nas_output_path = ?")
        values.append(nas_output_path)
    values.append(job_id)

    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?",
            values,
        )


def add_cost(job_id: str, delta_ntd: float, *, db_path: Optional[Path] = None) -> None:
    """累加 LLM 成本（NTD）。"""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET cost_ntd = cost_ntd + ?, updated_at = ? WHERE id = ?",
            (delta_ntd, _now_iso(), job_id),
        )


def get_job(job_id: str, *, db_path: Optional[Path] = None) -> Optional[dict]:
    """讀單一任務。"""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None


def list_jobs(
    *,
    user_email: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """列出任務，可依 email / status 過濾。預設按 created_at desc。"""
    sql = "SELECT * FROM jobs"
    conds, args = [], []
    if user_email:
        conds.append("user_email = ?"); args.append(user_email)
    if status:
        conds.append("status = ?"); args.append(status)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)

    with _connect(db_path) as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def claim_next_pending(*, db_path: Optional[Path] = None) -> Optional[dict]:
    """worker 用：拿一個 pending 任務並 atomic 標 transcribing。"""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'pending' "
            "ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE jobs SET status = 'transcribing', updated_at = ? WHERE id = ? AND status = 'pending'",
            (_now_iso(), row["id"]),
        )
        return dict(row)
