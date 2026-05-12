# whisperx-uxpipeline

Self-hosted audio/video transcription pipeline. WhisperX transcription +
pyannote speaker diarization + OpenCC Simplified→Traditional Chinese +
optional Claude LLM punctuation polish + a simple FastAPI web UI.

🎙️ Drop a recording in → get a readable transcript with timestamps and
speaker labels.

> **Audience.** This is for developers, sysadmins, or anyone comfortable
> running a few Terminal commands. It's not a one-click install. If you
> just want to transcribe a meeting and don't want to touch a terminal,
> use a hosted service like Otter or Descript instead.

## What's in the box

- WhisperX transcription with three quality tiers (small / medium / large-v3)
- pyannote.audio speaker diarization
- OpenCC for Simplified → Traditional Chinese
- Optional Claude Haiku for role classification + punctuation polish
- Drop-folder watcher + Web upload — both feed the same job queue
- SQLite job store, no Redis/Postgres
- Optional Slack-webhook completion notifications

## Prerequisites

You need these installed on your machine before you start:

| Requirement | macOS | Linux |
|-------------|-------|-------|
| Python 3.10 or newer | `brew install python@3.13` | `apt install python3.13 python3.13-venv` |
| ffmpeg | `brew install ffmpeg` | `apt install ffmpeg` |
| Git | usually preinstalled | `apt install git` |
| HuggingFace account | sign up at <https://huggingface.co/join> |
| Anthropic API key *(optional)* | <https://console.anthropic.com/> |

## Install

```bash
git clone https://github.com/uxbryan/whisperx-uxpipeline.git
cd whisperx-uxpipeline
./install.sh
```

`install.sh` verifies Python/ffmpeg are installed, creates a virtual
environment in `.venv/`, and installs all dependencies. Takes 5–10
minutes (~2.5 GB of Python packages).

## Configure (one-time)

### 1. Get a HuggingFace token + accept gated model terms

Log in to HuggingFace, then **on both of these pages**, click "Agree
and access repository":

- <https://huggingface.co/pyannote/speaker-diarization-3.1>
- <https://huggingface.co/pyannote/segmentation-3.0>

Both models are open-source academic licenses; you're just agreeing to
citation requirements. Approval is instant.

Then create a token at <https://huggingface.co/settings/tokens>:
choose the **"Read"** classic type, name it anything, copy the `hf_...`
string immediately (HuggingFace only shows it once).

### 2. Get an Anthropic API key (optional)

Skip this step if you don't want LLM-polished punctuation. Otherwise:
get a key at <https://console.anthropic.com/>, add ~$5 credit
(lasts hundreds of jobs).

### 3. Create `.env`

```bash
cp .env.example .env
```

Open `.env` in your editor and fill in:

```
ANTHROPIC_API_KEY=sk-ant-...   # or remove this line if skipping LLM
HF_TOKEN=hf_...
LLM_ENABLED=true               # set false to skip LLM steps
```

Optional extras (defaults shown):
```
PORT=8901
HOST=127.0.0.1
SOURCE_DIR=./data/source
RESULT_DIR=./data/result
SLACK_WEBHOOK_URL=             # paste a Slack incoming-webhook URL to get completion DMs
```

### 4. (Optional) Verify setup before launching

```bash
.venv/bin/python scripts/check_setup.py
```

Tests every prerequisite (Python version, ffmpeg, packages, API key
validity, pyannote terms acceptance, storage permissions) and prints
✓ / ✗ with concrete fix instructions for each item.

## Run

```bash
./run.sh
```

Open <http://localhost:8901> in your browser.

## What happens on first upload

When you upload your first audio file, pyannote (~500 MB) and Whisper
(`medium`, ~770 MB) auto-download to `~/.cache/huggingface/`. Subsequent
uploads are fast.

## Upload paths

Two ways to feed the pipeline:

1. **Web UI** — drag-drop at <http://localhost:8901/upload>, pick type
   + quality
2. **Drop folder** — drop files under `./data/source/<user>/<type>/<file>`
   - `<type>` ∈ `interview` | `lecture` | `other` (controls speaker labels)
   - `<user>` is any subfolder name; used as the job "owner" in the UI

Both produce a job, picked up by a single worker (Whisper saturates CPU
on its own; running two concurrently is slower than serial).

## Output

Each job produces a folder at `./data/result/YYYY/MM/<job_id>/`:

| File | Content |
|------|---------|
| `transcript.polished.txt` | The transcript you actually want (timestamps, speaker labels, LLM-polished punctuation) |
| `transcript.raw.txt` | Same content pre-LLM. Useful as a fallback if the LLM rewrote something. |
| `segments.json` | Structured segments with word-level alignment |
| `speakers.json` | First N samples per speaker (input to role classification) |
| `meta.json` | Job metadata |

## Auth and deployment

This server ships with **no authentication**. It's designed for
single-host single-user use.

For team deployment, put it behind a reverse proxy (nginx, Caddy,
Cloudflare Tunnel) with HTTP basic auth or your SSO of choice. Do not
expose it directly to the public internet without auth.

## Performance

Apple M4 Pro, CPU + int8:

| Audio | small | medium | large-v3 |
|-------|-------|--------|----------|
| 5 min | ~30 s | ~1 min | ~2 min |
| 30 min | ~3 min | ~6 min | ~12 min |
| 1 hour | ~6 min | ~12 min | ~25 min |

GPU (CUDA where supported) gives 3–10× speedup, mainly on `large-v3`.

## License

MIT — see [`LICENSE`](LICENSE).

## Acknowledgments

Built on:
- [WhisperX](https://github.com/m-bain/whisperX) — Whisper + alignment + diarization
- [pyannote.audio](https://github.com/pyannote/pyannote-audio)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [OpenCC](https://github.com/yichen0831/opencc-python)
- [Anthropic Claude](https://www.anthropic.com/) for the punctuation step
