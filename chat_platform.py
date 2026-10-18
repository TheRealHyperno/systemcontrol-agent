"""
Platform abstraction layer (chat_platform.py — NOT named platform.py, which would shadow Python's stdlib module).

Both Discord and Stoat bots implement the same Channel interface so
agent.py doesn't know or care which platform a message came from.
Multiple platforms can run simultaneously in the same process via asyncio.

The Channel wrapper normalises the two APIs:
  channel.send(text)       — send a plain text message
  channel.send_file(path)  — upload a file
  channel.history(limit)   — async generator of Message objects
  channel.typing()         — async context manager (no-op on Stoat)
  message.author           — str "Name#0000" or "Name" depending on platform
  message.content          — str
  message.id               — str
  message.created_at       — datetime (UTC)

Approval embeds use Discord's embed+reaction system on Discord, and a
plain-text reaction menu on Stoat (Stoat doesn't have embeds).
"""

from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import AsyncIterator, Any


# ── Normalised message ────────────────────────────────────────────────────────

@dataclass
class NormMessage:
    id: str
    author: str
    content: str
    created_at: datetime
    raw: Any = field(repr=False)


def sender_tag(display_name, username=None, user_id=None) -> str:
    """Build the "[from: ...]" prefix that tells the model who sent a message.
    Includes the stable user ID because display names can change or collide."""
    name = str(display_name or username or "unknown").replace("\n", " ").strip()
    extra = []
    if username and str(username) != name:
        extra.append(f"@{username}")
    if user_id:
        extra.append(f"id {user_id}")
    return f"[from: {name}" + (f" ({', '.join(extra)})" if extra else "") + "]"


# ── Base channel wrapper ──────────────────────────────────────────────────────

class BaseChannel:
    platform: str = "unknown"

    async def send(self, content: str) -> None:
        raise NotImplementedError

    async def send_file(self, path: str, caption: str = "") -> None:
        raise NotImplementedError

    async def history(self, limit: int = 20) -> AsyncIterator[NormMessage]:
        raise NotImplementedError
        yield  # make it a generator

    def typing(self):
        return _NullContext()

    @property
    def id(self) -> str:
        raise NotImplementedError

    @property
    def name(self) -> str:
        return str(self.id)


class _NullContext:
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass


# ── Discord wrapper ───────────────────────────────────────────────────────────

class DiscordChannel(BaseChannel):
    platform = "discord"

    def __init__(self, channel):
        self._ch = channel

    @property
    def id(self): return str(self._ch.id)
    @property
    def name(self): return getattr(self._ch, "name", str(self._ch.id))

    async def send(self, content: str) -> None:
        import discord as _discord
        chunks = [content[i:i+1900] for i in range(0, len(content), 1900)]
        for chunk in chunks:
            await self._ch.send(chunk)

    async def send_file(self, path: str, caption: str = "") -> None:
        import discord as _discord
        await self._ch.send(caption or "", file=_discord.File(path))

    async def history(self, limit: int = 20) -> AsyncIterator[NormMessage]:
        msgs = []
        async for m in self._ch.history(limit=limit):
            msgs.append(NormMessage(
                id=str(m.id),
                author=str(m.author),
                content=m.content,
                created_at=m.created_at.replace(tzinfo=timezone.utc) if m.created_at.tzinfo is None else m.created_at,
                raw=m,
            ))
        for m in reversed(msgs):
            yield m

    def typing(self):
        return self._ch.typing()

    async def request_approval(self, command: str, is_elevated: bool = False, timeout: int = 60) -> bool:
        import discord as _discord
        label = "🔴 ELEVATED (sudo)" if is_elevated else "⚠️ Approval required"
        embed = _discord.Embed(
            title=label,
            description=f"```\n{command[:1000]}\n```",
            color=_discord.Color.red() if is_elevated else _discord.Color.orange(),
        )
        embed.set_footer(text=f"React ✅ to run or ❌ to cancel ({timeout}s)")
        msg = await self._ch.send(embed=embed)
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")

        def check(reaction, user):
            return (
                not user.bot
                and str(reaction.emoji) in ("✅", "❌")
                and reaction.message.id == msg.id
            )

        client = self._ch._state._get_client()
        try:
            reaction, _ = await asyncio.wait_for(
                client.wait_for("reaction_add", check=check), timeout=timeout
            )
            approved = str(reaction.emoji) == "✅"
            await msg.edit(embed=_discord.Embed(
                title="✅ Approved" if approved else "❌ Denied",
                description=f"```\n{command[:1000]}\n```",
                color=_discord.Color.green() if approved else _discord.Color.red(),
            ))
            return approved
        except asyncio.TimeoutError:
            await msg.edit(embed=_discord.Embed(
                title="⏱️ Timed out — cancelled",
                description=f"```\n{command[:1000]}\n```",
                color=_discord.Color.greyple(),
            ))
            return False


