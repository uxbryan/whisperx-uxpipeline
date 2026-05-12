# Architecture

A self-hosted audio/video transcription pipeline. One web service, one SQLite DB,
one filesystem-based job queue, one optional cloud LLM call at the end.

---

## Pipeline (7 steps, 3 cost zones)

```
🎬 audio/video file
   ↓
🏠 Local (free, 5/7 steps)
   1. WhisperX transcription          large-v3 / medium / small
   2. Word-level alignment            WhisperX align
   3. Speaker diarization             pyannote.audio
   4. Simplified → Traditional         OpenCC
   5. Per-speaker sample collection   first N utterances per speaker
   ↓
☁️ Anthropic Claude (~NT$1–3 per 30-min audio)
   6. Role classification              SPEAKER_XX → interviewer / lecturer / Speaker A
   7. Punctuation polish               Add punctuation, do NOT rewrite content
   ↓
📄 Output: 5 files in ./data/result/YYYY/MM/<job_id>/
```

80% of compute happens locally. Set `llm.enabled=false` in `config.json` to skip
steps 6–7 entirely (no Anthropic API calls).

---

## Components

```
                      ┌──────────────────┐
                      │   Browser        │
                      └──────────────────┘
                              │ HTTP
                              ▼
   ┌────────────────────────────────────────────────────────────┐
   │  Web server (port 8901)                                     │
   │                                                             │
   │  ┌─────────────────┐                                        │
   │  │ web/server.py    │ ─── /upload, /jobs, /jobs/{id}, /api/* │
   │  │ FastAPI + Jinja  │                                        │
   │  └────┬─────────────┘                                        │
   │       │ lifespan                                             │
   │       │                                                      │
   │  ┌────▼──────────┐    ┌──────────────────┐                  │
   │  │ core/watcher  │    │ core/worker      │                  │
   │  │ watchdog      │    │ poll pending     │                  │
   │  └───────────────┘    └──────┬───────────┘                  │
   │                              │ run_job(job_id)               │
   │                              ▼                               │
   │  ┌────────────────────────────────────────────────┐          │
   │  │ core/orchestrator                              │          │
   │  │   pipeline  → role_classify  → polish          │          │
   │  │   writer    → notify (webhook, optional)       │          │
   │  └────────────────────────────────────────────────┘          │
   │                                                              │
   │  core/db.py  (SQLite ./data/jobs.db)                         │
   └────┬──────────────────────────────────────────────┬──────────┘
        │                                              │
        ▼                                              ▼
   📁 ./data/source/<user>/<type>/<file>          ☁️ Anthropic API
   📁 ./data/result/YYYY/MM/<job_id>/                Claude Haiku 4.5
   📁 ./data/archive/                                (optional)
```

---

## Module roles

| File | Role |
|------|------|
| `web/server.py` | FastAPI entry point, routes, lifespan |
| `web/templates/*.html` | base / upload / jobs / job_detail |
| `core/pipeline.py` | Steps 1–5: WhisperX + alignment + pyannote + OpenCC + sample collection |
| `core/role_classify.py` | Step 6: classify SPEAKERS per meeting type |
| `core/polish.py` | Step 7: Haiku punctuation, chunked + parallel |
| `core/orchestrator.py` | End-to-end driver + progress ticker + DB state machine |
| `core/watcher.py` | Filesystem watcher for `./data/source/` |
| `core/worker.py` | Single worker thread, drains pending jobs |
| `core/writer.py` | Writes 5 output files per job |
| `core/db.py` | SQLite schema + helpers, includes 60d/90d lifecycle columns |
| `core/notify.py` | Optional Slack incoming-webhook notify |
| `core/llm.py` | Anthropic SDK wrapper + NTD cost estimator |
| `core/lifecycle.py` | 60-day notify, 90-day archive (run via cron) |
| `scripts/run_lifecycle.py` | CLI entry for lifecycle (use with cron / systemd timer) |

---

## Storage layout

Defaults (configurable in `config.json`):

```
./data/
├── source/<user>/<type>/<file>      ← uploads land here
├── result/YYYY/MM/<job_id>/         ← processed output
│   ├── transcript.polished.txt
│   ├── transcript.raw.txt
│   ├── segments.json
│   ├── speakers.json
│   └── meta.json
├── archive/YYYY/MM/<job_id>/        ← 90-day auto-archive
├── cache/aligned/<job_id>.json      ← WhisperX alignment cache
└── jobs.db                          ← SQLite

~/.cache/huggingface/                ← Whisper + pyannote model weights
```

For multi-user team deployment, replace `./data/` with an NFS / SMB mount.

---

## Job state machine

