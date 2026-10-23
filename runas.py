"""
Run SystemC's backend processes (bot, web UI, Stoat bot) as another user.

Why this exists
---------------
When "Run under another user account (e.g. 'agent')" is selected, only the
agent's *shell commands* used to switch user.  The bot itself, and every file
it read or wrote, still ran as whoever launched the control panel.  This module
launches the whole backend as the target user instead, so shell commands,
Python snippets and file I/O all happen as that user -- while the control panel
window itself stays in your own desktop session (no xhost / XAUTHORITY / DISPLAY
sharing needed).

One-time setup (needs your sudo password once):

    python3 runas.py grant agent

or click "Allow panel to run as <user>" in the Setup tab.  That installs a
sudoers rule letting *you* run commands as that one user without a password
(it grants nothing beyond what an account with full sudo already has) and gives
your account read/write access to the app folder via ACLs, so the panel can
still edit .env and the approvals queue.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

IS_WINDOWS = os.name == "nt"
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\$?$")


def current_user() -> str:
    try:
        import getpass
        return getpass.getuser()
    except Exception:
        return ""


def valid_username(user: str) -> bool:
    return bool(_USER_RE.match(user or ""))


def needs_switch(user: str) -> bool:
    """True when `user` is set and is not the account we are running as."""
    user = (user or "").strip()
    return bool(user) and not IS_WINDOWS and user != current_user()


def _is_root() -> bool:
    return hasattr(os, "getuid") and os.getuid() == 0


def _home_of(user: str) -> str:
    try:
        import pwd
        return pwd.getpwnam(user).pw_dir
    except Exception:
        return f"/home/{user}"


def _prefix(user: str, is_root: Optional[bool] = None) -> List[str]:
    root = _is_root() if is_root is None else is_root
    if root:
        return ["runuser", "-u", user, "--"]
    # -n: never prompt (a prompt would hang a background process);
    # -H: set HOME to the target user's home.
    return ["sudo", "-n", "-u", user, "-H", "--"]


def wrap_command(args: List[str], user: str,
                 extra_env: Optional[Dict[str, str]] = None,
                 is_root: Optional[bool] = None) -> List[str]:
    """Return `args` prefixed so that it runs as `user`.

    sudo/runuser scrub or keep the environment inconsistently, so everything the
    child needs is passed explicitly through `env`.
    """
    env = {
        "HOME": _home_of(user),
        "USER": user,
        "LOGNAME": user,
    }
    env.update(extra_env or {})
    env_args = [f"{k}={v}" for k, v in env.items()]
    return _prefix(user, is_root) + ["env"] + env_args + list(args)


def can_switch(user: str) -> bool:
    """Can we start processes as `user` right now, without a password prompt?"""
    if not needs_switch(user):
        return True
    if _is_root():
        return shutil.which("runuser") is not None
    if not shutil.which("sudo"):
        return False
    try:
        # -k: ignore cached credentials, so this only passes for a real
        # NOPASSWD rule and not just a recently-typed password.
        r = subprocess.run(["sudo", "-k", "-n", "-u", user, "true"],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def resolve_python(user: str) -> Tuple[str, str]:
    """Pick an interpreter `user` can actually execute.

    Returns (path, warning).  `warning` is empty when the interpreter running
    this panel is usable as-is.
    """
    def usable(path: str) -> bool:
        try:
            r = subprocess.run(_prefix(user) + ["test", "-x", path],
                               capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    if usable(sys.executable):
        return sys.executable, ""
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found and usable(found):
            return found, (
                f"{sys.executable} isn't executable by '{user}' (often a venv inside your "
                f"home folder); using {found} instead. Packages installed only in the "
                f"panel's environment won't be visible to it."
            )
    return sys.executable, (
        f"'{user}' can't execute {sys.executable} and no system python3 was found."
    )


def no_switch_message(user: str) -> str:
    return (
        f"Can't run commands as '{user}' without a password yet. "
        f"Click \"Allow panel to run as {user}\" in the Setup tab, or run:  "
        f"python3 runas.py grant {user}"
    )


# ── one-time privileged setup ────────────────────────────────────────────────

_AGENT_ACCESS_SNIPPET = r"""
# Let AGENT use the app folder even when it lives in someone else's home
# (e.g. /home/<you>/SystemC): traverse the parent folders, read/write the folder.
if [ "$(stat -c %U "$DIR")" != "$AGENT" ]; then
    p=$(dirname "$DIR")
    while [ "$p" != "/" ] && [ -n "$p" ]; do
        [ $(( $(stat -c %a "$p") % 10 & 1 )) -eq 0 ] && setfacl -m "u:$AGENT:--x" "$p"
        p=$(dirname "$p")
    done
    setfacl -R -m "u:$AGENT:rwX" "$DIR"
    find "$DIR" -type d -exec setfacl -d -m "u:$AGENT:rwX" {} +
