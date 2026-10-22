@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHON_CMD=py -3"
where py >nul 2>nul
if errorlevel 1 set "PYTHON_CMD=python"

if not exist ".venv\Scripts\python.exe" (
    echo [Downloader] Creating virtual environment...
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 goto :error
)

".venv\Scripts\python.exe" -c "import yt_dlp, imageio_ffmpeg" >nul 2>nul
if errorlevel 1 (
    echo [Downloader] Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 goto :error
)

echo [Downloader] Starting...
".venv\Scripts\python.exe" app.py --open
if errorlevel 1 goto :error
exit /b 0

:error
echo.
echo [Downloader] Could not start the application.
echo Make sure Python 3.10+ is installed and the internet connection works.
echo If port 8765 is busy, run manually:
echo   .venv\Scripts\python.exe app.py --port 8766 --open
echo.
pause
exit /b 1
