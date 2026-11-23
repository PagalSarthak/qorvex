@echo off
setlocal
cd /d "%~dp0"
echo [1/3] dependencies
python -m pip install -U pyinstaller psutil pywebview pystray "yt-dlp[default]" libtorrent || goto :err
echo [2/3] extension
python tools\build_extension.py || goto :err
echo [3/3] executable
python -m PyInstaller --noconfirm --clean --onefile --windowed --name qorvex --icon qorvex.ico ^
  --add-data "qorvex\ui.html;qorvex" ^
  --add-data "qorvex\logo.png;qorvex" ^
  --add-data "extension;extension" ^
  --hidden-import psutil ^
  --hidden-import libtorrent ^
  --collect-all yt_dlp ^
  --collect-all webview ^
  --collect-all pystray ^
  --collect-all pythonnet ^
  --collect-all clr_loader ^
  --exclude-module tkinter ^
  run.py || goto :err
echo.
echo Done: dist\qorvex.exe (opens as a desktop window)
exit /b 0
:err
echo Build failed.
exit /b 1
