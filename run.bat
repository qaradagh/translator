@echo off
REM gametrans - launcher. Picks the subtitle region on first run.

setlocal
cd /d "%~dp0"

if not exist ".venv" (
    echo Virtual environment not found. Run setup-windows.bat first.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

if not exist "config.toml" copy /y config.example.toml config.toml >nul

python -m gametrans check
if errorlevel 1 (
    echo.
    echo Fix the items marked [--] above, then run this again.
    pause
    exit /b 1
)

python -m gametrans run %*
