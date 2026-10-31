#!/usr/bin/env bash
# One command to run PayMates locally: ./run.sh   then open http://localhost:8000
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
source .venv/bin/activate
pip install -q -r backend/requirements.txt
echo ""
echo "  PayMates is starting → http://localhost:8000"
echo ""
cd backend && exec uvicorn app.main:app --host 0.0.0.0 --port 8000
