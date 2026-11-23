@echo off
setlocal
cd /d "%~dp0"
echo [1/3] dependencies
python -m pip install -U pyinstaller psutil pywebview pystray "yt-dlp[default]" bgutil-ytdlp-pot-provider libtorrent || goto :err
echo [2/4] extension
python tools\build_extension.py || goto :err
echo [3/4] ffmpeg + deno (bundled into the exe)
if not exist build_cache\ffmpeg.exe (
  if not exist build_cache mkdir build_cache
  powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip' -OutFile 'build_cache\ffmpeg.zip'; Expand-Archive -Force 'build_cache\ffmpeg.zip' 'build_cache\unzip'" || goto :err
  copy /y "build_cache\unzip\ffmpeg-master-latest-win64-gpl\bin\ffmpeg.exe" "build_cache\ffmpeg.exe" >nul || goto :err
)
if not exist build_cache\deno.exe (
  if not exist build_cache mkdir build_cache
  powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip' -OutFile 'build_cache\deno.zip'; Expand-Archive -Force 'build_cache\deno.zip' 'build_cache\denozip'" || goto :err
  copy /y "build_cache\denozip\deno.exe" "build_cache\deno.exe" >nul || goto :err
)
set FFMPEG_FLAG=--add-binary "build_cache\ffmpeg.exe;." --add-binary "build_cache\deno.exe;."
echo [4/4] executable
python -m PyInstaller --noconfirm --clean --onefile --windowed --name qorvex --icon qorvex.ico ^
  --add-data "qorvex\ui.html;qorvex" ^
  --add-data "qorvex\logo.png;qorvex" ^
  --add-data "extension;extension" ^
  --hidden-import psutil ^
  --hidden-import libtorrent ^
  --collect-all yt_dlp ^
  --collect-all webview ^
  --collect-all pystray ^
  --collect-all yt_dlp_plugins ^
  --copy-metadata bgutil-ytdlp-pot-provider ^
  --collect-all pythonnet ^
  --collect-all clr_loader ^
  %FFMPEG_FLAG% ^
  --exclude-module tkinter ^
  run.py || goto :err
echo.
echo Done: dist\qorvex.exe (opens as a desktop window)
exit /b 0
:err
echo Build failed.
exit /b 1
