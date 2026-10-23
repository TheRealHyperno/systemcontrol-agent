"""
Terminal (TUI) control panel for SystemC Drovex — curses, stdlib-only.

Works over SSH or on a bare TTY with no desktop environment:

    python3 tui_panel.py [--profile <id>]

Keys:
    ↑/↓ or j/k : move      Enter : select / edit      q / Esc : back / quit
"""

from __future__ import annotations

import curses
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# ── .env read/write mirroring env_loader's helpers ───────────────────────────

def env_path(profile: str) -> Path:
    base = Path(os.environ.get("SYSTEMC_PROFILE_HOME") or HERE)
    if profile and profile != "default":
        p = base / ".profiles" / profile
        if p.is_dir():
            return p / ".env"
    return base / ".env"


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            env.setdefault(k.strip(), v)
    return env


def write_env(path: Path, key: str, value: str) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n")

# ── data model ───────────────────────────────────────────────────────────────

FIELDS_FRONTEND = [
    ("DISCORD_BOT_TOKEN", "Discord bot token", True),
    ("ALLOWED_CHANNEL_IDS", "Discord: allowed channel IDs (comma-sep)", False),
    ("STOAT_BOT_TOKEN", "Stoat bot token", True),
    ("STOAT_ALLOWED_CHANNEL_IDS", "Stoat: allowed channel IDs (comma-sep)", False),
    ("BOT_MODE", "Bot mode (wake / mention / always)", False),
    ("WAKE_WORD", "Wake word", False),
]
FIELDS_AGENT = [
    ("AGENT_MODEL", "Local Ollama model", False),
    ("OLLAMA_HOST", "Ollama host", False),
    ("OLLAMA_API_KEY", "Ollama API key (leave empty for local)", True),
    ("OLLAMA_API_KEYS", "Extra Ollama keys, comma-sep pool", True),
    ("AGENT_PROVIDER", "Cloud provider (empty = local only)", False),
    ("ANTHROPIC_API_KEY", "Anthropic API key", True),
    ("APPROVAL_MODE", "Approval mode (auto / manual)", False),
]
FIELDS_CONSOLE = [
    ("WEBUI_PORT", "Web UI port", False),
    ("SYSTEMC_API_TOKEN", "Web-UI auth token (empty = allow all on this IP)", True),
    ("SYSTEMC_WEBSITE", "Website the bots announce", False),
]
FIELDS_ML = [
    ("SUBAGENTS", "Subagent spec (empty = none)", False),
]

PILLARS = [
    ("Bots — Discord & Stoat front-ends", FIELDS_FRONTEND),
    ("Agent — model backend and approval style", FIELDS_AGENT),
    ("Console — web UI", FIELDS_CONSOLE),
    ("Subagents", FIELDS_ML),
]

# ── service actions (thin wrapper over systemd_service.py) ──────────────────

def service_run(action: str, profile: str = "default") -> None:
    script = HERE / "systemd_service.py"
    cmd = [sys.executable, str(script), action]
    if profile and profile != "default":
        cmd += ["--profile", profile]
    curses.def_prog_mode()
    curses.endwin()
    subprocess.call(cmd)
    print("\npress Enter…")
    sys.stdin.readline()
    curses.reset_prog_mode()
    curses.refresh()


def service_installed(profile: str) -> bool:
    name = "systemc.service" if profile in ("", "default") else f"systemc-{profile}.service"
    return (Path.home() / ".config/systemd/user" / name).exists()


def status_line(env: dict[str, str], profile: str) -> str:
    d = env.get("DISCORD_BOT_TOKEN", "")
    s = env.get("STOAT_BOT_TOKEN", "")
    bots = ("discord " if d else "") + ("stoat" if s else "") or "no bot token yet — configure Bots"
    unit = "systemc.service" if profile in ("", "default") else f"systemc-{profile}.service"
    svc = service_installed(profile) and "installed" or "service: no"
    return f" bots: {bots}   model: {env.get('AGENT_MODEL') or '(default)'}   " \
           f"provider: {env.get('AGENT_PROVIDER') or 'local'}   {svc} ({unit})   "


