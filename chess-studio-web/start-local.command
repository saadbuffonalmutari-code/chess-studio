#!/bin/bash
set -e
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 was not found. Install Python 3 and run this file again."
  read -r -p "Press Return to close..." _
  exit 1
fi
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
printf "\nChess Studio Web is starting. Open Chrome at: http://localhost:8765\nPress Control-C here to stop the server.\n\n"
python server.py
