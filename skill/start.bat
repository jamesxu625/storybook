@echo off
REM Picture Book Workbench launcher
REM %~dp0 expands to the skill\ directory (with trailing backslash)

set ROOT=%~dp0..
cd /d "%ROOT%"

echo.
echo   Starting Picture Book Workbench...
echo   Browser will open at http://localhost:5000
echo   Press Ctrl+C to stop.
echo.

start "" http://localhost:5000
python skill\app.py

pause
