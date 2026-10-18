"""
Account and workspace setup helper for SystemC Farink.
Cross-platform support for Linux, macOS, and Windows.

Handles configuring the agent to:
1. Run under /home/agent (or C:\\home\\agent on Windows) owned by the current user account.
2. Run under a separate user account (e.g. 'agent') with home in that directory.
3. Run under a local/custom workspace directory.

Provides elevation helpers:
- On Linux/macOS: sudo / su with password prompt (GUI & CLI), useradd, chown, chmod.
- On Windows: UAC Administrator elevation via PowerShell Start-Process -Verb RunAs,
  net user, icacls permissions, and environment setup.
"""

from __future__ import annotations

import os
import sys
import getpass
import subprocess
import shutil
import tempfile
from typing import Optional, Tuple, Dict, Any

try:
    import pwd
    import grp
except ImportError:
    pwd = None  # Windows
    grp = None


def get_default_agent_dir() -> str:
    """Return platform-appropriate default agent directory:
    - Windows: C:\\Users\\agent (or %SystemDrive%\\Users\\agent)
    - macOS:   /Users/agent
    - Linux:   /home/agent
    """
    if os.name == "nt":
        sys_drive = os.environ.get("SystemDrive", "C:")
        # Look for existing Users directory or default to %SystemDrive%\Users\agent
        public_dir = os.environ.get("PUBLIC")
        if public_dir and os.path.basename(public_dir).lower() == "public":
            parent = os.path.dirname(public_dir)
            return os.path.join(parent, "agent")
        return os.path.join(sys_drive + "\\", "Users", "agent")
    if sys.platform == "darwin":
        return "/Users/agent"
    return "/home/agent"


