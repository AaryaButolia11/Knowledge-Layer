#!/usr/bin/env bash
# Fact Knowledge Layer — one-command setup + run.
set -e
cd "$(dirname "$0")"

python3 -m venv .venv 2>/dev/null || true
# shellcheck disable=SC1091
source .venv/bin/activate

echo "Installing dependencies…"
pip install -q --upgrade pip
pip install -q -r requirements.txt

# Build the demo knowledge base from the bundled sample PDFs (offline, no key).
echo "Seeding knowledge base from sample_pdfs/ …"
( cd backend && python seed_demo.py )

echo ""
echo "Starting server on http://localhost:8000  (Ctrl-C to stop)"
cd backend && uvicorn main:app --host 0.0.0.0 --port 8000
