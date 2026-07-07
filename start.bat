@echo off
rem dashtop launcher for Windows 10 — creates a local venv on first run.
cd /d "%~dp0"

if not exist .venv (
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo Python 3 was not found. Install it from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH" during setup.
        pause
        exit /b 1
    )
)

call .venv\Scripts\activate.bat
python -m pip install --quiet -r requirements.txt
python server.py %*
pause
