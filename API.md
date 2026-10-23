# SystemC Integrations API

Everything the control panel's **Integrations** tab does, over HTTP, for scripts and other
programs: list integrations, switch them on and off, and manage the Discord Admin settings
(authorised admin IDs and which actions need approval). Integrations can add their own
endpoints (see [Adding endpoints to an integration](#adding-endpoints-to-an-integration)).

- **Base URL:** `http://127.0.0.1:7842/api/v1` (served by the Local web UI; start it from the
  control panel's *Run && Logs* tab or with `python webui_server.py`). It listens on localhost only.
- **Format:** JSON in, JSON out.
- **Changes are saved to `.env` and apply the next time the bot starts.** Every response that
  changed something includes `"restart_required": true`.

## Authentication

Every endpoint needs a bearer token:

```
Authorization: Bearer <token>
```

Create it in the control panel (**Integrations → API token → Generate**), or from a terminal:

```
python -c "import integration_settings as s; print(s.new_api_token())"
```

It is stored as `SYSTEMC_API_TOKEN` in `.env`. The server re-reads it on every request, so a new
token works immediately and the old one stops working at once. **With no token set, the API
refuses every request (503)** rather than being open. Treat the token like a password: it can
add admins and turn off approval prompts for destructive actions.

```bash
export TOKEN=...            # the token from the control panel
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:7842/api/v1/ping
```

## Errors

Errors use FastAPI's standard shape, `{"detail": "human-readable message"}`:

| Status | Meaning |
|---|---|
| 400 | Invalid input (bad ID, unknown setting value, body isn't the expected JSON) |
| 401 | Missing or wrong bearer token |
| 404 | No such integration or action, or the ID isn't in the list |
| 409 | Needs confirmation: `"never ask"` on a destructive action without `confirm_destructive: true` |
| 503 | No API token has been created yet |

---

## Integrations

An integration is listed here when its file declares a `PANEL` field (the same rule as the control
panel). Names are the file names in `integrations/` without `.py`.

Integration routes (list, get, `enabled`, `settings`) take an optional `?profile=<id>` query parameter to
read or change **that profile's** settings instead of the global `.env` (a profile's own values are
overrides; anything it hasn't set follows the global ones). Unknown profile id → 404.
Profiles also have personalization fields you can send to `POST /profiles`: `bot_name`, `gender`,
`pronouns`, `system_prompt` (`""` = built-in default; variables `{NAME}` `{GENDER}` `{PRONOUNS}`
`{MODEL}` `{SOURCE}` `{VERSION}`).

### `GET /integrations`

```json
{
  "integrations": [
    {
      "name": "discord_admin",
      "title": "Discord Admin",
      "icon": "🛡️",
      "description": "Create/edit channels and roles, ban/kick/timeout members, purge messages, and more.",
      "enabled": false,
      "submenu": [
        {"id": "admins",    "title": "Admins",    "description": "Who may use these tools besides the server owner. ..."},
        {"id": "approvals", "title": "Approvals", "description": "Which actions ask for ✅/❌ first. ..."}
      ],
      "has_api": true,
      "has_settings": false,
      "problems": []
    }
  ]
}
```

`enabled` reflects `INTEGRATIONS` in `.env` (`all` counts as everything on). `has_api` means the
integration contributes its own endpoints under `/integrations/{name}/`.

### `GET /integrations/{name}`

One entry from the list above. `404` if unknown.

### `PUT /integrations/{name}/enabled`

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"enabled": true}' http://127.0.0.1:7842/api/v1/integrations/discord_admin/enabled
```

Returns the updated entry plus `"restart_required": true`. This only edits `INTEGRATIONS` in
`.env`: other entries (including names with no file here) are kept, and `all` is expanded to an
explicit list when you switch one off.

### `GET /integrations/{name}/settings`

Settings the integration declares under `fields` in its `PANEL` (see `integrations/README.md`).
Works for any integration with no code on its side. `404` if the integration is unknown.

```json
{
  "name": "weather",
  "pages": [{
    "id": "settings", "title": "Settings", "description": "",
    "fields": [
      {"key": "WEATHER_UNITS", "label": "Units", "type": "choice", "choices": ["metric", "imperial"],
       "default": "metric", "value": "metric", "is_default": true, "description": "", "required": false,
       "min": null, "max": null, "pattern": ""},
      {"key": "WEATHER_API_KEY", "type": "secret", "value": {"set": false}, "...": "..."}
    ]
  }],
  "problems": []
}
```

`value` is typed (`bool`, number, list, string). Secrets are never returned: `value` is `{"set": true|false}`.
`problems` lists declared fields that were skipped and why.

### `PUT /integrations/{name}/settings`

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"values": {"WEATHER_UNITS": "imperial", "WEATHER_API_KEY": "abc123"}}' \
     http://127.0.0.1:7842/api/v1/integrations/weather/settings
```

Only the keys you send change. A key set to `null` goes back to its default. All-or-nothing: if any
value is invalid (`400`, with a message) or any key isn't one of the integration's declared fields,
nothing is saved. Returns the document above plus `"restart_required": true`. If an integration
defines its own `/settings` route in `api_router()`, that one is used instead.

### `GET /ping`

`{"ok": true}`. Handy for checking a token.

---

## Discord Admin — `/integrations/discord_admin`

### Admins

Who may use the admin tools besides the server owner (the owner is always allowed). Stored as
`DISCORD_ADMIN_USER_IDS`. All the endpoints below return
`{"admin_ids": ["123456789012345678", ...], "owner_always_allowed": true}`
(plus `restart_required` when they change something).

| Request | Body | Effect |
|---|---|---|
| `GET /admins` | | List the IDs |
| `POST /admins` | `{"user_id": "123456789012345678"}` | Add one. A `<@mention>` works too. Adding an existing ID is a no-op |
| `DELETE /admins/{user_id}` | | Remove one (`404` if it isn't listed) |
| `PUT /admins` | `{"admin_ids": ["...", "..."]}` | Replace the whole list. All-or-nothing: if one ID is invalid, nothing changes |

IDs are 15–25 digits (Discord snowflakes).

### Approvals

Which actions ask for ✅/❌ approval before the bot performs them. Two layers:

1. A **global mode** (`DISCORD_ADMIN_CONFIRM`): `destructive` (default: only risky actions ask),
   `all`, or `none`.
2. A **per-action setting** that beats the mode: `ask` (always ask), `never` (never ask) or
   `default` (follow the mode). Stored in `DISCORD_ADMIN_ALWAYS_ASK` / `DISCORD_ADMIN_NEVER_ASK`.

#### `GET /approvals`

```json
{
  "mode": "destructive",
  "modes": ["destructive", "all", "none"],
  "actions": [
    {"name": "ban_member",   "description": "Ban a user ...", "default_asks": true,  "setting": "default"},
    {"name": "post_message", "description": "Send a message ...", "default_asks": false, "setting": "ask"}
  ]
}
```

`default_asks` is whether the action would ask under the current mode with no setting of its own.
Action names have no `discord_` prefix (it is accepted if you send it).

#### `PUT /approvals/mode`

`{"mode": "all"}` → the full approvals document above.

#### `PUT /approvals/actions/{action}`

`{"setting": "ask" | "never" | "default"}` → the full approvals document.

Setting `never` on an action that is destructive by default (ban, kick, purge, delete channel/role,
and so on) is refused with **409** unless you pass `"confirm_destructive": true`, the same
confirmation the control panel asks for:

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"setting": "never", "confirm_destructive": true}' \
     http://127.0.0.1:7842/api/v1/integrations/discord_admin/approvals/actions/purge_messages
```

#### `PATCH /approvals/actions`

Several at once: `{"settings": {"set_nickname": "ask", "post_message": "default"}, "confirm_destructive": false}`.
All-or-nothing: if any entry is invalid or needs confirmation, none are applied.

---

## Using it from Python

```python
import httpx

API = "http://127.0.0.1:7842/api/v1"
client = httpx.Client(base_url=API, headers={"Authorization": f"Bearer {TOKEN}"})

client.put("/integrations/discord_admin/enabled", json={"enabled": True}).raise_for_status()
client.post("/integrations/discord_admin/admins", json={"user_id": "123456789012345678"})

r = client.put("/integrations/discord_admin/approvals/actions/ban_member", json={"setting": "ask"})
print(r.json()["restart_required"])   # True: restart the bot to apply
```

Code that runs on the same machine, in the same folder, can skip HTTP and call the layer the API
and the control panel both use. `integration_settings.py` has `list_integrations()`,
`set_enabled(name, on)`, `load_env()` / `save_env()`, `new_api_token()` and `SettingsError`;
`integrations/_discord_admin_settings.py` has the admin and approval functions.

---

## Adding endpoints to an integration

Define `api_router()` in your integration file. It must return a FastAPI `APIRouter`. The web
server mounts it under `/api/v1/integrations/{your_file_name}/` behind the same bearer token, so
you never write auth code. Plain settings don't need this at all: declare `fields` in `PANEL` and they are served for you at `/settings`.

Rules of thumb:

- **The bot must not import FastAPI.** Keep the routes in a separate module and import it *inside*
  `api_router()`, so the bot (which loads your integration but never serves HTTP) never needs it.
- **No `from __future__ import annotations` in the routes module.** FastAPI reads the handlers'
  annotations, and string annotations are looked up in that module's globals.
- Use the helpers in `integrations_api`: `json_body(request)` (JSON object or 400),
  `changed(**data)` (adds `restart_required`), `bad_request(e)` (turns a `SettingsError` into a 400).
- Put validation and `.env` reading/writing in plain functions (see `integration_settings.py`) so
  the API, the control panel and your integration share one implementation.
- Settings are read from the environment when the bot starts, so a settings endpoint should return
  through `changed(...)` to tell callers a restart is needed.

`integrations/my_thing.py`:

```python
NAME = "my_thing"
DESCRIPTION = "Does a thing."
TOOLS = [...]

async def handle(ctx, name, args): ...

def api_router():
    from integrations import _my_thing_api          # imported here, never by the bot
    return _my_thing_api.build_router()
```

`integrations/_my_thing_api.py` (leading `_` so it isn't loaded as an integration):

```python
from fastapi import APIRouter, HTTPException, Request
from integration_settings import SettingsError, load_env, save_env
from integrations_api import bad_request, changed, json_body


def build_router() -> APIRouter:
    r = APIRouter(tags=["my_thing"])

    @r.get("/greeting")
    async def get_greeting():
        return {"greeting": load_env().get("MY_THING_GREETING", "hello")}

    @r.put("/greeting")
    async def put_greeting(request: Request):
        text = (await json_body(request)).get("greeting")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(400, 'body must be {"greeting": "some text"}')
        env = load_env()
        env["MY_THING_GREETING"] = text.strip()
        save_env(env)
        return changed(greeting=text.strip())

    return r
```

These become `GET`/`PUT /api/v1/integrations/my_thing/greeting`. The whole real example is
`integrations/discord_admin.py` → `api_router()` and `integrations/_discord_admin_api.py`.

Custom endpoints don't create a control-panel page by themselves: declare `fields` for a generated
page, or add a drawer to `ControlPanel.INTEGRATION_PAGES` in `control_panel.py` (see `integrations/README.md`).

## Profiles (multi-bot) endpoints

Same token, same `Authorization: Bearer <SYSTEMC_API_TOKEN>` header. Handled by `profiles_api.py`.

| Method | Path | What it does |
|---|---|---|
| GET | `/api/v1/profiles` | All profiles + which are running |
| GET | `/api/v1/profiles/{id}` | One profile |
| POST | `/api/v1/profiles` | Create a profile, or update the one with that `id` (only the fields you send change) |
| DELETE | `/api/v1/profiles/{id}` | Delete (not `default`, not while running) |
| POST | `/api/v1/profiles/{id}/start` · `/stop` | Start / stop that bot instance |
| POST | `/api/v1/profiles/{id}/install_deps` | pip-install the profile's dependencies |
| POST | `/api/v1/profiles/migrate` | Import the legacy `.env` (body: `{"target_id": "default"}`; only `.env*` files inside the SystemC folder) |
| GET | `/api/v1/profiles/{id}/logs?limit=100` | Recent log lines |

Profile ids are `a-z`, `0-9`, `_` and `-` (1-40 characters). Bot tokens and API keys are never returned:
they come back as `{"set": true}`. When updating, leave a credential out (or send the masked object back)
to keep it, send a string to replace it, or `""` to clear it.

## Security notes

- The API binds to `127.0.0.1` only and every route needs the token. Don't expose port 7842 to a
  network without putting real access control in front of it.
- CORS on the web UI server is open (`*`); that is safe for these routes only because they need
  the bearer token, which a web page can't know.
- **The older `/tools/*` and `/ollama/chat` endpoints on the same server are not covered by this
  token.** They predate the API, take no credentials, and (with the open CORS setting) can be
  called by any web page open in your browser, including `/tools/run_shell`. Treat that as a
  separate problem to fix before relying on the web UI server's isolation.
