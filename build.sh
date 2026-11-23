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
mkdir -p build_cache
if [ "$(uname)" = "Darwin" ]; then
  DENO_URL="https://github.com/denoland/deno/releases/latest/download/deno-aarch64-apple-darwin.zip"
else
  DENO_URL="https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip"
fi
if [ ! -f build_cache/deno ]; then
  curl -fL -o build_cache/deno.zip "$DENO_URL" || echo "deno download failed: building without bundled JS runtime"
  unzip -o -q build_cache/deno.zip && cp deno build_cache/deno 2>/dev/null || true
  rm -f deno
fi
if [ "$(uname)" = "Darwin" ]; then
  if [ ! -f build_cache/ffmpeg ] && command -v brew >/dev/null 2>&1; then
    brew install ffmpeg || echo "brew ffmpeg failed: building without bundled ffmpeg"
    cp "$(brew --prefix ffmpeg)/bin/ffmpeg" build_cache/ffmpeg 2>/dev/null || true
  fi
elif [ ! -f build_cache/ffmpeg ]; then
  curl -fL -o build_cache/ffmpeg.tar.xz https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz
  tar -xf build_cache/ffmpeg.tar.xz -C build_cache
  cp build_cache/ffmpeg-master-latest-linux64-gpl/bin/ffmpeg build_cache/ffmpeg
fi
FFMPEG_FLAG=""
[ -f build_cache/ffmpeg ] && FFMPEG_FLAG="--add-binary build_cache/ffmpeg:."
[ -f build_cache/deno ] && FFMPEG_FLAG="$FFMPEG_FLAG --add-binary build_cache/deno:."
python tools/build_extension.py
python -m PyInstaller --noconfirm --clean --onefile --windowed --name qorvex \
  --add-data "qorvex/ui.html:qorvex" \
  --add-data "qorvex/logo.png:qorvex" \
  --add-data "extension:extension" \
  --hidden-import psutil \
  --hidden-import libtorrent \
  --collect-all yt_dlp \
  --collect-all webview \
  $FFMPEG_FLAG \
  --exclude-module tkinter \
  run.py
echo "Done: dist/qorvex (Linux) or dist/qorvex.app (macOS, use --onedir instead of --onefile)"
