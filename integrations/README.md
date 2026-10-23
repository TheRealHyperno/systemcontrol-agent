# Integrations

> Full guide to how integrations load, how the agent calls them, approvals, settings and the backend: [`../INTEGRATIONS.md`](../INTEGRATIONS.md).

An integration is a single Python file in this folder that gives the agent extra tools.

## Turning one on

In `.env`:

```
INTEGRATIONS=discord_admin            # comma-separated file names, or "all"
```

Restart the bot. Nothing is enabled by default. The startup log shows what loaded:
`[integrations] enabled: discord_admin (25 tools)`.

Or use the control panel's **5. Integrations** tab: tick or untick an integration and it
rewrites `INTEGRATIONS` in `.env` for you (restart the bot to apply).

## Writing one

Copy `_example.py` to `my_thing.py` (files starting with `_` are never loaded) and edit it.
A module needs `TOOLS` (schemas, same shape as in `tools.py`) and `handle(ctx, name, args)`:

| Name | Required | Purpose |
|---|---|---|
| `TOOLS` | yes | list of `{name, description, input_schema}` |
| `handle(ctx, name, args)` | yes | `async def` or plain `def`; return a JSON-friendly dict, `{"error": "..."}` on failure |
| `PROMPT` | no | text appended to the system prompt while the tools are active |
| `available(ctx)` | no | return False to hide the tools in a given chat (wrong platform, unauthorised user…) |
| `NAME`, `DESCRIPTION` | no | labels |
| `PANEL` | no | customises its Integrations tab entry and declares its settings (see below) |
| `api_router()` | no | returns a FastAPI `APIRouter` of extra HTTP endpoints (see `../API.md`); not needed for plain settings |

`ctx` gives you:

- `ctx.platform` — `"discord"` or `"stoat"`
- `ctx.author_id`, `ctx.author_name` — who sent the message. Reported by the platform, **not** parsed
  from text, so it can't be faked by typing a `[from: …]` tag. Use this for permission checks.
- `await ctx.confirm("Delete X?")` — ✅/❌ approval prompt (also answerable in the control panel); returns bool
- `await ctx.send("text")` — post in the chat
- `ctx.raw_channel` — the platform's own channel object

## Showing up in the control panel

Every integration file is listed in the control panel's Integrations tab automatically
(the file is read, not imported, so it must be plain data). `PANEL` is optional and only
customises the entry; `PANEL = False` hides the integration from the tab.

```python
PANEL = {
    "title": "Weather",             # optional, defaults to NAME / the file name
    "icon": "⛅",                    # optional
    "fields": [                     # optional: a Settings page, built for you
        {"key": "WEATHER_API_KEY", "type": "secret", "required": True},
        {"key": "WEATHER_UNITS", "type": "choice", "choices": ["metric", "imperial"], "default": "metric"},
    ],
}
```

### Settings pages without writing UI code

List your settings under `fields` and the control panel draws the page (inputs, Save, Reset),
the HTTP API serves it (`GET`/`PUT /api/v1/integrations/<n>/settings`), and values are validated
and saved to `.env` by the same code. For several pages use a `submenu`, each entry with its own
`fields`:

```python
PANEL = {"submenu": [
    {"id": "access", "title": "Access", "description": "Who may use it", "fields": [...]},
    {"id": "limits", "title": "Limits", "fields": [...]},
]}
```

A field is a dict:

| Key | Meaning |
|---|---|
| `key` | the `.env` name. **Must start with your file name in capitals** (`weather.py` → `WEATHER_...`), which stops integrations clashing with each other or with core settings |
| `type` | `text` (default), `secret`, `number`, `bool`, `choice`, `list` |
| `label`, `description` | shown above / below the input (label defaults to the key) |
| `default` | used when nothing is saved |
| `choices` | required for `choice` |
| `min`, `max` | for `number` |
| `pattern` | regex a `text` value, or each `list` item, must match |
| `required` | adds a `*` in the UI; it never blocks saving |

