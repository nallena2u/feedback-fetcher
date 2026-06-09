#!/bin/bash
# Double-clickable launcher for the Feedback Fetcher.
# Closing this window stops the server.
cd "$(dirname "$0")"
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

echo "Starting Feedback Fetcher…"
"$PY" app.py
status=$?

echo
if [ $status -ne 0 ]; then
  echo "The server exited with an error (code $status) — see the messages above."
  echo "A common cause is port 8765 already being in use by another copy."
fi
echo "Server stopped. You can close this window."
# Keep the Terminal window open so any error stays visible.
read -r -p "Press Return to close…" _
