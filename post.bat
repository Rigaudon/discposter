@echo off
rem Runs post.py on Windows. Double-click to start, or run from cmd with the same
rem arguments as post.py, e.g.:  post.bat --once   /   post.bat -c thread "hi"
setlocal
cd /d "%~dp0"
rem Make Python read/print UTF-8 so emoji in messages work.
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

rem Prefer the "py" launcher; fall back to "python" on PATH.
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo Python was not found. Install Python 3.10+ from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)

%PY% -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul
if errorlevel 1 (
    echo Python 3.10 or newer is required. Found:
    %PY% --version
    pause
    exit /b 1
)

if not exist config.json (
    echo config.json not found. Copy config.example.json to config.json and fill it in.
    pause
    exit /b 1
)

%PY% post.py %*
set "RC=%ERRORLEVEL%"

rem Keep the window open on errors, or when double-clicked, so output can be read.
if not "%RC%"=="0" pause
if "%RC%"=="0" if "%~1"=="" pause
exit /b %RC%
