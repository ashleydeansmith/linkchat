@echo off
rem ---------------------------------------------------------------------------
rem  LinkChat - setup
rem
rem  Run this once. It installs what LinkChat needs and puts a LinkChat icon on
rem  your desktop. It does not touch LinkedIn and it does not ask for a password.
rem ---------------------------------------------------------------------------
setlocal
echo.
echo   Setting up LinkChat. This takes a couple of minutes.
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo   Python is not installed.
  echo.
  echo   Get it free from https://www.python.org/downloads/
  echo   On the first screen of the installer, tick "Add python.exe to PATH".
  echo   Then run this file again.
  echo.
  pause
  exit /b 1
)

echo   [1 of 3] Installing what LinkChat needs...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r "%~dp0requirements.txt"
if errorlevel 1 goto failed

echo   [2 of 3] Setting up the browser LinkChat reads with...
python -m playwright install chromium
if errorlevel 1 goto failed

echo   [3 of 3] Putting LinkChat on your desktop...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\LinkChat.lnk');" ^
  "$py=(Get-Command pythonw).Source;" ^
  "$s.TargetPath=$py; $s.Arguments='-m engine desktop';" ^
  "$s.WorkingDirectory='%~dp0'.TrimEnd('\');" ^
  "$s.IconLocation='%~dp0web\public\favicon.ico,0';" ^
  "$s.Description='LinkChat'; $s.Save()"
if errorlevel 1 goto failed

echo.
echo   Done. There is now a LinkChat icon on your desktop.
echo   Double-click it. It will ask where your CRM folder is.
echo.
pause
exit /b 0

:failed
echo.
echo   That did not finish. Send Ashley the last few lines above.
echo.
pause
exit /b 1
