"""
Tool implementations.

On Linux/macOS, shell execution uses a PTY (pseudo-terminal) so programs
that check for interactive terminals (apt, docker, etc.) behave correctly
and colour output is preserved. On Windows (no PTY support), it falls back
to a plain subprocess with piped output — same {stdout, exit_code, cwd}
shape either way, so nothing upstream (agent.py, bot.py, webui_server.py)
needs to know which platform it's on.

Destructive commands (anything touching rm, sudo, pip install, apt, curl|sh,
mkfs, dd, del, format, etc.) go through an approval gate: the bot posts a
Discord embed (and a local control-panel entry) asking the user to approve
or deny before the command runs. This mirrors OpenClaw's exec-approvals
system.
"""

import os
import re
import sys
import shutil
import asyncio
import subprocess

IS_WINDOWS = os.name == "nt"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_WORKDIR = os.path.join(APP_DIR, "workspace")

_workdir_env = os.environ.get("AGENT_WORKDIR", "").strip()
AGENT_RUN_AS_USER = os.environ.get("AGENT_RUN_AS_USER", "").strip()
if _workdir_env:
    WORKDIR = _workdir_env
else:
    WORKDIR = DEFAULT_WORKDIR

if IS_WINDOWS:
    # Prefer PowerShell (7 > Windows PowerShell) for saner scripting; fall back to cmd.exe
    SHELL = (
        os.environ.get("SHELL")
        or shutil.which("pwsh")
        or shutil.which("powershell")
        or "cmd.exe"
    )
else:
    import fcntl
    import pty
    import select
    import signal
    import struct
    import termios

    SHELL = _sh = os.environ.get("SHELL", "/bin/bash")
    # Prefer bash/sh over exotic shells (fish etc.) — same logic as OpenClaw
    if os.path.basename(SHELL) in ("fish", "zsh", "csh", "tcsh"):
        for candidate in ("/bin/bash", "/bin/sh"):
            if os.path.exists(candidate):
                SHELL = candidate
                break

try:
    os.makedirs(WORKDIR, exist_ok=True)
except OSError as e:
    sys.stderr.write(
        f"Warning: Failed to create WORKDIR {WORKDIR!r} ({e}).\n"
        f"If using /home/agent, make sure it has been created and chowned via the Control Panel or setup script.\n"
        f"Falling back to {DEFAULT_WORKDIR!r}\n"
    )
    WORKDIR = DEFAULT_WORKDIR
    try:
        os.makedirs(WORKDIR, exist_ok=True)
    except OSError:
        user_workspace = os.path.join(os.path.expanduser("~"), "systemc_workspace")
        WORKDIR = user_workspace
        os.makedirs(WORKDIR, exist_ok=True)

SHELL_TIMEOUT = 60  # seconds
OUTPUT_CAP = 6000   # chars — keep Discord-friendly

# ── Destructive command detection ──────────────────────────────────────────
def is_destructive(command: str) -> bool:
    """
    Returns True if the command looks destructive and needs user approval.
    Checks for the command token at the start of a pipeline segment, not
    anywhere in the string, to avoid false positives like 'echo rm done'.
    Covers both Unix and Windows/PowerShell destructive command names, since
    the same regex runs regardless of which platform is actually executing.
    """
    # Tokens that are destructive when they appear as a command name
    _CMDS = re.compile(
        r"(?:^|[|;&`(]\s*)"          # start, pipe, semicolon, subshell
        r"(?:sudo\s+)?"              # optionally prefixed with sudo
        r"("
        r"rm|rmdir|shred|mkfs|fdisk|parted|wipefs"
        r"|dd"
        r"|shutdown|reboot|halt|poweroff"
        r"|sudo|su"
        r"|pip3?|apt(?:-get)?|dpkg|snap|flatpak"
        r"|npm|yarn|pnpm"
        r"|systemctl|service"
        r"|chown|chmod"
        r"|iptables|ip6tables|ufw|nft"
        r"|kill|killall|pkill"
        r"|del|erase|format|diskpart|cipher"
        r"|remove-item|clear-disk|stop-computer|restart-computer"
        r"|reg|taskkill|net"
        r")\b",
        re.IGNORECASE | re.MULTILINE,
    )
    # Pipe-to-shell pattern anywhere in the command
    _PIPE_SHELL = re.compile(r"\|\s*(ba)?sh\b|\|\s*(iex|invoke-expression)\b", re.IGNORECASE)
    return bool(_CMDS.search(command)) or bool(_PIPE_SHELL.search(command))


