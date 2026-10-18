"""
Main entry point. Runs Discord and/or Stoat bots simultaneously in the same
asyncio event loop, sharing a single Agent instance (and therefore the same
memory, history directory, and Ollama connection).

Which bots start is determined by which tokens are present in .env:
  - DISCORD_BOT_TOKEN set   → Discord bot starts
  - STOAT_BOT_TOKEN set     → Stoat bot starts
  - Both set                → both start simultaneously
  - Neither set             → setup wizard launches

Run with: python main.py
"""

import os
import sys
import asyncio
from pathlib import Path


def _load_dotenv():
    p = Path(__file__).parent / ".env"
    if not p.exists():
        return False
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())
    return True


def _check_config():
    found = _load_dotenv()
    has_discord = bool(os.environ.get("DISCORD_BOT_TOKEN"))
    has_stoat   = bool(os.environ.get("STOAT_BOT_TOKEN"))

    if not found or (not has_discord and not has_stoat):
        print("No config found — launching setup wizard...")
        try:
            import tkinter  # noqa
            import subprocess
            subprocess.run([sys.executable, "setup_gui.py"])
        except ImportError:
            import subprocess
            subprocess.run([sys.executable, "setup_cli.py"])
        print("\nSetup complete. Run 'python main.py' again.")
        sys.exit(0)


_check_config()

from agent import Agent
import integrations

agent = Agent()
integrations.print_startup_summary()

has_discord = bool(os.environ.get("DISCORD_BOT_TOKEN"))
has_stoat   = bool(os.environ.get("STOAT_BOT_TOKEN"))


async def run_discord():
    """Run the Discord bot inside the shared event loop."""
    import discord
    from bot import client, TOKEN
    # Patch the shared agent into the already-constructed client
    import bot as _bot
    _bot.agent = agent
    await client.start(TOKEN)


async def run_stoat():
    """Run the Stoat bot inside the shared event loop."""
    from stoat_bot import start_stoat_bot
    await start_stoat_bot(agent)


async def _guard(name, coro):
    """One frontend crashing shouldn't take the other one down."""
    try:
        await coro
    except Exception as e:
        import traceback
        print(f"[{name}] crashed: {e!r}")
        traceback.print_exc()


async def main():
    tasks = []
    if has_discord:
        print("Starting Discord bot...")
        tasks.append(asyncio.create_task(_guard("discord", run_discord())))
    if has_stoat:
        print("Starting Stoat bot...")
        tasks.append(asyncio.create_task(_guard("stoat", run_stoat())))
    if not tasks:
        print("No tokens configured.")
        sys.exit(1)
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
