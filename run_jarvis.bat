@echo off
rem run_jarvis.bat — start JARVIS under its guardian (auto-restart on crash).
rem If JARVIS is already running, the guardian will say so and exit.
cd /d "%~dp0"
if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" guardian.py
) else (
    python guardian.py
)
echo.
echo Guardian exited. Logs: logs\guardian.log and logs\jarvis.log
pause