# ── terminal helpers ────────────────────────────────────────────────────────

def prompt_line(win, label: str, secret: bool, current: str) -> str:
    curses.curs_set(1)
    curses.echo(False)
    maxy = win.getmaxyx()[0]
    prow, irow = maxy - 2, maxy - 1
    win.move(prow, 0)
    win.clrtoeol()
    hint = f"  (Enter keeps: {current})" if current and not secret else ""
    win.addstr(prow, 0, (label or "") + hint)
    win.move(irow, 0)
    win.refresh()
    buf: list[str] = []
    while True:
        ch = win.getch()
        if ch in (curses.KEY_ENTER, 10, 13):
            break
        if ch == 27:
            buf = [current]
            break
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            if buf:
                buf.pop()
        elif 32 <= ch < 127 or ch > 160:
            buf.append(chr(ch))
        win.move(irow, 0); win.clrtoeol()
        shown = "".join(buf)
        win.addstr(irow, 0, shown if not secret else "•" * len(shown))
        win.refresh()
    curses.curs_set(0)
    return "".join(buf) if buf else current


def run_pillar(stdscr, title: str, fields: list, env: dict, path: Path) -> None:
    sel = 0
    while True:
        stdscr.erase()
        stdscr.addstr(0, 1, "System ⊙ C — SystemC Drovex (terminal)", curses.A_BOLD)
        stdscr.addstr(1, 1, title, curses.color_pair(2))
        stdscr.addstr(3, 1, "-" * (curses.COLS - 2))
        for i, (key, label, secret) in enumerate(fields):
            v = env.get(key, "")
            shown = "●" * min(20, max(6, len(v) // 8 + 6)) if (secret and v) else (v or "(default)")
            if i == sel:
                stdscr.addstr(4 + i, 1, " ▸ ", curses.A_BOLD)
            stdscr.addstr(4 + i, 4, f"{label:<42}", curses.A_REVERSE if i == sel else 0)
            stdscr.addstr(4 + i, 48, shown[: curses.COLS - 50],
                          curses.color_pair(3) if i == sel else (curses.color_pair(1) if not v else 0))
        stdscr.addstr(4 + len(fields) + 1, 1, "  ↑/↓ move · Enter edit · q/Esc back", curses.A_DIM)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q"), 27):
            return
        if ch in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % len(fields)
        if ch in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % len(fields)
        if ch in (10, 13, curses.KEY_ENTER):
            key, label, secret = fields[sel]
            current = env.get(key, "")
            v = prompt_line(stdscr, label, secret, current)
            if v != current:
                write_env(path, key, v)
                env[key] = v

# ── main menu ───────────────────────────────────────────────────────────────

def main(stdscr, profile: str = "default") -> None:
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_YELLOW, -1)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_GREEN, -1)
    except Exception:
        pass
    curses.curs_set(0)

    while True:
        path = env_path(profile)
        env = read_env(path)
        items = ["Bots       — Discord / Stoat tokens, wake word",
                 "Agent      — model backend, approvals",
                 "Console    — web UI port & token",
                 "Subagents  — extra helper agents",
                 "Startup    — systemd service (install/stop/remove)",
                 "Edit .env  — open in $EDITOR",
                 "Exit"]
        sel = 0
        while True:
            stdscr.erase()
            stdscr.addstr(0, 1, "System ⊙ C", curses.A_BOLD | curses.color_pair(2))
            stdscr.addstr(0, 12, "SystemC Drovex — terminal control panel", curses.A_DIM)
            stdscr.addstr(1, 1, status_line(env, profile), curses.color_pair(1))
            stdscr.addstr(3, 1, "-" * (curses.COLS - 2))
            for i, it in enumerate(items):
                mark, attr = ("▸", curses.A_REVERSE) if i == sel else (" ", curses.A_NORMAL)
                stdscr.addstr(4 + i, 1, f" {mark} {it}", attr)
            stdscr.addstr(4 + len(items) + 1, 1,
                          "  ↑/↓ move · Enter select · q quit" + (f"  · profile: {profile}" if profile != "default" else ""),
                          curses.A_DIM)
            stdscr.refresh()
            ch = stdscr.getch()
            if ch in (ord("q"), ord("Q"), 27):
                return
            if ch in (curses.KEY_DOWN, ord("j")):
                sel = (sel + 1) % len(items)
            if ch in (curses.KEY_UP, ord("k")):
                sel = (sel - 1) % len(items)
            if ch in (10, 13, curses.KEY_ENTER):
                if sel == 6:
                    return
                if sel <= 3:
                    run_pillar(stdscr, *PILLARS[sel], env=env, path=path)
                elif sel == 4:
                    service_menu(stdscr, profile)
                elif sel == 5:
                    curses.def_prog_mode(); curses.endwin()
                    if not path.exists():
                        example = HERE / "example.env"
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(example.read_text() if example.exists() else "")
                    subprocess.call([os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano", str(path)])
                    curses.reset_prog_mode(); curses.refresh()


def service_menu(stdscr, profile: str) -> None:
    items = ["",
             "Install — start the bots at login  (user unit)",
             "Uninstall the service",
             "Add linger — start at true boot, before login",
             "Status",
             "Back"]
    sel = 0
    while True:
        u = "systemc.service" if profile in ("", "default") else f"systemc-{profile}.service"
        st_installed = service_installed(profile)
        def ctl(a): return subprocess.run(["systemctl", "--user", a, u], capture_output=True, text=True).stdout.strip() or "(?)"
        stdscr.erase()
        stdscr.addstr(0, 1, f"Startup — systemd ({u})", curses.A_BOLD | curses.color_pair(2))
        stdscr.addstr(1, 1, f" installed: {'yes' if st_installed else 'no'}   "
                           f"enabled: {ctl('is-enabled') if st_installed else 'n/a'}   "
                           f"active: {ctl('is-active')}")
        stdscr.addstr(3, 1, "-" * (curses.COLS - 2))
        for i, it in enumerate(items[1:], 1):
            mark, attr = ("▸", curses.A_REVERSE) if i - 1 == sel else (" ", curses.A_NORMAL)
            stdscr.addstr(3 + i + 1, 1, f" {mark} {it}", attr)
        stdscr.refresh()
        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q"), 27):
            return
        if ch in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % (len(items) - 1)
        if ch in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % (len(items) - 1)
        if ch in (10, 13, curses.KEY_ENTER):
            if sel == 0:
                service_run("install", profile)
            elif sel == 1:
                service_run("uninstall", profile)
            elif sel == 2:
                import systemd_service
                subprocess.run(["loginctl", "enable-linger", os.environ.get("USER", "")],
                               capture_output=True)
                msg(stdscr, "lingering enabled — the bots start at boot, before login")
            elif sel == 3:
                msg(stdscr, "press Enter to show full status…")
                curses.def_prog_mode(); curses.endwin()
                subprocess.call(["systemctl", "--user", "status", u])
                print("\npress Enter…"); sys.stdin.readline()
                curses.reset_prog_mode(); curses.refresh()
            else:
                return


def msg(stdscr, text: str) -> None:
    stdscr.addstr(20, 1, text.strip().splitlines()[0][: curses.COLS - 3], curses.color_pair(3))
    stdscr.refresh()
    stdscr.nodelay(True)
    try:
        stdscr.getch()
    finally:
        stdscr.nodelay(False)


if __name__ == "__main__":
    profile = "default"
    if "--profile" in sys.argv:
        try:
            profile = sys.argv[sys.argv.index("--profile") + 1]
        except (ValueError, IndexError):
            pass
    curses.wrapper(main, profile)
