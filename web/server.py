#!/usr/bin/env python3
"""
web/server.py — FastAPI entry point (no auth).

Routes:
  GET  /                    redirect to /upload
  GET  /upload              upload form + recent jobs
  POST /api/upload          multipart upload
  GET  /jobs                full job list
  GET  /jobs/{id}           job detail
  GET  /api/jobs            JSON job list
  GET  /api/jobs/{id}       JSON job detail
  GET  /api/jobs/{id}/download/{kind}   file download
  GET  /health              liveness

Lifespan:
  - init DB
  - start folder watcher (./data/source by default)
  - start worker thread (consumes pending jobs)

Single-host design: no authentication. If you deploy this for a team,
put it behind a reverse proxy (nginx/caddy) with HTTP basic auth or
your auth solution of choice.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"
sys.path.insert(0, str(BASE_DIR))

load_dotenv(ENV_PATH)

from core import db, watcher, notify  # noqa: E402
from core.worker import Worker  # noqa: E402

# ── Config ─────────────────────────────────────────────
with open(BASE_DIR / "config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

SOURCE_DIR = Path(os.path.expanduser(CONFIG["storage"]["source_dir"]))
SUPPORTED_EXT = set(CONFIG.get("supported_extensions", []))
PORT = int(os.environ.get("PORT", CONFIG["service"]["port"]))
HOST = os.environ.get("HOST", CONFIG["service"]["host"])
DEFAULT_USER = os.environ.get("DEFAULT_USER", "local")

_worker: Worker | None = None
_observer = None


# ── Setup mode detection ──────────────────────────────
def _needs_setup() -> bool:
    """First-run detection: .env missing or required keys absent."""
    if not ENV_PATH.exists():
        return True
    if not os.environ.get("HF_TOKEN") and not (Path.home() / ".cache" / "huggingface" / "token").exists():
        return True
    llm_enabled = os.environ.get("LLM_ENABLED", "true").lower() not in ("false", "0", "no")
    if llm_enabled and not os.environ.get("ANTHROPIC_API_KEY"):
        return True
    return False


class SetupRedirectMiddleware(BaseHTTPMiddleware):
    """When setup is needed, redirect all routes except /setup, /api/setup, /static, /health."""
    PUBLIC = ("/setup", "/api/setup", "/static", "/health", "/favicon")

    async def dispatch(self, request: Request, call_next):
        if _needs_setup() and not any(request.url.path.startswith(p) for p in self.PUBLIC):
            return RedirectResponse("/setup", status_code=302)
        return await call_next(request)


# ── Lifespan ──────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _worker, _observer
    db.init_db()
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)

    _worker = Worker(on_done=notify.notify_job)
    _worker.start()

    _observer = watcher.start_watcher(
        SOURCE_DIR,
        on_new_job=lambda jid: _worker.wake() if _worker else None,
    )

    yield

    if _observer:
        _observer.stop()
        _observer.join(timeout=5)
    if _worker:
        _worker.stop()


app = FastAPI(lifespan=lifespan, title="whisperx-uxpipeline")
app.add_middleware(SetupRedirectMiddleware)
app.mount("/static", StaticFiles(directory=BASE_DIR / "web" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "web" / "templates")


def _current_user(request: Request) -> str:
    """Single-host mode: everyone is DEFAULT_USER (default: 'local').
    For multi-tenant, replace with your auth layer (reverse proxy header, etc).
    """
    return os.environ.get("DEFAULT_USER", DEFAULT_USER)


# ── Routes ──────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"ok": True}


# ── Setup wizard (server-backed) ────────────────────────
@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    """Serve the wizard from the repo root. The wizard JS auto-detects
    that it's served via http and shows a 'Save & Continue' button that
    POSTs to /api/setup, instead of the copy/download fallback."""
    path = BASE_DIR / "setup.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.post("/api/setup")
async def api_setup(
    anthropic_api_key: str = Form(""),
    hf_token: str = Form(""),
    slack_webhook_url: str = Form(""),
    llm_enabled: str = Form("true"),
    port: str = Form(""),
    host: str = Form(""),
    source_dir: str = Form(""),
    result_dir: str = Form(""),
):
    """Receive form values from the wizard, write to .env, reload env vars."""
    lines = ["# Generated by /setup wizard"]
    if llm_enabled.lower() in ("false", "0", "no"):
        lines.append("LLM_ENABLED=false")
    elif anthropic_api_key.strip():
        lines.append(f"ANTHROPIC_API_KEY={anthropic_api_key.strip()}")
    if hf_token.strip():
        lines.append(f"HF_TOKEN={hf_token.strip()}")
    if slack_webhook_url.strip():
        lines.append(f"SLACK_WEBHOOK_URL={slack_webhook_url.strip()}")
    if port.strip() and port.strip() != "8901":
        lines.append(f"PORT={port.strip()}")
    if host.strip() and host.strip() != "127.0.0.1":
        lines.append(f"HOST={host.strip()}")
    if source_dir.strip() and source_dir.strip() != "./data/source":
        lines.append(f"SOURCE_DIR={source_dir.strip()}")
    if result_dir.strip() and result_dir.strip() != "./data/result":
        lines.append(f"RESULT_DIR={result_dir.strip()}")

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Reload env into the current process
    load_dotenv(ENV_PATH, override=True)

    return JSONResponse({
        "ok": True,
        "saved_to": str(ENV_PATH),
        "next": "/upload",
    })


@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse("/upload")


WHISPER_MODELS = CONFIG["whisper"].get("allowed_models", ["medium"])
WHISPER_DEFAULT = CONFIG["whisper"].get("default_model", "medium")


@app.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    user = _current_user(request)
    return templates.TemplateResponse(request, "upload.html", {
        "user": user,
        "supported_ext": sorted(SUPPORTED_EXT),
        "whisper_models": WHISPER_MODELS,
        "whisper_default": WHISPER_DEFAULT,
        "recent_jobs": db.list_jobs(user_email=user, limit=10),
    })


@app.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request):
    user = _current_user(request)
    jobs = db.list_jobs(user_email=user, limit=100)
    return templates.TemplateResponse(request, "jobs.html", {
        "user": user,
        "jobs": jobs,
    })


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail_page(request: Request, job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return templates.TemplateResponse(request, "job_detail.html", {
        "user": _current_user(request),
        "job": job,
    })


# ── API ───────────────────────────────────────────────
@app.post("/api/upload")
async def api_upload(
    request: Request,
    file: UploadFile = File(...),
    meeting_type: str = Form("other"),
    whisper_model: str = Form(""),
):
    if meeting_type not in ("interview", "lecture", "other"):
        raise HTTPException(400, f"Invalid meeting_type: {meeting_type}")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in SUPPORTED_EXT:
        raise HTTPException(400, f"Unsupported file type: {ext}")

    model = whisper_model if whisper_model in WHISPER_MODELS else WHISPER_DEFAULT

    user = _current_user(request)
    dest_dir = SOURCE_DIR / user / meeting_type
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (file.filename or "upload.bin")

    size = 0
    with open(dest, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
            size += len(chunk)

    job_id = db.create_job(
        user_email=user,
        original_name=file.filename or "upload.bin",
        size_bytes=size,
        nas_input_path=str(dest),
        meeting_type=meeting_type,
        options={"whisper_model": model},
    )
    return JSONResponse({
        "ok": True,
        "job_id": job_id,
        "stored": str(dest),
        "whisper_model": model,
    })


@app.get("/api/jobs")
async def api_jobs(request: Request):
    user = _current_user(request)
    jobs = db.list_jobs(user_email=user, limit=200)
    return {"jobs": jobs}


@app.get("/api/jobs/{job_id}")
async def api_job(request: Request, job_id: str):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.get("/api/jobs/{job_id}/download/{kind}")
async def api_download(request: Request, job_id: str, kind: str):
    file_map = {
        "polished": "transcript.polished.txt",
        "raw": "transcript.raw.txt",
        "segments": "segments.json",
        "speakers": "speakers.json",
        "meta": "meta.json",
    }
    if kind not in file_map:
        raise HTTPException(400, "Invalid kind")

    job = db.get_job(job_id)
    if not job or not job.get("nas_output_path"):
        raise HTTPException(404, "Output not ready")

    p = Path(job["nas_output_path"]) / file_map[kind]
    if not p.exists():
        raise HTTPException(404, "File not found")

    return FileResponse(p, filename=f"{job_id}_{file_map[kind]}")


# ── Run ───────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web.server:app", host=HOST, port=PORT, reload=False)
