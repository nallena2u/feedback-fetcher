#!/bin/bash
# Build a double-clickable macOS app bundle (dist/Feedback Fetcher.app) with PyInstaller.
# Run this on a Mac. The resulting .app embeds Python + all dependencies, so
# recipients do NOT need to install Python or run pip.
#
#   ./build_mac_app.sh
#
# Note: the app is UNSIGNED. On first launch recipients must right-click the app
# and choose "Open" (then confirm) to get past macOS Gatekeeper. To avoid that
# warning entirely you'd need an Apple Developer ID certificate ($99/yr) and to
# codesign + notarize the bundle.
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY="python3"

echo "Using interpreter: $PY"
"$PY" -m pip install --upgrade pyinstaller
"$PY" -m pip install -r requirements.txt

# Start clean so a half-finished previous build can't leave stale output.
rm -rf build dist "Feedback Fetcher.spec"

# certifi is bundled so SSL works inside the frozen app; the scraper deps are
# collected so their data files come along.
"$PY" -m PyInstaller \
  --name "Feedback Fetcher" \
  --windowed \
  --onedir \
  --noconfirm \
  --add-data "feedback_fetcher.py:." \
  --collect-all google_play_scraper \
  --collect-all app_store_web_scraper \
  --collect-all certifi \
  app.py

# onedir+windowed emits BOTH dist/Feedback Fetcher.app (self-contained) and a
# redundant dist/Feedback Fetcher/ collect folder. Drop the folder to avoid
# confusion — the .app is the only thing to ship.
rm -rf "dist/Feedback Fetcher"

if [ ! -d "dist/Feedback Fetcher.app" ]; then
  echo "ERROR: build did not produce dist/Feedback Fetcher.app — check the log above." >&2
  exit 1
fi

# Package it for sending. ditto (not zip) preserves the bundle correctly.
( cd dist && ditto -c -k --sequesterRsrc --keepParent "Feedback Fetcher.app" "Feedback Fetcher.zip" )

echo
echo "Done."
echo "  App:  dist/Feedback Fetcher.app"
echo "  Zip:  dist/Feedback Fetcher.zip  (send this)"
echo
echo "Recipient: unzip, then RIGHT-CLICK the app → Open → Open (first launch only,"
echo "because the app is unsigned)."
