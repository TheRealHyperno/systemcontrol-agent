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

To start the bot(s) at login/boot without this script running, hand them to systemd:
  python main.py --install-service [--profile <id>] [--linger]
  python main.py --uninstall-service [--profile <id>]
"""

from __future__ import annotations

import os
import sys
import asyncio
from pathlib import Path


def _handle_service_flags(argv: list[str]) -> None:
    """--install-service / --uninstall-service: hand off to systemd_service.py and exit."""
    if not any(a in argv for a in ("--install-service", "--uninstall-service")):
        return
    import subprocess
    action = "install" if "--install-service" in argv else "uninstall"
    script = Path(__file__).parent / "systemd_service.py"
    args = [os.environ.get("SYSTEMC_PYTHON") or sys.executable or "python3", str(script), action]
    if "--profile" in argv:
        try:
            args += ["--profile", argv[argv.index("--profile") + 1]]
        except ValueError:
            pass
    if "--linger" in argv and action == "install":
        args.append("--linger")
    raise SystemExit(subprocess.call(args))

# Keys a profile owns outright: when running under a profile these are never taken
# from .env, so a leftover token there can't start a platform the profile turned off.
_PROFILE_OWNED = {
    "DISCORD_BOT_TOKEN", "ALLOWED_CHANNEL_IDS", "STOAT_BOT_TOKEN", "STOAT_ALLOWED_CHANNEL_IDS",
    "OLLAMA_API_KEY", "OLLAMA_API_KEYS", "ANTHROPIC_API_KEY",
}


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _load_dotenv(skip: set | None = None):
    p = Path(__file__).parent / ".env"
    if not p.exists():
        return False
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if skip and k in skip:
            continue
        os.environ.setdefault(k, _unquote(v))
    return True


def _parse_profile_arg() -> str | None:
    for i, arg in enumerate(sys.argv):
        if arg in ("--profile", "-p") and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return os.environ.get("SYSTEMC_PROFILE_ID")


def _apply_profile(prof) -> None:
    env_dict = prof.to_env_dict()
    os.environ.update(env_dict)
    # Integration settings are per profile: this profile's overrides (INTEGRATIONS, DISCORD_ADMIN_USER_IDS, ...)
    # win over .env, including an override to "" (= back to that setting's default).
    overrides = {}
    try:
        import integration_settings
        overrides = integration_settings.load_overrides(prof.id)
    except Exception as e:
        print(f"⚠️ [profile] Could not read integration settings for '{prof.id}': {e}")
    os.environ.update(overrides)
    # Everything else in .env (the API token, settings this profile didn't override, ...)
    # still applies, except what the profile owns.
    _load_dotenv(skip=_PROFILE_OWNED | set(env_dict) | set(overrides))


def _load_config():
    profile_id = _parse_profile_arg()
    if profile_id:
        try:
            import profiles
            prof = profiles.manager.get_profile(profile_id)
            if prof:
                _apply_profile(prof)
                print(f"✨ [profile] Loaded profile: {prof.name} ({prof.id}) | Model: {prof.agent_model}")
                return True
            print(f"⚠️ [profile] Specified profile '{profile_id}' not found, falling back to .env")
        except Exception as e:
            print(f"⚠️ [profile] Could not load profile '{profile_id}': {e}")
        if _parse_profile_arg():
            # Started for a specific profile (by the control panel / API): never fall back to
            # some other config or pop up the setup wizard.
            print("Profile could not be loaded; exiting.")
            sys.exit(1)

    # Standard .env
    return _load_dotenv()


def _check_config():
    found = _load_config()
    has_discord = bool(os.environ.get("DISCORD_BOT_TOKEN"))
    has_stoat   = bool(os.environ.get("STOAT_BOT_TOKEN"))

    if not found or (not has_discord and not has_stoat):
        if _parse_profile_arg():
            print("This profile has no Discord or Stoat token enabled; nothing to start.")
            sys.exit(1)

        print("No config found — checking for profiles or launching setup wizard...")
        try:
            import profiles
            if profiles.manager.list_profiles():
                active = profiles.manager.get_active_profile()
                if active and (active.discord_token or active.stoat_token):
                    _apply_profile(active)
                    print(f"✓ Using active profile: {active.name} ({active.id})")
                    return
        except Exception:
            pass

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
    _handle_service_flags(sys.argv[1:])
    asyncio.run(main())
