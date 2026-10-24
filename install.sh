#!/usr/bin/env bash
#   curl -fsSL https://therealhyperno.github.io/systemcontrol-java/install.sh | bash
#
# Downloads the latest SystemC Drovex (Java) release into ~/.systemc, puts the
# `systemc` command on your PATH and prints next steps.
#
# Options via env:  PREFIX=...  VERSION=v1.0.0  REPO=user/repo
set -euo pipefail

REPO="${REPO:-therealhyperno/systemcontrol-agent}"
PREFIX="${PREFIX:-$HOME/.systemc}"
VERSION="${VERSION:-}"

B="\033[1;36m"; G="\033[1;32m"; Y="\033[1;33m"; R="\033[1;31m"; N="\033[0m"
say()  { printf "${B}▸${N} %s\n" "$*"; }
warn() { printf "${Y}⚠${N} %s\n" "$*"; }
die()  { printf "${R}✗ %s${N}\n" "$*" >&2; exit 1; }

for bin in curl tar; do
  command -v "$bin" >/dev/null 2>&1 || die "need $bin"
done

if [ -n "$VERSION" ]; then
  API="https://api.github.com/repos/$REPO/releases/tags/$VERSION"
else
  API="https://api.github.com/repos/$REPO/releases/latest"
fi

ASSET_URL="${DISTRIBUTION_URL:-}"
if [ -z "$ASSET_URL" ]; then
  if [ -n "$VERSION" ]; then
    API="https://api.github.com/repos/$REPO/releases/tags/$VERSION"
    say "Looking up release $VERSION in $REPO…"
  else
    API="https://api.github.com/repos/$REPO/releases/latest"
    say "Looking up the latest release in $REPO…"
  fi
  ASSET_URL="$(curl -fsSL "$API" | grep -o 'https://[^"]*download[^"]*linux-x64\.tar\.gz' | head -n1)"
fi
[ -n "$ASSET_URL" ] || die "no linux-x64.tar.gz asset attached to that release"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ARCHIVE="$TMP/systemc-java-linux-x64.tar.gz"
say "Downloading $ASSET_URL"
curl -fSL --progress-bar -o "$ARCHIVE" "$ASSET_URL"

# sha256 (if the release published a .sha256 file)
if curl -fsSL "$ASSET_URL.sha256" -o "$TMP/sum" 2>/dev/null && [ -s "$TMP/sum" ]; then
  (cd "$TMP" && sha256sum -c <(awk '{print $1"  systemc-java-linux-x64.tar.gz"}' "$TMP/sum") >/dev/null 2>&1) \
    || die "sha256 checksum mismatch"
  say "sha256 verified"
else
  warn "release has no .sha256 file — checksum skipped"
fi

mkdir -p "$PREFIX"
tar -xzf "$ARCHIVE" -C "$PREFIX" --strip-components=1
chmod +x "$PREFIX/bin/systemc" 2>/dev/null || true
say "Installed into $PREFIX"

# ── backend choice: the only question is "do you want java or python" ─
BACKEND_ANSWER=""
if [ -r /dev/tty ]; then
  printf "Backend — run the java or the python agent? [java]: " > /dev/tty
  read -r BACKEND_ANSWER < /dev/tty || BACKEND_ANSWER=""
fi
BACKEND_ANSWER="$(echo "${BACKEND_ANSWER:-java}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
case "$BACKEND_ANSWER" in
  python)
    if "$PREFIX/bin/systemc" config set backend python; then :; else
      warn "python backend install failed — switch later with: systemc config set backend python"
    fi ;;
  java) : ;;   # default; recorded on first run / whenever .env is written
  *) warn "unknown backend '$BACKEND_ANSWER' (java|python) — using java" ;;
esac

BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
ln -sf "$PREFIX/bin/systemc" "$BIN_DIR/systemc"
say "Command linked: $BIN_DIR/systemc"

# ── app-menu entry (.desktop) ────────────────────────────────────────
if command -v desktop-file-install >/dev/null 2>&1 || [ -d "$HOME/.local/share/applications" ]; then
  APPS_DIR="$HOME/.local/share/applications"
  ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"; ICON_DIR2="$HOME/.local/share/icons/hicolor/scalable/apps"
  ICON_PATH="$PREFIX/share/systemc.png"
  [ -f "$ICON_PATH" ] && mkdir -p "$ICON_DIR" "${ICON_DIR2}" && cp "$ICON_PATH" "$ICON_DIR/systemc.png" && cp "$ICON_PATH" "$ICON_DIR2/systemc.png"
  mkdir -p "$APPS_DIR"
  cat > "$APPS_DIR/systemc.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=SystemC Control Panel
Comment=SystemC — bots, approvals, profiles
Exec=$BIN_DIR/systemc panel
Icon=systemc
Terminal=false
Categories=Utility;Development;
Keywords=systemc;agent;bot;ollama;
StartupWMClass=SystemC
Actions=console;run;

[Desktop Action console]
Name=Open chat console (web UI)
Exec=xdg-open http://127.0.0.1:7842
Icon=webbrowser-app
Terminal=false

[Desktop Action run]
Name=Start / stop the bots
Exec=$BIN_DIR/systemc run
Icon=utilities-terminal
Terminal=true
EOF
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" 2>/dev/null || true
  say "App-menu entry created ($APPS_DIR/systemc.desktop)"
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on your PATH — add this to your shell rc:"
     printf '    export PATH="%s:$PATH"\n' "$BIN_DIR" ;;
esac

if ! command -v java >/dev/null 2>&1 && ! ls /usr/lib/jvm/*/bin/java >/dev/null 2>&1; then
  warn "No Java found. On Fedora:  sudo dnf install java-21-openjdk"
fi

printf "${G} ✔${N} SystemC Drovex (Java) installed!\n\n"
cat <<EOF
  cd $PREFIX
  cp example.env .env      # then edit: bot token + model backend
  systemc run              # bots + web UI on http://127.0.0.1:7842
  systemc panel            # Swing control panel
  systemc models           # list your Ollama models
EOF
