@echo off
REM JobPilot daily driver for Windows: preflight, discover, match, apply.
setlocal

echo === Preflight checks ===
python -m jobpilot doctor
if errorlevel 1 (
    echo.
    echo Fix the problems above, then run this again.
    pause
    exit /b 1
)

echo.
echo === Discover + match ===
python -m jobpilot run

echo.
echo === Apply (browser opens; solve any CAPTCHA it pauses on) ===
python -m jobpilot apply

echo.
python -m jobpilot status
pause