# ── Stoat wrapper ─────────────────────────────────────────────────────────────

class StoatChannel(BaseChannel):
    """Wraps a stoat.py channel (anything with send/history/typing)."""
    platform = "stoat"

    def __init__(self, channel, client):
        self._ch = channel
        self._client = client

    @property
    def id(self): return str(self._ch.id)
    @property
    def name(self): return getattr(self._ch, "name", None) or str(self._ch.id)

    async def send(self, content: str) -> None:
        # Keep chunks comfortably under Stoat's message length limit
        for i in range(0, len(content), 1900):
            await self._ch.send(content[i:i + 1900])

    async def send_file(self, path: str, caption: str = "") -> None:
        import os
        import tools
        full = path if os.path.isabs(path) else os.path.join(tools.WORKDIR, path)
        if not os.path.isfile(full):
            await self._ch.send(f"{caption or ''}\n[file not found: {path}]".strip())
            return
        with open(full, "rb") as f:
            data = f.read()
        # stoat.py attachments accept (filename, bytes) tuples and upload them
        await self._ch.send(caption or None, attachments=[(os.path.basename(full), data)])

    async def history(self, limit: int = 20) -> AsyncIterator[NormMessage]:
        # stoat.py's channel.history() is a coroutine returning list[Message]
        try:
            msgs = await self._ch.history(limit=max(1, min(int(limit), 100)))
        except Exception:
            return
        try:
            msgs = sorted(msgs, key=lambda m: m.created_at)   # oldest -> newest
        except Exception:
            msgs = list(reversed(msgs))
        for m in msgs:
            author = getattr(m, "author", None)
            yield NormMessage(
                id=str(m.id),
                author=str(getattr(author, "name", None) or getattr(m, "author_id", "unknown")),
                content=getattr(m, "content", "") or "",
                created_at=getattr(m, "created_at", None) or datetime.now(timezone.utc),
                raw=m,
            )

    def typing(self):
        try:
            return self._ch.typing()
        except Exception:
            return _NullContext()

    async def ask_approval(self, command: str, timeout: int = 60) -> bool:
        """Post the prompt in chat and wait for a yes/no reply. Returns True/False.
        (tools.request_approval races this against the control-panel Approvals tab.)"""
        import stoat

        await self._ch.send(
            f"**⚠️ Approval required**\n```\n{command[:1000]}\n```\n"
            f"Reply `yes` to run or `no` to cancel ({timeout}s timeout), "
            f"or use the control panel."
        )
        my_id = getattr(getattr(self._client, "me", None), "id", None)

        def check(event):
            m = getattr(event, "message", event)
            return (
                str(m.channel.id) == str(self._ch.id)
                and getattr(m, "author_id", None) != my_id
                and (m.content or "").lower().strip() in ("yes", "no")
            )

        event = await self._client.wait_for(stoat.MessageCreateEvent, check=check)
        m = getattr(event, "message", event)
        return m.content.lower().strip() == "yes"

    async def request_approval(self, command: str, is_elevated: bool = False, timeout: int = 60) -> bool:
        import tools
        return await tools.request_approval(self, command, timeout)