def is_admin_windows() -> bool:
    """Check if the current process has Windows Administrator privileges."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def get_current_user_and_group() -> Tuple[str, str, int, int]:
    """Return (username, groupname, uid, gid) for the current user."""
    if os.name == "nt":
        user = os.environ.get("USERNAME") or getpass.getuser() or "user"
        return user, "Users", 1000, 1000

    user = ""
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or "nobody"

    if pwd is not None:
        try:
            pw = pwd.getpwnam(user)
            uid = pw.pw_uid
            gid = pw.pw_gid
            group = grp.getgrgid(gid).gr_name if grp else user
            return user, group, uid, gid
        except Exception:
            pass

    uid = os.getuid() if hasattr(os, "getuid") else 1000
    gid = os.getgid() if hasattr(os, "getgid") else 1000
    return user, user, uid, gid


def check_workspace_status(workdir: str = "", run_as_user: str = "") -> Dict[str, Any]:
    """
    Check the current status of workdir and user setup.
    Works across Linux, macOS, and Windows.
    """
    if not workdir:
        workdir = get_default_agent_dir()

    workdir = os.path.abspath(workdir)
    current_user, current_grp, current_uid, current_gid = get_current_user_and_group()
    target_user = run_as_user.strip() if run_as_user else current_user

    exists = os.path.exists(workdir)
    is_dir = os.path.isdir(workdir) if exists else False
    user_exists = True

    # ── Windows implementation ───────────────────────────────────────────────
    if os.name == "nt":
        if run_as_user:
            try:
                r = subprocess.run(["net", "user", run_as_user.strip()], capture_output=True, text=True, timeout=5)
                user_exists = (r.returncode == 0)
            except Exception:
                user_exists = True

        writable = False
        if exists:
            probe = os.path.join(workdir, f".probe_{os.getpid()}.tmp")
            try:
                with open(probe, "w") as f:
                    f.write("probe")
                os.remove(probe)
                writable = True
            except Exception:
                writable = False

        needs_root = False
        reason = ""

        if run_as_user and not user_exists:
            needs_root = True
            reason = f"Windows user '{run_as_user}' does not exist (Administrator required to create)."
        elif not exists:
            parent = os.path.dirname(workdir)
            while parent and not os.path.exists(parent):
                parent = os.path.dirname(parent)
            parent_writable = False
            if parent and os.path.exists(parent):
                probe = os.path.join(parent, f".probe_{os.getpid()}.tmp")
                try:
                    with open(probe, "w") as f:
                        f.write("probe")
                    os.remove(probe)
                    parent_writable = True
                except Exception:
                    parent_writable = False
            if not parent_writable:
                needs_root = True
                reason = f"Directory '{workdir}' does not exist and requires Administrator privileges to create."
            else:
                reason = f"Directory '{workdir}' does not exist."
        elif not writable:
            needs_root = True
            reason = f"Directory exists but is not writable (Administrator required for icacls)."
        else:
            reason = f"Ready. Writable on Windows."

        return {
            "path": workdir,
            "exists": exists,
            "is_dir": is_dir,
            "writable": writable,
            "owner": target_user,
            "group": "Users",
            "user_exists": user_exists,
            "target_user": target_user,
            "needs_root": needs_root,
            "reason": reason,
        }

    # ── Unix implementation ──────────────────────────────────────────────────
    target_uid = current_uid
    target_gid = current_gid

    if run_as_user and pwd is not None:
        try:
            pw = pwd.getpwnam(run_as_user.strip())
            target_uid = pw.pw_uid
            target_gid = pw.pw_gid
        except KeyError:
            user_exists = False

    owner_user = ""
    owner_group = ""
    writable = False

    if exists:
        try:
            st = os.stat(workdir)
            if pwd is not None:
                try:
                    owner_user = pwd.getpwuid(st.st_uid).pw_name
                except Exception:
                    owner_user = str(st.st_uid)
            if grp is not None:
                try:
                    owner_group = grp.getgrgid(st.st_gid).gr_name
                except Exception:
                    owner_group = str(st.st_gid)

            if not run_as_user or run_as_user == current_user:
                writable = os.access(workdir, os.W_OK | os.X_OK)
            else:
                if user_exists and st.st_uid == target_uid:
                    writable = bool(st.st_mode & 0o200)
                else:
                    writable = False
        except Exception:
            pass

    needs_root = False
    reason = ""

    if run_as_user and not user_exists:
        needs_root = True
        reason = f"System user '{run_as_user}' does not exist yet."
    elif not exists:
        parent = os.path.dirname(workdir)
        while parent and not os.path.exists(parent):
            parent = os.path.dirname(parent)
        parent_writable = os.access(parent, os.W_OK | os.X_OK) if parent and os.path.exists(parent) else False
        if not parent_writable:
            needs_root = True
            reason = f"Directory '{workdir}' does not exist and requires root to create."
        else:
            reason = f"Directory '{workdir}' does not exist."
    elif not writable:
        needs_root = True
        reason = f"Directory exists but is not writable by '{target_user}' (owner: {owner_user}:{owner_group})."
    else:
        reason = f"Ready. Owned by {owner_user or target_user} and writable."

    return {
        "path": workdir,
        "exists": exists,
        "is_dir": is_dir,
        "writable": writable,
        "owner": owner_user,
        "group": owner_group,
        "user_exists": user_exists,
        "target_user": target_user,
        "needs_root": needs_root,
        "reason": reason,
    }


def can_sudo_without_password() -> bool:
    """Check if passwordless sudo is permitted on Unix, or admin on Windows."""
    if os.name == "nt":
        return is_admin_windows()
    if hasattr(os, "getuid") and os.getuid() == 0:
        return True
    if not shutil.which("sudo"):
        return False
    try:
        r = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def run_as_admin_windows(cmd_script: str) -> Tuple[bool, str]:
    """
    Run a command script with Administrator privileges on Windows.
    Triggers the native Windows UAC prompt via PowerShell Start-Process -Verb RunAs if needed.
    """
    tmp_dir = tempfile.gettempdir()
    batch_path = os.path.join(tmp_dir, f"farink_setup_{os.getpid()}.bat")
    log_path = os.path.join(tmp_dir, f"farink_setup_{os.getpid()}.log")
    code_path = os.path.join(tmp_dir, f"farink_setup_{os.getpid()}.code")

    # Clean previous files if any
    for p in (log_path, code_path):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass

    wrapped = (
        "@echo off\r\n"
        f"({cmd_script}) > \"{log_path}\" 2>&1\r\n"
        f"echo %errorlevel% > \"{code_path}\"\r\n"
    )
    try:
        with open(batch_path, "w", encoding="utf-8") as f:
            f.write(wrapped)
    except Exception as e:
        return False, f"Failed to create setup script: {e}"

    if is_admin_windows():
        try:
            r = subprocess.run(["cmd.exe", "/c", batch_path], capture_output=True, text=True, timeout=30)
        except Exception as e:
            return False, str(e)
    else:
        # Trigger UAC elevation via PowerShell
        ps_cmd = (
            f"Start-Process cmd.exe -ArgumentList '/c \"\"{batch_path}\"\"' "
            "-Verb RunAs -Wait -WindowStyle Hidden"
        )
        try:
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception as e:
            return False, f"Failed to launch Windows UAC elevation: {e}"

    output = ""
    exit_code = 0
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", errors="replace") as f:
                output = f.read().strip()
            os.remove(log_path)
        except Exception:
            pass

    if os.path.exists(code_path):
        try:
            with open(code_path, "r") as f:
                raw_code = f.read().strip()
                exit_code = int(raw_code) if raw_code.isdigit() else 0
            os.remove(code_path)
        except Exception:
            pass

    try:
        os.remove(batch_path)
    except Exception:
        pass

    if exit_code == 0:
        return True, output or "Windows Administrator setup completed successfully."
    return False, output or f"Setup exited with code {exit_code} (UAC cancelled or command failed)."


def _run_with_su_pty(cmd_script: str, password: str, timeout: int = 15) -> Tuple[int, str]:
    """Run a command as root on Unix using su via pseudo-terminal, feeding password."""
    try:
        import pty
        import select
        import time
    except ImportError:
        return -1, "pty not supported on this platform"

    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            ["su", "root", "-c", cmd_script],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
    except Exception as e:
        os.close(master_fd)
        os.close(slave_fd)
        return -1, f"Failed to execute su: {e}"

    os.close(slave_fd)
    out_chunks = []
    sent_password = False
    start_time = time.monotonic()

    while time.monotonic() - start_time < timeout:
        if proc.poll() is not None:
            break
        r, _, _ = select.select([master_fd], [], [], 0.2)
        if master_fd in r:
            try:
                data = os.read(master_fd, 1024)
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                out_chunks.append(text)
                combined = "".join(out_chunks).lower()
                if not sent_password and any(prompt in combined for prompt in ("password:", "mot de passe", "passwort")):
                    time.sleep(0.05)
                    os.write(master_fd, (password + "\n").encode("utf-8"))
                    sent_password = True
            except OSError:
                break

    try:
        os.close(master_fd)
    except OSError:
        pass

    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()

    return proc.returncode, "".join(out_chunks)


def run_as_root(script: str, root_password: Optional[str] = None) -> Tuple[bool, str]:
    """
    Execute a shell script as root / administrator.
    On Windows: triggers UAC elevation.
    On Linux/macOS: uses sudo / su.
    """
    if os.name == "nt":
        return run_as_admin_windows(script)

    # Already root
    if hasattr(os, "getuid") and os.getuid() == 0:
        try:
            r = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                return True, r.stdout.strip()
            return False, r.stderr.strip() or r.stdout.strip() or f"Exited with code {r.returncode}"
        except Exception as e:
            return False, str(e)

    # Passwordless sudo available
    if can_sudo_without_password():
        try:
            r = subprocess.run(["sudo", "-n", "sh", "-c", script], capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                return True, r.stdout.strip()
            return False, r.stderr.strip() or r.stdout.strip() or f"Exited with code {r.returncode}"
        except Exception as e:
            return False, str(e)

    # Password provided
    if root_password is not None:
        err = ""
        if shutil.which("sudo"):
            try:
                proc = subprocess.run(
                    ["sudo", "-S", "-k", "--", "sh", "-c", script],
                    input=(root_password + "\n"),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if proc.returncode == 0:
                    return True, proc.stdout.strip()
                err = (proc.stderr or "").strip()
            except Exception as e:
                err = str(e)
        else:
            err = "sudo not found"

        # Try su fallback via PTY
        code, su_out = _run_with_su_pty(script, root_password)
        if code == 0:
            return True, su_out.strip()

        combined_err = f"{err} {su_out}".strip()
        if any(term in combined_err.lower() for term in ("incorrect password", "authentication failure", "sorry", "failed")):
            return False, "Authentication failed: incorrect root or sudo password."
        return False, f"Elevation failed ({combined_err or 'unknown error'})."

    return False, "Root elevation required but no password was provided."


def setup_agent_directory(
    workdir: str = "",
    run_as_user: str = "",
    root_password: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Create workdir, create run_as_user if requested, and set ownership/permissions.
    Supports both Windows and Linux/macOS.
    """
    if not workdir:
        workdir = get_default_agent_dir()
    workdir = os.path.abspath(workdir)

    current_user, current_grp, current_uid, current_gid = get_current_user_and_group()
    target_user = run_as_user.strip() if run_as_user else current_user

    status = check_workspace_status(workdir, run_as_user)
    if not status["needs_root"]:
        try:
            os.makedirs(workdir, exist_ok=True)
            return True, f"Directory '{workdir}' is ready."
        except OSError:
            pass

    # ── Windows execution ────────────────────────────────────────────────────
    if os.name == "nt":
        batch_cmds = [
            f'if not exist "{workdir}" mkdir "{workdir}"',
        ]
        if run_as_user:
            batch_cmds.append(f'net user "{run_as_user}" >nul 2>nul || net user "{run_as_user}" /add')
            batch_cmds.append(f'icacls "{workdir}" /grant "{run_as_user}:(OI)(CI)F" /T /Q')
        else:
            batch_cmds.append(f'icacls "{workdir}" /grant "{current_user}:(OI)(CI)F" /T /Q')

        script = "\r\n".join(batch_cmds)
        ok, msg = run_as_admin_windows(script)
        if not ok:
            return False, msg

        if os.path.exists(workdir):
            return True, f"Successfully created '{workdir}' and set permissions on Windows."
        return False, f"Setup ran, but directory '{workdir}' was not found: {msg}"

    # ── Unix execution ───────────────────────────────────────────────────────
    target_group = target_user if run_as_user else current_grp

    commands = [
        "set -e",
    ]
    if run_as_user:
        commands.append(f"""\
if ! id "{target_user}" >/dev/null 2>&1; then
    useradd -m -d "{workdir}" -s /bin/bash "{target_user}" 2>/dev/null || useradd -d "{workdir}" -s /bin/bash "{target_user}"
fi
""")

    commands.append(f'mkdir -p "{workdir}"')
    commands.append(f'chown -R "{target_user}":"{target_group}" "{workdir}"')
    commands.append(f'chmod 755 "{workdir}"')

    script = "\n".join(commands)
    ok, msg = run_as_root(script, root_password=root_password)
    if not ok:
        return False, msg

    post_status = check_workspace_status(workdir, run_as_user)
    if post_status["exists"]:
        return True, f"Successfully created '{workdir}' and set ownership to '{target_user}'."
    return False, f"Setup ran, but directory '{workdir}' was not found: {msg}"


