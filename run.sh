#!/bin/bash
# run.sh — Launch the server.
# Run from the repo root: ./run.sh
#
# If .env is missing, the server will redirect you to a /setup page in the
# browser to configure on-the-fly. No manual .env editing required.

set -e

G='\033[32m'; R='\033[31m'; B='\033[1m'; N='\033[0m'

if [ ! -d .venv ]; then
  echo -e "${R}✗ .venv missing. Run ./install.sh first.${N}"
  exit 1
fi

if [ ! -f .env ]; then
  echo -e "${B}No .env found.${N} The server will start in setup mode."
  echo "  Open http://localhost:8901 in your browser — it will guide you."
  echo
fi

echo -e "${G}${B}Starting server on http://127.0.0.1:8901${N}"
echo "  Press Ctrl+C to stop."
echo
.venv/bin/python web/server.py
