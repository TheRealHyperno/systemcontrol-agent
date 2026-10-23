#!/usr/bin/env bash
# systemc installer (Linux/macOS)
#
# Creates a venv, installs Python deps, checks for tkinter (needed by the
# control panel GUI), and launches the control panel. Safe to re-run any
# time — it won't touch an existing .env or clobber your workspace/memory.

python3 version.py 2>/dev/null || echo "You are running a beta version of SystemC."
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

BOLD='\033[1m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${BOLD}==>${NC} $1"; }
warn()  { echo -e "${YELLOW}==>${NC} $1"; }
error() { echo -e "${RED}==>${NC} $1"; }

# ── 1. Find Python 3.10+ ─────────────────────────────────────────────────────
info "Looking for Python 3.10+..."
PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        ver=$("$candidate" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo "0.0")
        major=${ver%%.*}; minor=${ver##*.}
        if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON_BIN="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    error "Couldn't find Python 3.10 or newer."
    echo "  Fedora:  sudo dnf install python3"
    echo "  Debian/Ubuntu:  sudo apt install python3 python3-venv"
    echo "  macOS:   brew install python@3.12"
    exit 1
fi
info "Using $PYTHON_BIN ($("$PYTHON_BIN" --version))"

# ── 2. Check tkinter (needed for the control panel GUI) ─────────────────────
if ! "$PYTHON_BIN" -c "import tkinter" >/dev/null 2>&1; then
    warn "tkinter isn't available for $PYTHON_BIN — the control panel GUI needs it."
    echo "  Fedora:         sudo dnf install python3-tkinter"
    echo "  Debian/Ubuntu:  sudo apt install python3-tk"
    echo "  macOS (brew python):  brew install python-tk"
    echo "  Continuing anyway — bot.py and webui_server.py don't need tkinter,"
    echo "  only control_panel.py does."
fi

# ── 3. Create/reuse a venv ───────────────────────────────────────────────────
if [ ! -d venv ]; then
    info "Creating virtual environment in ./venv ..."
    "$PYTHON_BIN" -m venv venv
else
    info "Reusing existing ./venv"
fi

# shellcheck disable=SC1091
source venv/bin/activate

# ── 4. Install dependencies ──────────────────────────────────────────────────
info "Installing dependencies (discord.py, fastapi, uvicorn, httpx, ollama, stoat.py)..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
echo -e "${GREEN}Done.${NC}"

# ── 5. Ollama check (not installed by this script — separate download) ──────
if ! command -v ollama >/dev/null 2>&1; then
    warn "Ollama isn't installed / not on PATH. systemc talks to a local Ollama"
    echo "  server by default. Install it from https://ollama.com/download, or"
    echo "  set AGENT_PROVIDER=anthropic in .env to use the Claude API instead."
else
    info "Ollama found: $(ollama --version 2>/dev/null || echo 'installed')"
fi

# ── 6. Launch the control panel ──────────────────────────────────────────────
echo
info "Launching the control panel..."
echo "  (First run? Use the Setup tab to paste your Discord bot token, then"
echo "   Dependencies to pull an Ollama model, then Run && Logs to start.)"
echo
exec "$PYTHON_BIN" control_panel.py
