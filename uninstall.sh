#!/usr/bin/env bash
# Removes the SystemC Drovex (Java) installation. State files you created
# (.env, .history, .memory, .approvals, .profiles, workspace) are kept.
set -euo pipefail
PREFIX="${PREFIX:-$HOME/.systemc}"
rm -f "${HOME}/.local/bin/systemc"
rm -f "${HOME}/.local/share/applications/systemc.desktop"
rm -f "${HOME}/.local/share/icons/hicolor/256x256/apps/systemc.png"
rm -f "${HOME}/.local/share/icons/hicolor/scalable/apps/systemc.png"
rm -rf "$PREFIX"
echo "systemc uninstalled (app-menu entry + command removed; .env/.history/.memory were inside PREFIX and are gone)."
