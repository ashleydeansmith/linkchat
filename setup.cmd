@echo off
rem ###########################################################################
rem #                                                                        #
rem #  ON A MAC? THIS FILE IS NOT FOR YOU.                                   #
rem #                                                                        #
rem #  If you are reading this in TextEdit, you have double-clicked the      #
rem #  Windows installer. Close it and double-click setup-mac.command.       #
rem #                                                                        #
rem #  Not sure which computer you are on? Open Terminal in this folder      #
rem #  and type:    python3 doctor.py                                        #
rem #  It works it out and tells you which file to run.                      #
rem #                                                                        #
rem ###########################################################################
rem ---------------------------------------------------------------------------
rem  LinkChat - setup
rem
rem  Run this once. It installs what LinkChat needs and puts a LinkChat icon on
rem  your desktop. It does not touch LinkedIn and it does not ask for a password.
rem
rem  Two faults this file is written against, both found 2026-08-25:
rem
rem  1. Windows ships a stub called python.exe that is not Python. It sits in
rem     WindowsApps and its only job is to open the Microsoft Store. "where
rem     python" finds it and says yes. Running it opens a shop. So this asks
rem     Python for its version and only believes an answer that starts "Python 3".
rem
rem  2. This file used to find python one way and pythonw another way. A machine
rem     with two Pythons on it would install the parts into one and point the
rem     desktop icon at the other, and the icon would open nothing. The window
rem     is now started by the SAME Python the parts went into, asked for by name.
rem ---------------------------------------------------------------------------
setlocal
echo.
echo   Setting up LinkChat. This takes about five minutes, most of it downloading.
echo.

rem --- Is there a real Python here, or only the shop stub? -------------------
set "PYOK="
for /f "tokens=1,2" %%a in ('python --version 2^>^&1') do (
  if /i "%%a"=="Python" set "PYOK=%%b"
)
if not defined PYOK goto no_python

rem --- Is it new enough? Python 3.10 or later. -------------------------------
for /f "tokens=1,2 delims=." %%a in ("%PYOK%") do (
  set "MAJOR=%%a"
  set "MINOR=%%b"
)
if not "%MAJOR%"=="3" goto too_old
if %MINOR% LSS 10 goto too_old

echo   Found Python %PYOK%.
echo.

echo   [1 of 4] Installing the parts LinkChat needs...
python -m pip install --quiet --upgrade pip
if errorlevel 1 goto failed
python -m pip install --quiet -r "%~dp0requirements.txt"
if errorlevel 1 goto failed

echo   [2 of 4] Downloading the browser LinkChat reads with (about 150 MB)...
python -m playwright install chromium
if errorlevel 1 goto failed

echo   [3 of 4] Checking the parts actually landed...
python -c "import fastapi, uvicorn, pydantic, playwright, webview, psutil" 
if errorlevel 1 goto parts_missing

echo   [4 of 4] Putting LinkChat on your desktop...
rem  Ask THIS Python where its own windowless twin lives. Not a second search of
rem  the PATH, which is how the icon ends up pointing at a different Python.
for /f "delims=" %%p in ('python -c "import os,sys;w=os.path.join(os.path.dirname(sys.executable),'pythonw.exe');print(w if os.path.exists(w) else sys.executable)"') do set "PYW=%%p"
if not defined PYW goto failed

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\LinkChat.lnk');" ^
  "$s.TargetPath='%PYW%';" ^
  "$s.Arguments='-m engine desktop';" ^
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

:no_python
echo.
echo   Python is not installed on this computer.
echo.
echo   Windows may have a file called python.exe that only opens the Microsoft
echo   Store. That is not Python and LinkChat cannot use it.
echo.
echo   Get the real one, free, from https://www.python.org/downloads/
echo   On the FIRST screen of the installer, tick "Add python.exe to PATH".
echo   Then close this window, open a new one, and run this file again.
echo.
pause
exit /b 1

:too_old
echo.
echo   The Python on this computer is version %PYOK%. LinkChat needs 3.10 or later.
echo.
echo   Get a newer one, free, from https://www.python.org/downloads/
echo   On the FIRST screen of the installer, tick "Add python.exe to PATH".
echo   Then close this window, open a new one, and run this file again.
echo.
pause
exit /b 1

:parts_missing
echo.
echo   The parts installed but Python cannot find them. That usually means there
echo   is more than one Python on this computer and they went into the other one.
echo.
echo   Send the last few lines above, and the result of typing:  where python
echo.
pause
exit /b 1

:failed
echo.
echo   That did not finish. Send the last few lines above to whoever gave you this.
echo.
pause
exit /b 1
