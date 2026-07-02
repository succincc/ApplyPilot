@echo off
REM JobPilot one-time setup for Windows. Run this once from the jobpilot folder.
setlocal

where python >nul 2>nul
if errorlevel 1 (
    echo Python not found. Install Python 3.10+ from https://python.org
    echo IMPORTANT: check "Add python.exe to PATH" in the installer.
    pause
    exit /b 1
)

echo === Installing JobPilot ===
python -m pip install -e . || goto :err

echo === Downloading Chromium for the apply step ===
python -m playwright install chromium || goto :err

echo === Creating your config files ===
python -m jobpilot init

echo.
echo === Setup complete ===
echo Now edit these three files in this folder:
echo   config.yaml   - your job search preferences (location, radius, salary)
echo   profile.yaml  - your personal info + path to your resume PDF
echo   answers.yaml  - your answers to common application questions
echo.
echo Then double-click run.bat (or run: jobpilot doctor)
pause
exit /b 0

:err
echo.
echo Setup failed - see the error above.
pause
exit /b 1
