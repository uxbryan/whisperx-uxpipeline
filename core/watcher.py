"""
core/watcher.py — Filesystem watcher for the source/ folder.

Convention (drop files anywhere under SOURCE_DIR):
  <file>                            → user = 'local',  meeting_type = 'other'
  <user>/<file>                     → user = <user>,   meeting_type = 'other'
  <user>/interview/<file>           → user = <user>,   meeting_type = 'interview'
  <user>/lecture/<file>             → user = <user>,   meeting_type = 'lecture'
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileMovedEvent
from watchdog.observers import Observer

from . import db


SUPPORTED_EXT = {
    ".mp4", ".mov", ".mkv", ".webm",
    ".mp3", ".m4a", ".wav", ".flac", ".ogg",
}

MEETING_TYPE_FOLDERS = {"interview", "lecture", "other"}


def _classify_path(source_root: Path, file_path: Path) -> tuple[str, str]:
    """Resolve (user, meeting_type) from the drop-in path under source_root.

    Examples:
      [meeting.m4a]                       → ('local', 'other')
      [alice/meeting.m4a]                 → ('alice', 'other')
      [alice/interview/intv.mp3]          → ('alice', 'interview')
    """
    rel = file_path.relative_to(source_root)
    parts = rel.parts[:-1]

    if len(parts) == 0:
        return "local", "other"

    user = parts[0]
    meeting_type = "other"
    if len(parts) >= 2 and parts[1] in MEETING_TYPE_FOLDERS:
        meeting_type = parts[1]

    return user, meeting_type


def _wait_until_stable(path: Path, *, poll_sec: float = 1.0, stable_checks: int = 3) -> bool:
    """等檔案大小連續 N 次 poll 不變，視為傳輸完成。"""
    if not path.exists():
        return False
    last_size = -1
    stable_count = 0
    for _ in range(60):  # 最多等 60 秒
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size == last_size and size > 0:
            stable_count += 1
            if stable_count >= stable_checks:
                return True
        else:
            stable_count = 0
        last_size = size
        time.sleep(poll_sec)
    return False


class _Handler(FileSystemEventHandler):
    def __init__(self, source_root: Path, on_new_job: Callable[[str], None] | None = None):
        self.source_root = source_root
        self.on_new_job = on_new_job

    def on_created(self, event):
        if event.is_directory:
            return
        self._handle(Path(event.src_path))

    def on_moved(self, event):
        if event.is_directory:
            return
        # 跨目錄 mv 可能觸發此事件
        self._handle(Path(event.dest_path))

    def _handle(self, path: Path):
        if path.suffix.lower() not in SUPPORTED_EXT:
            return
        # 排除隱藏檔（macOS .DS_Store 等）
        if path.name.startswith("."):
            return

        if not _wait_until_stable(path):
            print(f"[watcher] file not stable, skip: {path}")
            return

        try:
            user, meeting_type = _classify_path(self.source_root, path)
        except ValueError:
            print(f"[watcher] cannot classify: {path}")
            return

        # Dedupe：若同一個 nas_input_path 已有 job（例如 web upload 已建），略過
        existing = db.list_jobs(limit=1000)
        if any(j.get("nas_input_path") == str(path) for j in existing):
            print(f"[watcher] already tracked, skip: {path.name}")
            return

        try:
            size = path.stat().st_size
        except OSError:
            size = None

        job_id = db.create_job(
            user_email=user,
            original_name=path.name,
            size_bytes=size,
            nas_input_path=str(path),
            meeting_type=meeting_type,
        )
        print(f"[watcher] new job {job_id}: {path.name} ({user}, {meeting_type})")
        if self.on_new_job:
            self.on_new_job(job_id)


def start_watcher(
    source_root: str | Path,
    *,
    on_new_job: Callable[[str], None] | None = None,
) -> Observer:
    """啟動 watchdog observer。回傳 observer，呼叫者可以 stop()/join()。"""
    root = Path(source_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)

    handler = _Handler(root, on_new_job=on_new_job)
    observer = Observer()
    observer.schedule(handler, str(root), recursive=True)
    observer.start()
    print(f"[watcher] watching {root}")
    return observer
