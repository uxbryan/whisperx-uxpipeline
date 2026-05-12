#!/bin/bash
# install.sh — One-time setup: create venv, install dependencies.
# Run from the repo root: ./install.sh
#
# What this does:
#   1. Verify Python 3.10+ and ffmpeg are installed
#   2. Create a Python virtual environment (.venv/) — keeps this project's
#      packages isolated from your system Python
#   3. Install all required packages (~2.5 GB, takes 5-10 min)
#
# Run once. After it finishes, use ./run.sh to launch the server.

set -e

# Colors
G='\033[32m'   # green
R='\033[31m'   # red
Y='\033[33m'   # yellow
B='\033[1m'    # bold
N='\033[0m'    # reset

echo -e "${B}whisperx-uxpipeline installer${N}"
echo

# ── Pick a python ────────────────────────────────────
PYTHON=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    VER=$("$cand" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
    MAJOR=$(echo "$VER" | cut -d. -f1)
    MINOR=$(echo "$VER" | cut -d. -f2)
    if [ "$MAJOR" -eq 3 ] && [ "$MINOR" -ge 10 ]; then
      PYTHON="$cand"
      echo -e "${G}✓${N} Python $VER ($(which "$cand"))"
      break
    fi
  fi
done
if [ -z "$PYTHON" ]; then
  echo -e "${R}✗ Python 3.10 or newer not found.${N}"
  echo "  macOS:  brew install python@3.13"
  echo "  Linux:  sudo apt install python3.13 python3.13-venv"
  echo "  Then re-run ./install.sh"
  exit 1
fi

# ── ffmpeg ───────────────────────────────────────────
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo -e "${R}✗ ffmpeg not found.${N}"
  echo "  macOS:   brew install ffmpeg"
  echo "  Ubuntu:  sudo apt install ffmpeg"
  echo "  Then re-run ./install.sh"
  exit 1
fi
echo -e "${G}✓${N} ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')"

# ── Venv ─────────────────────────────────────────────
if [ -d .venv ]; then
  echo -e "${Y}·${N} .venv already exists, reusing it"
else
  echo "  Creating .venv..."
  "$PYTHON" -m venv .venv
  echo -e "${G}✓${N} .venv created"
fi

# ── pip install ──────────────────────────────────────
echo
echo -e "${B}Installing Python packages (~2.5 GB, 5-10 min)...${N}"
echo "  This is the slow step. Go get coffee."
echo
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo -e "${G}✓${N} all packages installed"

# ── Final ────────────────────────────────────────────
echo
echo -e "${G}${B}Setup complete.${N}"
echo
echo -e "${B}Next: launch the server${N}"
echo "  ./run.sh"
echo
echo "  Then open http://localhost:8901 in your browser."
echo "  If .env is missing, the server will guide you through setup automatically."
