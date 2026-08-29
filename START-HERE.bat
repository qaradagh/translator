@echo off
REM gametrans - double-click launcher.
REM A menu so nothing has to be typed at a command prompt.

setlocal enabledelayedexpansion
cd /d "%~dp0"
chcp 65001 >nul 2>nul
title gametrans

if not exist ".venv\Scripts\activate.bat" (
    echo.
    echo   The app is not installed yet.
    echo   Run  setup-windows.bat  first, then come back here.
    echo.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

if not exist "config.toml" copy /y config.example.toml config.toml >nul 2>nul
if not exist ".env"        copy /y .env.example .env >nul 2>nul

:menu
cls
echo.
echo   ============================================================
echo                          g a m e t r a n s
echo             game subtitles, translated to Persian
echo   ============================================================
echo     folder: %CD%
echo   ============================================================
echo.
echo    SET UP
echo     1  -  Add or change an API key
echo     2  -  Check that everything is ready
echo     3  -  Choose the subtitle area on screen
echo.
echo     4  -  START TRANSLATING
echo.
echo    TUNE
echo     5  -  Test one translation (shows the real overlay)
echo     6  -  Change how the Persian text looks
echo     7  -  Compare local models (speed + Persian quality)
echo     8  -  Show which AI models are available
echo.
echo     9  -  Update to the newest version
echo     0  -  Exit
echo.
set "choice="
set /p "choice=  Type a number and press Enter: "

if "%choice%"=="1" goto key
if "%choice%"=="2" goto check
if "%choice%"=="3" goto region
if "%choice%"=="4" goto run
if "%choice%"=="5" goto testone
if "%choice%"=="6" goto settings
if "%choice%"=="7" goto compare
if "%choice%"=="8" goto models
if "%choice%"=="9" goto update
if "%choice%"=="0" exit /b 0
goto menu

:key
cls
echo.
echo   Which key do you want to add?
echo.
echo     1  -  Gemini   (best Persian quality)     https://aistudio.google.com/apikey
echo     2  -  Groq     (fastest, bigger daily limit)  https://console.groq.com/keys
echo.
echo     0  -  Back
echo.
set "which="
set /p "which=  Type a number and press Enter: "
echo.
if "%which%"=="1" python -m gametrans setkey gemini
if "%which%"=="2" python -m gametrans setkey groq
if "%which%"=="0" goto menu
echo.
pause
goto menu

:check
cls
echo.
python -m gametrans check
echo.
pause
goto menu

:region
cls
echo.
echo   Your screen will dim. Drag a box over the area where the
echo   game shows its subtitles, then let go. Press Esc to cancel.
echo.
pause
python -m gametrans pick-region
echo.
pause
goto menu

:run
cls
echo.
echo   Starting. Put the game in BORDERLESS WINDOWED mode.
echo.
echo     Ctrl+Alt+P   pause / resume
echo     Ctrl+Alt+H   hide / show the Persian text
echo     Ctrl+Alt+R   pick the subtitle area again
echo     Ctrl+Alt+Q   quit
echo.
python -m gametrans run
echo.
pause
goto menu

:testone
cls
echo.
set "sample="
set /p "sample=  Type an English sentence (or just press Enter for a sample): "
if "%sample%"=="" set "sample=You must reach the castle before nightfall, traveller."
echo.
echo   The Persian will look reversed in this black window - that is a
echo   limitation of the console, not the translation. A preview window
echo   will open showing how it really looks in game.
echo.
python -m gametrans translate --preview "%sample%"
echo.
pause
goto menu

:compare
cls
echo.
echo   This translates the same game lines with every local model you have
echo   installed, then opens a report so you can compare them side by side.
echo.
echo   Ollama must be running. Each model is loaded before timing starts, so
echo   loading is not counted against it - but there will be a wait before
echo   the first result appears.
echo.
pause
python -m gametrans compare-models
echo.
pause
goto menu

:models
cls
echo.
python -m gametrans models
echo.
pause
goto menu

:update
cls
echo.
REM A git clone can pull. A folder unzipped from GitHub cannot - it is not a
REM repository - so fall back to downloading and copying the current files.
REM Flat gotos rather than nested if-blocks: cmd parses a whole parenthesised
REM block before running it, which makes errorlevel checks inside one unreliable.
if not exist "%~dp0.git" goto update_zip
where git >nul 2>nul
if errorlevel 1 goto update_zip

echo   Downloading the newest version...
echo.
git pull
echo.
echo   Updating the installed files...
python -m pip install -e ".[windows,dxcam,hotkeys]" --quiet
echo.
echo   Done.
echo.
pause
goto menu

:update_zip
echo   This copy was unzipped rather than cloned, so it will be refreshed by
echo   downloading the current files from GitHub.
echo.
echo   Your API key, your settings and your cache are kept.
echo.
set "go="
set /p "go=  Continue? (y/n): "
if /i not "%go%"=="y" goto menu

REM cmd.exe re-reads this batch file from disk as it runs, so rewriting it
REM underneath a live process corrupts the rest of the run. Hand off to a
REM detached PowerShell and exit immediately.
if not exist "%~dp0tools\update.ps1" (
    echo.
    echo   This copy is too old to update itself - it predates the updater.
    echo.
    echo   Download it once by hand:
    echo     https://github.com/qaradagh/translator
    echo     green Code button  ^>  Download ZIP
    echo   Extract over this folder and choose Replace. Your key is kept.
    echo.
    pause
    goto menu
)

start "gametrans update" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\update.ps1" -ProjectDir "%~dp0."
exit /b 0

:settings
cls
echo.
python -m gametrans settings
echo.
pause
goto menu
