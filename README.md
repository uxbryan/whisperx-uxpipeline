# whisperx-uxpipeline

Self-hosted audio/video transcription with speaker diarization, Traditional Chinese
conversion, and LLM punctuation polish.

🎙️ Drop a recording in → get a readable transcript with timestamps and speaker labels.

## What's in the box

- **Web UI** for upload + job tracking (FastAPI + Bootstrap)
- **WhisperX** transcription with three quality tiers (small / medium / large-v3)
- **pyannote.audio** speaker diarization
- **OpenCC** for Simplified → Traditional Chinese (skip or swap if you don't need it)
- **Claude Haiku** for role classification and punctuation (optional, costs ~NT$1–3 per 30-min audio)
- **Drop-folder watcher** + Web upload — both routes converge to the same job queue
- **SQLite** job DB, no Redis/Postgres needed
- **Slack webhook** completion notifications (optional)

100% local pipeline except the last two steps (Claude API). Set `llm.enabled=false` in
`config.json` to skip those and get a raw timestamped transcript without sending text anywhere.

## Quick start

```bash
git clone https://github.com/uxbryan/whisperx-uxpipeline.git
cd whisperx-uxpipeline
```

**Option A — Setup wizard (recommended):** open
[**uxbryan.github.io/whisperx-uxpipeline/setup.html**](https://uxbryan.github.io/whisperx-uxpipeline/setup.html)
in your browser. It walks you through the API keys and writes a ready-to-use
`.env` file. The wizard runs 100% locally; nothing is sent anywhere.

**Option B — Manual:**

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# Edit .env: at minimum set ANTHROPIC_API_KEY and HF_TOKEN.
# HF_TOKEN: free at https://huggingface.co/settings/tokens
#           then visit https://huggingface.co/pyannote/speaker-diarization-3.1 to accept terms

.venv/bin/python web/server.py
# → open http://localhost:8901
```

First run downloads Whisper (~770 MB for `medium`) + pyannote models to `~/.cache/huggingface/`.

## Upload routes

Two ways to feed the pipeline:

1. **Web UI** — drag-drop at `http://localhost:8901/upload`, pick type + quality
2. **Drop folder** — drop files under `./data/source/<user>/<type>/<file>`
   - `<type>` ∈ `interview` | `lecture` | `other` (controls speaker labels)
   - `<user>` is whatever subfolder name; used as the "owner" in the UI

Both produce a job, picked up by a worker that processes one at a time (Whisper saturates CPU).

## Output

Each job creates a folder at `./data/result/YYYY/MM/<job_id>/` with:

| File | What |
|------|------|
| `transcript.polished.txt` | The transcript you actually want (timestamps, speaker labels, LLM-polished punctuation) |
| `transcript.raw.txt` | Same content but pre-LLM (no rewrites). Useful if you suspect the LLM messed something up. |
| `segments.json` | Structured segments with word-level alignment |
| `speakers.json` | First N samples per speaker (input to the role-classification LLM call) |
| `meta.json` | Job metadata |

## Configuration

Edit `config.json`:

```jsonc
{
  "service":     { "port": 8901, "host": "127.0.0.1" },
  "storage":     { "source_dir": "./data/source", "result_dir": "./data/result" },
  "whisper":     { "default_model": "medium", "device": "cpu", "compute_type": "int8" },
  "llm":         { "enabled": true, "model": "anthropic/claude-haiku-4-5" },
  "slack":       { "enabled": false }
}
```

Environment variables (see `.env.example`):

- `ANTHROPIC_API_KEY` — required if `llm.enabled` is true
- `HF_TOKEN` — required to first-download pyannote
- `SLACK_WEBHOOK_URL` — optional, completion notifications
- `PORT`, `HOST`, `DEFAULT_USER`, `DB_PATH`, `PUBLIC_URL` — overrides

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — pipeline internals, schema, decisions

## Auth and deployment

This server ships with **no authentication**. It's designed for single-host single-user use.

For team deployment:
- Put it behind a reverse proxy (nginx, Caddy, Cloudflare) with HTTP basic auth or SSO
- Or fork and add your auth layer of choice

Do NOT expose it directly to the public internet without auth.

## Performance

Tested on Apple M4 Pro (10 perf cores, CPU + int8):

| Audio | small | medium | large-v3 |
|-------|-------|--------|----------|
| 5 min | ~30s  | ~1 min | ~2 min   |
| 30 min| ~3 min| ~6 min | ~12 min  |
| 1 hour| ~6 min| ~12min | ~25 min  |

Linux x86_64 with int8 should be similar. With CUDA / MPS the gap widens (especially `large-v3`).

## License

MIT — see [`LICENSE`](LICENSE).

## Acknowledgments

Built on the shoulders of:

- [WhisperX](https://github.com/m-bain/whisperX) — Whisper + alignment + diarization
- [pyannote.audio](https://github.com/pyannote/pyannote-audio)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [OpenCC](https://github.com/yichen0831/opencc-python)
- [Anthropic Claude](https://www.anthropic.com/) for the punctuation step
