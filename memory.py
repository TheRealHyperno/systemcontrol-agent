"""
Lightweight file-based memory, modeled on OpenClaw's markdown-first approach:
plain files are the source of truth, no vector DB, everything human-readable
and git-friendly.

Two layers, stored in .memory/ next to the scripts (NOT inside the agent's
shell WORKDIR — this is the bot's own memory, it shouldn't be something the
agent's shell can `rm -rf` by accident):

  .memory/daily/YYYY-MM-DD.md  — append-only daily log. A short entry gets
                                  written after every turn automatically.
                                  Today's + yesterday's file are loaded back
                                  in as context on each turn, same as
                                  OpenClaw's "read today + yesterday" rule.

  .memory/MEMORY.md            — curated long-term facts. Only written when
                                  the model explicitly calls the `remember`
                                  tool (e.g. the user says "remember that I
                                  prefer X"), so it stays small and useful
                                  instead of filling up with noise.

This is intentionally the simple 90% version — no embeddings, no semantic
search, no promotion scoring. Just "read the last couple of files, inject
into the prompt." That's enough for a personal Discord bot; the vector
search / dreaming-sweep machinery is overkill here.

Memory is shared across all channels (single-owner personal bot), not
per-channel. Conversation history (who-said-what, reset by /reset, the
reset_session tool or the control panel's Sessions tab) is a separate,
per-channel thing handled by sessions.py's .history/ store.
"""

import os
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _get_memory_dir() -> str:
    profile_id = os.environ.get("SYSTEMC_PROFILE_ID", "").strip()
    if profile_id and profile_id != "default":      # "default" = the original single-bot folders
        pdir = os.path.join(BASE_DIR, ".memory", "profiles", profile_id)
        os.makedirs(os.path.join(pdir, "daily"), exist_ok=True)
        return pdir
    os.makedirs(os.path.join(BASE_DIR, ".memory", "daily"), exist_ok=True)
    return os.path.join(BASE_DIR, ".memory")


def _get_daily_dir() -> str:
    return os.path.join(_get_memory_dir(), "daily")


def _get_long_term_path() -> str:
    return os.path.join(_get_memory_dir(), "MEMORY.md")


MAX_DAILY_CHARS = 4000       # per day file, when loading into context
MAX_LONG_TERM_CHARS = 4000   # MEMORY.md, when loading into context
MAX_ENTRY_CHARS = 500        # per logged user/agent line


def _daily_path(date) -> str:
    return os.path.join(_get_daily_dir(), f"{date.isoformat()}.md")


def _read_capped(path: str, cap: int) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", errors="replace") as f:
            text = f.read()
    except Exception:
        return ""
    text = text.strip()
    if len(text) > cap:
        text = "...[truncated, older lines cut]...\n" + text[-cap:]
    return text


def load_context() -> str:
    """
    Return a markdown block combining MEMORY.md + yesterday's + today's daily
    log, meant to be appended to the system prompt before each turn. Returns
    "" if nothing has been recorded yet (so it adds nothing to the prompt).
    """
    today = datetime.now().date()
    yesterday = today - timedelta(days=1)

    long_term = _read_capped(_get_long_term_path(), MAX_LONG_TERM_CHARS)
    yesterday_log = _read_capped(_daily_path(yesterday), MAX_DAILY_CHARS)
    today_log = _read_capped(_daily_path(today), MAX_DAILY_CHARS)

    if not (long_term or yesterday_log or today_log):
        return ""

    parts = [
        "## Memory\n"
        "Notes carried over from previous sessions/days. Use them for context; "
        "don't recite them back verbatim unless asked."
    ]
    if long_term:
        parts.append(f"### Long-term notes\n{long_term}")
    if yesterday_log:
        parts.append(f"### Yesterday's log ({yesterday.isoformat()})\n{yesterday_log}")
    if today_log:
        parts.append(f"### Today's log ({today.isoformat()})\n{today_log}")
    return "\n\n".join(parts)


def _trim(text: str, limit: int = MAX_ENTRY_CHARS) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def log_turn(channel_label: str, user_text: str, assistant_text: str | None) -> None:
    """Append a short entry to today's daily log. Best-effort — never raises,
    a logging failure shouldn't break the bot's actual response."""
    try:
        ts = datetime.now().strftime("%H:%M")
        lines = [f"\n## {ts} — #{channel_label}", f"- user: {_trim(user_text)}"]
        if assistant_text:
            lines.append(f"- agent: {_trim(assistant_text)}")
        with open(_daily_path(datetime.now().date()), "a") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass


def remember(fact: str) -> dict:
    """Append a curated fact to MEMORY.md. Called via the `remember` tool."""
    fact = (fact or "").strip()
    if not fact:
        return {"error": "empty fact — nothing to remember"}
    date = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(_get_long_term_path(), "a") as f:
            f.write(f"- [{date}] {fact}\n")
        return {"remembered": fact}
    except Exception as e:
        return {"error": str(e)}


TOOL_SCHEMAS = [
    {
        "name": "remember",
        "description": (
            "Save a short, durable fact to long-term memory (MEMORY.md). Use this "
            "when the user asks you to remember something, or shares a stable "
            "preference/fact/decision that should persist across sessions and days "
            "— not for one-off details that don't matter later."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "The fact to remember, written concisely in plain text.",
                },
            },
            "required": ["fact"],
        },
    },
]
