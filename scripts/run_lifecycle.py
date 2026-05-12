#!/usr/bin/env python3
"""
scripts/run_lifecycle.py — Daily lifecycle cron entry.

Run once a day (cron / systemd timer / launchd / etc) to:
  - Notify owners of jobs older than 60 days
  - Move jobs older than 90 days to ./data/archive/

Manual run:
    .venv/bin/python scripts/run_lifecycle.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from core import lifecycle, db


def main() -> int:
    db.init_db()
    summary = lifecycle.run_daily()

    ts = datetime.now().isoformat(timespec="seconds")
    msg = {
        "ts": ts,
        "notified_count": len(summary["notified"]),
        "archived_count": len(summary["archived"]),
        "archive_errors_count": len(summary["archive_errors"]),
        "details": summary,
    }
    print(json.dumps(msg, ensure_ascii=False, indent=2))

    if summary["archive_errors"]:
        return 1  # 讓 launchd 記錯
    return 0


if __name__ == "__main__":
    sys.exit(main())
