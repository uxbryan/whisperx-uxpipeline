"""
core/notify.py — Optional Slack notification via Incoming Webhook.

Set SLACK_WEBHOOK_URL in env to enable. When unset, notification is a no-op.

The webhook posts to whatever channel the webhook was created for.
For per-user DMs you'd need a full Slack bot setup — out of scope for OSS.
"""

from __future__ import annotations

import json
import os
from urllib.error import URLError
from urllib.request import Request, urlopen


def _job_url(job_id: str) -> str:
    base = os.environ.get("PUBLIC_URL", "http://localhost:8901")
    return f"{base}/jobs/{job_id}"


def notify_job(job: dict) -> None:
    """Post a Slack message about job completion / failure.

    Silently no-ops if SLACK_WEBHOOK_URL is unset.
    Errors are logged but never propagate.
    """
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        return

    status = job.get("status")
    name = job.get("original_name", "—")
    user = job.get("user_email", "anonymous")
    url = _job_url(job["id"])

    if status == "done":
        text = f":white_check_mark: Transcript ready: *{name}* ({user})\n<{url}|Open / Download>"
    elif status == "failed":
        err = (job.get("error_msg") or "Unknown error").splitlines()[0][:200]
        text = f":x: Transcript failed: *{name}* ({user})\nReason: `{err}`\n<{url}|Details>"
    else:
        return

    try:
        req = Request(
            webhook,
            data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urlopen(req, timeout=5)
    except (URLError, OSError) as e:
        print(f"[notify] webhook failed: {e}")