def prompt_root_password_gui(
    parent=None,
    workdir: str = "",
    target_user: str = "current user",
    error_msg: str = "",
) -> Optional[str]:
    """
    Show a Tkinter modal dialog asking the user for authorization.
    On Windows: Explains UAC elevation and requests confirmation to trigger UAC.
    On Unix: Asks for root / sudo password.
    """
    if not workdir:
        workdir = get_default_agent_dir()

    try:
        import tkinter as tk
    except ImportError:
        return None

    result = {"password": None}

    dlg = tk.Toplevel(parent) if parent else tk.Tk()
    dlg.title("Authentication Required")
    dlg.geometry("490x300" if os.name != "nt" else "490x240")
    dlg.resizable(False, False)
    dlg.configure(bg="#1e1e2e")

    if parent:
        dlg.transient(parent)
    dlg.grab_set()

    bg = "#1e1e2e"
    panel = "#313244"
    text = "#cdd6f4"
    subtext = "#a6adc8"
    accent = "#89b4fa"
    red = "#f38ba8"

    container = tk.Frame(dlg, bg=bg, padx=20, pady=16)
    container.pack(fill="both", expand=True)

    hdr = tk.Frame(container, bg=bg)
    hdr.pack(fill="x", pady=(0, 10))
    tk.Label(hdr, text="🔐", font=("sans-serif", 20), bg=bg, fg=accent).pack(side="left", padx=(0, 10))
    title_text = "Administrator Privileges Required"
    title_lbl = tk.Label(
        hdr,
        text=title_text,
        font=("sans-serif", 12, "bold"),
        bg=bg,
        fg=text,
        justify="left",
    )
    title_lbl.pack(side="left", anchor="w")

    if os.name == "nt":
        desc = (
            f"Creating '{workdir}' and setting permissions for '{target_user}'\n"
            f"requires Windows Administrator privileges.\n\n"
            f"Click 'Authorize' to trigger the Windows User Account Control (UAC) prompt."
        )
    else:
        desc = (
            f"Creating '{workdir}' and setting ownership to '{target_user}'\n"
            f"requires root privileges. Please enter your root or sudo password:"
        )

    tk.Label(container, text=desc, font=("sans-serif", 9), bg=bg, fg=subtext, justify="left").pack(
        anchor="w", pady=(0, 10)
    )

    if error_msg:
        err_label = tk.Label(container, text=error_msg, font=("sans-serif", 9), bg=bg, fg=red, justify="left")
        err_label.pack(anchor="w", pady=(0, 6))

    pwd_var = tk.StringVar()
    if os.name != "nt":
        pwd_entry = tk.Entry(
            container,
            textvariable=pwd_var,
            show="•",
            bg=panel,
            fg=text,
            insertbackground=text,
            relief="flat",
            font=("sans-serif", 11),
            width=36,
        )
        pwd_entry.pack(anchor="w", pady=(0, 6))
        pwd_entry.focus_set()

        show_var = tk.BooleanVar(value=False)

        def toggle_show():
            pwd_entry.config(show="" if show_var.get() else "•")

        tk.Checkbutton(
            container,
            text="Show password",
            variable=show_var,
            command=toggle_show,
            bg=bg,
            fg=subtext,
            selectcolor=panel,
            activebackground=bg,
            activeforeground=text,
            relief="flat",
        ).pack(anchor="w", pady=(0, 12))

    btn_row = tk.Frame(container, bg=bg)
    btn_row.pack(fill="x", pady=(10, 0))

    def on_ok(event=None):
        if os.name == "nt":
            result["password"] = "__UAC__"
        else:
            val = pwd_var.get()
            if not val:
                return
            result["password"] = val
        dlg.destroy()

    def on_cancel(event=None):
        dlg.destroy()

    if os.name != "nt":
        pwd_entry.bind("<Return>", on_ok)
    dlg.bind("<Escape>", on_cancel)

    tk.Button(
        btn_row,
        text="Cancel",
        command=on_cancel,
        bg=panel,
        fg=text,
        relief="flat",
        padx=12,
        pady=5,
        cursor="hand2",
    ).pack(side="right", padx=(8, 0))

    tk.Button(
        btn_row,
        text="Authorize",
        command=on_ok,
        bg=accent,
        fg="#1e1e2e",
        font=("sans-serif", 10, "bold"),
        relief="flat",
        padx=14,
        pady=5,
        cursor="hand2",
    ).pack(side="right")

    dlg.wait_window()
    return result["password"]