fi
"""


def _access_script(user: str, app_dir: str) -> str:
    u, d = shlex.quote(user), shlex.quote(app_dir)
    return (f"set -e\nAGENT={u}\nDIR={d}\n"
            'command -v setfacl >/dev/null 2>&1 || { echo "setfacl not found - install it with: sudo apt install acl" >&2; exit 3; }\n'
            + _AGENT_ACCESS_SNIPPET)


def can_access(user: str, app_dir: str) -> bool:
    """Can `user` actually read/run/write the app folder? (False => the bot would
    die with 'Permission denied' the moment it starts.)"""
    if not needs_switch(user):
        return True
    try:
        r = subprocess.run(
            wrap_command(["sh", "-c",
                          'test -r "$0/bot.py" && test -x "$0" && test -w "$0" && { [ ! -e "$0/.env" ] || { test -r "$0/.env" && test -w "$0/.env"; }; }', app_dir], user),
            capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def grant_agent_access(user: str, app_dir: str) -> Tuple[bool, str]:
    """Give `user` access to the app folder using only *our own* rights (works
    when this account owns the folder -- no password needed)."""
    if IS_WINDOWS or not valid_username(user):
        return False, "Not supported here."
    try:
        r = subprocess.run(["bash", "-c", _access_script(user, os.path.abspath(app_dir))],
                           capture_output=True, text=True, timeout=120)
    except Exception as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def _grant_script(user: str, panel_user: str, app_dir: str) -> str:
    u, p, d = shlex.quote(user), shlex.quote(panel_user), shlex.quote(app_dir)
    rule_file = shlex.quote(f"/etc/sudoers.d/systemc-runas-{user}")
    return f"""\
set -e
AGENT={u}
PANEL={p}
DIR={d}
id "$AGENT" >/dev/null 2>&1 || {{ echo "user '$AGENT' does not exist" >&2; exit 2; }}

# 1. Let PANEL run commands as AGENT (only AGENT) without a password.
TMP=$(mktemp)
echo "$PANEL ALL=($AGENT) NOPASSWD: ALL" > "$TMP"
visudo -cf "$TMP" >/dev/null || {{ rm -f "$TMP"; echo "generated sudoers rule failed validation" >&2; exit 4; }}
install -m 0440 -o root -g root "$TMP" {rule_file}
rm -f "$TMP"

# 2. Give PANEL read/write access to the app folder (ACLs, so ownership stays AGENT's).
command -v setfacl >/dev/null 2>&1 || {{ echo "setfacl not found - install it with: sudo apt install acl" >&2; exit 3; }}
p=$(dirname "$DIR")
while [ "$p" != "/" ] && [ -n "$p" ]; do
    [ "$(stat -c %U "$p")" = "$AGENT" ] && setfacl -m "u:$PANEL:--x" "$p"
    p=$(dirname "$p")
