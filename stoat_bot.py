"""
Stoat bot entry point — a second frontend for the same Agent as the Discord bot.

Stoat is an open-source, privacy-focused Discord alternative (formerly Revolt).
Get a bot token at https://stoat.chat/settings -> My Bots
Library: `pip install stoat.py`  (imported as `stoat`)

Set in .env:
  STOAT_BOT_TOKEN=your_token_here
  STOAT_ALLOWED_CHANNEL_IDS=channelid1,channelid2
  STOAT_BOT_MODE=WAKEWORD          # WAKEWORD / MENTION / EVERYTHING
  STOAT_WAKE_WORD=hey claw         # only used in WAKEWORD mode

Two ways to run it:
  * python main.py        -> runs inside main.py's event loop (alongside Discord)
  * python stoat_bot.py   -> standalone (this is what the control panel starts)

Conversation history (see sessions.py) is keyed by "stoat:{channel_id}" so it never
collides with Discord channels. Long-term memory (.memory/) is shared with the Discord bot.
"""

import os
import re
import sys
import asyncio
from pathlib import Path

import sessions

import version
print(version.banner())
HERE = Path(__file__).parent


def _load_dotenv():
    p = HERE / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


async def _download_attachments(message) -> list[str]:
    """Save attachments on an incoming Stoat message into WORKDIR/attachments/."""
    atts = getattr(message, "attachments", None) or []
    if not atts:
        return []
    import tools
    os.makedirs(tools.ATTACHMENTS_DIR, exist_ok=True)
    saved = []
    for att in atts:
        try:
            safe = re.sub(r"[^\w.\-]", "_", att.filename)
            dest = os.path.join(tools.ATTACHMENTS_DIR, f"{message.id}_{safe}")
            with open(dest, "wb") as f:
                f.write(await att.read())
            saved.append(os.path.relpath(dest, tools.WORKDIR))
        except Exception:
            continue
    return saved


async def start_stoat_bot(agent):
    """
    Run the Stoat bot until it disconnects. `agent` is a shared Agent().
    Must be awaited from a running event loop (main.py does this).
    """
    try:
        import stoat
    except ImportError:
        print("[stoat] stoat.py not installed — run: pip install stoat.py", flush=True)
        return

    from chat_platform import StoatChannel

    TOKEN = os.environ.get("STOAT_BOT_TOKEN", "")
    if not TOKEN:
        print("[stoat] STOAT_BOT_TOKEN not set — Stoat bot disabled", flush=True)
        return

    ALLOWED = {
        x.strip() for x in os.environ.get("STOAT_ALLOWED_CHANNEL_IDS", "").split(",") if x.strip()
    }
    MODE      = os.environ.get("STOAT_BOT_MODE", "WAKEWORD").upper()
    WAKE_WORD = os.environ.get("STOAT_WAKE_WORD", "hey claw").lower()

    class SystemCStoat(stoat.Client):
        async def on_ready(self, event):
            print(f"✓ [stoat] logged in as {self.me}", flush=True)
            print(f"  mode:      {MODE}", flush=True)
            if MODE == "WAKEWORD":
                print(f'  wake word: "{WAKE_WORD}"')
            if ALLOWED:
                print(f"  channels:  {', '.join(ALLOWED)}")
            else:
                print("  WARNING: no STOAT_ALLOWED_CHANNEL_IDS set — responding everywhere")

        async def on_message_create(self, event):
            message = event.message

            # Ignore ourselves and other bots (prevents reply loops)
            if self.me is None or message.author_id == self.me.id:
                return
            if getattr(getattr(message, "author", None), "bot", None):
                return

            ch_id = str(message.channel.id)
            if ALLOWED and ch_id not in ALLOWED:
                return

            content = message.content or ""
            if not content and not getattr(message, "attachments", None):
                return

            # !command shortcut — bypasses the LLM, runs shell directly
            if content.startswith("!") and len(content) > 1:
                import tools
                command = content[1:].strip()
                channel = StoatChannel(message.channel, self)
                if tools.is_destructive(command):
                    if not await channel.request_approval(command):
                        return
                result = await asyncio.to_thread(tools.run_shell, command)
                stdout    = (result.get("stdout") or "").strip() or "(no output)"
                exit_code = result.get("exit_code", "?")
                if len(stdout) > 1800:
                    stdout = stdout[-1800:]
                await message.channel.send(f"```\n{stdout}\n```\nexit: `{exit_code}`")
                return

            # /reset
            if content.strip().lower() == "/reset":
                # Same clear as the reset_session tool and the control panel's Sessions tab
                sessions.reset(sessions.make_key("stoat", ch_id))
                await message.channel.send("🧹 Context cleared. Long-term memory untouched.")
                return

            # Wake word / mention / everything check
            me_mention = f"<@{self.me.id}>"
            mentioned  = me_mention in content
            c_lower    = content.lower().strip()

            if MODE == "EVERYTHING":
                should = True
            elif MODE == "MENTION":
                should = mentioned
            elif MODE == "WAKEWORD":
                should = mentioned or c_lower.startswith(WAKE_WORD)
            else:
                should = False
            if not should:
                return

            # Clean the prompt
            prompt = content.replace(me_mention, "").strip()
            if prompt.lower().startswith(WAKE_WORD):
                prompt = prompt[len(WAKE_WORD):].strip()
            prompt = prompt or content

            from chat_platform import sender_tag
            _a = getattr(message, "author", None)
            prompt = (
                f"{sender_tag(getattr(_a, 'display_name', None) or getattr(_a, 'name', None), getattr(_a, 'name', None), getattr(message, 'author_id', None))} "
                f"{prompt}"
            )

            saved = await _download_attachments(message)
            if saved:
                prompt += "\n\n[Attached files saved to workspace: " + ", ".join(saved) + "]"

            session = sessions.begin(sessions.make_key("stoat", ch_id))

            channel = StoatChannel(message.channel, self)
            try:
                history = await agent.run_turn(
                    channel, prompt, session.history,
                    author={"id": getattr(message, "author_id", None), "name": getattr(_a, "name", None)},
                )
            except Exception as e:
                print(f"[stoat] agent error: {type(e).__name__}: {e}", flush=True)
                await message.channel.send(f"⚠️ Error: `{type(e).__name__}: {e}`"[:1900])
                return

            # Same safe-trim as the Discord bot (never start on an orphaned tool result)
            history = history[-60:]
            while history and history[0]["role"] != "user":
                history.pop(0)
            # Saves it, unless the session was reset while this turn was running
            # (reset_session tool / control panel): then it's dropped.
            session.commit(history, label=getattr(message.channel, "name", None))

    print("[stoat] connecting...", flush=True)
    client = SystemCStoat(token=TOKEN, bot=True)
    try:
        await client.start()
    except Exception as e:
        print(f"[stoat] connection failed: {type(e).__name__}: {e}", flush=True)
        print("[stoat] check STOAT_BOT_TOKEN (stoat.chat -> Settings -> My Bots)", flush=True)
        raise


def run_stoat_bot(agent):
    """Blocking helper (own event loop)."""
    asyncio.run(start_stoat_bot(agent))


if __name__ == "__main__":
    # Standalone mode — used by the control panel's "Stoat bot" Start button.
    _load_dotenv()
    sys.path.insert(0, str(HERE))
    from agent import Agent
    run_stoat_bot(Agent())
