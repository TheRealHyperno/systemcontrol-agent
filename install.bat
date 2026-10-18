@echo off
REM systemc installer (Windows)
REM
REM Creates a venv, installs Python deps, and launches the control panel.
REM Safe to re-run any time - it won't touch an existing .env or clobber
REM your workspace/memory.

cd /d "%~dp0"
python version.py 2>nul
if errorlevel 1 echo You are running a beta version of SystemC.
echo ==^> Looking for Python 3.10+...
where python >nul 2>nul
if errorlevel 1 (
    echo ==^> Couldn't find "python" on PATH.
    echo     Install it from https://www.python.org/downloads/ ^(check
    echo     "Add python.exe to PATH" during setup^), then re-run this script.
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo ==^> Using python (%PYVER%)

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo ==^> Python 3.10+ is required ^(found %PYVER%^).
    echo     Install a newer version from https://www.python.org/downloads/
    pause
    exit /b 1
)

python -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo ==^> WARNING: tkinter isn't available - the control panel GUI needs it.
    echo     The official python.org installer includes it by default; if you
    echo     installed Python via the Microsoft Store, try the python.org
    echo     installer instead. Continuing anyway - bot.py and webui_server.py
    echo     don't need tkinter, only control_panel.py does.
)

if not exist venv (
    echo ==^> Creating virtual environment in .\venv ...
    python -m venv venv
) else (
    echo ==^> Reusing existing .\venv
)

call venv\Scripts\activate.bat

echo ==^> Installing dependencies ^(discord.py, fastapi, uvicorn, httpx, ollama, stoat.py^)...
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet
echo ==^> Done.

where ollama >nul 2>nul
if errorlevel 1 (
    echo ==^> Ollama isn't installed / not on PATH. systemc talks to a local
    echo     Ollama server by default. Install it from https://ollama.com/download,
    echo     or set AGENT_PROVIDER=anthropic in .env to use the Claude API instead.
) else (
    echo ==^> Ollama found on PATH.
)

echo.
echo ==^> Launching the control panel...
echo     ^(First run? Use the Setup tab to paste your Discord bot token, then
echo      Dependencies to pull an Ollama model, then Run ^&^& Logs to start.^)
echo.
python control_panel.py

echo.
echo Control panel closed.
pause
