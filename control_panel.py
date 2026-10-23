"""
systemc control panel — one window to set it up, check dependencies,
and start/stop the Discord bot + local web UI, with live logs.

Run with:  python control_panel.py

Put this file in the same folder as bot.py / webui_server.py / .env.
"""

import os
import re
import sys
import time
import importlib
import shutil
import queue
import threading
import subprocess
import urllib.request
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog

import approvals as approvals_store
import i18n
import integrations
import integration_settings as settings
import prompt_template
import sessions
import account_setup
import runas
import updater
import version
import profiles
import profile_transfer
from integrations import _discord_admin_settings as das
from i18n import tr

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(APP_DIR, ".env")
PYTHON = sys.executable  # use the same interpreter this GUI is running under

# import-name -> pip package name
print(version.banner())
REQUIRED_PACKAGES = {
    "discord": "discord.py",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "httpx": "httpx",
    "ollama": "ollama",
    "stoat": "stoat.py",
}

C = dict(
    bg="#1e1e2e", panel="#313244", panel2="#45475a",
    accent="#89b4fa", text="#cdd6f4", subtext="#a6adc8",
    green="#a6e3a1", red="#f38ba8", yellow="#f9e2af",
)


# ── env file helpers ─────────────────────────────────────────────────────────

def load_env() -> dict:
    return settings.load_env(ENV_PATH)


def save_env(env: dict):
    settings.save_env(env, ENV_PATH)


env_list = settings.env_list


# ── ollama helpers ───────────────────────────────────────────────────────────

def check_ollama_installed() -> bool:
    return shutil.which("ollama") is not None


def check_ollama_running() -> bool:
    try:
        urllib.request.urlopen("http://localhost:11434", timeout=2)
        return True
    except Exception:
        return False


def get_installed_models() -> list[str]:
    try:
        r = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=5)
        lines = r.stdout.strip().splitlines()
        models = []
        for line in lines[1:]:
            parts = line.split()
            if parts:
                models.append(parts[0])
        return models
    except Exception:
        return []


# ── a managed subprocess with streamed output ────────────────────────────────