Read a value in your integration with `from integrations import get_setting`, then
`get_setting("WEATHER_UNITS")`. It returns the right Python type (`bool`, `int`/`float`, `list`,
`str`) and falls back to the field's `default`. Like every setting, a change applies after the bot
restarts. `secret` fields are masked in the panel and never returned by the API (it only says
whether one is set). Fields with a bad key or type are skipped and the reason is listed under the
integration in the panel.

Something the field types can't express (a table, a picker, per-action toggles)? Write a drawer
yourself: `ControlPanel.INTEGRATION_PAGES`, keyed by file name and submenu id, takes precedence
over generated pages. A submenu entry with neither `fields` nor a drawer shows a "not wired up
yet" stub. Today discord_admin's Admins and Approvals use drawers.

## HTTP API

Declared `fields` are served over HTTP automatically. An integration can add more endpoints by defining
`api_router()`, mounted under `/api/v1/integrations/<name>/` behind a bearer token. See
[`../API.md`](../API.md) for the endpoint reference and a worked example.

Tool names must be unique across everything; prefix them with your integration's name.
A broken integration is skipped with a message in the log — it won't stop the bot.

## discord_admin

Channels, roles, kicks/bans/unbans, timeouts, nicknames, purge, pin, invites, permission overrides.

```
INTEGRATIONS=discord_admin
DISCORD_ADMIN_USER_IDS=123456789012345678     # optional extra admins; the server owner is always allowed
DISCORD_ADMIN_CONFIRM=destructive             # destructive (default) | all | none
DISCORD_ADMIN_ALWAYS_ASK=post_message         # optional: actions that always ask, e.g. ban_member,post_message
DISCORD_ADMIN_NEVER_ASK=set_nickname          # optional: actions that never ask
```

The two lists override `DISCORD_ADMIN_CONFIRM` per action (an action in both asks). You can set all of
this, and the admin IDs, in the control panel: Integrations tab → Discord Admin → Settings →
**Admins** / **Approvals**. Restart the bot to apply.

- Only the server owner and the listed IDs are even shown the tools.
- It only ever acts on the server the message was sent in.
- Ban, kick, delete channel/role, purge, and handing out powerful permissions need ✅ approval.
- Every action lands in the Audit Log with the requester's name.
- The bot can only do what its own Discord role permits. Give it a role with the permissions you want
  (Manage Channels, Manage Roles, Kick/Ban Members, Moderate Members, Manage Messages) and place that
  role **above** the roles it should manage.
- Optional: turn on **Server Members Intent** in the Discord developer portal and in `bot.py`
  (`intents.members = True`) to enable name search; without it, use user IDs / @mentions.

## JavaScript integrations (experimental)

JavaScript integration support is opt-in and unstable. To enable it:

1. Check the "Enable JS integrations (experimental)" box in the control panel
   Experiments tab and save, or set `JS_INTEGRATIONS_ENABLED=true` in `.env`.
2. Install Node.js (18+) and any npm packages your integration needs.
3. Optionally restrict which files load with `JS_INTEGRATIONS=hello_js,other` (file
   name or stem, comma/semicolon separated; default loads all `*.js` files).

JS integrations live as `*.js` files in `integrations/` or `integrations/js/` and
must not start with `_`. Each file implements a two-command protocol:

  node <file> manifest   -> prints one JSON manifest on stdout
  echo '<envelope>' | node <file> run -> prints one JSON result on stdout

Manifest:
  {
    "name": "unique_name",
    "description": "...",
    "prompt": "optional system-prompt text",
    "tools": [ { "name", "description", "input_schema" } ... ]
  }

Call envelope:
  { "tool": "tool_name", "args": {...},
    "context": { "author_id", "author_name", "platform" } }

Things JS integrations do NOT get:
  - `ctx.confirm` / `ctx.send` (they are stateless subprocesses)
  - Python settings helpers
  - control-panel metadata beyond the manifest name (no auto-generated panel entry)

Log lines printed to stderr are captured and shown on failure. Non-zero exit or
invalid JSON becomes a tool error reported back to the model.
