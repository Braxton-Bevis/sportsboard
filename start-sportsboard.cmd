@echo off
REM Sportsboard launcher (Windows)
REM Serves http://localhost:8080 - Ctrl+C to stop.
cd /d "%~dp0"

REM Unbuffered so the startup banner shows immediately in this window.
set PYTHONUNBUFFERED=1

REM ESPN's Akamai edge rejects spoofed browser User-Agents with 403.
REM config.json already sets an honest one; this is a belt-and-braces override.
if "%SPORTSBOARD_UA%"=="" set SPORTSBOARD_UA=Python-urllib/3.6

echo Starting Sportsboard on http://localhost:8080
echo (first %~n0 run redacts scores for 180s while the delay buffer fills)
echo.
python sportsboard.py
pause
