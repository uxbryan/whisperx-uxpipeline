#!/bin/bash
# run.sh — Launch the server.
# Run from the repo root: ./run.sh

set -e

G='\033[32m'; R='\033[31m'; B='\033[1m'; N='\033[0m'

if [ ! -d .venv ]; then
  echo -e "${R}✗ .venv missing. Run ./install.sh first.${N}"
  exit 1
fi

if [ ! -f .env ]; then
  echo -e "${R}✗ .env missing.${N} Copy .env.example to .env and fill in your tokens."
  echo "  See README for details."
  exit 1
fi

echo -e "${G}${B}Starting server on http://127.0.0.1:8901${N}"
echo "  Press Ctrl+C to stop."
echo
.venv/bin/python web/server.py