done
setfacl -R -m "u:$PANEL:rwX" "$DIR"
find "$DIR" -type d -exec setfacl -d -m "u:$PANEL:rwX" {{}} +
{_AGENT_ACCESS_SNIPPET}
echo "ok"
"""


def grant_panel_access(user: str, app_dir: str,
                       root_password: Optional[str] = None,
                       panel_user: Optional[str] = None) -> Tuple[bool, str]:
    """Install the sudoers rule + ACLs described in the module docstring."""
    panel_user = panel_user or current_user()
    if not valid_username(user) or not valid_username(panel_user):
        return False, "Refusing: usernames may only contain a-z, 0-9, '_' and '-'."
    if IS_WINDOWS:
        return False, "Running as another user is only implemented for Linux/macOS."
    if user == panel_user:
        return True, "Nothing to do: the panel already runs as that user."

    import account_setup
    script = _grant_script(user, panel_user, os.path.abspath(app_dir))
    ok, msg = account_setup.run_as_root(script, root_password=root_password)
    if ok:
        return True, (f"'{panel_user}' can now run the bot as '{user}' without a password, "
                      f"and has read/write access to {app_dir}.")
    return False, msg


# ── elevated commands (sudo) for the restricted account ──────────────────────

def _elevate_rule_path(user: str) -> str:
    return f"/etc/sudoers.d/systemc-elevate-{user}"


def elevation_ready(user: str) -> bool:
    """Can `user` run `sudo` without a password right now?"""
    if not valid_username(user) or IS_WINDOWS:
        return False
    try:
        if needs_switch(user):
            if not can_switch(user):
                return False
            cmd = wrap_command(["sudo", "-n", "true"], user)
        else:
            cmd = ["sudo", "-n", "true"]
        return subprocess.run(cmd, capture_output=True, timeout=8).returncode == 0
    except Exception:
        return False


def set_elevation(user: str, enabled: bool,
                  root_password: Optional[str] = None) -> Tuple[bool, str]:
    """Turn the OS-level side of "Allow running elevated?" on or off.

    On: installs a sudoers rule so `user` can run sudo without a password (the
    bot still asks you for approval in chat before each elevated command).
    Off: deletes that rule, so `user` genuinely has no sudo.
    """
    if IS_WINDOWS:
        return False, "Elevated commands aren't supported on Windows."
    if not valid_username(user):
        return False, "Refusing: usernames may only contain a-z, 0-9, '_' and '-'."
    import account_setup
    rule = shlex.quote(_elevate_rule_path(user))
    if enabled:
        script = f"""\
set -e
id {shlex.quote(user)} >/dev/null 2>&1 || {{ echo "user '{user}' does not exist" >&2; exit 2; }}
TMP=$(mktemp)
echo "{user} ALL=(ALL:ALL) NOPASSWD: ALL" > "$TMP"
visudo -cf "$TMP" >/dev/null || {{ rm -f "$TMP"; echo "generated sudoers rule failed validation" >&2; exit 4; }}
install -m 0440 -o root -g root "$TMP" {rule}
rm -f "$TMP"
echo ok
"""
    else:
        script = f"rm -f {rule}\necho ok\n"
    ok, msg = account_setup.run_as_root(script, root_password=root_password)
    if not ok:
        return False, msg
    if enabled:
        return True, (f"'{user}' can now run sudo without a password. Every elevated command "
                      f"the agent sends still needs your approval in chat first.")
    return True, f"Removed sudo access for '{user}'. Elevated commands are off."


def main(argv: List[str]) -> int:
    app_dir = os.path.dirname(os.path.abspath(__file__))
    if len(argv) >= 3 and argv[1] == "grant":
        user = argv[2]
        import account_setup
        pw = None
        if not _is_root() and not account_setup.can_sudo_without_password():
            pw = account_setup.prompt_root_password_cli(app_dir, user)
        ok, msg = grant_panel_access(user, app_dir, root_password=pw)
        print(("OK: " if ok else "FAILED: ") + msg)
        return 0 if ok else 1
    if len(argv) >= 4 and argv[1] == "elevate" and argv[3] in ("on", "off"):
        user = argv[2]
        import account_setup
        pw = None
        if not _is_root() and not account_setup.can_sudo_without_password():
            pw = account_setup.prompt_root_password_cli(app_dir, user)
        ok, msg = set_elevation(user, argv[3] == "on", root_password=pw)
        print(("OK: " if ok else "FAILED: ") + msg)
        return 0 if ok else 1
    if len(argv) >= 3 and argv[1] == "status":
        user = argv[2]
        print(f"panel user : {current_user()}")
        print(f"target user: {user}")
        print(f"can switch : {can_switch(user)}")
        print(f"has sudo   : {elevation_ready(user)}")
        return 0
    print("usage: python3 runas.py grant <user> | elevate <user> on|off | status <user>")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
