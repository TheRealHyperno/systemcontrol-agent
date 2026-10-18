# Integrations: how they work

How an integration is loaded, how the agent calls it, how its settings reach the control panel
and the HTTP API, and how to write a good one. For a five-minute start see
[`integrations/README.md`](integrations/README.md); for the HTTP endpoints see [`API.md`](API.md).

- [1. The idea](#1-the-idea)
- [2. The big picture](#2-the-big-picture)
- [3. Loading and enabling](#3-loading-and-enabling)
- [4. What happens on every message](#4-what-happens-on-every-message)
- [5. The module contract](#5-the-module-contract)
- [6. The `ctx` object](#6-the-ctx-object)
- [7. Approvals](#7-approvals)
- [8. Writing `handle()` well](#8-writing-handle-well)
- [9. Settings, the control panel and the API](#9-settings-the-control-panel-and-the-api)
- [10. Worked example](#10-worked-example)
- [11. Limits and gotchas](#11-limits-and-gotchas)
- [12. Troubleshooting](#12-troubleshooting)
- [13. File map](#13-file-map)

---

## 1. The idea

An integration is **one Python file in `integrations/`** that gives the agent extra tools. The
agent (the language model) sees them next to its built-in tools (shell, files, web search…), decides
when to call them, and the bot runs your `handle()` function and feeds the result back to the model.

Three things are deliberately separate:

| Concern | Where it lives | Needs the bot running? |
|---|---|---|
| **Tools** the model can call | `TOOLS` + `handle()` in your file | yes |
| **On/off switch** | `INTEGRATIONS=` in `.env` | no (applies at next bot start) |
| **Settings menu** | `PANEL` in your file, drawn by the control panel / API | no |

Nothing is enabled by default. Integrations can be powerful (`discord_admin` can ban people), so you
opt in per integration.

## 2. The big picture

Three separate processes share one folder and one `.env` file:

```
 control_panel.py (Tk)          webui_server.py (FastAPI)            main.py → bot.py / stoat_bot.py
 ─────────────────────          ─────────────────────────            ───────────────────────────────
 Integrations tab               /api/v1/...  (bearer token)          Agent.run_turn() per message
   • lists every file           • list / enable / disable            integrations.build() → tools
   • on/off checkbox            • GET/PUT .../settings               agent calls handle()
   • generated settings pages   • integration's own routes           reads settings from os.environ
          │                              │                                     ▲
          └──────────── integration_settings.py ───────────┐                    │
                         (validate + read/write .env)      ▼                    │
                                                         .env  ── loaded once at startup ──┘
```

- The **bot** is the only process that ever *runs* integration tools.
- The **control panel** and **web UI server** only *edit configuration*. They read your file as text
  (`ast`) to find `PANEL`, `NAME` and `DESCRIPTION`, so an integration shows up in the panel even if
  its libraries aren't installed. Nothing in your file runs when it is listed.
- Changes made in the panel or API are written to `.env` and take effect **the next time the bot
  starts**.
- The local web chat UI (`webui.html`, `webui_server.py`'s `/tools/*` routes) does **not** use
  integrations. They are available in the Discord and Stoat bots.

## 3. Loading and enabling

**Which files count.** Every `*.py` in `integrations/` whose name does *not* start with `_`.
Underscore files are helpers (`_discord_admin_settings.py`) or documentation (`_example.py`).

**Enabling.** `INTEGRATIONS` in `.env` is a comma- or semicolon-separated list of file names, or
`all`:

```
INTEGRATIONS=discord_admin,dice_roller
```

The control panel's checkbox rewrites this line (`integration_settings.set_enabled`). Names with no
matching file are kept, so nobody's config is lost, and switching one off while the value is `all`
expands `all` into an explicit list.

**Import.** At startup the first call to `integrations.discover()` imports every integration module
once and caches it. If a module raises on import (missing library, syntax error) it is recorded,
logged with a traceback, and skipped. **One broken integration never stops the bot or the others.**
`main.py` then prints a summary:

```
[integrations] enabled: discord_admin (25 tools)
[integrations] WARNING: 'typo' is in INTEGRATIONS but no integrations/typo.py exists
[integrations] failed to load my_thing: ModuleNotFoundError: ...
```

**.env into the environment.** `main.py` copies `.env` into `os.environ` with `setdefault`, so a
variable already set in the real environment **wins** over `.env`. Integrations read their settings
from `os.environ` (via `get_setting`), so a value edited in `.env` is only seen after a restart.

## 4. What happens on every message

When someone messages the bot, the platform layer (`bot.py` for Discord, `stoat_bot.py` for Stoat)
calls `Agent.run_turn(channel, text, history, author={"id", "name"})`. The author comes from the
platform itself, never from the message text.

```
run_turn
 │
 ├─ 1. Build ctx            integrations.Context(platform, author_id, author_name, confirm, send, channel)
 │
 ├─ 2. Build the toolset    integrations.build(ctx, reserved=<built-in tool names>)
 │      for each name in INTEGRATIONS that was discovered:
 │        • available(ctx) → False?   skip silently (not shown to the model this turn)
 │        • for each schema in TOOLS:
 │            malformed?              skip + log
 │            name already taken?     skip + log   (built-ins and earlier integrations win)
 │            otherwise               add to the turn's tool list
 │        • if it added ≥1 tool and has PROMPT: add "## Integration: <NAME>\n<PROMPT>"
 │
 ├─ 3. Build the system prompt   SYSTEM_PROMPT  +  integration prompts  +  memory
 │
 └─ 4. Loop (at most MAX_TURNS = 20 model calls)
        model.step(history, built_in_schemas + integration_schemas)
          ├─ no tool calls → send the text to the chat, done
          └─ tool calls → for each one, in order:
                 built-in tool?          run it
                 toolset.owns(name)?     toolset.call(name, args) → your handle(ctx, name, args)
                 otherwise               {"error": "unknown tool: ..."}
               any exception            → {"error": str(e)}
               result → JSON string → appended to history as a tool message → loop again
```

Consequences worth knowing:

- **The toolset is rebuilt every turn.** `available(ctx)` runs for every message, so it can depend on
  who is talking and where. It is how `discord_admin` shows its tools only to the server owner and
  listed admins, only in Discord server channels. Keep it fast and side-effect free.
- **The model chooses.** It sees your tool `name`, `description` and `input_schema` (plus your
  `PROMPT`) and decides whether to call it. Those strings are your interface to the model; write
  them like documentation for a careful but literal colleague.
- **Calls run one at a time**, in the order the model listed them, even when it asks for several at
  once.
- **Provider translation is automatic.** Anthropic gets the schemas as written; Ollama/OpenAI-style
  providers get each one wrapped as a function whose `parameters` is your `input_schema`. Write
  plain JSON Schema and it works on both.
- **Results become conversation history.** Each result is serialised (`dict` → JSON, anything else →
  `str`), stored in the channel's history, and re-sent to the model on later turns. Large results
  cost tokens every turn (see [8](#8-writing-handle-well)).
- **Subagents don't get integration tools.** `spawn_subagent(s)` workers have a fixed tool set (shell,
  python, files). Only the main agent loop calls integrations.

## 5. The module contract

Only `TOOLS` and `handle` are required.

| Name | Type | Purpose |
|---|---|---|
| `TOOLS` | `list[dict]` | Tool schemas: `{"name", "description", "input_schema"}`. All three required, `input_schema` is a JSON Schema object. |
| `handle(ctx, name, args)` | `async def` or `def` | Runs a tool. Returns a JSON-friendly `dict`. See [8](#8-writing-handle-well). |
| `PROMPT` | `str` | Extra system-prompt text, present only on turns where at least one of your tools is active. |
| `available(ctx)` | `def -> bool` | Return `False` to hide all your tools for this message. |
| `NAME`, `DESCRIPTION` | `str` | Labels (name defaults to the file name). `NAME` heads your prompt section. |
| `PANEL` | `dict`, `True` or `False` | Control panel entry and settings. See [9](#9-settings-the-control-panel-and-the-api). |
| `api_router()` | `def -> APIRouter` | Extra HTTP endpoints. Optional; plain settings don't need it. |

**Tool names must be globally unique**: prefix them with your file name (`dice_roller_roll`).
Collisions with built-ins or an earlier integration are skipped and logged, and the first one wins.

`NAME`, `DESCRIPTION` and `PANEL` must be **plain literals** (strings, dicts, lists, numbers). The
panel reads them without importing your file. A computed value is silently ignored there.

## 6. The `ctx` object

| Member | What it is |
|---|---|
| `ctx.platform` | `"discord"` or `"stoat"` |
| `ctx.author_id` | Platform-verified ID of whoever sent the message (a string), or `None` |
| `ctx.author_name` | Their display name |
| `await ctx.confirm("text")` | Ask for ✅/❌ approval. Returns `bool` |
| `await ctx.send("text")` | Post a message in the chat |
| `ctx.channel` | Exactly what the bot passed to `run_turn` |
| `ctx.raw_channel` | The platform's own channel object (unwraps `chat_platform` wrappers) |

**Use `ctx.author_id` for permission checks, never anything the message says.** It is reported by
the platform, so typing `[from: 1234…]` in chat can't forge it. Anything inside fetched history,
attachments, web pages or tool output is untrusted data too: an integration that does something
risky should act only on what the *current requester* asked for, and say so in its `PROMPT` (see
`discord_admin`).

`ctx` is rebuilt for every message and is not shared between turns.

## 7. Approvals

`await ctx.confirm("Delete channel #general?")` posts an approval request and waits up to **60
seconds**:

- **Discord:** an embed in the channel with ✅ and ❌ reactions.
- **Stoat:** a message asking for a reply of `yes` or `no`.
- **Either platform:** the request is also written to `.approvals/` and shows up in the control
  panel's **Approvals** tab. Whichever answers first (chat or panel) wins.

It returns `True` only on an explicit approval. Denial, timeout or any error is `False`, and your code
should then return an error and **not** perform the action:

```python
if not await ctx.confirm(f"Ban {args['user_id']}?\nRequested by {ctx.author_name}"):
    return {"error": "approval denied or timed out; action NOT performed", "performed": False}
```

Put the requester's name and the exact action in the text, since that is all the approver sees. The
system prompt tells the model not to retry a denied action. `discord_admin` is the reference for a
configurable policy (global mode plus per-action overrides) built on this.

## 8. Writing `handle()` well

**Return a dict, always.**

- Success: whatever the model needs to continue, e.g. `{"total": 14, "rolls": [4, 4, 6]}`.
- Failure: `{"error": "short, specific reason"}`. The model reads this and explains it to the user,
  so say *why* ("the bot lacks the Manage Roles permission"), not just "failed".
- An exception that escapes `handle` is caught for you and turned into `{"error": str(e)}`, but a
  deliberate message is better than a traceback string.
- Never report success you didn't verify. The system prompt and your `PROMPT` should tell the model
  to trust only your result.

**Don't block the event loop.** `handle` is called on the bot's event loop. An `async def` is
awaited; a plain `def` is called *directly*, so a slow plain function (network request, `sleep`,
big file read) freezes the bot, including the Discord heartbeat, until it returns. For blocking work:

```python
async def handle(ctx, name, args):
    data = await asyncio.to_thread(slow_blocking_call, args["url"])   # runs in a worker thread
    return {"data": data}
```

**Keep results small.** They're stored in history and re-sent every turn. Truncate lists and long
text, and tell the model how to get more (`{"items": [...20 items], "truncated": True, "total": 480}`).
Nothing truncates them for you.

**Validate the arguments.** The model fills `args` from your schema but can still send the wrong
type, a missing key or an out-of-range value. `args.get(...)` and check before use.

**Make the schema do work.** Good `description` strings and `required` lists reduce bad calls more
than any validation. Say what each argument means and what units, formats and limits apply.

**Write a short `PROMPT`** only for what the schema can't say: when to use the tools, order of
operations ("look up the ID first"), safety rules, what never to claim. It is added to the system
prompt on every turn the tools are active, so every line costs tokens.

**Gate with `available(ctx)`** for who/where, and **re-check inside `handle`** for anything
sensitive (defence in depth): the model can only call tools it was shown, but your code is the last
line of defence.

## 9. Settings, the control panel and the API

### Declaring settings

Add `fields` to `PANEL` and you get a working settings page with no UI code:

```python
PANEL = {
    "icon": "🎲",
    "fields": [
        {"key": "DICE_ROLLER_MAX_DICE", "label": "Most dice per roll", "type": "number",
         "min": 1, "max": 100, "default": 10},
        {"key": "DICE_ROLLER_SHOW_ROLLS", "type": "bool", "default": True},
    ],
}
```

For several pages use `"submenu": [{"id", "title", "description", "fields": [...]}, ...]`; entries
with `fields` get generated pages. A field takes:

| Key | Meaning |
|---|---|
| `key` | The `.env` variable. **Must start with your file name in capitals** (`dice_roller.py` → `DICE_ROLLER_`), which keeps integrations from clashing with each other or with core settings like `INTEGRATIONS` and `SYSTEMC_API_TOKEN` |
| `type` | `text` (default), `secret`, `number`, `bool`, `choice`, `list` |
| `label`, `description` | Shown in the UI (label defaults to the key) |
| `default` | Used when nothing is saved |
| `choices` | Required for `choice` |
| `min`, `max` | Bounds for `number` |
| `pattern` | Regex a `text` value, or each `list` item, must match |
| `required` | Adds a `*` in the UI; it never blocks saving |

How each type is stored in `.env` and read back by `get_setting`:

| Type | Saved as | `get_setting` returns |
|---|---|---|
| `text` / `secret` / `choice` | the string | `str` |
| `number` | `7` or `0.5` | `int` or `float` |
| `bool` | `true` / `false` | `bool` (`1 true yes on` count as true) |
| `list` | `a,b,c` (duplicates removed, no commas inside items) | `list[str]` |

An unset or unparseable value gives the field's `default`.

### Reading settings in your integration

```python
from integrations import get_setting

limit = get_setting("DICE_ROLLER_MAX_DICE")          # int, or the default (10)
extra = get_setting("SOMETHING_UNDECLARED", "x")     # undeclared: raw env string, or your fallback
```

`get_setting` reads `os.environ`, so it reflects `.env` as of the bot's last start. It's cheap enough
to call in `available()` and `handle()` on every turn.

### Validation

Every write goes through `integration_settings.set_settings`, shared by the control panel and the
API, so they can't disagree. It checks type, range, choices and pattern, rejects keys the integration
didn't declare, and is **all or nothing**: if any value is invalid, nothing is saved. A value of
`None` (or empty) removes the key so the default applies. Fields with a bad key or type are skipped
and the reason appears under the integration in the panel (and in `problems` in the API).

### Secrets

`secret` fields are masked in the panel (blank means "leave as is", with a clear checkbox) and
**never returned by the API**, which only says `{"set": true|false}`. They are stored in plain text
in `.env`, like every other secret in this app, so protect that file.

### The control panel

Tab **5. Integrations** lists every integration file (`PANEL = False` hides one). Each card has the
on/off checkbox and, when settings are declared, a **Settings** section. **Rescan** picks up a newly
dropped file without restarting the panel. A hand-written page registered in
`ControlPanel.INTEGRATION_PAGES` (as `discord_admin` has for Admins and Approvals) takes precedence
over a generated one, which is the escape hatch for UI the field types can't express (tables,
pickers, per-item toggles).

### The HTTP API

`webui_server.py` mounts `integrations_api.py` at `/api/v1`, behind a bearer token
(`SYSTEMC_API_TOKEN`; with none set the API refuses everything with 503). Beyond the on/off
endpoints, any integration with `fields` gets `GET`/`PUT /integrations/<name>/settings` for free.
Integrations needing more define `api_router()` (keep FastAPI imports *inside* it so the bot never
imports FastAPI). Full reference: [`API.md`](API.md).

## 10. Worked example

A complete integration with a tool, a prompt, per-user gating and a settings page. Save it as
`integrations/dice_roller.py`, press **Rescan** in the panel, tick it, set your options, restart
the bot.

```python
import random

from integrations import get_setting

NAME = "dice_roller"
DESCRIPTION = "Rolls dice for the agent."

PANEL = {
    "icon": "🎲",
    "fields": [
        {"key": "DICE_ROLLER_MAX_DICE", "label": "Most dice per roll", "type": "number",
         "min": 1, "max": 100, "default": 10},
        {"key": "DICE_ROLLER_SHOW_ROLLS", "label": "Report each die", "type": "bool", "default": True},
        {"key": "DICE_ROLLER_ALLOWED_USER_IDS", "label": "Only these users", "type": "list",
         "pattern": r"^\d{15,25}$",
         "description": "Discord/Stoat user IDs. Leave empty to allow everyone."},
    ],
}

PROMPT = "Use dice_roller_roll when someone asks for a dice roll. Never invent roll results."

TOOLS = [{
    "name": "dice_roller_roll",
    "description": "Roll some dice and return the total.",
    "input_schema": {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "description": "How many dice (default 1)"},
            "sides": {"type": "integer", "description": "Sides per die (default 6)"},
        },
    },
}]


def available(ctx) -> bool:                       # who sees the tool, decided per message
    allowed = get_setting("DICE_ROLLER_ALLOWED_USER_IDS")
    return not allowed or ctx.author_id in allowed


async def handle(ctx, name, args):
    if name != "dice_roller_roll":
        return {"error": f"unknown tool: {name}"}
    count, sides = args.get("count", 1), args.get("sides", 6)
    if not (isinstance(count, int) and isinstance(sides, int) and sides >= 2 and count >= 1):
        return {"error": "count must be >= 1 and sides >= 2 (both whole numbers)"}
    limit = get_setting("DICE_ROLLER_MAX_DICE")
    if count > limit:
        return {"error": f"at most {limit} dice per roll"}
    rolls = [random.randint(1, sides) for _ in range(count)]
    out = {"total": sum(rolls)}
    if get_setting("DICE_ROLLER_SHOW_ROLLS"):
        out["rolls"] = rolls
    return out
```

What the model sees on a turn where the tool is available:

```
system prompt = <SYSTEM_PROMPT>

                ## Integration: dice_roller
                Use dice_roller_roll when someone asks for a dice roll. Never invent roll results.

                <memory>
tools         = <built-in tools> + dice_roller_roll
```

and the exchange when someone says "roll 3d6":

```
model  → tool call  dice_roller_roll {"count": 3, "sides": 6}
bot    → handle(ctx, "dice_roller_roll", {"count": 3, "sides": 6})
       ← {"total": 14, "rolls": [4, 4, 6]}          (stored in history as a tool message)
model  → "You rolled 4, 4 and 6 for a total of 14."   (sent to the chat)
```

## 11. Limits and gotchas

- **Restart to apply.** Enabling, disabling and every setting are read once at bot start. The panel
  and API say so (`restart_required`).
- **Real environment variables beat `.env`** (`setdefault`). If a setting "won't change", check the
  environment the bot is launched from.
- **`.env` is rewritten whole on each save**, atomically (temp file, then swap). The write keeps only
  `KEY=value` lines: **comments and blank lines in `.env` are not preserved**, and empty values are
  dropped. Two programs saving at the same instant (say the panel and an API call) can overwrite each
  other, since each reads, changes, then writes the whole file.
- **API routes from `api_router()` are mounted when the web UI server starts.** A newly added
  integration's custom routes need a web UI restart. The generic `/settings` routes see new files
  immediately.
- **Panel metadata is read statically.** `PANEL`, `NAME` and `DESCRIPTION` must be literals; code
  that builds them at import time won't be seen by the panel.
- **Tool results are not truncated** and persist in the channel's history until it is reset.
- **A turn is capped at 20 model calls.** A tool that needs many round trips can hit
  "max tool-call limit"; prefer tools that do more per call.
- **Plain `def handle` blocks the bot** while it runs (see [8](#8-writing-handle-well)).
- **Not used by**: the local web chat UI and subagents.
- **Integrations run with the bot's full privileges.** There is no sandbox. Only install integration
  files you trust, and use `ctx.confirm` for anything destructive.

## 12. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Not in the Integrations tab | File name starts with `_`, isn't in `integrations/`, has `PANEL = False`, or has a syntax error (the panel skips unreadable files). Press **Rescan** |
| `[integrations] failed to load X` in the log | Import error. The traceback below it says which library is missing; install it on the Dependencies tab |
| `WARNING: 'x' is in INTEGRATIONS but no integrations/x.py exists` | Typo in `.env`, or the file was removed |
| Ticked but the agent never uses it | Bot not restarted; `available(ctx)` returned `False` for that user or channel; or the model decided not to (improve the tool `description` / `PROMPT`) |
| `tool name 'x' already taken — skipped` | Name collides with a built-in or earlier integration; add your file-name prefix |
| `skipped malformed tool schema` | A `TOOLS` entry lacks `name`, `description` or an `input_schema` dict |
| Settings page shows ⚠ lines | A field key doesn't start with your file name in capitals, has an unknown `type`, or a `choice` has no `choices` |
| Saved a setting but behaviour didn't change | Restart the bot; check no real environment variable overrides `.env` |
| Bot freezes during a tool call | A plain `def handle` (or blocking code in an `async def`) is doing slow work; use `asyncio.to_thread` |
| `unknown tool` error | `handle` doesn't match one of the names in `TOOLS` |

Startup log lines to look for: `[integrations] enabled: <name> (<n> tools)`; build-time messages
start with `[integrations] <name>:`.

## 13. File map

| File | Role |
|---|---|
| `integrations/__init__.py` | The loader: `discover`, `build`, `Toolset`, `Context`; panel metadata (`panel_entries`), field validation (`normalize_fields`), `get_setting` |
| `integrations/<name>.py` | An integration (yours) |
| `integrations/_*.py` | Helpers and documentation, never loaded as integrations |
| `agent.py` | `run_turn`: builds `ctx` and the toolset per message, runs the model loop, dispatches tool calls |
| `integration_settings.py` | Shared settings layer: `.env` read/write, on/off, declared-settings validation. Standard library only |
| `integrations_api.py` | HTTP API (`/api/v1`), bearer-token auth, generic `/settings` routes |
| `webui_server.py` | Serves the web UI and mounts the API |
| `control_panel.py` | Tk control panel; the Integrations tab and generated settings pages |
| `approvals.py`, `tools.request_approval` | The approval queue shared by chat and the panel's Approvals tab |
| `providers.py` | Model back ends; translates tool schemas for each provider |
| `main.py` | Loads `.env`, starts the bots, prints the integrations summary |
