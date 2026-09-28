@echo off
setlocal EnableExtensions
title Study Binder setup
echo.
echo   Setting up Study Binder. This takes about a minute...
echo   (Your notes are never deleted or changed by this setup.)
echo.

set "WORK=%TEMP%\study-binder-setup"
if exist "%WORK%" rmdir /s /q "%WORK%"
mkdir "%WORK%"

echo   Downloading the latest version...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $zip = Join-Path $env:WORK 'sb.zip'; Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/man1hutchinsom-dev/study-binder/archive/refs/heads/main.zip' -OutFile $zip; Expand-Archive -Force -Path $zip -DestinationPath $env:WORK"
if errorlevel 1 goto :nodownload

set "INSTALL=%WORK%\study-binder-main\desktop\install.py"
if not exist "%INSTALL%" goto :nodownload

rem Find Python (the same one your other apps use).
py -3 --version >nul 2>&1
if not errorlevel 1 (
  py -3 "%INSTALL%"
  goto :finished
)
python --version >nul 2>&1
if not errorlevel 1 (
  python "%INSTALL%"
  goto :finished
)
set "PY="
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY for /d %%D in ("%ProgramFiles%\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY goto :nopython
"%PY%" "%INSTALL%"

:finished
if errorlevel 1 goto :failed
rmdir /s /q "%WORK%" >nul 2>&1
echo   This window will close by itself.
timeout /t 10 >nul
exit /b 0

:nodownload
echo.
echo   Could not download Study Binder. Check you're connected to the internet, then try again.
echo.
pause
exit /b 1

:nopython
echo.
echo   Python wasn't found on this computer.
echo   Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this setup again.
echo.
pause
exit /b 1

:failed
echo.
echo   Setup didn't finish. Please send a photo of this window to Claude.
echo.
pause
exit /b 1
