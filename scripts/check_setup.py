#!/usr/bin/env python3
"""
scripts/check_setup.py — Verify your installation before launching the server.

Run from the repo root:
    .venv/bin/python scripts/check_setup.py

Tests in order (stops at first hard blocker):
  1. .env file exists at repo root
  2. Python version >= 3.10
  3. ffmpeg + ffprobe installed
  4. Required Python packages importable
  5. ANTHROPIC_API_KEY: format + live API test (1 token)
  6. HF_TOKEN: present (env or HF CLI cache)
  7. HuggingFace gated model access: terms accepted for both pyannote models
  8. Storage dirs writable

Output: ✓ pass / ✗ fail / ⚠ warning, each with the next action to take.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
WARN = "\033[33m⚠\033[0m"
INFO = "\033[34m·\033[0m"


def header(s: str) -> None:
    print(f"\n\033[1m{s}\033[0m")


def ok(msg: str) -> None:
    print(f"  {PASS} {msg}")


def err(msg: str, hint: str = "") -> None:
    print(f"  {FAIL} {msg}")
    if hint:
        print(f"      → {hint}")


def warn(msg: str, hint: str = "") -> None:
    print(f"  {WARN} {msg}")
    if hint:
        print(f"      → {hint}")


def info(msg: str) -> None:
    print(f"  {INFO} {msg}")


_failures = 0
_warnings = 0


def fail(msg: str, hint: str = "") -> None:
    global _failures
    _failures += 1
    err(msg, hint)


def soft(msg: str, hint: str = "") -> None:
    global _warnings
    _warnings += 1
    warn(msg, hint)


# ── 1. .env ────────────────────────────────────────
def check_env_file() -> dict[str, str]:
    header("[1/8] .env file")
    if not ENV_PATH.exists():
        fail(f".env not found at {ENV_PATH}",
             "Copy .env.example to .env and fill in your tokens. See README.")
        return {}

    ok(f"found at {ENV_PATH}")
    values = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    info(f"loaded {len(values)} variables")
    return values


# ── 2. Python ──────────────────────────────────────
def check_python() -> None:
    header("[2/8] Python version")
    v = sys.version_info
    if v < (3, 10):
        fail(f"Python {v.major}.{v.minor} — need 3.10+",
             "Use Python 3.13: /opt/homebrew/bin/python3.13 -m venv .venv")
    else:
        ok(f"Python {v.major}.{v.minor}.{v.micro}")

    if not sys.prefix.endswith(".venv") and "venv" not in sys.prefix.lower():
        soft("not running inside a venv",
             f"You should run this with .venv/bin/python, not system Python.")


# ── 3. ffmpeg ──────────────────────────────────────
def check_ffmpeg() -> None:
    header("[3/8] ffmpeg / ffprobe")
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        if not path:
            fail(f"{tool} not found in PATH",
                 "macOS: brew install ffmpeg · Ubuntu: apt install ffmpeg")
        else:
            try:
                out = subprocess.run([tool, "-version"], capture_output=True, text=True, timeout=5)
                ver_line = out.stdout.splitlines()[0] if out.stdout else "?"
                ok(f"{tool} ({ver_line.split()[2] if len(ver_line.split()) > 2 else '?'}) at {path}")
            except Exception as e:
                soft(f"{tool} found but version probe failed: {e}")


# ── 4. Python packages ─────────────────────────────
def check_packages() -> None:
    header("[4/8] Python packages")
    required = [
        "whisperx", "pyannote.audio", "torch", "torchaudio",
        "opencc", "fastapi", "uvicorn", "watchdog",
        "anthropic", "ulid",  "dotenv", "jinja2",
    ]
    missing = []
    for pkg in required:
        try:
            __import__(pkg.replace(".audio", ".audio").replace("-", "_"))
            ok(pkg)
        except ImportError as e:
            missing.append(pkg)
            err(f"{pkg}", str(e))
    if missing:
        fail(f"{len(missing)} packages missing",
             ".venv/bin/pip install -r requirements.txt")


# ── 5. ANTHROPIC ───────────────────────────────────
def check_anthropic(env: dict) -> None:
    header("[5/8] Anthropic API key")
    llm_enabled = env.get("LLM_ENABLED", "true").lower() not in ("false", "0", "no")
    if not llm_enabled:
        info("LLM_ENABLED=false in .env — skipping (no API key needed)")
        return

    key = env.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        fail("ANTHROPIC_API_KEY not set in .env",
             "Add ANTHROPIC_API_KEY=sk-ant-... to .env, OR set LLM_ENABLED=false to skip LLM steps.")
        return
    if not key.startswith("sk-ant-"):
        fail("ANTHROPIC_API_KEY doesn't start with 'sk-ant-'",
             "Check you copied the full key from console.anthropic.com.")
        return
    ok(f"format looks valid ({key[:10]}...{key[-4:]})")

    # Live API test (1 token)
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=key)
        client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
        ok("live API test passed (Haiku 4.5 reachable)")
    except ImportError:
        soft("anthropic package not installed — skipping live test",
             ".venv/bin/pip install anthropic")
    except Exception as e:
        msg = str(e)
        if "authentication" in msg.lower() or "invalid" in msg.lower() or "401" in msg:
            fail("key rejected by Anthropic",
                 "Generate a new key at console.anthropic.com and update .env.")
        elif "credit" in msg.lower() or "billing" in msg.lower():
            fail("Anthropic account out of credit",
                 "Add credit at console.anthropic.com → Billing. $5 lasts hundreds of jobs.")
        else:
            fail(f"API call failed: {msg[:120]}")


# ── 6. HF_TOKEN ────────────────────────────────────
def _get_hf_token(env: dict) -> str | None:
    """Same fallback order as core/pipeline.py."""
    t = env.get("HF_TOKEN", "").strip()
    if t:
        return t
    cached = Path.home() / ".cache" / "huggingface" / "token"
    if cached.exists():
        try:
            return cached.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    return None


def check_hf_token(env: dict) -> str | None:
    header("[6/8] HuggingFace token")
    t = _get_hf_token(env)
    if not t:
        fail("no HF_TOKEN in .env and no cached token in ~/.cache/huggingface/token",
             "Get a Classic Read token at huggingface.co/settings/tokens, paste into .env as HF_TOKEN=hf_...")
        return None
    if not t.startswith("hf_"):
        soft(f"token doesn't start with 'hf_' ({t[:6]}...)",
             "Check it's a HuggingFace user access token, not something else.")
    source = "from .env" if env.get("HF_TOKEN") else "from ~/.cache/huggingface/token (CLI login)"
    ok(f"token found ({source}, {t[:8]}...{t[-4:]})")
    return t


# ── 7. Gated model access ──────────────────────────
def check_pyannote_access(token: str | None) -> None:
    header("[7/8] pyannote model terms acceptance")
    if not token:
        info("skipped (no HF token)")
        return

    models = [
        ("pyannote/speaker-diarization-3.1",
         "https://huggingface.co/pyannote/speaker-diarization-3.1"),
        ("pyannote/segmentation-3.0",
         "https://huggingface.co/pyannote/segmentation-3.0"),
    ]
    for model, page_url in models:
        api_url = f"https://huggingface.co/api/models/{model}"
        try:
            req = Request(api_url, headers={"Authorization": f"Bearer {token}"})
            with urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    ok(f"{model} — access granted")
                else:
                    soft(f"{model} — unexpected status {resp.status}")
        except HTTPError as e:
            if e.code == 401:
                fail(f"{model} — HTTP 401, token rejected",
                     "Make sure the token is a Classic 'Read' token and not revoked.")
            elif e.code == 403:
                fail(f"{model} — HTTP 403, terms not accepted by this account",
                     f"Visit {page_url} (logged in as the SAME account that owns this token) and click 'Agree and access repository'.")
            elif e.code == 404:
                fail(f"{model} — model not found (URL changed?)")
            else:
                soft(f"{model} — HTTP {e.code}: {e.reason}")
        except URLError as e:
            soft(f"{model} — network error: {e.reason}")
        except Exception as e:
            soft(f"{model} — {type(e).__name__}: {e}")


# ── 8. Storage ─────────────────────────────────────
def check_storage() -> None:
    header("[8/8] Storage directories")
    import json
    cfg_path = REPO_ROOT / "config.json"
    if not cfg_path.exists():
        fail("config.json missing")
        return
    cfg = json.loads(cfg_path.read_text())
    storage = cfg.get("storage", {})
    for key in ("source_dir", "result_dir"):
        path_str = storage.get(key, "")
        if not path_str:
            soft(f"{key} not configured in config.json")
            continue
        p = Path(os.path.expanduser(path_str))
        if not p.is_absolute():
            p = REPO_ROOT / p
        try:
            p.mkdir(parents=True, exist_ok=True)
            test_file = p / ".write_test"
            test_file.write_text("ok")
            test_file.unlink()
            ok(f"{key}: {p} (writable)")
        except OSError as e:
            fail(f"{key} ({p}): {e}",
                 "Pick a writable location in config.json → storage")


# ── main ───────────────────────────────────────────
def main() -> int:
    print("\033[1mwhisperx-uxpipeline · setup check\033[0m")
    print(f"repo: {REPO_ROOT}")

    env = check_env_file()
    if not env:
        print(f"\n\033[31mBlocked: fix .env first, then re-run.\033[0m")
        return 1

    check_python()
    check_ffmpeg()
    check_packages()
    check_anthropic(env)
    token = check_hf_token(env)
    check_pyannote_access(token)
    check_storage()

    print()
    if _failures == 0 and _warnings == 0:
        print("\033[32m\033[1mAll checks passed — ready to launch:\033[0m")
        print("    .venv/bin/python web/server.py")
        return 0
    if _failures == 0:
        print(f"\033[33m\033[1m{_warnings} warning(s)\033[0m — you can launch but expect issues:")
        print("    .venv/bin/python web/server.py")
        return 0
    print(f"\033[31m\033[1m{_failures} failure(s)\033[0m, {_warnings} warning(s) — fix the ✗ items above, then re-run me:")
    print(f"    .venv/bin/python scripts/check_setup.py")
    return 1


if __name__ == "__main__":
    sys.exit(main())
