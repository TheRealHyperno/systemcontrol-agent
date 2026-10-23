"""
Discord bot entry point.

Bot modes (set BOT_MODE in .env):
  WAKEWORD  — responds only when message starts with the wake word, or bot is @mentioned
  MENTION   — responds only when @mentioned
  EVERYTHING — responds to every message in allowed channels

The !command shortcut bypasses the LLM entirely and runs shell directly.
"""

import os
import sys
import discord
import sessions
from agent import Agent


import version
print(version.banner())
def _load_dotenv(path: str = ".env"):
    if not os.path.exists(path):
        return False
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
    return True


def _check_config():
    """Launch the setup GUI if config is missing or incomplete, then exit so
    the user re-runs bot.py after filling it out."""
    found = _load_dotenv()
    if not found or not os.environ.get("DISCORD_BOT_TOKEN"):
        print("No config found — launching setup wizard...")
        import subprocess
        subprocess.run([sys.executable, "setup_gui.py"])
        print("\nSetup complete. Run 'python bot.py' again to start the bot.")
        sys.exit(0)


_check_config()

TOKEN              = os.environ["DISCORD_BOT_TOKEN"]
ALLOWED_CHANNEL_IDS = {
    int(x) for x in os.environ.get("ALLOWED_CHANNEL_IDS", "").split(",") if x.strip()
}
BOT_MODE  = os.environ.get("BOT_MODE", "WAKEWORD").upper()
WAKE_WORD = os.environ.get("WAKE_WORD", "hey claw").lower()

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
agent  = Agent()


def _channel_label(message: discord.Message) -> str:
    """Human-readable name for the control panel's Sessions tab."""
    try:
        name = getattr(message.channel, "name", None)
        if not name:
            return f"DM with {message.author.display_name}"
        guild = getattr(message.guild, "name", None)
        return f"#{name} ({guild})" if guild else f"#{name}"
    except Exception:
        return ""


def _should_respond(message: discord.Message) -> bool:
    """Return True if this message should trigger the agent."""
    if message.author == client.user:
        return False
    if ALLOWED_CHANNEL_IDS and message.channel.id not in ALLOWED_CHANNEL_IDS:
        return False
    if not message.content:
        return False
    # !command shortcut is handled separately
    if message.content.startswith("!"):
        return False

    mentioned = client.user in message.mentions
    content_lower = message.content.lower()

    if BOT_MODE == "EVERYTHING":
        return True
    if BOT_MODE == "MENTION":
        return mentioned
    if BOT_MODE == "WAKEWORD":
        return mentioned or content_lower.startswith(WAKE_WORD)
    return False


def _is_reset_command(message: discord.Message) -> bool:
    """True if the message is (optionally @mentioning the bot, then) just
    '/reset' — a control command, not something routed to the LLM."""
    content = message.content
    mention_str = f"<@{client.user.id}>"
    mention_str_bang = f"<@!{client.user.id}>"
    content = content.replace(mention_str, "").replace(mention_str_bang, "").strip()
    return content.lower() == "/reset"


def _clean_input(message: discord.Message) -> str:
    """Strip the wake word or @mention from the front of the message."""
    content = message.content

    # Strip mention
    mention_str = f"<@{client.user.id}>"
    mention_str_bang = f"<@!{client.user.id}>"
    content = content.replace(mention_str, "").replace(mention_str_bang, "").strip()

    # Strip wake word (case-insensitive)
    if content.lower().startswith(WAKE_WORD):
        content = content[len(WAKE_WORD):].strip()

    return content or message.content  # fallback to original if we stripped everything


@client.event
async def on_ready():
    print(f"✓ logged in as {client.user}")
    print(f"  mode:      {BOT_MODE}")
    if BOT_MODE == "WAKEWORD":
        print(f"  wake word: \"{WAKE_WORD}\"")
    if ALLOWED_CHANNEL_IDS:
        print(f"  channels:  {', '.join(str(c) for c in ALLOWED_CHANNEL_IDS)}")
    else:
        print("  WARNING: no ALLOWED_CHANNEL_IDS set — responding everywhere")


@client.event
async def on_message(message: discord.Message):
    # ── !command shortcut ──────────────────────────────────────────────────
    if (
        message.author != client.user
        and message.content.startswith("!")
        and len(message.content) > 1
        and (not ALLOWED_CHANNEL_IDS or message.channel.id in ALLOWED_CHANNEL_IDS)
    ):
        command = message.content[1:].strip()
        async with message.channel.typing():
            import tools
            if tools.is_destructive(command):
                approved = await tools.request_approval(message.channel, command)
                if not approved:
                    return
            result = tools.run_shell(command)
            stdout    = result.get("stdout", "").strip()
            exit_code = result.get("exit_code", "?")
            out = stdout if stdout else "(no output)"
            if len(out) > 1900:
                out = out[-1900:]
            await message.channel.send(f"```\n{out}\n```\nexit: `{exit_code}`")
        return

    # ── /reset — clear this channel's conversation context ─────────────────
    # Only wipes the per-channel chat history (.history/*.json, plus the cached
    # copy via sessions.reset). The agent's reset_session tool and the control
    # panel's Sessions tab do the same thing.
    # Long-term memory (.memory/MEMORY.md and .memory/daily/*.md) is untouched
    # and still gets loaded back in as system-prompt context on the next turn
    # — this clears "what did we just say to each other", not "what does the
    # bot remember about me".
    if (
        message.author != client.user
        and _is_reset_command(message)
        and (not ALLOWED_CHANNEL_IDS or message.channel.id in ALLOWED_CHANNEL_IDS)
    ):
        sessions.reset(sessions.make_key("discord", message.channel.id))
        await message.channel.send(
            "🧹 Context cleared for this channel. Long-term memory is untouched."
        )
        return

    # ── Agent turn ─────────────────────────────────────────────────────────
    if not _should_respond(message):
        return

    from chat_platform import sender_tag
    prompt = _clean_input(message)
    prompt = f"{sender_tag(message.author.display_name, message.author.name, message.author.id)} {prompt}"

    async with message.channel.typing():
        if message.attachments:
            import tools
            saved = await tools.download_attachments(message)
            if saved:
                prompt += "\n\n[Attached files saved to workspace: " + ", ".join(saved) + "]"

        session = sessions.begin(sessions.make_key("discord", message.channel.id))
        try:
            history = await agent.run_turn(
                message.channel, prompt, session.history,
                author={"id": message.author.id, "name": message.author.display_name},
            )
        except Exception as e:
            print(f"[bot] agent error: {type(e).__name__}: {e}", flush=True)
            await message.channel.send(f"⚠️ {type(e).__name__}: {e}"[:1900])
            return
        history = history[-60:]
        # A flat slice can land inside a tool-call group, leaving a "tool"
        # message whose triggering "assistant" tool_use entry got trimmed
        # off the front — providers (Anthropic in particular) reject that
        # as an orphaned tool_result. "user" messages are always safe turn
        # boundaries (nothing after them depends on anything before them),
        # so walk forward to the first one.
        while history and history[0]["role"] != "user":
            history.pop(0)
        # Saves it, unless the session was reset while this turn was running
        # (e.g. by the reset_session tool or the control panel): then it's dropped.
        session.commit(history, label=_channel_label(message))


if __name__ == "__main__":
    client.run(TOKEN)
