#!/bin/sh
# Builds dist/qorvex (Linux or macOS single-file executable)
set -e
cd "$(dirname "$0")"
python -m pip install -U pyinstaller psutil pywebview "yt-dlp[default]"
python -m pip install -U libtorrent || echo "libtorrent unavailable: building without torrents"
python tools/build_extension.py
python -m PyInstaller --noconfirm --clean --onefile --windowed --name qorvex \
  --add-data "qorvex/ui.html:qorvex" \
  --add-data "qorvex/logo.png:qorvex" \
  --add-data "extension:extension" \
  --hidden-import psutil \
  --hidden-import libtorrent \
  --collect-all yt_dlp \
  --collect-all webview \
  --exclude-module tkinter \
  run.py
echo "Done: dist/qorvex"
