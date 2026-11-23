@echo off
setlocal
cd /d "%~dp0"

if not exist "dist\qorvex.exe" (
  echo dist\qorvex.exe not found - run build.bat first.
  exit /b 1
)

set "INSTALL=%LOCALAPPDATA%\Qorvex"
echo Installing Qorvex to %INSTALL% ...

if not exist "%INSTALL%" mkdir "%INSTALL%"
copy /y "dist\qorvex.exe" "%INSTALL%\qorvex.exe" >nul || goto :err
copy /y "qorvex.ico" "%INSTALL%\qorvex.ico" >nul 2>&1

rem --- shortcuts (Start Menu + Desktop), icon = the Qorvex logo baked in the exe
powershell -NoProfile -Command ^
 "$ws = New-Object -ComObject WScript.Shell;" ^
 "$sm = [Environment]::GetFolderPath('Programs') + '\Qorvex';" ^
 "New-Item -ItemType Directory -Force -Path $sm | Out-Null;" ^
 "$l = $ws.CreateShortcut($sm + '\Qorvex.lnk');" ^
 "$l.TargetPath = '%INSTALL%\qorvex.exe';" ^
 "$l.IconLocation = '%INSTALL%\qorvex.ico,0';" ^
 "$l.WorkingDirectory = '%INSTALL%';" ^
 "$l.Description = 'Qorvex download manager';" ^
 "$l.Save();" ^
 "$desk = [Environment]::GetFolderPath('Desktop');" ^
 "$l2 = $ws.CreateShortcut($desk + '\Qorvex.lnk');" ^
 "$l2.TargetPath = '%INSTALL%\qorvex.exe';" ^
 "$l2.IconLocation = '%INSTALL%\qorvex.ico,0';" ^
 "$l2.WorkingDirectory = '%INSTALL%';" ^
 "$l2.Description = 'Qorvex download manager';" ^
 "$l2.Save()" || goto :err

rem --- uninstaller next to the app
> "%INSTALL%\uninstall.bat" (
  echo @echo off
  echo taskkill /IM qorvex.exe /F ^>nul 2^>^&1
  echo reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v Qorvex ^>nul 2^>^&1
  echo powershell -NoProfile -Command "Remove-Item -Recurse -Force ([Environment]::GetFolderPath('Programs') + '\Qorvex'), ([Environment]::GetFolderPath('Desktop') + '\Qorvex.lnk') -ErrorAction SilentlyContinue"
  echo timeout /t 1 /nobreak ^>nul
  echo start "" cmd /c "del /q \"%%~f0\" ^& rmdir /s /q \"%INSTALL%\""
)

echo Done.
echo   Start menu : Qorvex
echo   Desktop    : Qorvex
echo   Uninstall  : %INSTALL%\uninstall.bat
choice /M "Launch Qorvex now"
if errorlevel 2 exit /b 0
start "" "%INSTALL%\qorvex.exe"
exit /b 0

:err
echo Install failed.
exit /b 1
