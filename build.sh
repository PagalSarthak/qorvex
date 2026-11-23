#!/bin/sh
# Builds dist/qorvex (Linux) or dist/qorvex.app (macOS)
#
# Linux desktop window: pip install "pywebview[qt]" (bundles Qt, no system deps)
#   or install GTK webkit: sudo apt install python3-gi gir1.2-webkit2-4.1
# macOS: pyobjc is pulled in by pywebview automatically.
set -e
cd "$(dirname "$0")"
python -m pip install -U pyinstaller psutil pystray "yt-dlp[default]"
if [ "$(uname)" = "Darwin" ]; then
    python -m pip install -U pywebview
else
    python -m pip install -U "pywebview[qt]"
fi
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
echo "Done: dist/qorvex (Linux) or dist/qorvex.app (macOS, use --onedir instead of --onefile)"