# ── PTY-based shell exec (Linux/macOS) ──────────────────────────────────────
def run_shell(command: str) -> dict:
    """Run a shell command in the workspace. Dispatches to the PTY-based
    implementation on Linux/macOS, or the piped-subprocess implementation on
    Windows. Both return {"stdout": str, "exit_code": int, "cwd": str}."""
    if IS_WINDOWS:
        return _run_shell_windows(command)
    return _run_shell_unix(command)


def _run_shell_unix(command: str) -> dict:
    """
    Run a shell command via PTY so interactive programs work correctly.
    Returns {"stdout": str, "exit_code": int, "cwd": str}.
    """
    master_fd, slave_fd = pty.openpty()

    # Set a sensible terminal size
    winsize = struct.pack("HHHH", 24, 220, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

    proc_env = os.environ.copy()
    if os.path.exists(WORKDIR):
        proc_env["HOME"] = WORKDIR
        proc_env["PWD"] = WORKDIR

    current_user = None
    try:
        import getpass
        current_user = getpass.getuser()
    except Exception:
        pass

    if AGENT_RUN_AS_USER and AGENT_RUN_AS_USER != current_user:
        if hasattr(os, "getuid") and os.getuid() == 0:
            cmd_args = ["runuser", "-u", AGENT_RUN_AS_USER, "--", SHELL, "-c", command]
        else:
            cmd_args = ["sudo", "-n", "-u", AGENT_RUN_AS_USER, "-H", "--", SHELL, "-c", command]
    else:
        cmd_args = [SHELL, "-c", command]

    try:
        proc = subprocess.Popen(
            cmd_args,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=WORKDIR if os.path.exists(WORKDIR) else None,
            env=proc_env,
            close_fds=True,
            preexec_fn=os.setsid,
        )
        os.close(slave_fd)

        output_bytes = bytearray()
        deadline = asyncio.get_event_loop().time() if False else None  # sync path
        import time
        end_time = time.monotonic() + SHELL_TIMEOUT

        while True:
            if time.monotonic() > end_time:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                return {
                    "stdout": _clean(bytes(output_bytes)),
                    "exit_code": -1,
                    "error": f"timed out after {SHELL_TIMEOUT}s",
                    "cwd": WORKDIR,
                }
            r, _, _ = select.select([master_fd], [], [], 0.2)
            if r:
                try:
                    chunk = os.read(master_fd, 4096)
                    if chunk:
                        output_bytes.extend(chunk)
                        continue
                except OSError:
                    break
            if proc.poll() is not None:
                # Drain any remaining output
                while True:
                    r, _, _ = select.select([master_fd], [], [], 0.1)
                    if not r:
                        break
                    try:
                        chunk = os.read(master_fd, 4096)
                        if chunk:
                            output_bytes.extend(chunk)
                        else:
                            break
                    except OSError:
                        break
                break

        proc.wait()
        return {
            "stdout": _clean(bytes(output_bytes)),
            "exit_code": proc.returncode,
            "cwd": WORKDIR,
        }
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass


def _windows_shell_args(command: str) -> list[str]:
    """Build the argv for launching `command` under the configured Windows shell."""
    shell_name = os.path.basename(SHELL).lower()
    if "powershell" in shell_name or shell_name.startswith("pwsh"):
        return [SHELL, "-NoProfile", "-NonInteractive", "-Command", command]
    return [SHELL, "/c", command]


def _run_shell_windows(command: str) -> dict:
    """
    Run a shell command via a plain piped subprocess (no PTY on Windows).
    Kills the whole process tree with taskkill on timeout, since
    proc.terminate() alone can leave child processes orphaned on Windows.
    Returns {"stdout": str, "exit_code": int, "cwd": str}.
    """
    args = _windows_shell_args(command)
    proc_env = os.environ.copy()
    if os.path.exists(WORKDIR):
        proc_env["USERPROFILE"] = WORKDIR
        proc_env["HOME"] = WORKDIR
        drive, p = os.path.splitdrive(WORKDIR)
        proc_env["HOMEDRIVE"] = drive or "C:"
        proc_env["HOMEPATH"] = p or "\\"
    proc = subprocess.Popen(
        args,
        cwd=WORKDIR if os.path.exists(WORKDIR) else None,
        env=proc_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        stdout, _ = proc.communicate(timeout=SHELL_TIMEOUT)
        return {"stdout": _clean(stdout), "exit_code": proc.returncode, "cwd": WORKDIR}
    except subprocess.TimeoutExpired:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
        stdout, _ = proc.communicate()
        return {
            "stdout": _clean(stdout),
            "exit_code": -1,
            "error": f"timed out after {SHELL_TIMEOUT}s",
            "cwd": WORKDIR,
        }


def _clean(raw) -> str:
    """Decode (if bytes), strip ANSI escape codes, cap length."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else raw
    # Strip ANSI escapes
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    text = re.sub(r"\x1b\][^\x07]*\x07", "", text)
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"\r", "\n", text)
    if len(text) > OUTPUT_CAP:
        half = OUTPUT_CAP // 2
        text = text[:half] + f"\n... [truncated] ...\n" + text[-half:]
    return text.strip()


# ── Elevated shell exec ─────────────────────────────────────────────────────
def elevated_enabled() -> bool:
    """Is run_shell_elevated offered on this box?

    AGENT_ALLOW_ELEVATED=1/0 (set by the control panel's "Allow running elevated?"
    box) decides.  When unset: on for a normal install (the agent runs as the
    owner, who has sudo), off when the agent runs under a separate user account.
    """
    v = os.environ.get("AGENT_ALLOW_ELEVATED", "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return not AGENT_RUN_AS_USER


def run_shell_elevated(command: str) -> dict:
    """
    Run a shell command with elevated privileges. Always requires approval --
    the agent.py exec_shell_elevated coroutine handles the gate before
    calling this.
    """
    if not elevated_enabled():
        return {
            "stdout": "",
            "exit_code": -1,
            "error": ("Elevated commands are turned off on this machine "
                      "(Control Panel > Setup > 'Allow running elevated?')."),
            "cwd": WORKDIR,
        }
    if IS_WINDOWS:
        return {
            "stdout": "",
            "exit_code": -1,
            "error": (
                "Elevated (admin) commands aren't supported on Windows here — there's no "
                "sudo equivalent that reliably pipes output back. Run the control panel "
                "itself as Administrator if the whole bot needs elevated rights, or use "
                "WSL for full sudo support."
            ),
            "cwd": WORKDIR,
        }
    if AGENT_RUN_AS_USER and hasattr(os, "getuid") and os.getuid() != 0:
        # Running as the restricted account: sudo must work without a prompt,
        # otherwise it would just hang until the shell timeout.
        try:
            ok = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5).returncode == 0
        except Exception:
            ok = False
        if not ok:
            return {
                "stdout": "",
                "exit_code": -1,
                "error": ("This account has no passwordless sudo rule. Tick 'Allow running "
                          "elevated?' in the Control Panel and save, or run: "
                          "python3 runas.py elevate <user> on"),
                "cwd": WORKDIR,
            }
    return run_shell(f"sudo {command}")


# ── File tools ──────────────────────────────────────────────────────────────
def read_file(path: str) -> dict:
    full = os.path.join(WORKDIR, path) if not os.path.isabs(path) else path
    try:
        with open(full, "r", errors="replace") as f:
            content = f.read(8000)
        return {"content": content, "path": full, "truncated": len(content) >= 8000}
    except Exception as e:
        return {"error": str(e)}


def write_file(path: str, content: str) -> dict:
    full = os.path.join(WORKDIR, path) if not os.path.isabs(path) else path
    try:
        os.makedirs(os.path.dirname(os.path.abspath(full)), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
        return {"written": len(content), "path": full}
    except Exception as e:
        return {"error": str(e)}


def list_dir(path: str = ".") -> dict:
    full = os.path.join(WORKDIR, path) if not os.path.isabs(path) else path
    try:
        entries = []
        for name in sorted(os.listdir(full)):
            fp = os.path.join(full, name)
            entries.append({
                "name": name,
                "type": "dir" if os.path.isdir(fp) else "file",
                "size": os.path.getsize(fp) if os.path.isfile(fp) else None,
            })
        return {"path": full, "entries": entries}
    except Exception as e:
        return {"error": str(e)}


# ── Python exec tool ─────────────────────────────────────────────────────────
def run_python(code: str) -> dict:
    """
    Run a Python snippet in the workspace using the same interpreter the bot
    itself runs under (so it sees whatever's pip-installed). Unlike run_shell
    this doesn't use a PTY — plain piped stdout/stderr is all a script needs,
    and it means run_python behaves identically on every platform.
    Returns {stdout, exit_code, cwd}, same shape as run_shell.
    """
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".py", dir=WORKDIR)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(code)
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass

        proc_env = os.environ.copy()
        if os.path.exists(WORKDIR):
            proc_env["HOME"] = WORKDIR
            proc_env["PWD"] = WORKDIR
            if IS_WINDOWS:
                proc_env["USERPROFILE"] = WORKDIR
                drive, drv_p = os.path.splitdrive(WORKDIR)
                proc_env["HOMEDRIVE"] = drive or "C:"
                proc_env["HOMEPATH"] = drv_p or "\\"

        current_user = None
        try:
            import getpass
            current_user = getpass.getuser()
        except Exception:
            pass

        if not IS_WINDOWS and AGENT_RUN_AS_USER and AGENT_RUN_AS_USER != current_user:
            if hasattr(os, "getuid") and os.getuid() == 0:
                py_cmd = ["runuser", "-u", AGENT_RUN_AS_USER, "--", sys.executable, path]
            else:
                py_cmd = ["sudo", "-n", "-u", AGENT_RUN_AS_USER, "-H", sys.executable, path]
        else:
            py_cmd = [sys.executable, path]

        proc = subprocess.run(
            py_cmd,
            cwd=WORKDIR if os.path.exists(WORKDIR) else None,
            env=proc_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=SHELL_TIMEOUT,
        )
        return {"stdout": _clean(proc.stdout), "exit_code": proc.returncode, "cwd": WORKDIR}
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        return {
            "stdout": _clean(out),
            "exit_code": -1,
            "error": f"timed out after {SHELL_TIMEOUT}s",
            "cwd": WORKDIR,
        }
    except Exception as e:
        return {"error": str(e), "exit_code": -1, "cwd": WORKDIR}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ── Web search ───────────────────────────────────────────────────────────────
async def web_search(query: str, max_results: int = 5) -> dict:
    """
    Search the web via DuckDuckGo's HTML endpoint (no API key needed).
    Returns {"results": [{"title", "url", "snippet"}, ...]}.
    """
    import httpx
    import html as htmllib
    from urllib.parse import unquote, parse_qs, urlparse

    max_results = max(1, min(int(max_results), 10))
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (compatible; systemc-agent/1.0)"},
            )
            resp.raise_for_status()
            body = resp.text
    except Exception as e:
        return {"error": f"search failed: {e}"}

    def _strip_tags(s: str) -> str:
        return htmllib.unescape(re.sub(r"<[^>]+>", "", s)).strip()

    # Find each title link first, then look for a snippet link in the stretch
    # of HTML between this title and the next one (bounded, in case one is
    # missing entirely). Doing it in two passes avoids the classic trap of an
    # optional group after a lazy `.*?` silently matching empty.
    title_re = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL
    )
    snippet_re = re.compile(
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL
    )

    title_matches = list(title_re.finditer(body))
    results = []
    for i, m in enumerate(title_matches):
        raw_href, raw_title = m.group(1), m.group(2)
        window_end = title_matches[i + 1].start() if i + 1 < len(title_matches) else min(len(body), m.end() + 2000)
        window = body[m.end():window_end]
        sm = snippet_re.search(window)
        raw_snippet = sm.group(1) if sm else ""

        # DuckDuckGo wraps result URLs in a redirect: //duckduckgo.com/l/?uddg=<encoded>&...
        url = raw_href
        if "uddg=" in raw_href:
            qs = parse_qs(urlparse(raw_href).query)
            if qs.get("uddg"):
                url = unquote(qs["uddg"][0])

        results.append({
            "title": _strip_tags(raw_title),
            "url": url,
            "snippet": _strip_tags(raw_snippet),
        })
        if len(results) >= max_results:
            break

    if not results:
        return {"results": [], "note": "no results parsed — DuckDuckGo may have changed its markup"}
    return {"results": results}


# ── Discord tools ────────────────────────────────────────────────────────────
async def fetch_channel_messages(channel, limit: int = 20) -> dict:
    limit = max(1, min(int(limit), 100))
    msgs = []
    async for m in channel.history(limit=limit):
        msgs.append(m)
    msgs.reverse()
    results = []
    for m in msgs:
        entry = {
            "id": str(m.id),
            "author": str(m.author),
            "content": m.content,
            "timestamp": m.created_at.isoformat(),
        }
        if m.reference and m.reference.resolved:
            ref = m.reference.resolved
            entry["reply_to"] = {
                "author": str(getattr(ref, "author", "?")),
                "content": str(getattr(ref, "content", ""))[:200],
            }
        results.append(entry)
    return {"messages": results, "count": len(results)}


async def send_message(channel, content: str) -> dict:
    # Discord hard limit; split if needed
    chunks = [content[i:i+1900] for i in range(0, len(content), 1900)]
    for chunk in chunks:
        await channel.send(chunk)
    return {"sent": True, "chunks": len(chunks)}


MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # Discord's default (non-boosted) attachment cap


async def send_file(channel, path: str, caption: str = "") -> dict:
    """Upload a workspace file to the current Discord channel as an attachment."""
    import discord

    full = os.path.join(WORKDIR, path) if not os.path.isabs(path) else path
    if not os.path.isfile(full):
        return {"error": f"no such file: {full}"}
    size = os.path.getsize(full)
    if size > MAX_UPLOAD_BYTES:
        return {"error": f"file is {size} bytes, over Discord's {MAX_UPLOAD_BYTES}-byte limit"}
    await channel.send(content=caption or None, file=discord.File(full))
    return {"sent": True, "path": full, "bytes": size}


ATTACHMENTS_DIR = os.path.join(WORKDIR, "attachments")


async def download_attachments(message) -> list[str]:
    """
    Save any attachments on an incoming Discord message into
    WORKDIR/attachments/, using discord.py's built-in Attachment.save().
    Returns a list of paths (relative to WORKDIR) that were saved, so bot.py
    can tell the agent what's now available for read_file/run_python to use.
    """
    if not message.attachments:
        return []
    os.makedirs(ATTACHMENTS_DIR, exist_ok=True)
    saved = []
    for att in message.attachments:
        safe_name = re.sub(r"[^\w.\-]", "_", att.filename)
        dest = os.path.join(ATTACHMENTS_DIR, f"{message.id}_{safe_name}")
        try:
            await att.save(dest)
            saved.append(os.path.relpath(dest, WORKDIR))
        except Exception:
            continue  # skip files we couldn't save; don't fail the whole turn
    return saved


# ── Approval gate ────────────────────────────────────────────────────────────
async def request_approval(channel, command: str, timeout: int = 60) -> bool:
    """
    Post an approval request to Discord AND register it in the local
    .approvals/ queue, then wait on whichever resolves first — a Discord
    ✅/❌ reaction, or an Approve/Deny click in the control panel's
    Approvals tab. Returns True if approved, False if denied or timed out.
    """
    import approvals as approvals_store

    # Non-Discord platforms (e.g. Stoat) expose ask_approval(); race their
    # in-chat yes/no reply against the control panel's Approvals tab.
    if hasattr(channel, "ask_approval"):
        req_id = approvals_store.create(command)

        async def wait_local_generic() -> bool:
            while True:
                status = approvals_store.get_status(req_id)
                if status in ("approved", "denied"):
                    return status == "approved"
                await asyncio.sleep(1)

        tasks = [
            asyncio.create_task(channel.ask_approval(command, timeout)),
            asyncio.create_task(wait_local_generic()),
        ]
        approved = False
        try:
            done, pending = await asyncio.wait(
                tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            if done:
                try:
                    approved = bool(done.pop().result())
                except Exception:
                    approved = False
            else:
                try:
                    await channel.send("⏱️ Timed out — command cancelled.")
                except Exception:
                    pass
        finally:
            approvals_store.cleanup(req_id)
        return approved

    import discord

    req_id = approvals_store.create(command)

    embed = discord.Embed(
        title="⚠️ Approval required",
        description=f"```\n{command[:1000]}\n```",
        color=discord.Color.orange(),
    )
    embed.set_footer(text=f"React ✅/❌ here, or approve from the control panel ({timeout}s timeout)")
    msg = await channel.send(embed=embed)
    await msg.add_reaction("✅")
    await msg.add_reaction("❌")

    def check(reaction, user):
        return (
            not user.bot
            and str(reaction.emoji) in ("✅", "❌")
            and reaction.message.id == msg.id
        )

    client = channel._state._get_client()

    async def wait_discord() -> bool:
        reaction, _ = await client.wait_for("reaction_add", check=check)
        return str(reaction.emoji) == "✅"

    async def wait_local() -> bool:
        while True:
            status = approvals_store.get_status(req_id)
            if status in ("approved", "denied"):
                return status == "approved"
            await asyncio.sleep(1)

    tasks = [asyncio.create_task(wait_discord()), asyncio.create_task(wait_local())]
    approved = False
    timed_out = True
    try:
        done, pending = await asyncio.wait(
            tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        if done:
            timed_out = False
            approved = done.pop().result()
    finally:
        approvals_store.cleanup(req_id)

    if timed_out:
        await msg.edit(embed=discord.Embed(
            title="⏱️ Timed out — command cancelled",
            description=f"```\n{command[:1000]}\n```",
            color=discord.Color.greyple(),
        ))
        return False

    label = "✅ Approved" if approved else "❌ Denied"
    await msg.edit(embed=discord.Embed(
        title=label,
        description=f"```\n{command[:1000]}\n```",
        color=discord.Color.green() if approved else discord.Color.red(),
    ))
    return approved


# ── Tool schemas ─────────────────────────────────────────────────────────────
LOCAL_TOOL_SCHEMAS = [
    {
        "name": "run_shell",
        "description": (
            "Run a shell command on the owner's computer (starts in the agent's workspace). "
            "Returns stdout, exit_code, and cwd as structured fields. "
            "Destructive commands (rm, sudo, apt install, etc.) will be sent "
            "to Discord for user approval before running."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "run_shell_elevated",
        "description": (
            "Run a shell command with sudo (root/elevated privileges). "
            "ALWAYS requires user approval via Discord before running — "
            "use this only when a normal run_shell fails due to permissions. "
            "Returns stdout, exit_code, and cwd."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run as root"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file from the workspace. Returns content and path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write text content to a file in the workspace (overwrites if it exists).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_dir",
        "description": "List files and directories in a workspace path. Returns name, type, size.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path relative to workspace, defaults to workspace root"}},
        },
    },
    {
        "name": "run_python",
        "description": (
            "Run a Python snippet in the workspace using the bot's own interpreter "
            "(same installed packages). Prefer this over run_shell for anything "
            "beyond a couple lines of Python — no need to escape quotes for -c. "
            "Returns stdout (prints + errors combined), exit_code, and cwd."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The Python source to run"},
            },
            "required": ["code"],
        },
    },
]

# No run_shell_elevated at all on boxes where it's toggled off: the model never
# sees the tool, so it can't ask for it.
if not elevated_enabled():
    LOCAL_TOOL_SCHEMAS = [s for s in LOCAL_TOOL_SCHEMAS if s["name"] != "run_shell_elevated"]


WEB_TOOL_SCHEMAS = [
    {
        "name": "web_search",
        "description": (
            "Search the web and get back a list of {title, url, snippet} results. "
            "Use this for anything current or outside your training data — news, "
            "docs, prices, 'what version is X on now', etc. Doesn't fetch full "
            "page content; follow up with run_shell (curl) if you need a page body."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "How many results to return (1-10, default 5)"},
            },
            "required": ["query"],
        },
    },
]

DISCORD_TOOL_SCHEMAS = [
    {
        "name": "fetch_channel_messages",
        "description": "Fetch recent message history from the current Discord channel, including reply context.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Number of messages to fetch (1-100, default 20)"}},
        },
    },
    {
        "name": "send_message",
        "description": "Send a message to the current Discord channel. Automatically splits messages over 1900 chars.",
        "input_schema": {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
    },
    {
        "name": "send_file",
        "description": (
            "Upload a file from the workspace to the current Discord channel as an "
            "attachment (e.g. a chart you generated, a log file, an edited document). "
            "10MB limit. Optionally include a short caption."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file, relative to the workspace"},
                "caption": {"type": "string", "description": "Optional message to send alongside the file"},
            },
            "required": ["path"],
        },
    },
]