def prompt_root_password_cli(
    workdir: str = "",
    target_user: str = "current user",
) -> Optional[str]:
    """Prompt for root/sudo password or UAC confirmation in terminal CLI mode."""
    if not workdir:
        workdir = get_default_agent_dir()

    if os.name == "nt":
        print(f"\n🔐 Administrator privileges required to create '{workdir}' and set permissions for '{target_user}'.")
        ans = input("Proceed with Windows UAC prompt? (y/n): ").strip().lower()
        return "__UAC__" if ans == "y" else None

    print(f"\n🔐 Root privileges required to create '{workdir}' and set ownership to '{target_user}'.")
    try:
        pw = getpass.getpass("Enter root or sudo password: ")
        return pw.strip() or None
    except Exception:
        return None


def ensure_workspace_interactive(
    workdir: str = "",
    run_as_user: str = "",
    parent_window=None,
    is_gui: bool = True,
) -> Tuple[bool, str]:
    """
    Ensure the workspace exists and is properly owned and accessible.
    Works seamlessly on Linux, macOS, and Windows.
    """
    if not workdir:
        workdir = get_default_agent_dir()

    status = check_workspace_status(workdir, run_as_user)
    if not status["needs_root"]:
        try:
            os.makedirs(workdir, exist_ok=True)
            return True, f"Workspace '{workdir}' is ready."
        except OSError:
            pass

    # Windows fast-path: if already admin, run setup immediately
    if os.name == "nt" and is_admin_windows():
        return setup_agent_directory(workdir, run_as_user)

    # Unix fast-path: if passwordless sudo available, run setup immediately
    if os.name != "nt" and can_sudo_without_password():
        ok, msg = setup_agent_directory(workdir, run_as_user, root_password=None)
        if ok:
            return True, msg

    # Elevation prompt required
    current_user, _, _, _ = get_current_user_and_group()
    target_user = run_as_user.strip() if run_as_user else current_user

    attempts = 0
    max_attempts = 1 if os.name == "nt" else 3
    last_err = ""

    while attempts < max_attempts:
        attempts += 1
        if is_gui:
            pwd_or_uac = prompt_root_password_gui(
                parent=parent_window,
                workdir=workdir,
                target_user=target_user,
                error_msg=last_err,
            )
        else:
            if last_err:
                print(f"❌ {last_err}")
            pwd_or_uac = prompt_root_password_cli(workdir=workdir, target_user=target_user)

        if pwd_or_uac is None:
            return False, "Authorization cancelled by user."

        password = None if os.name == "nt" else pwd_or_uac
        ok, msg = setup_agent_directory(workdir, run_as_user, root_password=password)
        if ok:
            return True, msg
        last_err = msg

    return False, f"Elevation failed: {last_err}"