```
pending → transcribing → aligning → diarizing → classifying → polishing → done
                                                                            failed (any step)
```

Each transition updates `jobs.status` and `jobs.progress_pct`. A background
"ticker" thread updates `progress_pct` every 3s during transcribe/align/diarize
to give the UI smooth feedback (WhisperX doesn't expose mid-step progress).

---

## SQLite schema

```sql
CREATE TABLE jobs (
  id              TEXT PRIMARY KEY,   -- ULID, sortable by creation time
  user_email      TEXT NOT NULL,      -- in single-host mode, this is DEFAULT_USER
  original_name   TEXT NOT NULL,
  size_bytes      INTEGER,
  duration_sec    INTEGER,
  status          TEXT NOT NULL,
  progress_pct    INTEGER DEFAULT 0,
  error_msg       TEXT,
  nas_input_path  TEXT,
  nas_output_path TEXT,
  meeting_type    TEXT,                -- interview | lecture | other
  options_json    TEXT,                -- {"whisper_model": "medium", ...}
  cost_ntd        REAL DEFAULT 0,      -- estimated LLM cost (TWD)
  created_at      DATETIME,
  updated_at      DATETIME,
  notified_at     DATETIME,            -- 60-day warning sent
  archived_at     DATETIME             -- 90-day archive moved
);
```

Field name `nas_input_path` / `nas_output_path` is historic — they're just
filesystem paths now (not necessarily on a NAS).

---

## External dependencies

| Service | Purpose | Failure mode |
|---------|---------|--------------|
| **HuggingFace Hub** | First-time download of Whisper + pyannote models | Can't download new models; existing cache still works |
| **Anthropic API** | Steps 6–7 (role classification + punctuation) | Job fails at `polishing` if API unavailable. Set `llm.enabled=false` to skip |
| **ffmpeg** | Audio decoding (used by WhisperX internally) | Install via system package manager |

No telemetry. No background calls. Network only reaches HF (model download,
infrequent) and Anthropic (step 6–7 of each job).

---

## Performance characteristics

On Apple M4 Pro (CPU + int8), one-shot realtime factor for the full pipeline
(transcribe + align + diarize, excluding LLM):

| Model    | Realtime factor | 30-min audio ≈ |
|----------|----------------|----------------|
| small    | 0.10×          | ~3 min         |
| medium   | 0.25×          | ~7 min         |
| large-v3 | 0.50×          | ~15 min        |

LLM step (Haiku) is ~1 second per chunk × parallel = negligible.

GPU (CUDA / MPS where supported) gives 3–10× speedup, mainly on `large-v3`.

---

## Design decisions

| Decision | Reason |
|----------|--------|
| Default to `medium`, not `large-v3` | Large is 2–3× slower on CPU; medium is "good enough" for most Chinese transcripts. Important interviews can opt up. |
| `device=cpu, compute_type=int8` defaults | faster-whisper / ctranslate2 doesn't support MPS. CPU + int8 is the pragmatic best on Apple Silicon. CUDA users override in config.json. |
| ULID as job_id | URL-safe, time-sortable, no collisions, hard to guess (privacy) |
| 5 output files retained but UI shows 1 | The `polished.txt` is what humans want; the others are debug fallbacks if polish goes wrong |
| Progress ticker covers transcribe→diarize | Without it, UI sits at 10% for 5+ minutes during long transcripts and looks frozen |
| Single-threaded worker | Whisper saturates CPU on its own; running two concurrently is slower than serial |
| No auth | Self-host single-user. For team deployment, use a reverse proxy with HTTP basic / SSO. |
| OpenCC for s2t | Pure rules, instant, no model. Skip if you don't need Simplified→Traditional |

---

## Running in production

No deployment scripts shipped — they're OS-specific. Examples:

### Linux (systemd)

`/etc/systemd/system/whisperx-uxpipeline.service`:

```ini
[Unit]
Description=whisperx-uxpipeline
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/opt/whisperx-uxpipeline
ExecStart=/opt/whisperx-uxpipeline/.venv/bin/python web/server.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now whisperx-uxpipeline
```

### macOS (launchd)

Create `~/Library/LaunchAgents/com.local.whisperx-uxpipeline.plist` pointing to
`web/server.py` and use `launchctl load`.

### Docker

Not shipped. Contributions welcome.

### Lifecycle cron

Run `scripts/run_lifecycle.py` daily (cron / launchd / systemd timer):
- Notify owners of jobs older than 60 days
- Move jobs older than 90 days to `./data/archive/`

```
0 4 * * * cd /opt/whisperx-uxpipeline && .venv/bin/python scripts/run_lifecycle.py
```
