"""
core/worker.py — 任務 worker loop

從 DB poll pending 任務，序列化處理（一次跑一個，WhisperX 已經吃滿 CPU）。
跑完通知（Slack）下一個自動接上。

啟動方式：
  - FastAPI lifespan 起背景 thread（單機 MVP）
  - 或 scripts/run_worker.py 獨立 process
"""

from __future__ import annotations

import threading
import time
import traceback

from . import db
from .orchestrator import run_job


class Worker:
    """單一 thread 的 job worker。"""

    def __init__(self, *, poll_interval: float = 2.0, on_done: callable | None = None):
        self.poll_interval = poll_interval
        self.on_done = on_done  # callback(job_dict): notify slack 等
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="vtt-worker", daemon=True
        )
        self._thread.start()
        print("[worker] started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        print("[worker] stopped")

    def wake(self) -> None:
        """新任務時的喚醒提示（目前用 poll 不用 event，留 hook 給未來）。"""
        pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = db.claim_next_pending()
            if not job:
                self._stop.wait(self.poll_interval)
                continue

            job_id = job["id"]
            print(f"[worker] processing {job_id}: {job['original_name']}")
            try:
                run_job(job_id)
                final = db.get_job(job_id)
                print(f"[worker] done {job_id}: status={final['status']}")
                if self.on_done and final:
                    try:
                        self.on_done(final)
                    except Exception as cb_err:
                        print(f"[worker] on_done callback error: {cb_err}")
            except Exception:
                print(f"[worker] exception in run_job({job_id}):")
                traceback.print_exc()
                # run_job 已經把狀態標 failed
                final = db.get_job(job_id)
                if self.on_done and final:
                    try:
                        self.on_done(final)
                    except Exception:
                        pass
