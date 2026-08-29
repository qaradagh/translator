@echo off
REM gametrans - one-shot Windows setup.
REM Creates a virtualenv, installs the fast Windows stack, and copies the config.

setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python not found on PATH.
    echo Install Python 3.9+ from https://www.python.org/downloads/
    echo and tick "Add Python to PATH" during installation.
    pause
    exit /b 1
)

if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv || goto :failed
)

call .venv\Scripts\activate.bat

echo Installing gametrans and the Windows OCR/capture stack...
python -m pip install --upgrade pip >nul
python -m pip install -e ".[windows,dxcam,hotkeys]" || goto :failed

if not exist "config.toml" (
    copy /y config.example.toml config.toml >nul
    echo Created config.toml
)

if not exist ".env" (
    copy /y .env.example .env >nul
    echo Created .env
)

echo.
echo ============================================================
echo  Setup complete.
echo.
echo  From now on you do not need to type anything.
echo.
echo  Just double-click this file in the gametrans folder:
echo.
echo      START-HERE.bat
echo.
echo  It opens a menu: add your API key, pick the subtitle
echo  area, and start translating.
echo ============================================================
echo.
pause
exit /b 0

:failed
echo.
echo Setup failed. See the errors above.
pause
exit /b 1