class ManagedProcess:
    """Wraps a Popen so the GUI can start/stop it and stream its output.

    If `auto_restart_var` is a tk.BooleanVar that's checked, an unexpected
    exit (crash) triggers an automatic restart after a short delay. A
    deliberate Stop click never triggers a restart.
    """

    def __init__(self, name: str, args: list[str], log_queue: queue.Queue,
                 auto_restart_var: "tk.BooleanVar | None" = None,
                 run_as=None):
        self.name = name
        self.args = args
        self.log_queue = log_queue
        self.auto_restart_var = auto_restart_var
        # Optional callable returning a username. When it names someone other than
        # the account running this panel, the process is launched as that user
        # (sudo/runuser) so the bot, its shell commands and its file I/O all run
        # as them -- see runas.py.
        self.run_as = run_as
        self._switched_user = ""
        self.proc = None
        self._reader_thread = None
        self.manual_stop = False

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _launch_args(self):
        """(args, error). Wraps self.args so they run as the configured user."""
        user = (self.run_as() if self.run_as else "") or ""
        user = user.strip()
        self._switched_user = ""
        if not runas.needs_switch(user):
            return list(self.args), None
        if not runas.can_switch(user):
            return None, runas.no_switch_message(user)
        if not runas.can_access(user, APP_DIR):
            # e.g. saving .env recreates it owner-only and drops the agent's ACL entry.
            # Re-grant (no password needed when this account owns the folder).
            runas.grant_agent_access(user, APP_DIR)
        py, warning = runas.resolve_python(user)
        if warning:
            self.log_queue.put((self.name, f"[warning] {warning}\n"))
        args = [py] + list(self.args[1:])
        self._switched_user = user
        return runas.wrap_command(
            args, user,
            extra_env={"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
        ), None

    def start(self):
        if self.is_running():
            return
        self.manual_stop = False
        args, err = self._launch_args()
        if err:
            self.log_queue.put((self.name, f"[{self.name} not started] {err}\n"))
            return
        self.log_queue.put((self.name, f"$ {' '.join(args)}\n"))
        self.proc = subprocess.Popen(
            args,
            cwd=APP_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            # Without this, print() output is block-buffered when piped and
            # never shows up in the Logs tab (only logging/stderr did).
            env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
        )
        self._reader_thread = threading.Thread(target=self._pump, daemon=True)
        self._reader_thread.start()

    def _pump(self):
        try:
            for line in self.proc.stdout:
                self.log_queue.put((self.name, line))
        except Exception:
            pass
        code = self.proc.wait() if self.proc else None
        self.log_queue.put((self.name, f"\n[{self.name} exited, code {code}]\n"))

        should_restart = (
            not self.manual_stop
            # A clean exit (code 0) isn't a crash — e.g. bot.py exits 0 on
            # purpose when it launches the setup wizard for missing config.
            # Restarting on that would spawn a fresh setup_gui.py every 3s
            # forever. Only auto-restart on an actual nonzero/crash exit.
            and code != 0
            and self.auto_restart_var is not None
            and self.auto_restart_var.get()
        )
        if should_restart:
            self.log_queue.put((self.name, f"[{self.name} crashed — restarting in 3s...]\n"))
            time.sleep(3)
            if not self.manual_stop:
                self.start()

    def stop(self):
        self.manual_stop = True
        if not self.is_running():
            return
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self._kill_as_user()
        except Exception:
            pass

    def _kill_as_user(self):
        """Killing the sudo wrapper can orphan the real process; finish it off."""
        if not self._switched_user or len(self.args) < 2:
            return
        try:
            subprocess.run(
                runas.wrap_command(["pkill", "-KILL", "-f", f" {self.args[1]}$"],
                                   self._switched_user),
                capture_output=True, timeout=5,
            )
        except Exception:
            pass


# ── main app ──────────────────────────────────────────────────────────────────

class ControlPanel(tk.Tk):
    # (integration file, submenu id) -> method that draws that submenu page by hand.
    # Optional: a submenu entry that declares "fields" gets a generated page (_page_fields),
    # and one with neither shows the "not wired up yet" stub.
    INTEGRATION_PAGES = {
        ("discord_admin", "admins"): "_page_discord_admins",
        ("discord_admin", "approvals"): "_page_discord_approvals",
    }

    def __init__(self):
        super().__init__()
        self.title("systemc control panel")  # translated in _apply_language
        self.geometry("980x680")
        self.configure(bg=C["bg"])

        self.log_queue: queue.Queue = queue.Queue()
        env = load_env()

        self.bot_auto_restart = tk.BooleanVar(value=True)
        self.webui_auto_restart = tk.BooleanVar(value=True)
        self.bot_proc = ManagedProcess("bot", [PYTHON, "bot.py"], self.log_queue,
                                        auto_restart_var=self.bot_auto_restart,
                                        run_as=self._run_as_user)
        self.webui_proc = ManagedProcess("webui", [PYTHON, "webui_server.py"], self.log_queue,
                                          auto_restart_var=self.webui_auto_restart,
                                          run_as=self._run_as_user)

        # ── state vars, seeded from existing .env if present ──
        # Which profile the Setup tab is currently editing. None = it was filled from the
        # global .env, so Save writes .env; set by _load_profile_into_ui so Save writes
        # that profile instead of leaking its values into the global .env.
        self._setup_pid = None
        self.token_var = tk.StringVar(value=env.get("DISCORD_BOT_TOKEN", ""))
        self.channels_var = tk.StringVar(value=env.get("ALLOWED_CHANNEL_IDS", ""))
        self.channels_all_var = tk.BooleanVar(value=not env.get("ALLOWED_CHANNEL_IDS", "").strip())
        self.mode_var = tk.StringVar(value=env.get("BOT_MODE", "WAKEWORD"))
        self.wake_var = tk.StringVar(value=env.get("WAKE_WORD", "hey claw"))
        self.model_var = tk.StringVar(value=env.get("AGENT_MODEL", "llama3.2"))
        self.agent_name_var = tk.StringVar(value=env.get("AGENT_NAME", ""))
        self.update_repo_var = tk.StringVar(value=env.get("UPDATE_REPO", "") or updater.DEFAULT_REPO)
        self.update_pre_var = tk.BooleanVar(
            value=env.get("UPDATE_PRERELEASE", "").lower() in ("1", "on", "true", "yes"))
        self._update_info = None
        self._update_busy = False
        self._update_q = queue.Queue()
        _workdir = env.get("AGENT_WORKDIR", "").strip()
        _run_as = env.get("AGENT_RUN_AS_USER", "").strip()
        _mode = env.get("AGENT_ACCOUNT_MODE", "").strip()
        default_dir = account_setup.get_default_agent_dir()
        if not _mode:
            if _run_as:
                _mode = "separate_user"
            elif _workdir in ("/home/agent", default_dir):
                _mode = "home_agent"
            elif not _workdir:
                _mode = "home_agent"
            else:
                _mode = "custom"
        self.account_mode_var = tk.StringVar(value=_mode)
        self.run_as_user_var = tk.StringVar(value=_run_as or "agent")
        self.allow_elevated_var = tk.BooleanVar(
            value=env.get("AGENT_ALLOW_ELEVATED", "").strip().lower() in ("1", "true", "yes", "on"))
        if not _workdir:
            _workdir = default_dir if _mode in ("home_agent", "separate_user") else os.path.join(APP_DIR, "workspace")
        self.workdir_var = tk.StringVar(value=_workdir)
        self.ollama_host_var = tk.StringVar(
            value=env.get("OLLAMA_HOST", "http://localhost:11434")
        )
        i18n.set_lang(env.get("UI_LANG") or i18n.detect_lang())
        # Ollama Cloud (https://ollama.com) — local or cloud, plus an API key
        _mode = env.get("OLLAMA_MODE", "").lower()
        if _mode not in ("local", "cloud"):
            _mode = "cloud" if (env.get("OLLAMA_API_KEY") or env.get("OLLAMA_API_KEYS")) else "local"
        self.ollama_mode_var = tk.StringVar(value=_mode)
        from providers import collect_api_keys
        self.api_keys = collect_api_keys(env)           # priority order: first = primary
        self.ollama_key_var = tk.StringVar(value="")    # the "add a key" entry box
        # Stoat
        self.stoat_token_var    = tk.StringVar(value=env.get("STOAT_BOT_TOKEN", ""))
        self.stoat_channels_var = tk.StringVar(value=env.get("STOAT_ALLOWED_CHANNEL_IDS", ""))
        self.stoat_channels_all_var = tk.BooleanVar(value=not env.get("STOAT_ALLOWED_CHANNEL_IDS", "").strip())
        self.stoat_mode_var     = tk.StringVar(value=env.get("STOAT_BOT_MODE", "WAKEWORD"))
        self.stoat_wake_var     = tk.StringVar(value=env.get("STOAT_WAKE_WORD", "hey claw"))

        self.stoat_auto_restart = tk.BooleanVar(value=True)
        self.stoat_proc = ManagedProcess("stoat", [PYTHON, "stoat_bot.py"], self.log_queue,
                                         auto_restart_var=self.stoat_auto_restart,
                                         run_as=self._run_as_user)
        self._build_ui()
        self.after(200, self._refresh_dependency_status)
        self.after(200, self._refresh_ollama_status)
        self.after(100, self._drain_log_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── layout ───────────────────────────────────────────────────────────────

    def _build_ui(self):
        hdr = tk.Frame(self, bg=C["panel"], pady=8)
        hdr.pack(fill="x")
        tk.Label(hdr, text="systemc control panel", font=("monospace", 13, "bold"),
                  bg=C["panel"], fg=C["text"]).pack()
        # Language dropdown, small, top-right corner
        self.lang_combo = ttk.Combobox(hdr, state="readonly", width=15,
                                       values=list(i18n.LANG_NAMES.values()))
        self.lang_combo.set(i18n.LANG_NAMES[i18n.get_lang()])
        self.lang_combo.place(relx=1.0, rely=0.5, x=-12, anchor="e")
        self.lang_combo.bind("<<ComboboxSelected>>", self._on_lang_change)

        style = ttk.Style(self)
        style.theme_use("default")
        # The sidebar is the navigation, so the notebook's own tab strip is
        # hidden (empty tab layout); nb.select()/nb.index() still work.
        style.layout("Hidden.TNotebook.Tab", [])
        style.configure("Hidden.TNotebook", background=C["bg"], borderwidth=0,
                         tabmargins=0, padding=0)
        style.configure("Treeview", background=C["panel"], fieldbackground=C["panel"],
                         foreground=C["text"], borderwidth=0)
        style.configure("Treeview.Heading", background=C["panel2"], foreground=C["text"])
        style.map("Treeview", background=[("selected", C["accent"])],
                   foreground=[("selected", C["bg"])])

        # Main layout: Claude-style left sidebar + right content
        main_pane = tk.Frame(self, bg=C["bg"])
        main_pane.pack(fill="both", expand=True)

        # ── Left Claude-style Sidebar ──
        self.sidebar = tk.Frame(main_pane, bg="#181820", width=190)
        self.sidebar.pack(side="left", fill="y", padx=(6, 0), pady=6)
        self.sidebar.pack_propagate(False)

        # Profile Switcher in Sidebar Top
        prof_box = tk.Frame(self.sidebar, bg="#24242e", padx=8, pady=8)
        prof_box.pack(fill="x", padx=6, pady=(6, 10))
        tk.Label(prof_box, text="ACTIVE PROFILE", font=("sans-serif", 8, "bold"), bg="#24242e", fg=C["accent"]).pack(anchor="w")
        
        self.sidebar_prof_combo = ttk.Combobox(prof_box, state="readonly", width=18)
        self.sidebar_prof_combo.pack(fill="x", pady=(4, 0))
        self._refresh_sidebar_profiles()
        self.sidebar_prof_combo.bind("<<ComboboxSelected>>", self._on_sidebar_profile_select)

        self.sidebar_nav_btns = {}
        nav_labels = [
            ("Profiles", 0),
            ("Setup", 1),
            ("Personalization", 2),
            ("Dependencies", 3),
            ("Run & Logs", 4),
            ("Approvals", 5),
            ("Integrations", 6),
            ("Sessions", 7),
            ("Updates", 8),
        ]
        nav_items = [(text, text, idx) for text, idx in nav_labels]

        def select_nav(idx):
            self.nb.select(idx)
            for k, btn in self.sidebar_nav_btns.items():
                if k == idx:
                    btn.config(bg=C["accent"], fg=C["bg"], activebackground=C["accent"],
                               font=("sans-serif", 10, "bold"))
                else:
                    btn.config(bg="#181820", fg=C["text"], activebackground=C["panel"],
                               font=("sans-serif", 10))

        for key, text, idx in nav_items:
            b = tk.Button(
                self.sidebar, text=text, anchor="w", padx=10, pady=7,
                bg="#181820", fg=C["text"], relief="flat", cursor="hand2",
                borderwidth=0, highlightthickness=0, activeforeground=C["text"],
                command=lambda i=idx: select_nav(i)
            )
            b.pack(fill="x", padx=6, pady=1)
            self.sidebar_nav_btns[idx] = b
            b.bind("<Enter>", lambda _e, i=idx, w=b: w.config(bg=C["panel"]) if self.nb.index("current") != i else None)
            b.bind("<Leave>", lambda _e, i=idx, w=b: w.config(bg="#181820") if self.nb.index("current") != i else None)

        # Bot status at bottom of sidebar
        sidebar_bottom = tk.Frame(self.sidebar, bg="#181820")
        sidebar_bottom.pack(side="bottom", fill="x", padx=6, pady=8)

        self.sidebar_running_lbl = tk.Label(
            sidebar_bottom, text="Bots Active: 0", font=("monospace", 8),
            bg="#24242e", fg=C["green"], pady=4
        )
        self.sidebar_running_lbl.pack(fill="x")

        # Right container for notebook
        content_box = tk.Frame(main_pane, bg=C["bg"])
        content_box.pack(side="right", fill="both", expand=True, padx=(6, 6), pady=6)

        nb = ttk.Notebook(content_box, style="Hidden.TNotebook")
        self.nb = nb
        nb.pack(fill="both", expand=True)

        # Tab 0: Profiles & Instances
        self.tab_profiles = tk.Frame(nb, bg=C["bg"])

        # Tab 1: Setup
        setup_outer = tk.Frame(nb, bg=C["bg"])
        setup_canvas = tk.Canvas(setup_outer, bg=C["bg"], highlightthickness=0)
        setup_bar = ttk.Scrollbar(setup_outer, orient="vertical", command=setup_canvas.yview)
        setup_canvas.configure(yscrollcommand=setup_bar.set)
        setup_bar.pack(side="right", fill="y")
        setup_canvas.pack(side="left", fill="both", expand=True)
        self.tab_setup = tk.Frame(setup_canvas, bg=C["bg"])
        setup_win = setup_canvas.create_window((0, 0), window=self.tab_setup, anchor="nw")
        self.tab_setup.bind("<Configure>",
                            lambda e: setup_canvas.configure(scrollregion=setup_canvas.bbox("all")))
        setup_canvas.bind("<Configure>",
                          lambda e: setup_canvas.itemconfigure(setup_win, width=e.width))

        def _wheel(e):
            if e.num == 4:
                setup_canvas.yview_scroll(-3, "units")
            elif e.num == 5:
                setup_canvas.yview_scroll(3, "units")
            else:
                setup_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

        def _bind_wheel(_e):
            for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                setup_canvas.bind_all(ev, _wheel)

        def _unbind_wheel(_e):
            for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                setup_canvas.unbind_all(ev)

        setup_canvas.bind("<Enter>", _bind_wheel)
        setup_canvas.bind("<Leave>", _unbind_wheel)
        self.tab_personal = tk.Frame(nb, bg=C["bg"])
        self.tab_deps = tk.Frame(nb, bg=C["bg"])
        self.tab_run = tk.Frame(nb, bg=C["bg"])
        self.tab_approvals = tk.Frame(nb, bg=C["bg"])
        self.tab_integrations = tk.Frame(nb, bg=C["bg"])
        self.tab_sessions = tk.Frame(nb, bg=C["bg"])
        self.tab_updates = tk.Frame(nb, bg=C["bg"])
        self.tab_experiments = tk.Frame(nb, bg=C["bg"])

        nb.add(self.tab_profiles, text="0. Profiles")
        nb.add(setup_outer, text="1. Setup")
        nb.add(self.tab_personal, text="Personalization")
        nb.add(self.tab_deps, text="2. Dependencies")
        nb.add(self.tab_run, text="3. Run && Logs")
        nb.add(self.tab_approvals, text="4. Approvals")
        nb.add(self.tab_integrations, text="5. Integrations")
        nb.add(self.tab_sessions, text="6. Sessions")
        nb.add(self.tab_updates, text="7. Updates")
        nb.add(self.tab_experiments, text="8. Experiments")

        self._build_profiles_tab()
        self._build_setup_tab()
        self._build_personalization_tab()
        self._build_deps_tab()
        self._build_run_tab()
        self._build_approvals_tab()
        self._build_integrations_tab()
        self._build_sessions_tab()
        self._build_updates_tab()
        self._build_experiments_tab()
        self._apply_language()

        select_nav(0)
        self.nb.bind("<<NotebookTabChanged>>", lambda e: select_nav(self.nb.index("current")))

    # ── language ────────────────────────────────────────────────────────────

    def _on_lang_change(self, _event=None):
        chosen = self.lang_combo.get()
        code = next((c for c, n in i18n.LANG_NAMES.items() if n == chosen), "en")
        i18n.set_lang(code)
        env = load_env()
        env["UI_LANG"] = code
        save_env(env)
        self._apply_language()

    def _apply_language(self):
        """Re-label every widget from its *current* text, so live status labels
        (e.g. running/stopped) keep their state when the language changes."""
        self.title(tr("systemc control panel"))
        fr = i18n.TRANSLATIONS["fr"]
        # any known text (English or translated) -> its English key
        to_en = {k: k for k in fr}
        for lang in i18n.TRANSLATIONS.values():
            for en, txt in lang.items():
                to_en.setdefault(txt, en)

        def walk(w):
            try:
                cur = w.cget("text")
            except Exception:
                cur = None
            if cur and cur in to_en:
                w.config(text=tr(to_en[cur]))
            for c in w.winfo_children():
                walk(c)

        walk(self)
        for i in range(self.nb.index("end")):
            cur = self.nb.tab(i, "text")
            if cur in to_en:
                self.nb.tab(i, text=tr(to_en[cur]))
        for col in ("id", "command", "age"):
            self.approval_tree.heading(col, text=tr(col.capitalize()))
        for col, en in self.SESSION_COLUMNS:
            self.session_tree.heading(col, text=tr(en))
        if hasattr(self, "keys_list"):
            self._refresh_keys_list()
        if hasattr(self, "ollama_status_lbl"):
            self._refresh_ollama_status()

    # ── profiles & multi-instance management ───────────────────────────────

    def _refresh_sidebar_profiles(self):
        try:
            profs = profiles.manager.list_profiles()
            active = profiles.manager.get_active_profile()
            values = [f"{p.name} ({p.id})" for p in profs]
            if hasattr(self, "sidebar_prof_combo"):
                self.sidebar_prof_combo["values"] = values
                if active:
                    self.sidebar_prof_combo.set(f"{active.name} ({active.id})")
        except Exception:
            pass

    def _on_sidebar_profile_select(self, _e=None):
        sel = self.sidebar_prof_combo.get()
        if not sel or "(" not in sel:
            return
        pid = sel.split("(")[-1].rstrip(")")
        self._switch_to_profile(pid)

    def _switch_to_profile(self, profile_id: str):
        profiles.manager.set_active_profile(profile_id)
        prof = profiles.manager.get_profile(profile_id)
        if prof:
            self._load_profile_into_ui(prof)
            self._sync_run_tab_to_profile()
            self._refresh_sidebar_profiles()
            if hasattr(self, "_refresh_profiles_list"):
                self._refresh_profiles_list()
            if hasattr(self, "int_scope_combo"):
                self._refresh_int_scope_choices()
                self._refresh_integrations()

    def _load_profile_into_ui(self, p):
        self._setup_pid = p.id
        self.token_var.set(p.discord_token)
        self.channels_var.set(p.discord_channels)
        self.channels_all_var.set(not p.discord_channels.strip())
        self.mode_var.set(p.discord_bot_mode)
        self.wake_var.set(p.discord_wake_word)
        self.model_var.set(p.agent_model)
        self.ollama_mode_var.set(p.ollama_mode)
        self.ollama_host_var.set(p.ollama_host)
        # "default" keeps the original workspace/ folder (see BotProfile.to_env_dict / start_instance)
        self.workdir_var.set(p.agent_workdir or (
            os.path.join(APP_DIR, "workspace") if p.id == "default"
            else os.path.join(APP_DIR, "workspace", "profiles", p.id)))
        self.account_mode_var.set(p.agent_account_mode)
        self.run_as_user_var.set(p.agent_run_as_user)
        self.allow_elevated_var.set(p.agent_allow_elevated == "1")
        self.stoat_token_var.set(p.stoat_token)
        self.stoat_channels_var.set(p.stoat_channels)
        self.stoat_channels_all_var.set(not p.stoat_channels.strip())
        self.stoat_mode_var.set(p.stoat_bot_mode)
        self.stoat_wake_var.set(p.stoat_wake_word)
        keys = ([p.api_key] if p.api_key else []) + list(p.api_keys)
        self.api_keys = list(dict.fromkeys(k for k in keys if k))
        if hasattr(self, "keys_list"):
            self._refresh_keys_list()
        self._on_ollama_mode_change()
        if hasattr(self, "prompt_text"):
            self._load_personalization(p)

    def _create_new_profile_dialog(self):
        dlg = tk.Toplevel(self)
        dlg.title("New Bot Profile")
        dlg.geometry("400x220")
        dlg.configure(bg=C["bg"])
        dlg.transient(self)
        dlg.grab_set()

        tk.Label(dlg, text="Create New Bot Profile", font=("sans-serif", 11, "bold"),
                 bg=C["bg"], fg=C["text"]).pack(anchor="w", padx=16, pady=(14, 8))

        name_var = tk.StringVar(value="New Bot")
        id_var = tk.StringVar(value=f"bot_{int(time.time()) % 1000}")

        f = tk.Frame(dlg, bg=C["bg"])
        f.pack(fill="x", padx=16)

        tk.Label(f, text="Display Name:", bg=C["bg"], fg=C["subtext"], font=("monospace", 9)).grid(row=0, column=0, sticky="w", pady=4)
        tk.Entry(f, textvariable=name_var, bg=C["panel"], fg=C["text"], relief="flat", width=26).grid(row=0, column=1, pady=4)

        tk.Label(f, text="Profile ID (slug):", bg=C["bg"], fg=C["subtext"], font=("monospace", 9)).grid(row=1, column=0, sticky="w", pady=4)
        tk.Entry(f, textvariable=id_var, bg=C["panel"], fg=C["text"], relief="flat", width=26).grid(row=1, column=1, pady=4)

        def save_new():
            pid = id_var.get().strip().lower()
            name = name_var.get().strip() or "Custom Bot"
            if not profiles.valid_profile_id(pid):
                messagebox.showerror("Error", "Profile ID must be 1-40 characters: lowercase letters, numbers, "
                                              "underscores or dashes (starting with a letter or number).", parent=dlg)
                return
            if profiles.manager.get_profile(pid):
                messagebox.showerror("Error", f"A profile with the ID '{pid}' already exists.", parent=dlg)
                return
            new_prof = profiles.BotProfile(
                id=pid,
                name=name,
                agent_workdir=os.path.join(APP_DIR, "workspace", "profiles", pid),
            )
            profiles.manager.save_profile(new_prof, set_active=True)
            self._refresh_sidebar_profiles()
            self._switch_to_profile(pid)
            dlg.destroy()
            messagebox.showinfo("Profile Created", f"Created profile '{name}' ({pid}). Configure its tokens and model in the Setup tab.")

        btn_row = tk.Frame(dlg, bg=C["bg"])
        btn_row.pack(fill="x", padx=16, pady=16)
        tk.Button(btn_row, text="Create Profile", command=save_new, bg=C["accent"], fg=C["bg"],
                  font=("sans-serif", 9, "bold"), relief="flat", padx=12, pady=4, cursor="hand2").pack(side="right")
        tk.Button(btn_row, text="Cancel", command=dlg.destroy, bg=C["panel2"], fg=C["text"],
                  relief="flat", padx=10, pady=4, cursor="hand2").pack(side="right", padx=6)

    # ── export / import profiles (logic lives in profile_transfer.py) ──────────

    @staticmethod
    def _transfer_detail(part, infos):
        """One-line size/count text for an export/import part, summed over `infos`."""
        if part in ("memory", "history", "workspace"):
            n = sum(i.get("files", 0) for i in infos)
            size = sum(i.get("bytes", 0) for i in infos)
            plus = "+" if any(i.get("capped") for i in infos) else ""
            return f"{n}{plus} file{'s' if n != 1 else ''}, {profile_transfer.human_size(size)}"
        n = sum(i.get("count", 0) for i in infos)
        if part == "secrets":
            return f"{n} saved"
        if part == "dependencies":
            return f"{n} package{'s' if n != 1 else ''}"
        return f"{n} enabled" if n else "settings"

    def _export_profiles_dialog(self):
        try:
            profs = profiles.manager.list_profiles()
            active_id = profiles.manager.get_active_profile().id
        except Exception as e:
            messagebox.showerror("Export", f"Couldn't read the profiles: {e}")
            return
        if not profs:
            messagebox.showinfo("Export", "There are no profiles to export.")
            return
        desc = {}
        for p in profs:
            try:
                desc[p.id] = profile_transfer.describe(p.id)
            except Exception:
                desc[p.id] = {"name": p.name, "parts": {}, "notes": {}}

        dlg = tk.Toplevel(self)
        dlg.title("Export Profiles")
        dlg.configure(bg=C["bg"])
        dlg.transient(self)
        dlg.grab_set()

        tk.Label(dlg, text="Export profiles", font=("sans-serif", 11, "bold"),
                 bg=C["bg"], fg=C["text"]).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(dlg, text="Saved as one .zip file. Settings and personalization are always included; "
                           "pick what else goes in.", bg=C["bg"], fg=C["subtext"], font=("monospace", 8),
                 wraplength=470, justify="left").pack(anchor="w", padx=16, pady=(0, 8))

        sel = {}
        box = tk.Frame(dlg, bg=C["panel"], padx=10, pady=8)
        box.pack(fill="x", padx=16, pady=4)
        tk.Label(box, text="Profiles", bg=C["panel"], fg=C["text"], font=("sans-serif", 9, "bold")).pack(anchor="w")
        part_widgets = {}
        part_vars = {part: tk.BooleanVar(value=False) for part in profile_transfer.PARTS}
        warn_var = tk.StringVar(value="")

        def refresh(*_):
            chosen = [pid for pid, v in sel.items() if v.get()]
            for part, cb in part_widgets.items():
                infos = [desc[pid]["parts"][part] for pid in chosen if part in desc[pid]["parts"]]
                label = profile_transfer.LABELS[part]
                if infos:
                    cb.config(text=f"{label}  —  {self._transfer_detail(part, infos)}", state="normal")
                else:
                    why = next((desc[pid]["notes"][part] for pid in chosen if part in desc[pid]["notes"]), "")
                    cb.config(text=f"{label}  —  nothing to export" + (f" ({why})" if why else ""),
                              state="disabled")
                    part_vars[part].set(False)
            risky = part_vars["secrets"].get() or part_vars["integrations"].get()
            warn_var.set("⚠ This file will contain secrets (bot tokens, API keys). Anyone who has it can "
                         "use your bots. Don't share it." if risky else "")

        for p in profs:
            sel[p.id] = tk.BooleanVar(value=(p.id == active_id))
            tk.Checkbutton(box, text=f"{p.name}  ({p.id})", variable=sel[p.id], command=refresh,
                           bg=C["panel"], fg=C["text"], selectcolor=C["bg"],
                           activebackground=C["panel"], anchor="w").pack(anchor="w")

        box2 = tk.Frame(dlg, bg=C["panel"], padx=10, pady=8)
        box2.pack(fill="x", padx=16, pady=4)
        tk.Label(box2, text="Include", bg=C["panel"], fg=C["text"], font=("sans-serif", 9, "bold")).pack(anchor="w")
        tk.Checkbutton(box2, text="Settings & personalization (always)", state="disabled",
                       bg=C["panel"], fg=C["subtext"], selectcolor=C["bg"], anchor="w").pack(anchor="w")
        for part in profile_transfer.PARTS:
            part_widgets[part] = tk.Checkbutton(
                box2, text=profile_transfer.LABELS[part], variable=part_vars[part], command=refresh,
                bg=C["panel"], fg=C["text"], selectcolor=C["bg"], activebackground=C["panel"], anchor="w")
            part_widgets[part].pack(anchor="w")

        tk.Label(dlg, text="Not exported: this computer's working folder, run-as user and elevation settings. "
                           "Workspace export skips caches (.cache, .local, node_modules, venvs).",
                 bg=C["bg"], fg=C["subtext"], font=("monospace", 8), wraplength=470, justify="left"
                 ).pack(anchor="w", padx=16, pady=(4, 0))
        tk.Label(dlg, textvariable=warn_var, bg=C["bg"], fg=C["red"], font=("monospace", 8, "bold"),
                 wraplength=470, justify="left").pack(anchor="w", padx=16, pady=(4, 0))

        def do_export():
            ids = [pid for pid, v in sel.items() if v.get()]
            if not ids:
                messagebox.showwarning("Export", "Pick at least one profile.", parent=dlg)
                return
            parts = {k for k, v in part_vars.items() if v.get()}
            path = filedialog.asksaveasfilename(
                parent=dlg, title="Export profiles to…", defaultextension=".zip",
                initialfile=(ids[0] if len(ids) == 1 else "systemc-profiles") + ".zip",
                filetypes=[("SystemC profile (zip)", "*.zip"), ("All files", "*")])
            if not path:
                return
            export_btn.config(state="disabled", text="Exporting…")

            def work():
                res = err = None
                try:
                    res = profile_transfer.export_profiles(ids, parts, path)
                except profile_transfer.TransferError as e:
                    err = str(e)
                except Exception as e:
                    err = f"Unexpected error: {e}"
                self.after(0, lambda: done(res, err))

            def done(res, err):
                if err:
                    export_btn.config(state="normal", text="Export…")
                    messagebox.showerror("Export failed", err, parent=dlg)
                    return
                dlg.destroy()
                messagebox.showinfo(
                    "Export complete",
                    f"Exported {len(res['profiles'])} profile(s): {res['files']} data file(s), "
                    f"{profile_transfer.human_size(res['bytes'])}.\n\n{res['path']}")

            threading.Thread(target=work, daemon=True).start()

        btn_row = tk.Frame(dlg, bg=C["bg"])
        btn_row.pack(fill="x", padx=16, pady=14)
        export_btn = tk.Button(btn_row, text="Export…", command=do_export, bg=C["accent"], fg=C["bg"],
                               font=("sans-serif", 9, "bold"), relief="flat", padx=12, pady=4, cursor="hand2")
        export_btn.pack(side="right")
        tk.Button(btn_row, text="Cancel", command=dlg.destroy, bg=C["panel2"], fg=C["text"],
                  relief="flat", padx=10, pady=4, cursor="hand2").pack(side="right", padx=6)
        refresh()

    def _import_profiles_dialog(self):
        path = filedialog.askopenfilename(
            parent=self, title="Import profiles from…",
            filetypes=[("SystemC profile (zip)", "*.zip"), ("All files", "*")])
        if not path:
            return
        try:
            info = profile_transfer.inspect_archive(path)
        except profile_transfer.TransferError as e:
            messagebox.showerror("Import", str(e))
            return
        except Exception as e:
            messagebox.showerror("Import", f"Couldn't read that file: {e}")
            return

        dlg = tk.Toplevel(self)
        dlg.title("Import Profiles")
        dlg.configure(bg=C["bg"])
        dlg.transient(self)
        dlg.grab_set()

        tk.Label(dlg, text="Import profiles", font=("sans-serif", 11, "bold"),
                 bg=C["bg"], fg=C["text"]).pack(anchor="w", padx=16, pady=(14, 2))
        made = " · ".join(x for x in (info["exported_at"], info["app"]) if x)
        tk.Label(dlg, text=("Exported " + made) if made else "Choose what to bring in.",
                 bg=C["bg"], fg=C["subtext"], font=("monospace", 8)).pack(anchor="w", padx=16)
        tk.Label(dlg, text="Every profile is added as a NEW one; nothing existing is changed. Only import files "
                           "you trust: a profile carries the agent's instructions.",
                 bg=C["bg"], fg=C["subtext"], font=("monospace", 8), wraplength=470, justify="left"
                 ).pack(anchor="w", padx=16, pady=(0, 6))

        # parts that make a bot run code stay off unless you tick them
        default_on = {"secrets": True, "memory": True, "history": True, "workspace": True,
                      "dependencies": False, "integrations": False}
        rows = {}

        def build_row(sid, pinfo):
            frame = tk.Frame(dlg, bg=C["panel"], padx=10, pady=8)
            frame.pack(fill="x", padx=16, pady=4)
            use = tk.BooleanVar(value=True)
            tk.Checkbutton(frame, text=f"Import “{pinfo['name']}”  ({pinfo['model']})", variable=use,
                           bg=C["panel"], fg=C["text"], selectcolor=C["bg"], activebackground=C["panel"],
                           font=("sans-serif", 9, "bold"), anchor="w").pack(anchor="w")
            name_var = tk.StringVar(value=pinfo["name"])
            nr = tk.Frame(frame, bg=C["panel"])
            nr.pack(fill="x", pady=2)
            tk.Label(nr, text="Name:", bg=C["panel"], fg=C["subtext"], font=("monospace", 9)).pack(side="left")
            tk.Entry(nr, textvariable=name_var, bg=C["panel2"], fg=C["text"], relief="flat",
                     width=28).pack(side="left", padx=6)
            id_lbl = tk.Label(frame, text="", bg=C["panel"], fg=C["subtext"], font=("monospace", 8))
            id_lbl.pack(anchor="w")

            def show_id(*_):
                pid = profile_transfer.unique_id(profiles.slugify(name_var.get(), fallback=sid))
                id_lbl.config(text=f"will be saved with the ID “{pid}” (a number is added if it's taken)")
            name_var.trace_add("write", show_id)
            show_id()

            pvars = {}
            for part in profile_transfer.PARTS:
                if part not in pinfo["available"]:
                    continue
                pvars[part] = tk.BooleanVar(value=default_on[part])
                text = f"{profile_transfer.LABELS[part]}  —  " \
                       f"{self._transfer_detail(part, [pinfo['available'][part]])}"
                if part == "dependencies":
                    text += "  (" + ", ".join(pinfo["available"][part]["items"][:4]) + ")"
                tk.Checkbutton(frame, text=text, variable=pvars[part], bg=C["panel"], fg=C["text"],
                               selectcolor=C["bg"], activebackground=C["panel"], anchor="w").pack(anchor="w")
            if "dependencies" in pvars or "integrations" in pvars:
                tk.Label(frame, text="Dependencies are pip-installed and integrations give the bot extra tools, "
                                     "so they're off unless you tick them.", bg=C["panel"], fg=C["subtext"],
                         font=("monospace", 8), wraplength=440, justify="left").pack(anchor="w", pady=(2, 0))
            rows[sid] = {"use": use, "name": name_var, "parts": pvars}

        for sid, pinfo in info["profiles"].items():
            build_row(sid, pinfo)

        def do_import():
            selections = {sid: {"name": r["name"].get().strip(),
                                "parts": {p for p, v in r["parts"].items() if v.get()}}
                          for sid, r in rows.items() if r["use"].get()}
            if not selections:
                messagebox.showwarning("Import", "Tick at least one profile.", parent=dlg)
                return
            import_btn.config(state="disabled", text="Importing…")

            def work():
                res = err = None
                try:
                    res = profile_transfer.import_archive(path, selections)
                except profile_transfer.TransferError as e:
                    err = str(e)
                except Exception as e:
                    err = f"Unexpected error: {e}"
                self.after(0, lambda: done(res, err))

            def done(res, err):
                if err:
                    import_btn.config(state="normal", text="Import")
                    messagebox.showerror("Import failed", err, parent=dlg)
                    return
                dlg.destroy()
                self._refresh_sidebar_profiles()
                self._refresh_profiles_list()
                lines = []
                for r in res:
                    extra = (" — " + ", ".join(r["parts"])) if r["parts"] else ""
                    lines.append(f"• {r['name']}  (ID: {r['id']}){extra}")
                tip = ""
                if any("secrets" not in r["parts"] for r in res):
                    tip = "\n\nBot tokens weren't imported for some profiles: add them in the Setup tab."
                messagebox.showinfo("Import complete", "Imported:\n" + "\n".join(lines) + tip)

            threading.Thread(target=work, daemon=True).start()

        btn_row = tk.Frame(dlg, bg=C["bg"])
        btn_row.pack(fill="x", padx=16, pady=14)
        import_btn = tk.Button(btn_row, text="Import", command=do_import, bg=C["accent"], fg=C["bg"],
                               font=("sans-serif", 9, "bold"), relief="flat", padx=12, pady=4, cursor="hand2")
        import_btn.pack(side="right")
        tk.Button(btn_row, text="Cancel", command=dlg.destroy, bg=C["panel2"], fg=C["text"],
                  relief="flat", padx=10, pady=4, cursor="hand2").pack(side="right", padx=6)

    def _quick_migrate_dialog(self):
        env_path = os.path.join(APP_DIR, ".env")
        if not os.path.exists(env_path):
            messagebox.showinfo("Migration", "No legacy .env file found in root folder to migrate.")
            return
        if messagebox.askyesno("Migrate .env", "Found legacy .env file.\n\nMigrate it into a profile now? (A backup will be saved to .env.legacy.backup)"):
            ok, msg = profiles.manager.migrate_from_env(env_path, "default")
            if ok:
                messagebox.showinfo("Migrated", msg)
                self._refresh_sidebar_profiles()
                self._switch_to_profile("default")
            else:
                messagebox.showerror("Error", msg)

    def _build_profiles_tab(self):
        f = self.tab_profiles
        top = tk.Frame(f, bg=C["bg"])
        top.pack(fill="x", pady=(12, 6))
        tk.Label(top, text="Bot Profiles & Multi-Instance Manager", bg=C["bg"], fg=C["text"],
                 font=("sans-serif", 12, "bold")).pack(side="left")
        
        btn_box = tk.Frame(top, bg=C["bg"])
        btn_box.pack(side="right")

        tk.Button(btn_box, text="New Profile", command=self._create_new_profile_dialog,
                  bg=C["accent"], fg=C["bg"], relief="flat", padx=10, pady=3,
                  font=("sans-serif", 9, "bold"), cursor="hand2").pack(side="left", padx=4)

        tk.Button(btn_box, text="⬇ Import…", command=self._import_profiles_dialog,
                  bg=C["panel2"], fg=C["text"], relief="flat", padx=10, pady=3,
                  cursor="hand2").pack(side="left", padx=4)

        tk.Button(btn_box, text="⬆ Export…", command=self._export_profiles_dialog,
                  bg=C["panel2"], fg=C["text"], relief="flat", padx=10, pady=3,
                  cursor="hand2").pack(side="left", padx=4)

        tk.Button(btn_box, text="Migrate .env", command=self._quick_migrate_dialog,
                  bg=C["panel2"], fg=C["text"], relief="flat", padx=10, pady=3,
                  cursor="hand2").pack(side="left", padx=4)

        tk.Button(btn_box, text="↻ Refresh", command=self._refresh_profiles_list,
                  bg=C["panel2"], fg=C["text"], relief="flat", padx=10, pady=3,
                  cursor="hand2").pack(side="left", padx=4)

        desc = tk.Label(f, text="Run as many bots at once as you like. Each profile has its own tokens, API keys, AI model, memory, chat history, isolated workspace and custom dependencies (the default profile keeps the original folders). Select a profile to edit its settings.",
                        bg=C["bg"], fg=C["subtext"], font=("monospace", 9), wraplength=680, justify="left")
        desc.pack(anchor="w", pady=(0, 8))

        # Scrollable container for profile cards
        outer = tk.Frame(f, bg=C["bg"])
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=C["bg"], highlightthickness=0)
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self.profiles_container = tk.Frame(canvas, bg=C["bg"])
        win = canvas.create_window((0, 0), window=self.profiles_container, anchor="nw")
        self.profiles_container.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))

        self._refresh_profiles_list()
        self.after(2500, self._poll_bot_statuses)

    def _poll_bot_statuses(self):
        try:
            running_count = sum(1 for p in profiles.manager.list_profiles() if profiles.supervisor.is_running(p.id))
            if hasattr(self, "sidebar_running_lbl"):
                self.sidebar_running_lbl.config(
                    text=f"Bots Active: {running_count}",
                    fg=C["green"] if running_count > 0 else C["subtext"]
                )
            self._update_run_bot_status()
        except Exception:
            pass
        self.after(2500, self._poll_bot_statuses)

    def _refresh_profiles_list(self):
        if not hasattr(self, "profiles_container"):
            return
        for w in self.profiles_container.winfo_children():
            w.destroy()

        profs = profiles.manager.list_profiles()
        active_prof = profiles.manager.get_active_profile()

        for p in profs:
            self._render_profile_card(p, is_active=(active_prof and active_prof.id == p.id))

    def _render_profile_card(self, p, is_active=False):
        card = tk.Frame(self.profiles_container, bg=C["panel"], padx=12, pady=10)
        card.pack(fill="x", pady=(0, 8))

        head = tk.Frame(card, bg=C["panel"])
        head.pack(fill="x")

        title_txt = f"{p.name}  [{p.id}]"
        tk.Label(head, text=title_txt, font=("sans-serif", 10, "bold"),
                 bg=C["panel"], fg=C["text"]).pack(side="left")

        if is_active:
            tk.Label(head, text="★ ACTIVE", font=("monospace", 8, "bold"),
                     bg=C["accent"], fg=C["bg"], padx=6, pady=1).pack(side="left", padx=8)

        is_running = profiles.supervisor.is_running(p.id)
        status_lbl = tk.Label(
            head,
            text=f"● running (PID {profiles.supervisor.instances.get(p.id, {}).get('pid', '?')})" if is_running else "● stopped",
            font=("monospace", 9),
            bg=C["panel"],
            fg=C["green"] if is_running else C["red"]
        )
        status_lbl.pack(side="right")

        # Details row
        details = tk.Frame(card, bg=C["panel"])
        details.pack(fill="x", pady=(4, 6))

        platforms = [p.discord_token and "Discord", p.stoat_token and "Stoat"]
        plat_str = " + ".join(x for x in platforms if x) or "None configured"
        deps_str = ", ".join(p.dependencies) if p.dependencies else "None"

        info_txt = f"Model: {p.agent_model} ({p.agent_provider})  •  Platforms: {plat_str}  •  Deps: {deps_str}"
        tk.Label(details, text=info_txt, font=("monospace", 8), bg=C["panel"], fg=C["subtext"]).pack(anchor="w")

        # Controls row
        btn_row = tk.Frame(card, bg=C["panel"])
        btn_row.pack(fill="x", pady=(4, 0))

        def toggle_run():
            if profiles.supervisor.is_running(p.id):
                profiles.supervisor.stop_instance(p.id)
            else:
                ok, msg = profiles.supervisor.start_instance(p.id)
                if not ok:
                    messagebox.showerror("Start Error", msg)
            self._refresh_profiles_list()

        def pull_deps():
            def worker():
                self.log_queue.put((p.id, f"Pulling dependencies for [{p.name}]...\n"))
                ok, msg = profiles.manager.pull_dependencies(p.id, log_queue=self.log_queue)
                self.log_queue.put((p.id, msg + "\n"))
                if ok:
                    self.after(0, lambda: messagebox.showinfo("Dependencies", f"Dependencies for '{p.name}' pulled successfully!"))
                else:
                    self.after(0, lambda: messagebox.showerror("Dependency Error", msg))
            threading.Thread(target=worker, daemon=True).start()
            self._switch_to_logs_hint()

        def set_active():
            self._switch_to_profile(p.id)
            messagebox.showinfo("Active Profile", f"Switched active profile to '{p.name}'. Setup tab updated.")

        def delete_prof():
            if p.id == "default":
                messagebox.showerror("Error", "Cannot delete the default profile.")
                return
            if profiles.supervisor.is_running(p.id):
                messagebox.showerror("Error", "Stop this bot instance before deleting.")
                return
            if messagebox.askyesno("Delete Profile", f"Delete profile '{p.name}' ({p.id})?"):
                profiles.manager.delete_profile(p.id)
                self._refresh_sidebar_profiles()
                self._refresh_profiles_list()

        tk.Button(btn_row, text="■ Stop" if is_running else "▶ Start", command=toggle_run,
                  bg=C["red"] if is_running else C["green"], fg=C["bg"], relief="flat",
                  padx=10, pady=2, font=("sans-serif", 8, "bold"), cursor="hand2").pack(side="left", padx=(0, 6))

        tk.Button(btn_row, text="Pull Dependencies", command=pull_deps,
                  bg=C["panel2"], fg=C["text"], relief="flat", padx=8, pady=2,
                  font=("sans-serif", 8), cursor="hand2").pack(side="left", padx=4)

        if not is_active:
            tk.Button(btn_row, text="★ Set Active", command=set_active,
                      bg=C["panel2"], fg=C["text"], relief="flat", padx=8, pady=2,
                      font=("sans-serif", 8), cursor="hand2").pack(side="left", padx=4)

        if p.id != "default":
            tk.Button(btn_row, text="Delete", command=delete_prof,
                      bg=C["panel2"], fg=C["red"], relief="flat", padx=8, pady=2,
                      font=("sans-serif", 8), cursor="hand2").pack(side="right")

    # ── tab 1: setup ────────────────────────────────────────────────────────

    def _build_setup_tab(self):
        f = self.tab_setup

        def label(text, **kw):
            tk.Label(f, text=text, bg=C["bg"], fg=C["subtext"],
                      font=("monospace", 9), justify="left", wraplength=680, **kw).pack(
                anchor="w", pady=(8, 2))

        def entry(var, show=""):
            kwargs = dict(textvariable=var, bg=C["panel"], fg=C["text"],
                           insertbackground=C["text"], relief="flat", width=60)
            if show:
                kwargs["show"] = show
            tk.Entry(f, **kwargs).pack(anchor="w", pady=(0, 4))

        label("Agent name, gender, pronouns and the system prompt are on the Personalization tab.")

        tk.Label(f, text="Discord bot token", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(10, 0))
        label("discord.com/developers/applications → your app → Bot → Reset Token.\n"
              "Also turn on 'Message Content Intent' on that same page.")
        entry(self.token_var, show="•")

        tk.Label(f, text="Allowed channel ID(s)", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(10, 0))
        label("Right-click a channel in Discord → Copy Channel ID (enable Developer Mode "
              "in Discord settings first). Comma-separate multiple.")
        entry(self.channels_var)
        tk.Checkbutton(f, text="Accept from all channels", variable=self.channels_all_var,
                       bg=C["bg"], fg=C["text"], selectcolor=C["panel"]).pack(anchor="w", pady=(0, 4))

        tk.Label(f, text="Bot mode", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(10, 0))
        row = tk.Frame(f, bg=C["bg"])
        row.pack(anchor="w")
        for val, txt in [("WAKEWORD", "Wake word / @mention"), ("MENTION", "@mention only"),
                          ("EVERYTHING", "Every message")]:
            tk.Radiobutton(row, text=txt, variable=self.mode_var, value=val,
                            bg=C["bg"], fg=C["text"], selectcolor=C["panel"],
                            activebackground=C["bg"], font=("monospace", 9)).pack(
                side="left", padx=(0, 12))

        tk.Label(f, text="Wake word", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(10, 0))
        entry(self.wake_var)

        ttk.Separator(f, orient="horizontal").pack(fill="x", pady=(14, 6))
        tk.Label(f, text="Stoat (optional)", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 11, "bold")).pack(anchor="w")
        label("Stoat is an open-source Discord alternative. Leave blank to skip.\n"
              "Get a bot token at stoat.chat → Settings → My Bots → New Bot.")
        tk.Label(f, text="Stoat bot token", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(8, 0))
        entry(self.stoat_token_var, show="•")
        tk.Label(f, text="Stoat channel ID(s)", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(8, 0))
        entry(self.stoat_channels_var)
        tk.Checkbutton(f, text="Accept from all channels", variable=self.stoat_channels_all_var,
                       bg=C["bg"], fg=C["text"], selectcolor=C["panel"]).pack(anchor="w", pady=(0, 4))
        tk.Label(f, text="Stoat bot mode", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(8, 0))
        stoat_row = tk.Frame(f, bg=C["bg"])
        stoat_row.pack(anchor="w")
        for val, txt in [("WAKEWORD", "Wake word / @mention"), ("MENTION", "@mention only"),
                          ("EVERYTHING", "Every message")]:
            tk.Radiobutton(stoat_row, text=txt, variable=self.stoat_mode_var, value=val,
                            bg=C["bg"], fg=C["text"], selectcolor=C["panel"],
                            activebackground=C["bg"], font=("monospace", 9)).pack(side="left", padx=(0, 12))
        tk.Label(f, text="Stoat wake word", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(8, 0))
        entry(self.stoat_wake_var)
        ttk.Separator(f, orient="horizontal").pack(fill="x", pady=(14, 6))

        tk.Label(f, text="Ollama source", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(10, 0))
        label("Local = an Ollama server on this machine. Ollama Cloud = run big models on "
              "ollama.com with an API key (no GPU needed).")
        src_row = tk.Frame(f, bg=C["bg"])
        src_row.pack(anchor="w")
        for val, txt in [("local", "Local Ollama"), ("cloud", "Ollama Cloud (API key)")]:
            tk.Radiobutton(src_row, text=txt, variable=self.ollama_mode_var, value=val,
                            bg=C["bg"], fg=C["text"], selectcolor=C["panel"],
                            activebackground=C["bg"], font=("monospace", 9),
                            command=self._on_ollama_mode_change).pack(side="left", padx=(0, 12))

        self.cloud_holder = tk.Frame(f, bg=C["bg"])      # fixed slot; frame inside is shown/hidden
        self.cloud_holder.pack(anchor="w")
        self.cloud_frame = tk.Frame(self.cloud_holder, bg=C["bg"])
        tk.Label(self.cloud_frame, text="Ollama API keys", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(8, 0))
        tk.Label(self.cloud_frame, bg=C["bg"], fg=C["subtext"], font=("monospace", 9),
                  justify="left", wraplength=680,
                  text="Add one or more keys from https://ollama.com/settings/keys. They're used in "
                       "order: if one hits a usage limit or is rejected, the next one takes over "
                       "automatically. Use ▲ ▼ to choose which is the primary.").pack(
            anchor="w", pady=(2, 4))
        keys_row = tk.Frame(self.cloud_frame, bg=C["bg"])
        keys_row.pack(anchor="w")
        self.keys_list = tk.Listbox(keys_row, height=4, width=42, activestyle="none",
                                     bg=C["panel"], fg=C["text"], selectbackground=C["panel2"],
                                     selectforeground=C["text"], highlightthickness=0,
                                     relief="flat", font=("monospace", 10), exportselection=False)
        self.keys_list.pack(side="left")
        key_btns = tk.Frame(keys_row, bg=C["bg"])
        key_btns.pack(side="left", padx=8, anchor="n")
        for txt, cmd in [("▲", lambda: self._move_key(-1)), ("▼", lambda: self._move_key(1)),
                         ("Remove", self._remove_key)]:
            tk.Button(key_btns, text=txt, command=cmd, bg=C["panel2"], fg=C["text"],
                       relief="flat", padx=8, pady=1, cursor="hand2").pack(fill="x", pady=1)
        add_row = tk.Frame(self.cloud_frame, bg=C["bg"])
        add_row.pack(anchor="w", pady=(6, 4))
        key_entry = tk.Entry(add_row, textvariable=self.ollama_key_var, show="•", width=44,
                              bg=C["panel"], fg=C["text"], insertbackground=C["text"], relief="flat")
        key_entry.pack(side="left")
        key_entry.bind("<Return>", lambda e: self._add_key())
        tk.Button(add_row, text="Add key", command=self._add_key, bg=C["panel2"], fg=C["text"],
                   relief="flat", padx=10, pady=2, cursor="hand2").pack(side="left", padx=8)
        self._refresh_keys_list()

        tk.Label(f, text="Model", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(10, 0))
        label("Needs tool calling (e.g. gpt-oss:120b, qwen3, llama3.1). Not sure of the exact ID? "
              "Click 'Fetch available models' and pick one from the list.")
        model_row = tk.Frame(f, bg=C["bg"])
        model_row.pack(anchor="w", pady=(0, 4))
        self.model_combo = ttk.Combobox(model_row, textvariable=self.model_var, width=42)
        self.model_combo.pack(side="left")
        tk.Button(model_row, text="Fetch available models", command=self._fetch_models,
                   bg=C["panel2"], fg=C["text"], relief="flat", padx=10, pady=3,
                   cursor="hand2").pack(side="left", padx=8)
        self.model_fetch_lbl = tk.Label(f, text="", bg=C["bg"], fg=C["subtext"],
                                          font=("monospace", 9))
        self.model_fetch_lbl.pack(anchor="w")
        self._on_ollama_mode_change()

        tk.Label(f, text=tr("Agent workspace & account"), bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(12, 0))
        label(tr("Configure where the agent stores files and runs shell commands."))

        curr_user, _, _, _ = account_setup.get_current_user_and_group()

        account_frame = tk.Frame(f, bg=C["panel"], padx=10, pady=8)
        account_frame.pack(fill="x", pady=(4, 6))

        default_agent_dir = account_setup.get_default_agent_dir()

        def _on_account_mode_change():
            mode = self.account_mode_var.get()
            if mode == "home_agent":
                self.workdir_var.set(default_agent_dir)
                user_row.pack_forget()
                elev_row.pack_forget()
                dir_row.pack_forget()
            elif mode == "separate_user":
                if not self.workdir_var.get() or self.workdir_var.get().endswith("workspace"):
                    self.workdir_var.set(default_agent_dir)
                user_row.pack(anchor="w", fill="x", pady=2)
                elev_row.pack(anchor="w", fill="x", pady=2)
                dir_row.pack(anchor="w", fill="x", pady=2)
            else:  # custom
                user_row.pack_forget()
                elev_row.pack_forget()
                dir_row.pack(anchor="w", fill="x", pady=2)
            self._update_workspace_status_ui()

        r1 = tk.Radiobutton(
            account_frame,
            text=f"Use {default_agent_dir} from your account ({curr_user})",
            variable=self.account_mode_var,
            value="home_agent",
            command=_on_account_mode_change,
            bg=C["panel"], fg=C["text"], selectcolor=C["panel2"],
            activebackground=C["panel"], font=("sans-serif", 9, "bold"),
        )
        r1.pack(anchor="w", pady=2)
        tk.Label(
            account_frame,
            text=tr("Agent $HOME and files live in {path}, owned by your account.", path=default_agent_dir),
            bg=C["panel"], fg=C["subtext"], font=("sans-serif", 8),
        ).pack(anchor="w", padx=20)

        r2 = tk.Radiobutton(
            account_frame,
            text=tr("Run under another user account (e.g. 'agent')"),
            variable=self.account_mode_var,
            value="separate_user",
            command=_on_account_mode_change,
            bg=C["panel"], fg=C["text"], selectcolor=C["panel2"],
            activebackground=C["panel"], font=("sans-serif", 9, "bold"),
        )
        r2.pack(anchor="w", pady=(6, 2))
        tk.Label(
            account_frame,
            text=tr("Runs the whole bot (shell commands, files, everything) as a dedicated system user (e.g. agent) via sudo/runuser. This window stays in your own session."),
            bg=C["panel"], fg=C["subtext"], font=("sans-serif", 8),
        ).pack(anchor="w", padx=20)

        r3 = tk.Radiobutton(
            account_frame,
            text=tr("Local app workspace or custom directory"),
            variable=self.account_mode_var,
            value="custom",
            command=_on_account_mode_change,
            bg=C["panel"], fg=C["text"], selectcolor=C["panel2"],
            activebackground=C["panel"], font=("sans-serif", 9, "bold"),
        )
        r3.pack(anchor="w", pady=(6, 2))
        tk.Label(
            account_frame,
            text=tr("Standard folder inside this app or a custom path of your choice."),
            bg=C["panel"], fg=C["subtext"], font=("sans-serif", 8),
        ).pack(anchor="w", padx=20)

        details_frame = tk.Frame(account_frame, bg=C["panel"])
        details_frame.pack(fill="x", pady=(6, 0))

        user_row = tk.Frame(details_frame, bg=C["panel"])
        tk.Label(user_row, text=tr("Account username:"), bg=C["panel"], fg=C["text"], width=18, anchor="w").pack(side="left")
        tk.Entry(user_row, textvariable=self.run_as_user_var, bg=C["bg"], fg=C["text"],
                 insertbackground=C["text"], relief="flat", width=24).pack(side="left")

        elev_row = tk.Frame(details_frame, bg=C["panel"])
        tk.Checkbutton(
            elev_row, text=tr("Allow running elevated?"),
            variable=self.allow_elevated_var,
            bg=C["panel"], fg=C["text"], selectcolor=C["panel2"],
            activebackground=C["panel"], activeforeground=C["text"],
            font=("sans-serif", 9, "bold"),
        ).pack(side="left")
        tk.Label(elev_row, text=tr("Gives the run_shell_elevated tool (sudo, each use needs your approval). "
                                   "Off = the tool doesn't exist and the account has no sudo."),
                 bg=C["panel"], fg=C["subtext"], font=("sans-serif", 8),
                 wraplength=520, justify="left").pack(side="left", padx=8)

        dir_row = tk.Frame(details_frame, bg=C["panel"])
        tk.Label(dir_row, text=tr("Workspace directory:"), bg=C["panel"], fg=C["text"], width=18, anchor="w").pack(side="left")
        tk.Entry(dir_row, textvariable=self.workdir_var, bg=C["bg"], fg=C["text"],
                 insertbackground=C["text"], relief="flat", width=36).pack(side="left")

        setup_action_row = tk.Frame(account_frame, bg=C["panel"])
        setup_action_row.pack(fill="x", pady=(8, 2))

        tk.Button(
            setup_action_row,
            text=tr("Set up / Verify directory"),
            command=self._setup_workspace_action,
            bg=C["panel2"], fg=C["text"], relief="flat", padx=8, pady=2, cursor="hand2",
            font=("sans-serif", 9, "bold"),
        ).pack(side="left")

        tk.Button(
            setup_action_row,
            text=tr("Allow panel to run as user"),
            command=self._grant_runas_action,
            bg=C["panel2"], fg=C["text"], relief="flat", padx=8, pady=2, cursor="hand2",
            font=("sans-serif", 9, "bold"),
        ).pack(side="left", padx=(6, 0))

        self.workspace_status_lbl = tk.Label(
            setup_action_row,
            text="",
            bg=C["panel"], fg=C["subtext"], font=("monospace", 9),
        )
        self.workspace_status_lbl.pack(side="left", padx=10)

        _on_account_mode_change()

        btn_row = tk.Frame(f, bg=C["bg"])
        btn_row.pack(anchor="w", pady=16)
        tk.Button(btn_row, text="Save config", command=self._save_config,
                   bg=C["accent"], fg=C["bg"], relief="flat", padx=14, pady=8,
                   font=("sans-serif", 10, "bold"), cursor="hand2").pack(side="left")
        self.setup_status = tk.Label(btn_row, text="", bg=C["bg"], fg=C["green"],
                                       font=("monospace", 9))
        self.setup_status.pack(side="left", padx=12)

    def _run_as_user(self) -> str:
        """Username the backend should run as ('' = same account as this panel)."""
        try:
            return self._get_target_workdir_and_user()[1]
        except Exception:
            return ""

    def _ensure_runas(self) -> bool:
        """Before starting a backend process: make sure we can launch it as the
        configured user. Offers the one-time setup (asks for your sudo password
        once) instead of failing."""
        user = self._run_as_user()
        if not runas.needs_switch(user):
            return True
        if runas.can_switch(user):
            if runas.can_access(user, APP_DIR):
                return True
            # Switching works but the agent can't read/write the app folder (e.g. it
            # lives in your home). Fix it silently if this account owns the folder;
            # otherwise fall through to the full setup below (sudo password once).
            runas.grant_agent_access(user, APP_DIR)
            if runas.can_access(user, APP_DIR):
                return True
        elif not messagebox.askyesno(
            tr("Run as {user}", user=user),
            tr("To run the agent as '{user}', this account needs permission to start "
               "processes as that user without a password, and write access to the app "
               "folder.\n\nSet that up now? (asks for your sudo password once)", user=user),
        ):
            return False
        return self._grant_runas_action(user)

    def _grant_runas_action(self, user: str = "") -> bool:
        user = user or self._run_as_user()
        if not user:
            messagebox.showinfo(tr("Run as another user"),
                                tr("Pick \"Run under another user account\" first."))
            return False
        pw = None
        if not account_setup.can_sudo_without_password():
            pw = account_setup.prompt_root_password_gui(
                parent=self, workdir=APP_DIR, target_user=user)
            if pw is None:
                return False
        ok, msg = runas.grant_panel_access(user, APP_DIR, root_password=pw)
        if ok:
            messagebox.showinfo(tr("Run as {user}", user=user), msg)
        else:
            messagebox.showerror(tr("Run as {user}", user=user), msg)
        return ok

    def _get_target_workdir_and_user(self):
        default_dir = account_setup.get_default_agent_dir()
        mode = self.account_mode_var.get()
        if mode == "home_agent":
            return default_dir, ""
        elif mode == "separate_user":
            return self.workdir_var.get().strip() or default_dir, self.run_as_user_var.get().strip() or "agent"
        else:
            return self.workdir_var.get().strip() or os.path.join(APP_DIR, "workspace"), ""

    def _update_workspace_status_ui(self):
        workdir, run_as = self._get_target_workdir_and_user()
        status = account_setup.check_workspace_status(workdir, run_as)
        if status["exists"] and status["writable"]:
            self.workspace_status_lbl.config(
                text=tr("✓ Ready (owner: {owner})", owner=status["owner"] or status["target_user"]),
                fg=C["green"],
            )
        elif not status["exists"]:
            self.workspace_status_lbl.config(
                text=tr("Does not exist (root required to create)"),
                fg=C["yellow"],
            )
        elif not status["writable"]:
            self.workspace_status_lbl.config(
                text=tr("Exists but not writable by {target}", target=status["target_user"]),
                fg=C["red"],
            )
        elif status["needs_root"]:
            self.workspace_status_lbl.config(
                text=tr("Needs setup / root privileges"),
                fg=C["yellow"],
            )

    def _setup_workspace_action(self):
        workdir, run_as = self._get_target_workdir_and_user()
        ok, msg = account_setup.ensure_workspace_interactive(
            workdir=workdir,
            run_as_user=run_as,
            parent_window=self,
            is_gui=True,
        )
        self._update_workspace_status_ui()
        if ok:
            messagebox.showinfo(tr("Workspace setup"), msg)
        else:
            messagebox.showerror(tr("Workspace setup error"), msg)

    def _save_config(self):
        has_discord = bool(self.token_var.get().strip())
        has_stoat = bool(self.stoat_token_var.get().strip())
        if not has_discord and not has_stoat:
            messagebox.showerror(tr("Missing token"), tr("Paste a Discord bot token and/or a Stoat bot token first."))
            return
        if has_discord and not self.channels_var.get().strip():
            messagebox.showerror(tr("Missing channel"), tr("Enter at least one Discord channel ID first."))
            return
        cloud = self.ollama_mode_var.get() == "cloud"
        if cloud:
            self._add_key()          # don't lose a key that was typed but not added yet
        if cloud and not self.api_keys:
            messagebox.showerror(tr("Missing API key"),
                                 tr("Add your Ollama API key (https://ollama.com/settings/keys), "
                                    "or switch to Local Ollama."))
            return

        workdir, run_as = self._get_target_workdir_and_user()
        mode = self.account_mode_var.get()

        status = account_setup.check_workspace_status(workdir, run_as)
        if status["needs_root"]:
            ok, msg = account_setup.ensure_workspace_interactive(
                workdir=workdir,
                run_as_user=run_as,
                parent_window=self,
                is_gui=True,
            )
            self._update_workspace_status_ui()
            if not ok:
                messagebox.showerror(tr("Workspace setup error"), msg)
                return
        else:
            try:
                os.makedirs(workdir, exist_ok=True)
            except OSError as e:
                messagebox.showerror(tr("Error"), f"Could not create workspace directory:\n{e}")
                return

        # "Allow running elevated?" only exists for a separate user account. Make the
        # OS match the box (sudoers rule present <=> ticked) before saving it.
        elevated = ""
        if mode == "separate_user" and run_as:
            want = bool(self.allow_elevated_var.get())
            have = runas.elevation_ready(run_as)
            if want != have:
                pw = None
                if not account_setup.can_sudo_without_password():
                    pw = account_setup.prompt_root_password_gui(
                        parent=self, workdir=APP_DIR, target_user=run_as)
                    if pw is None:
                        self.allow_elevated_var.set(have)
                        messagebox.showwarning(
                            tr("Allow running elevated?"),
                            tr("Not changed (no password given). Elevated is still {state}.",
                               state=tr("ON") if have else tr("OFF")))
                        want = have
                if want != have:
                    ok, msg = runas.set_elevation(run_as, want, root_password=pw)
                    if not ok:
                        self.allow_elevated_var.set(have)
                        messagebox.showerror(tr("Allow running elevated?"), msg)
                        want = have
            elevated = "1" if want else "0"

        # The Setup tab was filled from a profile (sidebar dropdown): save into that profile.
        # Writing it to the global .env would leak the profile's token/model into the legacy
        # Run-tab bot, and the profile itself would never change.
        pid = getattr(self, "_setup_pid", None)
        if pid:
            prof = profiles.manager.get_profile(pid)
            if prof is None:
                messagebox.showerror(tr("Error"), tr("Profile not found."))
                return
            self._save_config_to_profile(prof, has_discord, has_stoat, cloud,
                                         mode, workdir, run_as, elevated)
            return

        # Start from the existing .env so hand-added keys (e.g. ANTHROPIC_API_KEY) survive
        env = load_env()
        for k in ("DISCORD_BOT_TOKEN", "ALLOWED_CHANNEL_IDS", "STOAT_BOT_TOKEN",
                  "STOAT_ALLOWED_CHANNEL_IDS", "STOAT_BOT_MODE", "STOAT_WAKE_WORD",
                  "OLLAMA_API_KEY", "OLLAMA_API_KEYS"):
            env.pop(k, None)
        env.update({
            "DISCORD_BOT_TOKEN": self.token_var.get().strip(),
            "ALLOWED_CHANNEL_IDS": "" if self.channels_all_var.get() else self.channels_var.get().strip(),
            "BOT_MODE": self.mode_var.get(),
            "WAKE_WORD": self.wake_var.get().strip() or "hey claw",
            "AGENT_NAME": " ".join(self.agent_name_var.get().split()),
            "AGENT_PROVIDER": "ollama",
            "AGENT_MODEL": self.model_var.get().strip() or ("gpt-oss:120b" if cloud else "llama3.2"),
            "OLLAMA_MODE": "cloud" if cloud else "local",
            "OLLAMA_HOST": ("https://ollama.com" if cloud
                            else self.ollama_host_var.get().strip() or "http://localhost:11434"),
            "AGENT_ACCOUNT_MODE": mode,
            "AGENT_WORKDIR": workdir,
            "AGENT_RUN_AS_USER": run_as,
            "AGENT_ALLOW_ELEVATED": elevated,   # "" (dropped) outside separate-user mode
        })
        if cloud:
            env["OLLAMA_API_KEY"] = self.api_keys[0]                     # primary
            if len(self.api_keys) > 1:
                env["OLLAMA_API_KEYS"] = ",".join(self.api_keys[1:])     # fallbacks, in order
        if has_stoat:
            env["STOAT_BOT_TOKEN"]            = self.stoat_token_var.get().strip()
            env["STOAT_ALLOWED_CHANNEL_IDS"]  = "" if self.stoat_channels_all_var.get() else self.stoat_channels_var.get().strip()
            env["STOAT_BOT_MODE"]             = self.stoat_mode_var.get()
            env["STOAT_WAKE_WORD"]            = self.stoat_wake_var.get().strip() or "hey claw"

        save_env(env)
        self._refresh_ollama_status()
        self._update_workspace_status_ui()
        self.setup_status.config(text=tr("✓ saved to .env"))
        self.after(3000, lambda: self.setup_status.config(text=""))

    def _save_config_to_profile(self, p, has_discord, has_stoat, cloud, mode, workdir, run_as, elevated):
        """Write the Setup tab's fields into profile `p` (and not into .env).

        Fields the Setup tab has no widget for stay as they are: provider, bot name /
        gender / pronouns / prompt (Personalization tab), subagents, dependencies, ...
        """
        fallback = (os.path.join(APP_DIR, "workspace") if p.id == "default"
                    else os.path.join(APP_DIR, "workspace", "profiles", p.id))
        keys = list(self.api_keys)            # kept even in local mode, like the profile had them
        data = p.to_dict()
        data.update({
            "discord_enabled": has_discord,
            "discord_token": self.token_var.get().strip(),
            "discord_channels": "" if self.channels_all_var.get() else self.channels_var.get().strip(),
            "discord_bot_mode": self.mode_var.get(),
            "discord_wake_word": self.wake_var.get().strip() or "hey claw",
            "stoat_enabled": has_stoat,
            "stoat_token": self.stoat_token_var.get().strip(),
            "stoat_channels": "" if self.stoat_channels_all_var.get() else self.stoat_channels_var.get().strip(),
            "stoat_bot_mode": self.stoat_mode_var.get(),
            "stoat_wake_word": self.stoat_wake_var.get().strip() or "hey claw",
            "agent_model": self.model_var.get().strip() or ("gpt-oss:120b" if cloud else "llama3.2"),
            "ollama_mode": "cloud" if cloud else "local",
            "ollama_host": ("https://ollama.com" if cloud
                            else self.ollama_host_var.get().strip() or "http://localhost:11434"),
            "api_key": keys[0] if keys else "",
            "api_keys": keys[1:],
            "agent_account_mode": mode,
            # the profile's own default folder is stored as "" so it keeps following it
            "agent_workdir": "" if os.path.normpath(workdir) == os.path.normpath(fallback) else workdir,
            "agent_run_as_user": run_as,
            "agent_allow_elevated": "1" if elevated == "1" else "0",
        })
        try:
            profiles.manager.save_profile(profiles.BotProfile.from_dict(data))
        except Exception as e:
            messagebox.showerror(tr("Couldn't save"), str(e))
            return
        self._refresh_ollama_status()
        self._update_workspace_status_ui()
        self._refresh_profiles_list()
        self.setup_status.config(text=tr("✓ saved for {name} — restart that bot to apply", name=p.name))
        self.after(5000, lambda: self.setup_status.config(text=""))

    def _on_ollama_mode_change(self):
        if self.ollama_mode_var.get() == "cloud":
            self.cloud_frame.pack(anchor="w")
        else:
            self.cloud_frame.pack_forget()
        if hasattr(self, "ollama_status_lbl"):
            self._refresh_ollama_status()

    # ── API key list ────────────────────────────────────────────────────────

    def _refresh_keys_list(self, select: "int | None" = None):
        self.keys_list.delete(0, "end")
        for i, k in enumerate(self.api_keys):
            role = tr("primary") if i == 0 else tr("fallback")
            self.keys_list.insert("end", f"#{i + 1}  …{k[-4:]}   ({role})")
        if select is not None and 0 <= select < len(self.api_keys):
            self.keys_list.selection_set(select)

    def _add_key(self):
        key = self.ollama_key_var.get().strip()
        if not key or any(c.isspace() for c in key):
            self.ollama_key_var.set("")
            return
        if key not in self.api_keys:
            self.api_keys.append(key)
        self.ollama_key_var.set("")
        self._refresh_keys_list(select=self.api_keys.index(key))

    def _remove_key(self):
        sel = self.keys_list.curselection()
        if sel:
            del self.api_keys[sel[0]]
            self._refresh_keys_list(select=min(sel[0], len(self.api_keys) - 1))

    def _move_key(self, delta: int):
        sel = self.keys_list.curselection()
        if not sel:
            return
        i, j = sel[0], sel[0] + delta
        if 0 <= j < len(self.api_keys):
            self.api_keys[i], self.api_keys[j] = self.api_keys[j], self.api_keys[i]
            self._refresh_keys_list(select=j)

    def _fetch_models(self):
        """List the model IDs the chosen Ollama (local or cloud) actually offers."""
        from providers import list_ollama_models, OLLAMA_CLOUD_HOST
        cloud = self.ollama_mode_var.get() == "cloud"
        host = OLLAMA_CLOUD_HOST if cloud else (self.ollama_host_var.get().strip()
                                                or "http://localhost:11434")
        if cloud:
            self._add_key()
        keys = list(self.api_keys) if cloud else [""]
        if cloud and not keys:
            messagebox.showinfo(tr("API key needed"),
                                tr("Add your Ollama API key first (https://ollama.com/settings/keys)."))
            return
        self.model_fetch_lbl.config(text=tr("fetching…"), fg=C["yellow"])

        def worker():
            names, err = None, None
            for key in keys:                      # first key that works wins
                try:
                    names = list_ollama_models(host, key)
                    break
                except RuntimeError as e:
                    err = e
            if names is None:
                self.after(0, lambda: self.model_fetch_lbl.config(text=f"{err}", fg=C["red"]))
                return

            def done():
                self.model_combo["values"] = names
                self.model_fetch_lbl.config(
                    text=tr("✓ {n} model(s) — open the dropdown to choose", n=len(names)), fg=C["green"])
                if names and self.model_var.get().strip() not in names:
                    self.model_combo.set(names[0])
            self.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    # ── tab: personalization ─────────────────────────────────────────────────

    def _build_personalization_tab(self):
        f = self.tab_personal
        self.gender_var = tk.StringVar()
        self.pronouns_var = tk.StringVar()
        self._personal_pid = None

        top = tk.Frame(f, bg=C["bg"])
        top.pack(fill="x", pady=(14, 2))
        tk.Label(top, text="Personalization", bg=C["bg"], fg=C["text"],
                 font=("sans-serif", 11, "bold")).pack(side="left")
        self.personal_profile_lbl = tk.Label(top, text="", bg=C["bg"], fg=C["accent"],
                                             font=("monospace", 9, "bold"))
        self.personal_profile_lbl.pack(side="right")
        tk.Label(f, text="Who this profile's agent is, and the system prompt it runs on. Each profile "
                         "has its own. Restart that bot to apply changes.",
                 bg=C["bg"], fg=C["subtext"], font=("monospace", 9), wraplength=680,
                 justify="left").pack(anchor="w", pady=(0, 8))

        # name / gender / pronouns: plain text boxes, so anything can be typed
        fields = tk.Frame(f, bg=C["bg"])
        fields.pack(fill="x")
        for col, (title, var, hint) in enumerate((
            ("Bot name", self.agent_name_var, "Blank = the profile's name"),
            ("Gender", self.gender_var, "Anything: female, male, Gender Non-Conforming…"),
            ("Pronouns", self.pronouns_var, "Anything: he/him, she/her, xe/xer…"),
        )):
            cell = tk.Frame(fields, bg=C["bg"])
            cell.grid(row=0, column=col, sticky="nw", padx=(0, 14))
            tk.Label(cell, text=title, bg=C["bg"], fg=C["text"],
                     font=("sans-serif", 10, "bold")).pack(anchor="w")
            tk.Entry(cell, textvariable=var, bg=C["panel"], fg=C["text"], insertbackground=C["text"],
                     relief="flat", width=24).pack(anchor="w", pady=(2, 1), ipady=3)
            tk.Label(cell, text=hint, bg=C["bg"], fg=C["subtext"], font=("monospace", 8),
                     wraplength=210, justify="left").pack(anchor="w")

        # system prompt
        head = tk.Frame(f, bg=C["bg"])
        head.pack(fill="x", pady=(12, 2))
        tk.Label(head, text="System prompt", bg=C["bg"], fg=C["text"],
                 font=("sans-serif", 10, "bold")).pack(side="left")
        self.prompt_mode_lbl = tk.Label(head, text="", bg=C["bg"], fg=C["subtext"], font=("monospace", 8))
        self.prompt_mode_lbl.pack(side="right")
        tk.Label(f, text="This is the full default prompt: edit it to fine-tune, or replace it entirely. "
                         "Click a variable to insert it where the cursor is. Integration tools and memory "
                         "are added after your prompt automatically.",
                 bg=C["bg"], fg=C["subtext"], font=("monospace", 9), wraplength=680,
                 justify="left").pack(anchor="w", pady=(0, 4))

        chips = tk.Frame(f, bg=C["bg"])
        chips.pack(fill="x", pady=(0, 2))
        tk.Label(chips, text="Variables:", bg=C["bg"], fg=C["subtext"],
                 font=("monospace", 8)).pack(side="left", padx=(0, 4))
        for token, _desc in prompt_template.VARIABLES:
            tk.Button(chips, text=token, command=lambda t=token: self._insert_prompt_var(t),
                      bg=C["panel2"], fg=C["accent"], relief="flat", padx=6, pady=1,
                      font=("monospace", 8, "bold"), cursor="hand2").pack(side="left", padx=2)
        tk.Label(f, text="   ".join(f"{t} = {d}" for t, d in prompt_template.VARIABLES[:5]),
                 bg=C["bg"], fg=C["subtext"], font=("monospace", 8), wraplength=680,
                 justify="left").pack(anchor="w", pady=(0, 4))

        box = tk.Frame(f, bg=C["bg"])
        box.pack(fill="both", expand=True)
        bar = ttk.Scrollbar(box, orient="vertical")
        bar.pack(side="right", fill="y")
        self.prompt_text = tk.Text(box, wrap="word", undo=True, bg=C["panel"], fg=C["text"],
                                   insertbackground=C["text"], relief="flat", font=("monospace", 9),
                                   padx=8, pady=6, yscrollcommand=bar.set, height=10)
        self.prompt_text.pack(side="left", fill="both", expand=True)
        bar.config(command=self.prompt_text.yview)
        self.prompt_text.bind("<<Modified>>", self._on_prompt_modified)

        btns = tk.Frame(f, bg=C["bg"])
        btns.pack(fill="x", pady=(8, 8))
        tk.Button(btns, text="Save", command=self._save_personalization, bg=C["green"], fg=C["bg"],
                  relief="flat", padx=12, pady=4, font=("sans-serif", 9, "bold"),
                  cursor="hand2").pack(side="left", padx=(0, 6))
        tk.Button(btns, text="Preview", command=self._preview_prompt, bg=C["panel2"], fg=C["text"],
                  relief="flat", padx=10, pady=4, cursor="hand2").pack(side="left", padx=(0, 6))
        tk.Button(btns, text="↺ Reset to default prompt", command=self._reset_prompt, bg=C["panel2"],
                  fg=C["text"], relief="flat", padx=10, pady=4, cursor="hand2").pack(side="left")
        self.personal_status = tk.Label(btns, text="", bg=C["bg"], fg=C["green"], font=("monospace", 9),
                                        wraplength=420, justify="left")
        self.personal_status.pack(side="left", padx=10)

        try:
            self._load_personalization(profiles.manager.get_active_profile())
        except Exception:
            self._set_prompt_text(prompt_template.DEFAULT_PROMPT_TEMPLATE)

    def _set_prompt_text(self, text: str):
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", text)
        self.prompt_text.edit_reset()
        self.prompt_text.edit_modified(False)
        self._update_prompt_mode()

    def _prompt_text_value(self) -> str:
        return self.prompt_text.get("1.0", "end-1c").replace("\r\n", "\n")

    def _prompt_is_default(self) -> bool:
        t = self._prompt_text_value().strip()
        return not t or t == prompt_template.DEFAULT_PROMPT_TEMPLATE.strip()

    def _update_prompt_mode(self):
        self.prompt_mode_lbl.config(
            text=tr("built-in default (follows updates)") if self._prompt_is_default()
            else tr("custom prompt"))

    def _on_prompt_modified(self, _e=None):
        if self.prompt_text.edit_modified():
            self.prompt_text.edit_modified(False)
            self._update_prompt_mode()

    def _insert_prompt_var(self, token: str):
        self.prompt_text.insert("insert", token)
        self.prompt_text.focus_set()

    def _load_personalization(self, p):
        """Fill the tab from a profile. A profile with no custom prompt shows the default one."""
        self._personal_pid = p.id
        self.agent_name_var.set(p.bot_name)
        self.gender_var.set(p.gender)
        self.pronouns_var.set(p.pronouns)
        self.personal_profile_lbl.config(text=f"{p.name} ({p.id})")
        self._set_prompt_text(p.system_prompt or prompt_template.DEFAULT_PROMPT_TEMPLATE)
        self.personal_status.config(text="")

    def _reset_prompt(self):
        if not self._prompt_is_default() and not messagebox.askyesno(
                tr("Reset prompt?"), tr("Replace the prompt in the box with the built-in default? "
                                        "Your edits will be lost unless you press Save to keep them.")):
            return
        self._set_prompt_text(prompt_template.DEFAULT_PROMPT_TEMPLATE)
        self.personal_status.config(text=tr("Default restored — press Save to apply."), fg=C["yellow"])

    def _save_personalization(self):
        p = profiles.manager.get_profile(self._personal_pid or "")
        if not p:
            messagebox.showerror(tr("Error"), tr("Profile not found."))
            return
        data = p.to_dict()
        data["bot_name"] = self.agent_name_var.get()
        data["gender"] = self.gender_var.get()
        data["pronouns"] = self.pronouns_var.get()
        # The default is stored as "" so the profile keeps following the built-in prompt when it is updated.
        data["system_prompt"] = "" if self._prompt_is_default() else self._prompt_text_value()
        try:
            profiles.manager.save_profile(profiles.BotProfile.from_dict(data))
        except Exception as e:
            messagebox.showerror(tr("Couldn't save"), str(e))
            return
        known = {t.strip("{}") for t, _ in prompt_template.VARIABLES}
        unknown = sorted({m for m in re.findall(r"\{([A-Z][A-Z_]*)\}", self._prompt_text_value())} - known)
        msg = tr("✓ saved for {name} — restart that bot to apply", name=p.name)
        if unknown:
            msg += "\n" + tr("Not a variable (sent as typed): {names}",
                              names=", ".join("{" + u + "}" for u in unknown))
        self.personal_status.config(text=msg, fg=C["yellow"] if unknown else C["green"])
        self._refresh_profiles_list()

    def _preview_prompt(self):
        p = profiles.manager.get_profile(self._personal_pid or "")
        text = prompt_template.render(
            self._prompt_text_value() if self._prompt_text_value().strip() else prompt_template.DEFAULT_PROMPT_TEMPLATE,
            name=self.agent_name_var.get() or (p.name if p else ""),
            gender=self.gender_var.get(), pronouns=self.pronouns_var.get(),
            model=p.agent_model if p else "",
            source=prompt_template.source_label(
                p.agent_provider if p else "", p.ollama_mode if p else "", p.ollama_host if p else "",
                bool(p and (p.api_key or p.api_keys))) if p else "",
        )
        dlg = tk.Toplevel(self)
        dlg.title(tr("Prompt preview"))
        dlg.geometry("720x520")
        dlg.configure(bg=C["bg"])
        dlg.transient(self)
        tk.Label(dlg, text=tr("What the model sees (variables filled in; integration tools and memory come after)"),
                 bg=C["bg"], fg=C["subtext"], font=("monospace", 9)).pack(anchor="w", padx=12, pady=(10, 4))
        st = scrolledtext.ScrolledText(dlg, wrap="word", bg=C["panel"], fg=C["text"], relief="flat",
                                       font=("monospace", 9), padx=8, pady=6)
        st.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        st.insert("1.0", text)
        st.config(state="disabled")

    # ── tab 2: dependencies ─────────────────────────────────────────────────

    def _build_deps_tab(self):
        f = self.tab_deps
        tk.Label(f, text="Python packages", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 11, "bold")).pack(anchor="w", pady=(14, 6))

        self.dep_rows = {}
        for pkg in REQUIRED_PACKAGES:
            row = tk.Frame(f, bg=C["bg"])
            row.pack(anchor="w", fill="x", pady=1)
            dot = tk.Label(row, text="●", bg=C["bg"], fg=C["yellow"], font=("monospace", 11))
            dot.pack(side="left")
            tk.Label(row, text=f"  {REQUIRED_PACKAGES[pkg]}", bg=C["bg"], fg=C["subtext"],
                      font=("monospace", 10), width=20, anchor="w").pack(side="left")
            self.dep_rows[pkg] = dot

        btn_row = tk.Frame(f, bg=C["bg"])
        btn_row.pack(anchor="w", pady=12)
        tk.Button(btn_row, text="Re-check", command=self._refresh_dependency_status,
                   bg=C["panel2"], fg=C["text"], relief="flat", padx=10, pady=6,
                   cursor="hand2").pack(side="left")
        tk.Button(btn_row, text="Install missing", command=self._install_missing,
                   bg=C["accent"], fg=C["bg"], relief="flat", padx=10, pady=6,
                   font=("sans-serif", 9, "bold"), cursor="hand2").pack(side="left", padx=8)

        ttk.Separator(f, orient="horizontal").pack(fill="x", pady=14)

        tk.Label(f, text="Ollama", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 11, "bold")).pack(anchor="w")
        self.ollama_status_lbl = tk.Label(f, text="checking...", bg=C["bg"], fg=C["yellow"],
                                            font=("monospace", 9), justify="left")
        self.ollama_status_lbl.pack(anchor="w", pady=4)

        self.model_list = tk.Listbox(f, bg=C["panel"], fg=C["subtext"], relief="flat",
                                       height=5, selectbackground=C["accent"],
                                       selectforeground=C["bg"])
        self.model_list.pack(anchor="w", fill="x", pady=(0, 8))

        btn_row2 = tk.Frame(f, bg=C["bg"])
        btn_row2.pack(anchor="w")
        tk.Button(btn_row2, text="Re-check Ollama", command=self._refresh_ollama_status,
                   bg=C["panel2"], fg=C["text"], relief="flat", padx=10, pady=6,
                   cursor="hand2").pack(side="left")
        tk.Button(btn_row2, text="Pull recommended model (qwen2.5)",
                   command=lambda: self._pull_model("qwen2.5"),
                   bg=C["panel2"], fg=C["text"], relief="flat", padx=10, pady=6,
                   cursor="hand2").pack(side="left", padx=8)

    def _refresh_dependency_status(self):
        for pkg, dot in self.dep_rows.items():
            ok = self._is_importable(pkg)
            dot.config(fg=C["green"] if ok else C["red"])

    @staticmethod
    def _is_importable(module_name: str) -> bool:
        r = subprocess.run([PYTHON, "-c", f"import {module_name}"],
                            capture_output=True)
        return r.returncode == 0

    def _install_missing(self):
        missing = [pkg for pkg, dot in self.dep_rows.items()
                   if dot.cget("fg") != C["green"]]
        if not missing:
            messagebox.showinfo(tr("Nothing to do"), tr("All required packages are already installed."))
            return
        pip_names = [REQUIRED_PACKAGES[p] for p in missing]

        def worker():
            self.log_queue.put(("setup", f"$ pip install {' '.join(pip_names)}\n"))
            proc = subprocess.Popen(
                [PYTHON, "-m", "pip", "install", *pip_names],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            )
            for line in proc.stdout:
                self.log_queue.put(("setup", line))
            proc.wait()
            self.log_queue.put(("setup", "\n[pip install finished]\n"))
            self.after(0, self._refresh_dependency_status)

        threading.Thread(target=worker, daemon=True).start()
        self._switch_to_logs_hint()

    def _refresh_ollama_status(self):
        if self.ollama_mode_var.get() == "cloud":
            self.ollama_status_lbl.config(
                text=tr("Ollama Cloud mode — no local Ollama needed (uses your API key)"),
                fg=C["green"])
            return
        if not check_ollama_installed():
            self.ollama_status_lbl.config(
                text=tr("ollama not found — install from https://ollama.com"), fg=C["red"])
            return
        if not check_ollama_running():
            self.ollama_status_lbl.config(
                text=tr("installed but not running — run: ollama serve"), fg=C["yellow"])
            return
        models = get_installed_models()
        self.model_list.delete(0, "end")
        for m in models:
            self.model_list.insert("end", m)
        if models:
            self.ollama_status_lbl.config(
                text=tr("✓  running — {n} model(s) installed", n=len(models)), fg=C["green"])
        else:
            self.ollama_status_lbl.config(
                text=tr("running, but no models pulled — run: ollama pull qwen2.5"), fg=C["yellow"])

    def _pull_model(self, model_name: str):
        def worker():
            self.log_queue.put(("setup", f"$ ollama pull {model_name}\n"))
            proc = subprocess.Popen(["ollama", "pull", model_name],
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, bufsize=1)
            for line in proc.stdout:
                self.log_queue.put(("setup", line))
            proc.wait()
            self.log_queue.put(("setup", f"\n[ollama pull {model_name} finished]\n"))
            self.after(0, self._refresh_ollama_status)

        threading.Thread(target=worker, daemon=True).start()
        self._switch_to_logs_hint()

    def _switch_to_logs_hint(self):
        if not hasattr(self, "_hinted"):
            self._hinted = True
            messagebox.showinfo(tr("Working..."), tr("Progress is streaming into the 'Run && Logs' tab."))

    # ── tab 3: run & logs ────────────────────────────────────────────────────

    def _build_run_tab(self):
        f = self.tab_run

        controls = tk.Frame(f, bg=C["bg"])
        controls.pack(fill="x", pady=(10, 6))

        # Discord bot controls
        bot_box = tk.Frame(controls, bg=C["panel"], padx=12, pady=10)
        bot_box.pack(side="left", padx=(0, 8), fill="both", expand=True)
        self.run_bot_title = tk.Label(bot_box, text="Selected bot", bg=C["panel"], fg=C["text"],
                                       font=("sans-serif", 10, "bold"))
        self.run_bot_title.pack(anchor="w")
        self.bot_status_dot = tk.Label(bot_box, text=tr("●  stopped"), bg=C["panel"], fg=C["red"],
                                         font=("monospace", 9))
        self.bot_status_dot.pack(anchor="w", pady=(2, 6))
        br = tk.Frame(bot_box, bg=C["panel"])
        br.pack(anchor="w")
        tk.Button(br, text="▶ Start", command=self._start_bot,
                   bg=C["green"], fg=C["bg"], relief="flat", padx=10, pady=4,
                   cursor="hand2").pack(side="left")
        tk.Button(br, text="■ Stop", command=self._stop_bot,
                   bg=C["red"], fg=C["bg"], relief="flat", padx=10, pady=4,
                   cursor="hand2").pack(side="left", padx=6)
        tk.Checkbutton(bot_box, text="Auto-restart on crash", variable=self.bot_auto_restart,
                        command=self._on_run_auto_restart_toggle,
                        bg=C["panel"], fg=C["subtext"], selectcolor=C["bg"],
                        activebackground=C["panel"], font=("monospace", 8)).pack(anchor="w", pady=(6, 0))

        # Web UI controls
        webui_box = tk.Frame(controls, bg=C["panel"], padx=12, pady=10)
        webui_box.pack(side="left", fill="both", expand=True)
        tk.Label(webui_box, text="Local web UI", bg=C["panel"], fg=C["text"],
                  font=("sans-serif", 10, "bold")).pack(anchor="w")
        self.webui_status_dot = tk.Label(webui_box, text=tr("●  stopped"), bg=C["panel"], fg=C["red"],
                                           font=("monospace", 9))
        self.webui_status_dot.pack(anchor="w", pady=(2, 6))
        wr = tk.Frame(webui_box, bg=C["panel"])
        wr.pack(anchor="w")
        tk.Button(wr, text="▶ Start", command=self._start_webui,
                   bg=C["green"], fg=C["bg"], relief="flat", padx=10, pady=4,
                   cursor="hand2").pack(side="left")
        tk.Button(wr, text="■ Stop", command=self._stop_webui,
                   bg=C["red"], fg=C["bg"], relief="flat", padx=10, pady=4,
                   cursor="hand2").pack(side="left", padx=6)
        tk.Button(wr, text="Open", command=lambda: webbrowser.open("http://localhost:7842"),
                   bg=C["panel2"], fg=C["text"], relief="flat", padx=10, pady=4,
                   cursor="hand2").pack(side="left", padx=6)
        tk.Checkbutton(webui_box, text="Auto-restart on crash", variable=self.webui_auto_restart,
                        bg=C["panel"], fg=C["subtext"], selectcolor=C["bg"],
                        activebackground=C["panel"], font=("monospace", 8)).pack(anchor="w", pady=(6, 0))

        stoat_box = tk.Frame(controls, bg=C["panel"], padx=12, pady=10)
        stoat_box.pack(side="left", fill="both", expand=True)
        tk.Label(stoat_box, text="Stoat bot", bg=C["panel"], fg=C["text"],
                  font=("sans-serif", 10, "bold")).pack(anchor="w")
        self.stoat_status_dot = tk.Label(stoat_box, text=tr("●  stopped"), bg=C["panel"], fg=C["red"],
                                          font=("monospace", 9))
        self.stoat_status_dot.pack(anchor="w", pady=(2, 6))
        sr = tk.Frame(stoat_box, bg=C["panel"])
        sr.pack(anchor="w")
        tk.Button(sr, text="▶ Start", command=self._start_stoat,
                   bg=C["green"], fg=C["bg"], relief="flat", padx=10, pady=4,
                   cursor="hand2").pack(side="left")
        tk.Button(sr, text="■ Stop", command=self._stop_stoat,
                   bg=C["red"], fg=C["bg"], relief="flat", padx=10, pady=4,
                   cursor="hand2").pack(side="left", padx=6)
        tk.Checkbutton(stoat_box, text="Auto-restart on crash", variable=self.stoat_auto_restart,
                        bg=C["panel"], fg=C["subtext"], selectcolor=C["bg"],
                        activebackground=C["panel"], font=("monospace", 8)).pack(anchor="w", pady=(6, 0))

        tk.Label(f, text="Logs", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(6, 2))
        self.log_box = scrolledtext.ScrolledText(
            f, bg="#11111b", fg=C["subtext"], insertbackground=C["text"],
            font=("monospace", 9), relief="flat", height=20, wrap="word",
        )
        self.log_box.pack(fill="both", expand=True)
        self.log_box.tag_config("bot", foreground="#89b4fa")
        self.log_box.tag_config("webui", foreground="#a6e3a1")
        self.log_box.tag_config("setup", foreground="#f9e2af")
        self.log_box.tag_config("stoat", foreground="#cba6f7")
        self._sync_run_tab_to_profile()

    def _start_stoat(self):
        env = load_env()
        if not env.get("STOAT_BOT_TOKEN"):
            messagebox.showwarning(tr("Not configured"),
                                     tr("Add your Stoat bot token in the Setup tab first."))
            return
        if not self._ensure_runas():
            return
        self.stoat_proc.start()
        self.stoat_status_dot.config(text=tr("●  running"), fg=C["green"])

    def _stop_stoat(self):
        self.stoat_proc.stop()
        self.stoat_status_dot.config(text=tr("●  stopped"), fg=C["red"])

    def _require_config(self) -> bool:
        env = load_env()
        if not env.get("DISCORD_BOT_TOKEN"):
            messagebox.showwarning(tr("Not configured"),
                                     tr("Save your config in the Setup tab first."))
            return False
        return True

    # The Run tab's bot box controls the profile selected in the sidebar, through the same
    # supervisor as the Profiles tab (one launcher per profile, so a bot can't be started twice).
    def _run_profile(self):
        try:
            return profiles.manager.get_active_profile()
        except Exception:
            return None

    def _update_run_bot_status(self):
        p = self._run_profile()
        if p is None or not hasattr(self, "bot_status_dot"):
            return
        if profiles.supervisor.is_running(p.id):
            self.bot_status_dot.config(text=tr("●  running"), fg=C["green"])
        else:
            self.bot_status_dot.config(text=tr("●  stopped"), fg=C["red"])

    def _sync_run_tab_to_profile(self):
        """Title, status and auto-restart box follow the selected profile."""
        p = self._run_profile()
        if p is None or not hasattr(self, "run_bot_title"):
            return
        self.run_bot_title.config(text=f"Bot: {p.name}")
        self.bot_auto_restart.set(bool(p.auto_restart))
        self._update_run_bot_status()

    def _on_run_auto_restart_toggle(self):
        p = self._run_profile()
        if p is None:
            return
        p.auto_restart = bool(self.bot_auto_restart.get())
        try:
            profiles.manager.save_profile(p)
        except Exception as e:
            messagebox.showerror(tr("Couldn't save"), str(e))

    def _start_bot(self):
        p = self._run_profile()
        if p is None:
            messagebox.showwarning(tr("Not configured"), tr("No profile selected."))
            return
        ok, msg = profiles.supervisor.start_instance(p.id)
        if not ok:
            messagebox.showerror("Start Error", msg)
        self._update_run_bot_status()
        if hasattr(self, "_refresh_profiles_list"):
            self._refresh_profiles_list()

    def _stop_bot(self):
        p = self._run_profile()
        if p is None:
            return
        profiles.supervisor.stop_instance(p.id)
        self._update_run_bot_status()
        if hasattr(self, "_refresh_profiles_list"):
            self._refresh_profiles_list()

    def _start_webui(self):
        if not self._ensure_runas():
            return
        self.webui_proc.start()
        self.webui_status_dot.config(text=tr("●  running"), fg=C["green"])

    def _stop_webui(self):
        self.webui_proc.stop()
        self.webui_status_dot.config(text=tr("●  stopped"), fg=C["red"])

    # ── tab 4: local approvals ───────────────────────────────────────────────

    def _build_approvals_tab(self):
        f = self.tab_approvals
        tk.Label(f, text="Pending approvals", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 11, "bold")).pack(anchor="w", pady=(14, 6))
        tk.Label(f, text="Destructive commands (rm, sudo, apt install, etc.) show up here too — "
                          "approve or deny without needing Discord open.",
                  bg=C["bg"], fg=C["subtext"], font=("monospace", 9),
                  wraplength=680, justify="left").pack(anchor="w", pady=(0, 8))

        columns = ("id", "command", "age")
        self.approval_tree = ttk.Treeview(f, columns=columns, show="headings", height=10)
        for col, w in [("id", 80), ("command", 480), ("age", 70)]:
            self.approval_tree.heading(col, text=tr(col.capitalize()))
            self.approval_tree.column(col, width=w, anchor="w")
        self.approval_tree.pack(fill="both", expand=True, pady=(0, 8))

        btn_row = tk.Frame(f, bg=C["bg"])
        btn_row.pack(anchor="w")
        tk.Button(btn_row, text="Approve selected", command=lambda: self._resolve_selected(True),
                   bg=C["green"], fg=C["bg"], relief="flat", padx=10, pady=6,
                   font=("sans-serif", 9, "bold"), cursor="hand2").pack(side="left")
        tk.Button(btn_row, text="Deny selected", command=lambda: self._resolve_selected(False),
                   bg=C["red"], fg=C["bg"], relief="flat", padx=10, pady=6,
                   font=("sans-serif", 9, "bold"), cursor="hand2").pack(side="left", padx=8)

        # ── approval policy / editable command list / usage display ───────
        cfg = tk.Frame(f, bg=C["bg"])
        cfg.pack(fill="x", pady=(12, 2))
        tk.Label(cfg, text="Approval policy", bg=C["bg"], fg=C["text"],
                 font=("sans-serif", 10, "bold")).pack(anchor="w")
        self.approval_mode_var = tk.StringVar(value="ask")
        mode_row = tk.Frame(cfg, bg=C["bg"])
        mode_row.pack(anchor="w", pady=(2, 6))
        tk.Label(mode_row, text="Mode:", bg=C["bg"], fg=C["text"]).pack(side="left")
        self.approval_mode_combo = ttk.Combobox(mode_row, textvariable=self.approval_mode_var,
                                                 values=["ask", "approve_all", "deny_all"],
                                                 state="readonly", width=14)
        self.approval_mode_combo.pack(side="left", padx=6)

        self.show_cmds_var = tk.BooleanVar(value=False)
        self.show_tools_var = tk.BooleanVar(value=False)
        tog = tk.Frame(cfg, bg=C["bg"])
        tog.pack(anchor="w", pady=(0, 6))
        tk.Checkbutton(tog, text="Show commands used before reply",
                       variable=self.show_cmds_var, bg=C["bg"], fg=C["text"],
                       selectcolor=C["panel"]).pack(side="left", padx=(0, 12))
        tk.Checkbutton(tog, text="Show tools used before reply",
                       variable=self.show_tools_var, bg=C["bg"], fg=C["text"],
                       selectcolor=C["panel"]).pack(side="left")

        tk.Label(cfg, text="Commands requiring approval (one per line or comma):",
                 bg=C["bg"], fg=C["text"], font=("monospace", 9)).pack(anchor="w", pady=(6, 2))
        self.approval_cmds_text = tk.Text(cfg, height=6, bg=C["panel"], fg=C["text"],
                                         insertbackground=C["text"], relief="flat", font=("monospace", 9))
        self.approval_cmds_text.pack(fill="x")
        tk.Button(cfg, text="Save policy", command=self._save_approval_policy,
                  bg=C["accent"], fg=C["bg"], relief="flat", padx=10, pady=4,
                  font=("sans-serif", 9, "bold"), cursor="hand2").pack(anchor="w", pady=(6, 0))

        self._load_approval_policy()
        self.after(1000, self._refresh_approvals)

    def _refresh_approvals(self):
        try:
            pending = approvals_store.list_pending()
            selected = set(self.approval_tree.selection())
            self.approval_tree.delete(*self.approval_tree.get_children())
            now = time.time()
            for req in pending:
                age = int(now - req.get("created_at", now))
                iid = req["id"]
                self.approval_tree.insert("", "end", iid=iid,
                                            values=(iid, req["command"][:100], f"{age}s"))
                if iid in selected:
                    self.approval_tree.selection_add(iid)
        except Exception:
            pass
        self.after(1000, self._refresh_approvals)

    def _resolve_selected(self, approved: bool):
        sel = self.approval_tree.selection()
        if not sel:
            return
        for req_id in sel:
            approvals_store.resolve(req_id, approved)
        self._refresh_approvals()

    def _load_approval_policy(self):
        try:
            env = load_env()
            self.approval_mode_var.set(env.get("APPROVAL_MODE", "ask"))
            self.show_cmds_var.set(env.get("SHOW_COMMANDS_USED", "").lower() in ("1", "true", "yes", "on"))
            self.show_tools_var.set(env.get("SHOW_TOOLS_USED", "").lower() in ("1", "true", "yes", "on"))
            cmds = env.get("APPROVAL_COMMANDS", "")
            if not cmds:
                try:
                    import tools as _t
                    cmds = ",".join(_t.DEFAULT_APPROVAL_COMMANDS)
                except Exception:
                    cmds = ""
            self.approval_cmds_text.delete("1.0", "end")
            self.approval_cmds_text.insert("1.0", cmds.replace(",", "\n"))
        except Exception:
            pass

    def _save_approval_policy(self):
        try:
            env = load_env()
            env["APPROVAL_MODE"] = self.approval_mode_var.get().strip()
            env["SHOW_COMMANDS_USED"] = "true" if self.show_cmds_var.get() else ""
            env["SHOW_TOOLS_USED"] = "true" if self.show_tools_var.get() else ""
            raw = self.approval_cmds_text.get("1.0", "end-1c")
            items = [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()]
            env["APPROVAL_COMMANDS"] = ",".join(items)
            save_env(env)
            messagebox.showinfo("Saved", "Approval policy saved. Restart the bot to apply.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _build_experiments_tab(self):
        f = self.tab_experiments
        if not hasattr(self, "js_integrations_var"):
            try:
                env = load_env()
                enabled = env.get("JS_INTEGRATIONS_ENABLED", "").lower() in ("1", "true", "yes", "on")
            except Exception:
                enabled = False
            self.js_integrations_var = tk.BooleanVar(value=enabled)
        tk.Label(f, text="Experiments (unstable)", bg=C["bg"], fg=C["text"],
                 font=("sans-serif", 11, "bold")).pack(anchor="w", pady=(14, 6))
        tk.Label(f, text="JavaScript integration support is EXPERIMENTAL and off by default. "
                          "It is not implemented in the shipped bot yet; enabling it only records "
                          "your preference. You must install Node.js and the required npm "
                          "dependencies before anything will run.",
                 bg=C["bg"], fg=C["subtext"], font=("monospace", 9), wraplength=680,
                 justify="left").pack(anchor="w", pady=(0, 8))
        tk.Checkbutton(f, text="Enable JS integrations (experimental)",
                       variable=self.js_integrations_var, bg=C["bg"], fg=C["text"],
                       selectcolor=C["panel"]).pack(anchor="w", pady=(0, 8))
        box = tk.Text(f, height=8, bg=C["panel"], fg=C["text"], relief="flat",
                      font=("monospace", 9))
        box.pack(fill="x", pady=(4, 8))
        box.insert("1.0",
            "Required dependencies:\n"
            "  1) Node.js 18+  (nodejs.org or your package manager)\n"
            "  2) npm install discord.js  (for voice/chat bridges)\n"
            "  3) ffmpeg (for audio)\n\n"
            "Status check not available yet.")
        box.config(state="disabled")
        tk.Button(f, text="Save", command=self._save_experiments,
                  bg=C["green"], fg=C["bg"], relief="flat", padx=10, pady=4,
                  font=("sans-serif", 9, "bold"), cursor="hand2").pack(anchor="w", pady=(6, 0))

    def _save_experiments(self):
        try:
            env = load_env()
            env["JS_INTEGRATIONS_ENABLED"] = "true" if self.js_integrations_var.get() else ""
            save_env(env)
            messagebox.showinfo("Saved", "Experiment settings saved. Restart the bot to apply.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    # ── tab 5: integrations ──────────────────────────────────────────────────

    def _build_integrations_tab(self):
        f = self.tab_integrations
        top = tk.Frame(f, bg=C["bg"])
        top.pack(fill="x", pady=(14, 6))
        tk.Label(top, text="Integrations", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 11, "bold")).pack(side="left")
        tk.Button(top, text="↻ Rescan", command=self._refresh_integrations,
                   bg=C["panel2"], fg=C["text"], relief="flat", padx=10, pady=3,
                   cursor="hand2").pack(side="right")
        tk.Label(f, text="Switch extra tool packs on or off and set their options. Each profile has its "
                          "own settings; anything a profile hasn't changed follows the Global ones. "
                          "Changes take effect the next time that bot starts.",
                  bg=C["bg"], fg=C["subtext"], font=("monospace", 9),
                  wraplength=680, justify="left").pack(anchor="w", pady=(0, 6))
        scope_row = tk.Frame(f, bg=C["bg"])
        scope_row.pack(anchor="w", pady=(0, 6))
        tk.Label(scope_row, text="Editing settings for:", bg=C["bg"], fg=C["text"],
                 font=("sans-serif", 9, "bold")).pack(side="left", padx=(0, 6))
        self.int_scope_combo = ttk.Combobox(scope_row, state="readonly", width=44)
        self.int_scope_combo.pack(side="left")
        self.int_scope_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_integrations())
        self._refresh_int_scope_choices()
        self._build_api_token_box(f)
        self.integrations_status = tk.Label(f, text="", bg=C["bg"], fg=C["green"],
                                            font=("monospace", 9))
        self.integrations_status.pack(anchor="w", pady=(0, 4))

        # scrollable list of integration cards
        outer = tk.Frame(f, bg=C["bg"])
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=C["bg"], highlightthickness=0)
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.integrations_list = tk.Frame(canvas, bg=C["bg"])
        win = canvas.create_window((0, 0), window=self.integrations_list, anchor="nw")
        self.integrations_list.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))

        def _wheel(e):
            if e.num == 4:
                canvas.yview_scroll(-3, "units")
            elif e.num == 5:
                canvas.yview_scroll(3, "units")
            else:
                canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

        canvas.bind("<Enter>", lambda _e: [canvas.bind_all(ev, _wheel)
                                           for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>")])
        canvas.bind("<Leave>", lambda e: None if str(getattr(e, "detail", "")) == "NotifyInferior"
                    else [canvas.unbind_all(ev) for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>")])

        self._refresh_integrations()

    def _build_api_token_box(self, parent: tk.Frame):
        """Token for the HTTP API (API.md). The web UI server reads it on every request,
        so a new token works at once and the old one stops working."""
        box = tk.Frame(parent, bg=C["panel"], padx=12, pady=8)
        box.pack(fill="x", pady=(0, 8))
        tk.Label(box, text="API token", bg=C["panel"], fg=C["text"],
                  font=("sans-serif", 10, "bold")).pack(anchor="w")
        tk.Label(box, text="For scripts and other programs. The API is served by the Local web UI at "
                           "http://127.0.0.1:7842/api/v1 (see API.md). Keep the token secret.",
                  bg=C["panel"], fg=C["subtext"], font=("monospace", 9),
                  wraplength=640, justify="left").pack(anchor="w", pady=(2, 6))
        row = tk.Frame(box, bg=C["panel"])
        row.pack(anchor="w")
        self.api_token_var = tk.StringVar(value=settings.get_api_token(ENV_PATH))
        entry = tk.Entry(row, textvariable=self.api_token_var, show="•", state="readonly",
                          readonlybackground=C["bg"], fg=C["text"], relief="flat", width=46)
        entry.pack(side="left")
        shown = {"on": False}

        def toggle_show():
            shown["on"] = not shown["on"]
            entry.config(show="" if shown["on"] else "•")
            show_btn.config(text=tr("🙈 Hide") if shown["on"] else tr("👁 Show"))

        def generate():
            if self.api_token_var.get() and not messagebox.askyesno(
                    tr("Replace the API token?"),
                    tr("Anything using the current token will stop working.")):
                return
            self.api_token_var.set(settings.new_api_token(ENV_PATH))
            self.integrations_status.config(text=tr("✓ new API token saved"))
            self.after(4000, lambda: self.integrations_status.config(text=""))

        def copy():
            if self.api_token_var.get():
                self.clipboard_clear()
                self.clipboard_append(self.api_token_var.get())
                self.integrations_status.config(text=tr("✓ copied"))
                self.after(2500, lambda: self.integrations_status.config(text=""))

        tk.Button(row, text=tr("🔑 Generate"), command=generate, bg=C["accent"], fg=C["bg"],
                   relief="flat", padx=10, pady=3, cursor="hand2").pack(side="left", padx=(8, 4))
        show_btn = tk.Button(row, text=tr("👁 Show"), command=toggle_show, bg=C["panel2"],
                              fg=C["text"], relief="flat", padx=10, pady=3, cursor="hand2")
        show_btn.pack(side="left", padx=4)
        tk.Button(row, text=tr("📋 Copy"), command=copy, bg=C["panel2"], fg=C["text"],
                   relief="flat", padx=10, pady=3, cursor="hand2").pack(side="left", padx=4)

    # ── integration settings scope: one profile, or the global .env ──────────

    GLOBAL_SCOPE_LABEL = "🌐 Global defaults (.env, every profile)"

    def _active_profile_label(self) -> str:
        try:
            p = profiles.manager.get_active_profile()
            return f"{p.icon} {p.name} ({p.id})"
        except Exception:
            return ""

    def _refresh_int_scope_choices(self):
        """Offer 'this profile' (the active one) and 'global'; keep the user's choice across profile switches."""
        if not hasattr(self, "int_scope_combo"):
            return
        was_global = self.int_scope_combo.get() == self.GLOBAL_SCOPE_LABEL
        mine = self._active_profile_label()
        self.int_scope_combo["values"] = [x for x in (mine, self.GLOBAL_SCOPE_LABEL) if x]
        self.int_scope_combo.set(self.GLOBAL_SCOPE_LABEL if (was_global or not mine) else mine)

    def _int_scope_is_profile(self) -> bool:
        combo = getattr(self, "int_scope_combo", None)
        return bool(combo and combo.get() and combo.get() != self.GLOBAL_SCOPE_LABEL)

    def _int_scope_name(self) -> str:
        try:
            return profiles.manager.get_active_profile().name
        except Exception:
            return "this profile"

    def _int_env_path(self) -> str:
        """The file the Integrations tab reads and writes right now (see integration_settings.py)."""
        if self._int_scope_is_profile():
            try:
                return settings.profile_env_path(profiles.manager.get_active_profile().id)
            except Exception:
                pass
        return ENV_PATH

    def _refresh_integrations(self):
        """Rebuild the cards from the integrations folder and the INTEGRATIONS value in .env."""
        for w in self.integrations_list.winfo_children():
            w.destroy()
        self.integration_vars = {}
        rows = integrations.panel_entries(settings.load_env(self._int_env_path()).get("INTEGRATIONS", ""))
        if not rows:
            tk.Label(self.integrations_list,
                     text=tr("No integrations were found in the integrations folder."),
                     bg=C["bg"], fg=C["subtext"], font=("monospace", 9)).pack(anchor="w", pady=8)
        for row in rows:
            self._build_integration_card(row)

    def _build_integration_card(self, row: dict):
        card = tk.Frame(self.integrations_list, bg=C["panel"], padx=12, pady=10)
        card.pack(fill="x", pady=(0, 8))

        head = tk.Frame(card, bg=C["panel"])
        head.pack(fill="x")
        var = tk.BooleanVar(value=row["enabled"])
        self.integration_vars[row["name"]] = var
        title = f"{row['icon']}  {row['title']}" if row["icon"] else row["title"]
        status = tk.Label(head, bg=C["panel"], font=("monospace", 9))

        def paint():
            on = var.get()
            status.config(text=tr("●  enabled") if on else tr("●  disabled"),
                          fg=C["green"] if on else C["red"])

        tk.Checkbutton(head, text=title, variable=var,
                        command=lambda: self._toggle_integration(row["name"], var, paint),
                        bg=C["panel"], fg=C["text"], selectcolor=C["bg"],
                        activebackground=C["panel"], activeforeground=C["text"],
                        font=("sans-serif", 10, "bold"), cursor="hand2").pack(side="left")
        status.pack(side="right")
        paint()

        if row["description"]:
            tk.Label(card, text=row["description"], bg=C["panel"], fg=C["subtext"],
                      font=("monospace", 9), wraplength=640, justify="left").pack(
                anchor="w", pady=(4, 0))

        if row.get("problems"):
            tk.Label(card, text="⚠ " + "\n⚠ ".join(row["problems"]), bg=C["panel"], fg=C["yellow"],
                      font=("monospace", 9), wraplength=640, justify="left").pack(anchor="w", pady=(4, 0))

        if row["submenu"]:
            self._build_integration_submenu(card, row)

    def _build_integration_submenu(self, card: tk.Frame, row: dict):
        """Collapsible submenu. The entries come from the integration's PANEL field.
        Entries listed in INTEGRATION_PAGES draw a real page; the rest show a stub."""
        body = tk.Frame(card, bg=C["panel"])
        pills = tk.Frame(body, bg=C["panel"])
        pills.pack(anchor="w", pady=(6, 4))
        page = tk.Frame(body, bg=C["panel2"], padx=10, pady=8)
        page.pack(fill="x")
        buttons = {}

        def show(item):
            for iid, b in buttons.items():
                b.config(bg=C["accent"] if iid == item["id"] else C["panel2"],
                         fg=C["bg"] if iid == item["id"] else C["text"])
            for w in page.winfo_children():
                w.destroy()
            tk.Label(page, text=item["title"], bg=C["panel2"], fg=C["text"],
                      font=("sans-serif", 10, "bold")).pack(anchor="w")
            if item["description"]:
                tk.Label(page, text=item["description"], bg=C["panel2"], fg=C["subtext"],
                          font=("monospace", 9), wraplength=620, justify="left").pack(
                    anchor="w", pady=(2, 4))
            drawer = self.INTEGRATION_PAGES.get((row["name"], item["id"]))
            if drawer:
                getattr(self, drawer)(page)
            elif item.get("fields"):
                self._page_fields(page, row["name"], item["id"])
            else:
                tk.Label(page, text=tr("🚧 Not wired up yet."), bg=C["panel2"], fg=C["yellow"],
                          font=("monospace", 9)).pack(anchor="w")

        for item in row["submenu"]:
            b = tk.Button(pills, text=item["title"], command=lambda it=item: show(it),
                           bg=C["panel2"], fg=C["text"], relief="flat", padx=10, pady=3,
                           cursor="hand2")
            b.pack(side="left", padx=(0, 6))
            buttons[item["id"]] = b
        show(row["submenu"][0])

        toggle = tk.Button(card, text=tr("▸ Settings"), bg=C["panel"], fg=C["accent"],
                            relief="flat", activebackground=C["panel"], cursor="hand2",
                            font=("monospace", 9), anchor="w")

        def flip():
            if body.winfo_ismapped():
                body.pack_forget()
                toggle.config(text=tr("▸ Settings"))
            else:
                body.pack(fill="x", after=toggle)
                toggle.config(text=tr("▾ Settings"))

        toggle.config(command=flip)
        toggle.pack(anchor="w", pady=(6, 0))

    def _toggle_integration(self, name: str, var: tk.BooleanVar, repaint):
        try:
            settings.set_enabled(name, var.get(), self._int_env_path())
        except KeyError:
            var.set(not var.get())
            self._refresh_integrations()
            return
        repaint()
        self._integrations_saved()

    def _integrations_saved(self):
        where = self._int_scope_name() if self._int_scope_is_profile() else ".env"
        self.integrations_status.config(
            text=tr("✓ saved to {where} — restart the bot to apply", where=where))
        self.after(4000, lambda: self.integrations_status.config(text=""))

    # ── generated settings pages (an integration's PANEL "fields") ───────────

    def _page_fields(self, page: tk.Frame, name: str, page_id: str):
        """Draw a settings page from the integration's declared fields. Nothing here knows
        about any particular integration: types, validation and saving live in
        integration_settings.get_settings / set_settings (shared with the HTTP API)."""
        body = tk.Frame(page, bg=C["panel2"])
        body.pack(fill="x")
        BG = C["panel2"]

        def render():
            for w in body.winfo_children():
                w.destroy()
            try:
                data = settings.get_settings(name, self._int_env_path())
                pg = next(p for p in data["pages"] if p["id"] == page_id)
            except (KeyError, StopIteration):
                tk.Label(body, text=tr("This integration's settings could not be read. Press Rescan."),
                          bg=BG, fg=C["red"], font=("monospace", 9)).pack(anchor="w")
                return
            widgets = {}                     # key -> (field, read(), initial)
            for f in pg["fields"]:
                widgets[f["key"]] = (f,) + self._field_widget(body, f)
            msg = tk.Label(body, text="", bg=BG, fg=C["red"], font=("monospace", 9),
                            wraplength=620, justify="left")

            def save():
                changes = {}
                for key, (f, read, initial) in widgets.items():
                    now = read()
                    if now != initial:
                        changes[key] = now
                if not changes:
                    msg.config(text=tr("Nothing changed."), fg=C["subtext"])
                    return
                try:
                    settings.set_settings(name, changes, self._int_env_path())
                except settings.SettingsError as e:
                    msg.config(text=tr(str(e)), fg=C["red"])
                    return
                render()
                self._integrations_saved()

            def reset():
                if not messagebox.askyesno(tr("Reset to defaults?"),
                                           tr("Every setting on this page goes back to its default.")):
                    return
                settings.set_settings(name, {k: None for k in widgets}, self._int_env_path())
                render()
                self._integrations_saved()

            btns = tk.Frame(body, bg=BG)
            btns.pack(anchor="w", pady=(8, 0))
            tk.Button(btns, text=tr("💾 Save"), command=save, bg=C["green"], fg=C["bg"], relief="flat",
                       padx=10, pady=3, cursor="hand2").pack(side="left", padx=(0, 6))
            tk.Button(btns, text=tr("↺ Reset page to defaults"), command=reset, bg=C["panel"],
                       fg=C["text"], relief="flat", padx=10, pady=3, cursor="hand2").pack(side="left")
            msg.pack(anchor="w", pady=(4, 0))

        render()

    def _field_widget(self, parent: tk.Frame, f: dict):
        """One labelled input for a declared field. Returns (read, initial): read() gives the
        value to save right now (None = back to default), initial is what read() returned when
        drawn, so the page only saves what the user actually changed."""
        BG = C["panel2"]
        t, value = f["type"], f["value"]
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=(0, 8))

        def entry(var, **kw):
            e = tk.Entry(row, textvariable=var, bg=C["panel"], fg=C["text"],
                          insertbackground=C["text"], relief="flat", width=44, **kw)
            e.pack(anchor="w")
            return e

        def hint(text):
            if text:
                tk.Label(row, text=text, bg=BG, fg=C["subtext"], font=("monospace", 9),
                          wraplength=620, justify="left").pack(anchor="w", pady=(2, 0))

        if t == "bool":
            var = tk.BooleanVar(value=bool(value))
            tk.Checkbutton(row, text=f["label"], variable=var, bg=BG, fg=C["text"], selectcolor=C["bg"],
                            activebackground=BG, activeforeground=C["text"],
                            font=("sans-serif", 10), cursor="hand2").pack(anchor="w")
            hint(f["description"])
            return (lambda: var.get()), bool(value)

        tk.Label(row, text=f["label"] + (" *" if f["required"] else ""), bg=BG, fg=C["text"],
                  font=("sans-serif", 10)).pack(anchor="w")

        if t == "choice":
            box = ttk.Combobox(row, state="readonly", width=42, values=f["choices"])
            box.set(value if value in f["choices"] else "")
            box.pack(anchor="w")
            hint(f["description"])
            return (lambda: box.get() or None), (box.get() or None)

        if t == "secret":
            var, clear = tk.StringVar(), tk.BooleanVar()
            entry(var, show="•")
            is_set = bool(value and value.get("set"))
            if is_set:
                tk.Checkbutton(row, text=tr("Clear the saved value"), variable=clear, bg=BG,
                                fg=C["subtext"], selectcolor=C["bg"], activebackground=BG,
                                activeforeground=C["text"], font=("monospace", 9),
                                cursor="hand2").pack(anchor="w")
            hint((tr("A value is saved — type here to replace it. ") if is_set
                  else tr("Not set. ")) + f["description"])
            # blank = leave alone, so the saved secret is never shown or re-sent
            return (lambda: None if clear.get() else (var.get() or "")), ""

        if t == "list":
            shown = ", ".join(value or [])
            hint_extra = tr("Separate items with commas. ")
        elif t == "number":
            shown = "" if value is None else str(value)
            hint_extra = ""
        else:
            shown = "" if value is None else str(value)
            hint_extra = ""
        var = tk.StringVar(value=shown)
        entry(var)
        default = f["default"]
        default_txt = ", ".join(default) if isinstance(default, list) else default
        hint(hint_extra + f["description"]
             + (tr(" (default: {d})", d=default_txt) if default_txt not in (None, "") else ""))
        return (lambda: var.get().strip() or None), (shown.strip() or None)

    # ── discord_admin submenu pages ──────────────────────────────────────────

    def _page_discord_admins(self, page: tk.Frame):
        """Manage DISCORD_ADMIN_USER_IDS (extra people who may use the admin tools)."""
        tk.Label(page, text=tr("The server owner can always use these tools. Add the Discord user "
                               "IDs of anyone else who may (right-click a user → Copy User ID, with "
                               "Developer Mode on)."),
                  bg=C["panel2"], fg=C["subtext"], font=("monospace", 9),
                  wraplength=620, justify="left").pack(anchor="w", pady=(0, 6))
        lst = tk.Listbox(page, height=6, bg=C["panel"], fg=C["text"], relief="flat",
                          selectbackground=C["accent"], selectforeground=C["bg"],
                          highlightthickness=0, exportselection=False)
        lst.pack(fill="x", pady=(0, 6))
        new_var = tk.StringVar()
        msg = tk.Label(page, text="", bg=C["panel2"], fg=C["red"], font=("monospace", 9))

        def reload():
            lst.delete(0, "end")
            for i in das.get_admin_ids(self._int_env_path()):
                lst.insert("end", i)

        def add(_e=None):
            try:
                das.add_admin(new_var.get(), self._int_env_path())
            except settings.SettingsError as e:
                msg.config(text=tr(str(e)))
                return
            msg.config(text="")
            new_var.set("")
            reload()
            self._integrations_saved()

        def remove():
            sel = [lst.get(i) for i in lst.curselection()]
            if sel:
                das.remove_admins(sel, self._int_env_path())
                reload()
                self._integrations_saved()

        row = tk.Frame(page, bg=C["panel2"])
        row.pack(anchor="w")
        ent = tk.Entry(row, textvariable=new_var, bg=C["panel"], fg=C["text"],
                        insertbackground=C["text"], relief="flat", width=32)
        ent.pack(side="left")
        ent.bind("<Return>", add)
        tk.Button(row, text=tr("➕ Add"), command=add, bg=C["green"], fg=C["bg"], relief="flat",
                   padx=10, pady=3, cursor="hand2").pack(side="left", padx=6)
        tk.Button(row, text=tr("🗑 Remove"), command=remove, bg=C["red"], fg=C["bg"],
                   relief="flat", padx=10, pady=3, cursor="hand2").pack(side="left")
        msg.pack(anchor="w", pady=(4, 0))
        reload()

    def _page_discord_approvals(self, page: tk.Frame):
        """Global approval mode plus a per-action override (always ask / never ask)."""
        try:
            mod = importlib.import_module("integrations.discord_admin")
            actions = mod.panel_actions()
        except Exception as e:
            tk.Label(page, text=tr("Couldn't load discord_admin ({e}). Install the missing packages "
                                   "on the Dependencies tab, then press Rescan.", e=e),
                      bg=C["panel2"], fg=C["red"], font=("monospace", 9),
                      wraplength=620, justify="left").pack(anchor="w")
            return
        by_name = {a["name"]: a for a in actions}
        modes = {"destructive": tr("Risky actions only (default)"),
                 "all": tr("Everything"), "none": tr("Nothing")}

        def cur_mode():
            return das.get_mode(self._int_env_path())

        top = tk.Frame(page, bg=C["panel2"])
        top.pack(anchor="w", pady=(0, 6))
        tk.Label(top, text=tr("Ask for approval on:"), bg=C["panel2"], fg=C["text"],
                  font=("monospace", 9)).pack(side="left", padx=(0, 6))
        mode_box = ttk.Combobox(top, state="readonly", width=28, values=list(modes.values()))
        mode_box.set(modes[cur_mode()])
        mode_box.pack(side="left")
        tk.Label(page, text=tr("Per-action settings below win over this. Select one or more "
                               "actions, then choose a setting."),
                  bg=C["panel2"], fg=C["subtext"], font=("monospace", 9),
                  wraplength=620, justify="left").pack(anchor="w", pady=(0, 6))

        tree = ttk.Treeview(page, columns=("action", "default", "setting"), show="headings",
                             height=len(actions), selectmode="extended")
        for col, w in (("action", 190), ("default", 130), ("setting", 130)):
            tree.heading(col, text=tr(col.capitalize()))
            tree.column(col, width=w, anchor="w")
        tree.pack(fill="x", pady=(0, 6))
        desc = tk.Label(page, text="", bg=C["panel2"], fg=C["subtext"], font=("monospace", 9),
                         wraplength=620, justify="left")

        def fill():
            mode, overrides = cur_mode(), das.action_settings(self._int_env_path())
            keep = set(tree.selection())
            tree.delete(*tree.get_children())
            for a in actions:
                n = a["name"]
                setting = {"ask": tr("always ask"), "never": tr("never ask")}.get(
                    overrides.get(n), tr("default"))
                tree.insert("", "end", iid=n, values=(
                    n, tr("asks") if das.default_asks(mode, a["has_confirm"]) else tr("doesn't ask"),
                    setting))
                if n in keep:
                    tree.selection_add(n)

        def on_mode(_e=None):
            chosen = next(k for k, v in modes.items() if v == mode_box.get())
            das.set_mode(chosen, self._int_env_path())
            fill()
            self._integrations_saved()

        def on_select(_e=None):
            sel = tree.selection()
            desc.config(text=by_name[sel[0]]["description"] if len(sel) == 1 else "")

        def apply(setting):                      # "ask" | "never" | None (= back to default)
            sel = list(tree.selection())
            if not sel:
                return
            changes = {n: setting or "default" for n in sel}
            try:
                try:
                    das.set_action_settings(changes, actions, env_path=self._int_env_path())
                except das.NeedsConfirmation as e:
                    if not messagebox.askyesno(
                            tr("Skip approval?"),
                            tr("These can be destructive and normally ask first:\n\n{names}\n\n"
                               "Let the bot do them without asking?", names=", ".join(e.actions))):
                        return
                    das.set_action_settings(changes, actions, confirm_destructive=True,
                                            env_path=self._int_env_path())
            except settings.SettingsError as e:
                messagebox.showerror(tr("Couldn't save"), str(e))
                return
            fill()
            self._integrations_saved()

        mode_box.bind("<<ComboboxSelected>>", on_mode)
        tree.bind("<<TreeviewSelect>>", on_select)
        btns = tk.Frame(page, bg=C["panel2"])
        btns.pack(anchor="w")
        for text, val, bg in ((tr("🔒 Always ask"), "ask", C["green"]),
                              (tr("🔓 Never ask"), "never", C["red"]),
                              (tr("↺ Use default"), None, C["panel"])):
            tk.Button(btns, text=text, command=lambda v=val: apply(v), bg=bg,
                       fg=C["bg"] if bg != C["panel"] else C["text"], relief="flat",
                       padx=10, pady=3, cursor="hand2").pack(side="left", padx=(0, 6))
        desc.pack(anchor="w", pady=(6, 0))
        fill()

    # ── tab 6: sessions ──────────────────────────────────────────────────────

    # (column id, English heading)
    SESSION_COLUMNS = (
        ("platform", "Platform"), ("channel", "Channel"), ("turns", "Turns"),
        ("active", "Last active"), ("last", "Last message"),
    )

    def _build_sessions_tab(self):
        f = self.tab_sessions
        top = tk.Frame(f, bg=C["bg"])
        top.pack(fill="x", pady=(14, 6))
        tk.Label(top, text="Sessions", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 11, "bold")).pack(side="left")
        tk.Button(top, text="↻ Refresh", command=self._refresh_sessions,
                   bg=C["panel2"], fg=C["text"], relief="flat", padx=10, pady=3,
                   cursor="hand2").pack(side="right")
        tk.Label(f, text="Every channel the bot talks in has its own conversation session. "
                          "Resetting one wipes what was said in it, so the next message starts "
                          "fresh. Long-term memory is not touched. Works whether or not the "
                          "bots are running.",
                  bg=C["bg"], fg=C["subtext"], font=("monospace", 9),
                  wraplength=680, justify="left").pack(anchor="w", pady=(0, 8))

        table = tk.Frame(f, bg=C["bg"])
        table.pack(fill="both", expand=True)
        self.session_tree = ttk.Treeview(table, columns=[c for c, _ in self.SESSION_COLUMNS],
                                         show="headings", height=10, selectmode="extended")
        for (col, en), w in zip(self.SESSION_COLUMNS, (70, 190, 50, 90, 250)):
            self.session_tree.heading(col, text=tr(en))
            self.session_tree.column(col, width=w, anchor="w", stretch=(col == "last"))
        bar = ttk.Scrollbar(table, orient="vertical", command=self.session_tree.yview)
        self.session_tree.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.session_tree.pack(side="left", fill="both", expand=True)

        btn_row = tk.Frame(f, bg=C["bg"])
        btn_row.pack(fill="x", pady=(8, 0))
        tk.Button(btn_row, text="Reset selected", command=self._reset_selected_sessions,
                   bg=C["red"], fg=C["bg"], relief="flat", padx=10, pady=6,
                   font=("sans-serif", 9, "bold"), cursor="hand2").pack(side="left")
        self.sessions_status = tk.Label(btn_row, text="", bg=C["bg"], fg=C["green"],
                                        font=("monospace", 9))
        self.sessions_status.pack(side="left", padx=10)

        self._refresh_sessions()
        self.nb.bind("<<NotebookTabChanged>>", lambda _e: self._poll_sessions(once=True), add="+")
        self.after(3000, self._poll_sessions)

    @staticmethod
    def _ago(ts: float) -> str:
        secs = max(0, int(time.time() - ts))
        if secs < 60:
            return f"{secs}s ago"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"

    def _refresh_sessions(self):
        """Sync the table with .history/ in place (keeps selection and scroll position)."""
        try:
            rows = sessions.list_sessions()
        except Exception:
            return
        tree = self.session_tree
        wanted = []
        for r in rows:
            wanted.append(r["key"])
            values = (r["platform"], r["label"] or r["channel_id"], r["turns"],
                      self._ago(r["last_active"]), r["preview"])
            if tree.exists(r["key"]):
                tree.item(r["key"], values=values)
            else:
                tree.insert("", "end", iid=r["key"], values=values)
        for iid in tree.get_children():
            if iid not in wanted:
                tree.delete(iid)
        for i, iid in enumerate(wanted):
            tree.move(iid, "", i)

    def _poll_sessions(self, once: bool = False):
        # only do the disk reads while the tab is actually in front
        try:
            if self.nb.select() == str(self.tab_sessions):
                self._refresh_sessions()
        except Exception:
            pass
        if not once:
            self.after(3000, self._poll_sessions)

    def _reset_selected_sessions(self):
        picked = list(self.session_tree.selection())
        if not picked:
            self.sessions_status.config(text=tr("Select one or more sessions first."), fg=C["yellow"])
            return
        if not messagebox.askyesno(
                tr("Reset sessions"),
                tr("Reset {n} session(s)? Their conversation history will be erased. "
                   "Long-term memory is not affected.", n=len(picked))):
            return
        done, failed = 0, 0
        for key in picked:
            try:
                ok = sessions.reset(key)["ok"]
            except Exception:
                ok = False
            done += ok
            failed += not ok
        self._refresh_sessions()
        if failed:
            self.sessions_status.config(
                text=tr("✓ {done} reset, {failed} failed (check permissions on .history/)",
                        done=done, failed=failed), fg=C["red"])
        else:
            self.sessions_status.config(text=tr("✓ {n} session(s) reset", n=done), fg=C["green"])

    # ── log pump ─────────────────────────────────────────────────────────────

    def _drain_log_queue(self):
        try:
            while True:
                tag, line = self.log_queue.get_nowait()
                self.log_box.insert("end", f"[{tag}] {line}", (tag,))
                self.log_box.see("end")
                dot = {"bot": self.bot_status_dot, "webui": self.webui_status_dot, "stoat": self.stoat_status_dot}.get(tag)
                if dot is not None:
                    if line.startswith("$ "):
                        dot.config(text=tr("●  running"), fg=C["green"])
                    elif "exited" in line:
                        dot.config(text=tr("●  stopped"), fg=C["red"])
        except queue.Empty:
            pass
        try:                                   # bots started from the Profiles tab log here too
            for pid, line in profiles.supervisor.drain_logs():
                self.log_box.insert("end", f"[{pid}] {line}", (pid,))
                self.log_box.see("end")
        except Exception:
            pass
        self.after(150, self._drain_log_queue)

    # ── tab 7: updates ──────────────────────────────────────────────────────

    def _build_updates_tab(self):
        f = self.tab_updates
        tk.Label(f, text="Updates", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 11, "bold")).pack(anchor="w", pady=(14, 2))
        tk.Label(f, text="Checks a GitHub repo's releases for a newer SystemC and installs it. "
                          "Your .env, memory, history and workspace are never touched, and every "
                          "file an update overwrites is backed up first so you can roll back. "
                          "Only use a repo you trust: an update replaces the program's code, and "
                          "this program can run shell commands. Restart the bots and this panel "
                          "afterwards to run the new version.",
                  bg=C["bg"], fg=C["subtext"], font=("monospace", 9),
                  wraplength=680, justify="left").pack(anchor="w", pady=(0, 8))

        self.update_version_lbl = tk.Label(f, text="", bg=C["bg"], fg=C["text"],
                                           font=("monospace", 10, "bold"))
        self.update_version_lbl.pack(anchor="w", pady=(0, 6))

        tk.Label(f, text="Update source (GitHub owner/repo)", bg=C["bg"], fg=C["text"],
                  font=("sans-serif", 10, "bold")).pack(anchor="w", pady=(6, 0))
        tk.Entry(f, textvariable=self.update_repo_var, bg=C["panel"], fg=C["text"],
                  insertbackground=C["text"], relief="flat", width=60).pack(anchor="w", pady=(2, 4))
        tk.Checkbutton(f, text="Include pre-releases (betas)", variable=self.update_pre_var,
                        bg=C["bg"], fg=C["text"], selectcolor=C["panel"],
                        activebackground=C["bg"], font=("monospace", 9)).pack(anchor="w")

        row = tk.Frame(f, bg=C["bg"])
        row.pack(anchor="w", pady=(10, 4))
        self.update_check_btn = tk.Button(
            row, text="Check for updates", command=self._updates_check,
            bg=C["panel2"], fg=C["text"], relief="flat", padx=12, pady=6, cursor="hand2")
        self.update_check_btn.pack(side="left")
        self.update_install_btn = tk.Button(
            row, text=" Update now", command=self._updates_install, state="disabled",
            bg=C["accent"], fg=C["bg"], relief="flat", padx=12, pady=6,
            font=("sans-serif", 9, "bold"), cursor="hand2")
        self.update_install_btn.pack(side="left", padx=8)
        self.update_rollback_btn = tk.Button(
            row, text="Roll back last update", command=self._updates_rollback,
            bg=C["panel2"], fg=C["text"], relief="flat", padx=12, pady=6, cursor="hand2")
        self.update_rollback_btn.pack(side="left")
        self.update_status = tk.Label(f, text="", bg=C["bg"], fg=C["subtext"],
                                      font=("monospace", 9), wraplength=680, justify="left")
        self.update_status.pack(anchor="w", pady=(2, 4))

        self.update_log = scrolledtext.ScrolledText(
            f, height=12, bg=C["panel"], fg=C["text"], insertbackground=C["text"],
            relief="flat", font=("monospace", 9), state="disabled", wrap="word")
        self.update_log.pack(fill="both", expand=True, pady=(0, 8))

        self._updates_refresh_state()
        self.after(150, self._updates_poll)

    def _updates_refresh_state(self):
        name, tag = updater.codename(APP_DIR), updater.installed_tag(APP_DIR)
        self.update_version_lbl.config(
            text=f"Installed: {name}" + (f"  (release {tag})" if tag else "  (release unknown)"))
        if not self._update_busy:
            self.update_rollback_btn.config(
                state="normal" if updater.list_backups(APP_DIR) else "disabled")

    def _updates_log(self, msg):
        self.update_log.config(state="normal")
        self.update_log.insert("end", msg.rstrip("\n") + "\n")
        self.update_log.see("end")
        self.update_log.config(state="disabled")

    def _updates_set_busy(self, busy, status=""):
        self._update_busy = busy
        self.update_status.config(text=status)
        state = "disabled" if busy else "normal"
        self.update_check_btn.config(state=state)
        if busy:
            self.update_install_btn.config(state="disabled")
            self.update_rollback_btn.config(state="disabled")
        else:
            ready = bool(self._update_info and self._update_info.get("update_available"))
            self.update_install_btn.config(state="normal" if ready else "disabled")
            self._updates_refresh_state()

    def _updates_save_source(self):
        """Validate + save the source to .env; returns the normalised repo or None."""
        try:
            repo = updater.normalize_repo(self.update_repo_var.get())
        except updater.UpdateError as e:
            messagebox.showerror(tr("Update source"), str(e))
            return None
        self.update_repo_var.set(repo)
        env = load_env()
        env["UPDATE_REPO"] = repo
        env["UPDATE_PRERELEASE"] = "on" if self.update_pre_var.get() else ""
        save_env(env)
        return repo

    def _updates_worker(self, job):
        """Run job(log) on a thread; everything it reports goes through the queue."""
        def run():
            try:
                self._update_q.put(("done",) + tuple(job(lambda m: self._update_q.put(("log", m)))))
            except updater.UpdateError as e:
                self._update_q.put(("error", str(e)))
            except Exception as e:                     # never let the thread die silently
                self._update_q.put(("error", f"Unexpected error: {e!r}"))
        threading.Thread(target=run, daemon=True).start()

    def _updates_poll(self):
        try:
            while True:
                item = self._update_q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._updates_log(item[1])
                elif kind == "error":
                    self._updates_log("✗ " + item[1])
                    self._updates_set_busy(False, "✗ " + item[1].splitlines()[0])
                elif kind == "done":
                    self._updates_finished(item[1], item[2])
        except queue.Empty:
            pass
        self.after(150, self._updates_poll)

    def _updates_check(self):
        if self._update_busy:
            return
        repo = self._updates_save_source()
        if not repo:
            return
        pre = self.update_pre_var.get()
        self._update_info = None
        self._updates_set_busy(True, tr("Checking…"))
        self._updates_log(f"Checking {repo}…")

        def job(log):
            return "check", updater.check(repo, pre, APP_DIR)

        self._updates_worker(job)

    def _updates_install(self):
        info = self._update_info
        if self._update_busy or not info or not info.get("update_available"):
            return
        if not messagebox.askyesno(
                tr("Update now"),
                f"Install {info['tag']} from {info['repo']}?\n\n"
                "Files it contains will be replaced (a backup is made first), and your .env, "
                "memory, history and workspace are left alone. Running bots keep using the old "
                "code until you restart them."):
            return
        self._updates_set_busy(True, tr("Updating…"))
        prog = {"last": -1}

        def progress(done, total):
            pct = int(done * 100 / total) if total else done // (1 << 20)
            if pct != prog["last"] and (not total or pct % 10 == 0):
                prog["last"] = pct
                self._update_q.put(("log", f"  downloaded {pct}%" if total else f"  downloaded {pct} MB"))

        def job(log):
            return "installed", updater.install(info, log, progress, APP_DIR)

        self._updates_worker(job)

    def _updates_rollback(self):
        if self._update_busy:
            return
        if not messagebox.askyesno(
                tr("Roll back last update"),
                "Restore the files from before the last update? Files that update added are "
                "removed. Your .env, memory, history and workspace are left alone."):
            return
        self._updates_set_busy(True, tr("Rolling back…"))

        def job(log):
            return "rolled", updater.rollback(log, APP_DIR)

        self._updates_worker(job)

    def _updates_finished(self, kind, result):
        if kind == "check":
            info = result
            self._update_info = info
            notes = (info.get("notes") or "").strip()
            self._updates_log(f"Latest release: {info['tag']}"
                              + (" (pre-release)" if info["prerelease"] else "")
                              + f"   Installed: {info['installed_tag'] or 'unknown'}")
            if notes:
                self._updates_log(notes[:1200] + ("…" if len(notes) > 1200 else ""))
            if info["update_available"]:
                msg = f"Update available: {info['tag']}. Press Update now to install it."
            else:
                msg = f"You're up to date ({info['installed_tag'] or info['tag']})."
            self._updates_log(msg)
            self._updates_set_busy(False, msg)
            return

        self._update_info = None
        if kind == "installed":
            msg = f"✓ Updated to {result['tag']} ({result['files']} files)."
            if result.get("requirements_changed") and not result.get("pip_ok"):
                msg += " Some dependencies failed to install; see the Dependencies tab."
        else:
            msg = "✓ Rolled back" + (f" to {result['tag']}." if result.get("tag") else ".")
        self._updates_log(msg)
        self._updates_set_busy(False, msg)
        if messagebox.askyesno(
                tr("Restart now?"),
                msg + "\n\nRestart the control panel now to run the new code? Any running "
                "bots and the web UI will be stopped; start them again from the Run tab."):
            self._restart_panel()

    def _restart_panel(self):
        for proc in (self.bot_proc, self.webui_proc, self.stoat_proc):
            proc.stop()
        subprocess.Popen([PYTHON, os.path.join(APP_DIR, "control_panel.py"), *sys.argv[1:]],
                          cwd=APP_DIR)
        self.destroy()

    def _on_close(self):
        try:
            profiles.supervisor.stop_all()     # don't leave Profiles-tab bots running without a log reader
        except Exception:
            pass
        self.bot_proc.stop()
        self.webui_proc.stop()
        self.stoat_proc.stop()
        self.destroy()


if __name__ == "__main__":
    app = ControlPanel()
    app.mainloop()
