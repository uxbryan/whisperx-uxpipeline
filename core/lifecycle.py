"""
core/lifecycle.py — 歸檔生命週期

規則：
  - 60 天：Slack DM 通知擁有者「即將歸檔」（只通知一次）
  - 90 天：直接搬到 NAS archive/YYYY/MM/<job_id>/

只處理 status=done 的任務。failed / pending 不動。
從 created_at 起算（之後若加 last_accessed_at 再換）。

排程：launchd 每日凌晨 04:00 跑 scripts/run_lifecycle.py。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .db import _connect, _now_iso

NOTIFY_AGE_DAYS = 60
ARCHIVE_AGE_DAYS = 90


def _config() -> dict:
    cfg = Path(__file__).resolve().parent.parent / "config.json"
    with open(cfg, "r", encoding="utf-8") as f:
        return json.load(f)


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def find_jobs_to_notify(db_path=None) -> list[dict]:
    """status=done、created_at < now-60d、尚未通知過。"""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE status = 'done'
                 AND notified_at IS NULL
                 AND archived_at IS NULL
                 AND created_at < ?
               ORDER BY created_at""",
            (_cutoff(NOTIFY_AGE_DAYS),),
        ).fetchall()
        return [dict(r) for r in rows]


def find_jobs_to_archive(db_path=None) -> list[dict]:
    """status=done、created_at < now-90d、尚未歸檔。"""
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE status = 'done'
                 AND archived_at IS NULL
                 AND created_at < ?
               ORDER BY created_at""",
            (_cutoff(ARCHIVE_AGE_DAYS),),
        ).fetchall()
        return [dict(r) for r in rows]


def _mark_notified(job_id: str, db_path=None) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET notified_at = ? WHERE id = ?",
            (_now_iso(), job_id),
        )


def _mark_archived(job_id: str, new_path: str, db_path=None) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET archived_at = ?, nas_output_path = ? WHERE id = ?",
            (_now_iso(), new_path, job_id),
        )


def send_archive_warning(job: dict) -> None:
    """DM 通知擁有者：30 天後將歸檔。失敗靜默。"""
    from .notify import _resolve_slack_id, _slack_client, _job_url

    sid = _resolve_slack_id(job.get("user_email", ""))
    client = _slack_client()
    if not sid or not client:
        return

    days_left = ARCHIVE_AGE_DAYS - NOTIFY_AGE_DAYS  # 30
    text = (
        f":wave: 你 {NOTIFY_AGE_DAYS} 天前處理的逐字稿 *{job['original_name']}* "
        f"將於 {days_left} 天後（{ARCHIVE_AGE_DAYS} 天滿）歸檔。\n"
        f"若仍會用到，請下載備份：<{_job_url(job['id'])}|打開 →>"
    )
    try:
        client.chat_postMessage(channel=sid, text=text)
    except Exception as e:
        print(f"[lifecycle] notify failed for {job['id']}: {e}")


def archive_job(job: dict) -> tuple[bool, str]:
    """搬 result/.../<job_id>/ → archive/YYYY/MM/<job_id>/。

    回傳 (success, 新路徑 or 錯誤訊息)。
    若原資料夾已不存在，視為已搬走，標記 archived_at 並回 True。
    """
    cfg = _config()
    archive_root = Path(cfg["storage"]["archive_dir"]).expanduser()
    current_path = job.get("nas_output_path")
    if not current_path:
        return False, "no nas_output_path"

    src = Path(current_path)

    created = job["created_at"]
    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    dest = archive_root / f"{dt.year:04d}" / f"{dt.month:02d}" / job["id"]

    if not src.exists():
        # 已被人移走/刪除，標記 archived 不再追蹤
        _mark_archived(job["id"], str(dest))
        return True, f"source missing, marked archived: {dest}"

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)  # 重跑時清掉之前部分搬過去的
        shutil.move(str(src), str(dest))
        _mark_archived(job["id"], str(dest))
        return True, str(dest)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def run_daily() -> dict:
    """完整一輪：先通知、再歸檔。回傳 summary 給 log。"""
    summary = {"notified": [], "archived": [], "archive_errors": []}

    for job in find_jobs_to_notify():
        send_archive_warning(job)
        _mark_notified(job["id"])
        summary["notified"].append(job["id"])

    for job in find_jobs_to_archive():
        ok, info = archive_job(job)
        if ok:
            summary["archived"].append({"id": job["id"], "dest": info})
        else:
            summary["archive_errors"].append({"id": job["id"], "err": info})

    return summary
