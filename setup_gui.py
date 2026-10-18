"""
Setup wizard. Launched automatically by bot.py when .env is missing.
Can also be run directly: python setup_gui.py
"""

import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox
import threading
import urllib.request
import account_setup

import version
print(version.banner())
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_WORKDIR = os.path.join(APP_DIR, "workspace")

# ── Helpers ─────────────────────────────────────────────────────────────────

def check_ollama_running() -> bool:
    try:
        urllib.request.urlopen("http://localhost:11434", timeout=2)
        return True
    except Exception:
        return False


def check_ollama_installed() -> bool:
    import shutil
    return shutil.which("ollama") is not None


def get_installed_models() -> list[str]:
    try:
        import subprocess
        r = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=5)
        lines = r.stdout.strip().splitlines()
        models = []
        for line in lines[1:]:  # skip header
            parts = line.split()
            if parts:
                models.append(parts[0])
        return models if models else []
    except Exception:
        return []


# ── Main GUI ─────────────────────────────────────────────────────────────────

class SetupWizard(tk.Tk):
    PAGES = ["Welcome", "Discord", "Bot Behaviour", "Ollama", "Stoat", "Done"]

    def __init__(self):
        super().__init__()
        self.title("systemc — setup wizard")
        self.geometry("580x560")
        self.resizable(False, False)
        self.configure(bg="#1e1e2e")

        # ── state vars ──
        self.token_var        = tk.StringVar()
        self.channels_var     = tk.StringVar()
        self.mode_var         = tk.StringVar(value="WAKEWORD")
        self.wake_var         = tk.StringVar(value="hey claw")
        self.model_var        = tk.StringVar(value="llama3.2")
        self.account_mode_var = tk.StringVar(value="home_agent")
        self.run_as_user_var  = tk.StringVar(value="agent")
        self.workdir_var      = tk.StringVar(value=account_setup.get_default_agent_dir())

        self._current_page = 0
        self._build_ui()
        self._show_page(0)

    # ── chrome ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # header
        hdr = tk.Frame(self, bg="#313244", pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="🦞  systemc setup", font=("monospace", 16, "bold"),
                 bg="#313244", fg="#cdd6f4").pack()

        # progress bar
        self._progress = ttk.Progressbar(self, maximum=len(self.PAGES)-1, length=560)
        self._progress.pack(fill="x")
        self._style_progress()

        # page container
        self._container = tk.Frame(self, bg="#1e1e2e")
        self._container.pack(fill="both", expand=True, padx=20, pady=10)

        # nav buttons
        nav = tk.Frame(self, bg="#1e1e2e", pady=8)
        nav.pack(fill="x", padx=20)
        self._back_btn = tk.Button(nav, text="← Back", command=self._prev,
                                   bg="#45475a", fg="#cdd6f4", relief="flat",
                                   padx=12, pady=6, cursor="hand2")
        self._back_btn.pack(side="left")
        self._next_btn = tk.Button(nav, text="Next →", command=self._next,
                                   bg="#89b4fa", fg="#1e1e2e", relief="flat",
                                   padx=12, pady=6, font=("sans-serif", 10, "bold"),
                                   cursor="hand2")
        self._next_btn.pack(side="right")

    def _style_progress(self):
        s = ttk.Style(self)
        s.theme_use("default")
        s.configure("TProgressbar", troughcolor="#313244", background="#89b4fa", thickness=4)

    # ── page rendering ───────────────────────────────────────────────────────

    def _show_page(self, idx: int):
        for w in self._container.winfo_children():
            w.destroy()
        self._current_page = idx
        self._progress["value"] = idx
        self._back_btn.config(state="normal" if idx > 0 else "disabled")
        label = "Finish 🎉" if idx == len(self.PAGES) - 1 else "Next →"
        self._next_btn.config(text=label)

        pages = [
            self._page_welcome,
            self._page_discord,
            self._page_behaviour,
            self._page_ollama,
            self._page_stoat,
            self._page_done,
        ]
        pages[idx]()

    def _prev(self):
        if self._current_page > 0:
            self._show_page(self._current_page - 1)

    def _next(self):
        if self._current_page == len(self.PAGES) - 1:
            self._save_and_exit()
            return
        validators = {1: self._validate_discord}
        v = validators.get(self._current_page)
        if v and not v():
            return
        self._show_page(self._current_page + 1)

    # ── pages ────────────────────────────────────────────────────────────────

    def _page_welcome(self):
        f = self._frame()
        self._title(f, "👋  Welcome!")
        self._body(f, (
            "This wizard sets up systemc — a Discord bot that gives an AI\n"
            "agent real shell access to this machine.\n\n"
            "⚠️  Only run this on a machine you're okay with the bot having\n"
            "full access to (a VM is ideal).\n\n"
            "This will take about 2 minutes. Let's go!"
        ))

    def _page_discord(self):
        f = self._frame()
        self._title(f, "🤖  Discord Bot")
        self._body(f, (
            "You need a Discord bot token. To get one:\n"
            "1. Go to discord.com/developers/applications\n"
            "2. New Application → name it anything\n"
            "3. Bot → Reset Token → copy it\n"
            "4. Turn on Message Content Intent (same page)\n"
            "5. OAuth2 → URL Generator → tick bot + Send/Read perms\n"
            "   → open the URL → invite to your server"
        ))
        self._field(f, "Bot Token:", self.token_var, show="•")
        self._body(f, "Channel ID (right-click channel → Copy ID):")
        self._field(f, "", self.channels_var)

    def _page_behaviour(self):
        f = self._frame()
        self._title(f, "💬  How should it listen?")

        modes = [
            ("WAKEWORD",   "Wake word — only responds when you say the magic word\n"
                           "            (or @mention it)"),
            ("MENTION",    "Mention only — only responds when @mentioned"),
            ("EVERYTHING", "Everything — responds to every single message\n"
                           "             (noisy, good for a private channel)"),
        ]
        for val, desc in modes:
            row = tk.Frame(f, bg="#1e1e2e")
            row.pack(anchor="w", pady=3)
            tk.Radiobutton(row, variable=self.mode_var, value=val,
                           bg="#1e1e2e", fg="#cdd6f4", selectcolor="#313244",
                           activebackground="#1e1e2e").pack(side="left")
            tk.Label(row, text=desc, bg="#1e1e2e", fg="#a6adc8",
                     font=("monospace", 9), justify="left").pack(side="left")

        tk.Label(f, text="\nWake word:", bg="#1e1e2e", fg="#cdd6f4").pack(anchor="w")
        tk.Entry(f, textvariable=self.wake_var, bg="#313244", fg="#cdd6f4",
                 insertbackground="#cdd6f4", relief="flat", width=30).pack(anchor="w")

        tk.Label(f, text="\nAgent workspace & account:",
                 bg="#1e1e2e", fg="#cdd6f4", font=("sans-serif", 10, "bold")).pack(anchor="w")

        curr_user, _, _, _ = account_setup.get_current_user_and_group()
        default_dir = account_setup.get_default_agent_dir()

        def _on_mode_change():
            m = self.account_mode_var.get()
            if m == "home_agent":
                self.workdir_var.set(default_dir)
                user_row.pack_forget()
                dir_row.pack_forget()
            elif m == "separate_user":
                if not self.workdir_var.get() or self.workdir_var.get().endswith("workspace"):
                    self.workdir_var.set(default_dir)
                user_row.pack(anchor="w", pady=2)
                dir_row.pack(anchor="w", pady=2)
            else:
                user_row.pack_forget()
                dir_row.pack(anchor="w", pady=2)

        for val, title, sub in [
            ("home_agent", f"Use {default_dir} from your account ({curr_user})", f"Agent files and $HOME in {default_dir}, owned by you"),
            ("separate_user", "Run under another user account (e.g. 'agent')", "Runs shell commands as a dedicated system user"),
            ("custom", "Local workspace (./workspace) or custom folder", "Standard folder inside this app or custom path"),
        ]:
            r_box = tk.Frame(f, bg="#1e1e2e")
            r_box.pack(anchor="w", pady=1)
            tk.Radiobutton(r_box, variable=self.account_mode_var, value=val,
                           command=_on_mode_change,
                           bg="#1e1e2e", fg="#cdd6f4", selectcolor="#313244",
                           activebackground="#1e1e2e").pack(side="left")
            col = tk.Frame(r_box, bg="#1e1e2e")
            col.pack(side="left")
            tk.Label(col, text=title, bg="#1e1e2e", fg="#cdd6f4", font=("sans-serif", 9, "bold")).pack(anchor="w")
            tk.Label(col, text=sub, bg="#1e1e2e", fg="#a6adc8", font=("sans-serif", 8)).pack(anchor="w")

        user_row = tk.Frame(f, bg="#1e1e2e")
        tk.Label(user_row, text="Username:", bg="#1e1e2e", fg="#a6adc8", width=12, anchor="w").pack(side="left")
        tk.Entry(user_row, textvariable=self.run_as_user_var, bg="#313244", fg="#cdd6f4",
                 insertbackground="#cdd6f4", relief="flat", width=20).pack(side="left")

        dir_row = tk.Frame(f, bg="#1e1e2e")
        tk.Label(dir_row, text="Directory:", bg="#1e1e2e", fg="#a6adc8", width=12, anchor="w").pack(side="left")
        tk.Entry(dir_row, textvariable=self.workdir_var, bg="#313244", fg="#cdd6f4",
                 insertbackground="#cdd6f4", relief="flat", width=36).pack(side="left")

        _on_mode_change()

    def _page_ollama(self):
        f = self._frame()
        self._title(f, "🧠  AI Model")

        # async status check
        status_var = tk.StringVar(value="Checking Ollama...")
        models_var = tk.StringVar()
        status_lbl = tk.Label(f, textvariable=status_var, bg="#1e1e2e",
                              fg="#f9e2af", font=("monospace", 9))
        status_lbl.pack(anchor="w", pady=(0, 6))

        self._body(f, "Type a model name (or pick from installed ones below):")
        entry = tk.Entry(f, textvariable=self.model_var, bg="#313244", fg="#cdd6f4",
                         insertbackground="#cdd6f4", relief="flat", width=30)
        entry.pack(anchor="w")

        model_list = tk.Listbox(f, bg="#313244", fg="#a6adc8", relief="flat",
                                height=4, selectbackground="#89b4fa",
                                selectforeground="#1e1e2e")
        model_list.pack(fill="x", pady=(4, 0))

        def on_select(evt):
            sel = model_list.curselection()
            if sel:
                self.model_var.set(model_list.get(sel[0]))

        model_list.bind("<<ListboxSelect>>", on_select)

        def _check():
            if not check_ollama_installed():
                status_var.set("⚠️  Ollama not found — run setup.sh first!")
                return
            if not check_ollama_running():
                status_var.set("⚠️  Ollama installed but not running (run: ollama serve)")
                return
            models = get_installed_models()
            if models:
                status_var.set(f"✓  Ollama running — {len(models)} model(s) installed")
                model_list.delete(0, "end")
                for m in models:
                    model_list.insert("end", m)
                if models and not self.model_var.get():
                    self.model_var.set(models[0])
            else:
                status_var.set("⚠️  Ollama running but no models pulled yet\n"
                               "    run: ollama pull llama3.2")

        threading.Thread(target=_check, daemon=True).start()

        self._body(f, (
            "\nRecommended models for your specs (8GB RAM):\n"
            "  llama3.2   ~2GB — fast, good general use\n"
            "  qwen2.5    ~2GB — best at tool use / shell commands"
        ))


    def _page_stoat(self):
        f = self._frame()
        self._title(f, "🦦  Stoat (optional)")
        self._body(f, (
            "Stoat is a free, open-source, privacy-focused Discord alternative.\n"
            "You can run SystemC on Discord, Stoat, or both at the same time.\n\n"
            "Skip this if you only want Discord."
        ))
        row = tk.Frame(f, bg="#1e1e2e")
        row.pack(anchor="w", pady=6)
        tk.Checkbutton(row, variable=self.stoat_enabled_var, bg="#1e1e2e",
                       fg="#cdd6f4", selectcolor="#313244",
                       activebackground="#1e1e2e",
                       command=lambda: self._show_page(self._current_page)).pack(side="left")
        tk.Label(row, text="Enable Stoat bot", bg="#1e1e2e", fg="#cdd6f4").pack(side="left")

        if not self.stoat_enabled_var.get():
            return

        self._body(f, (
            "1. stoat.chat → Settings → My Bots → New Bot → copy token\n"
            "2. Invite bot to your server\n"
            "3. Right-click channel → Copy ID"
        ))
        self._field(f, "Stoat bot token:", self.stoat_token_var, show="•")
        self._field(f, "Channel ID(s):", self.stoat_ch_var)

        tk.Label(f, text="Mode:", bg="#1e1e2e", fg="#cdd6f4").pack(anchor="w")
        for val in ("WAKEWORD", "MENTION", "EVERYTHING"):
            tk.Radiobutton(f, variable=self.stoat_mode_var, value=val,
                           text=val, bg="#1e1e2e", fg="#a6adc8",
                           selectcolor="#313244",
                           activebackground="#1e1e2e").pack(anchor="w")

        tk.Label(f, text="\nWake word:", bg="#1e1e2e", fg="#cdd6f4").pack(anchor="w")
        tk.Entry(f, textvariable=self.stoat_wake_var, bg="#313244", fg="#cdd6f4",
                 insertbackground="#cdd6f4", relief="flat", width=30).pack(anchor="w")

    def _page_done(self):
        f = self._frame()
        self._title(f, "🎉  All set!")
        self._body(f, (
            "Your config will be saved to .env\n\n"
            "Click Finish, then run:\n\n"
            "    python bot.py\n\n"
            "The bot will come online in your Discord channel.\n\n"
            "Tips:\n"
            "• Say your wake word (or @mention the bot) to chat with the agent\n"
            "• Type !ls in Discord to run shell commands directly\n"
            "• Destructive commands get an approval prompt before running\n"
            "• Delete .env and re-run bot.py to redo this wizard"
        ))

    # ── validation ───────────────────────────────────────────────────────────

    def _validate_discord(self) -> bool:
        if not self.token_var.get().strip():
            messagebox.showerror("Missing token",
                                 "Paste your Discord bot token before continuing.")
            return False
        if not self.channels_var.get().strip():
            messagebox.showerror("Missing channel",
                                 "Enter at least one channel ID before continuing.")
            return False
        return True

    # ── save ────────────────────────────────────────────────────────────────

    def _save_and_exit(self):
        mode = self.account_mode_var.get()
        default_dir = account_setup.get_default_agent_dir()
        if mode == "home_agent":
            workdir = default_dir
            run_as = ""
        elif mode == "separate_user":
            workdir = self.workdir_var.get().strip() or default_dir
            run_as = self.run_as_user_var.get().strip() or "agent"
        else:
            workdir = self.workdir_var.get().strip() or DEFAULT_WORKDIR
            run_as = ""

        status = account_setup.check_workspace_status(workdir, run_as)
        if status["needs_root"]:
            ok, msg = account_setup.ensure_workspace_interactive(
                workdir=workdir,
                run_as_user=run_as,
                parent_window=self,
                is_gui=True,
            )
            if not ok:
                messagebox.showerror("Workspace Setup Error", msg)
                return
        else:
            try:
                os.makedirs(workdir, exist_ok=True)
            except OSError as e:
                messagebox.showerror("Error", f"Could not create workspace directory:\n{e}")
                return

        env = {
            "DISCORD_BOT_TOKEN":  self.token_var.get().strip(),
            "ALLOWED_CHANNEL_IDS": self.channels_var.get().strip(),
            "BOT_MODE":            self.mode_var.get(),
            "WAKE_WORD":           self.wake_var.get().strip() or "hey claw",
            "AGENT_PROVIDER":      "ollama",
            "AGENT_MODEL":         self.model_var.get().strip() or "llama3.2",
            "OLLAMA_HOST":         "http://localhost:11434",
            "AGENT_ACCOUNT_MODE":  mode,
            "AGENT_WORKDIR":       workdir,
            "AGENT_RUN_AS_USER":   run_as,
        }
        if hasattr(self, "stoat_enabled_var") and self.stoat_enabled_var.get() and hasattr(self, "stoat_token_var") and self.stoat_token_var.get().strip():
            env["STOAT_BOT_TOKEN"]           = self.stoat_token_var.get().strip()
            env["STOAT_ALLOWED_CHANNEL_IDS"] = self.stoat_ch_var.get().strip() if hasattr(self, "stoat_ch_var") else ""
            env["STOAT_BOT_MODE"]            = self.stoat_mode_var.get() if hasattr(self, "stoat_mode_var") else "WAKEWORD"
            env["STOAT_WAKE_WORD"]           = (self.stoat_wake_var.get().strip() if hasattr(self, "stoat_wake_var") else "") or "hey claw"

        with open(".env", "w") as f:
            f.write("# systemc config — generated by setup_gui.py\n")
            for k, v in env.items():
                if v != "":
                    f.write(f"{k}={v}\n")
        self.destroy()

    # ── widget helpers ───────────────────────────────────────────────────────

    def _frame(self) -> tk.Frame:
        f = tk.Frame(self._container, bg="#1e1e2e")
        f.pack(fill="both", expand=True)
        return f

    def _title(self, parent, text: str):
        tk.Label(parent, text=text, font=("sans-serif", 13, "bold"),
                 bg="#1e1e2e", fg="#cdd6f4").pack(anchor="w", pady=(0, 8))

    def _body(self, parent, text: str):
        tk.Label(parent, text=text, bg="#1e1e2e", fg="#a6adc8",
                 font=("monospace", 9), justify="left", wraplength=500).pack(anchor="w", pady=2)

    def _field(self, parent, label: str, var: tk.Variable, show: str = ""):
        if label:
            tk.Label(parent, text=label, bg="#1e1e2e", fg="#cdd6f4").pack(anchor="w")
        kwargs = dict(textvariable=var, bg="#313244", fg="#cdd6f4",
                      insertbackground="#cdd6f4", relief="flat", width=48)
        if show:
            kwargs["show"] = show
        tk.Entry(parent, **kwargs).pack(anchor="w", pady=(0, 6))


if __name__ == "__main__":
    app = SetupWizard()
    app.mainloop()
