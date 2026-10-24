# SystemC Drovex — complete documentation

Covers the **Java** port (the `java` branch) and notes where the **Python** original
(the `main` branch) differs. Everything applies to release **v1.0.8**.

Contents:

- [1. What SystemC is](#1-what-systemc-is)
- [2. Repos and branches](#2-repos-and-branches)
- [3. Installing](#3-installing)
- [4. Running it — the `systemc` command](#4-running-it--the-systemc-command)
- [5. Configuration: `.env` reference](#5-configuration-env-reference)
- [6. Architecture](#6-architecture)
- [7. The agent loop and tools](#7-the-agent-loop-and-tools)
- [8. Approvals (the safety gate)](#8-approvals-the-safety-gate)
- [9. Sessions, memory and subagents](#9-sessions-memory-and-subagents)
- [10. Chat platforms: Discord and Stoat](#10-chat-platforms-discord-and-stoat)
- [11. The web console and HTTP API](#11-the-web-console-and-http-api)
- [12. Integrations (extending the agent)](#12-integrations-extending-the-agent)
- [13. Integrations menus (all three UIs)](#13-integrations-menus-all-three-uis)
- [14. Starting at login / boot (systemd)](#14-starting-at-login--boot-systemd)
- [15. The control surfaces](#15-the-control-surfaces)
- [16. Python version: parity and differences](#16-python-version-parity-and-differences)
- [17. Troubleshooting](#17-troubleshooting)

---

## 1. What SystemC is

SystemC Drovex is a self-hosted AI agent that lives in your Discord and Stoat chat as
a bot, but acts on the **real computer** it runs on:

- a shell (PTY), file system, python and web-search toolset,
- an LLM driving that toolset (local Ollama first-class, or a cloud key),
- an approval gate for anything destructive,
- per-channel conversation sessions + long-term memory + parallel subagents,
- one local web console and one desktop/terminal control panel for configuration.

You talk to it in chat ("check why my disk filled up"), and it does the work on the host
with the same guardrails you would use yourself: it shows destructive commands and asks
you "y/n" before running them.

Both the Java and the Python version share the same `.env` file layout, history layout
and feature set — you can run just the Java one and not care about the other.

## 2. Repos and branches

Everything lives in **`TheRealHyperno/systemcontrol-agent`**:

| Branch | What's there |
|---|---|
| `main` | the **Python** version of the agent (the original lineage), incl. its integrations docs and this port's systemd/TUI additions |
| `java` | the **Java** port: all sources, the installer, and the website (Pages serves `index.html`, `logo.png`, `favicon.png`, `install.sh`, `uninstall.sh`, `INTEGRATIONS.md` from this branch's root) |
| `systemcontrol-java` | a legacy one-file shim repo keeping the old `…/systemcontrol-java/install.sh` one-liner URL alive by forwarding |

Releases (tarballs + sha256) live on `systemcontrol-agent`'s release page:
v1.0.0–v1.0.5 were java-numbered; the version lines are **merged** from there
on — java and python backends share one release line (same repo, same tag
space, same "Latest"), each attaching its own asset(s): the java build ships
as `systemc-java-linux-x64.tar.gz` (+ `.sha256`), the python build as
`systemc-<tag>.zip` (+ `.sha256`). A release carrying only one backend's asset
is fine — the other backend's tooling reports it. Both backends choose "latest"
by publication date, never by tag number. (Choosing java vs python at
install/run time — the BACKEND key — is described under
[Backends](#backends--backendjavapython) below.)

## 3. Installing

```bash
curl -fsSL https://therealhyperno.github.io/systemcontrol-agent/install.sh | bash
```

- links a `systemc` command into `~/.local/bin`,
- installs the app into `~/.systemc/` (`bin/systemc` launcher, `lib/*.jar`, `share/`),
- creates a `.desktop` menu entry ("SystemC Control Panel") with your SystemC favicon
  as icon, plus right-click Actions ("open web console", "start/stop the bots".
 left-click boots the Swing control panel),
- verifies the tarball sha256 before extracting.

The legacy URL `https://therealhyperno.github.io/systemcontrol-java/install.sh`
still installs, forwarding to the current one.

Uninstall:

```bash
# on the system
bash ~/.systemc/dist/uninstall.sh 2>/dev/null || https://therealhyperno.github.io/systemcontrol-agent/uninstall.sh
# …or delete ~/​.systemc, ~/​.local/bin/systemc and the menu/icon/​desktop files
```

Requirements: Linux x86-64, **Java 21+** (Fedora: `sudo dnf install java-21-openjdk`).
Needs python3 only if you want python-compat integrations or the python version.

## 4. Running it — the `systemc` command

| Command | What it does |
|---|---|
| `systemc run` | the bot(s) + web console on `127.0.0.1:7842`. This is the process you want either in a terminal or under systemd |
| `systemc panel` | Swing control panel (the "macOS-style" desktop app) |
| `systemc tui` | **terminal** control panel — same configuration over SSH/no desktop. Arrow keys/Enter/j/k, `q` walks back, secrets masked |
| `systemc models` | list the Ollama models reachable at `OLLAMA_HOST` (first smoke test after install) |
| `systemc service install \| status \| stop \| restart \| uninstall \| linger [--profile id]` | squash the bot into a systemd **user unit** — see section 14 |
| `systemc update [--check \| --rollback] [--repo owner/name] [--pre-release]` | self-update from GitHub releases: always takes GitHub's **Latest** release (by publication date, not tag number) → download → sha256-verify → back up → replace (see also the Updates panel tab) |
| `systemc config [show \| set KEY VALUE]` | show the backend/version/update-repo, or write a KEY (e.g. `UPDATE_REPO`) into `.env` from the terminal |
| `systemc version` / `systemc jar` | which release/jar is running |

First run:

```bash
cd ~/.systemc
cp example.env .env
nano .env            # put in a DISCORD_BOT_TOKEN and/or STOAT_BOT_TOKEN, pick a model
systemc models       # check Ollama is reachable
systemc run          # or: from the desktop panel / app menu
```

The bot then appears on the chat platforms chosen in `.env`. Talk to it in an allowed
channel and it answers; give it tasks and it uses its tools on the host.

## 5. Configuration: `.env` reference

`.env` lives in the install dir (or in `.profiles/<id>.env` per profile). Later
sections explain behavior behind each group.

### Bots

| Key | Meaning |
|---|---|
| `DISCORD_BOT_TOKEN` | Discord bot token. Set = the Discord bot starts |
| `STOAT_BOT_TOKEN` | Stoat bot token. Set = Stoat bot starts. Both set = both run |
| `ALLOWED_CHANNEL_IDS` | Discord channel ids (comma list) allowed to use the agent; empty = any |
| `STOAT_ALLOWED_CHANNEL_IDS` | same for Stoat |
| `BOT_MODE` / `STOAT_BOT_MODE` | `WAKEWORD` (default) = reply when the wake word is used; `MENTION` = reply when mentioned/​@-ed; `EVERYTHING` = every message |
| `WAKE_WORD` / `STOAT_WAKE_WORD` | woken string (default `hey claw`) |

### Model backend (exactly one is used)

| Key | Meaning |
|---|---|
| `AGENT_MODEL` | Ollama model tag (e.g. `qwen2.5:0.5b` — the released default, chosen for poor machines) |
| `OLLAMA_HOST` | `http://localhost:11434` by default; can point at Ollama Cloud when `OLLAMA_API_KEY` set |
| `OLLAMA_API_KEY` / `OLLAMA_API_KEYS` | cloud key, or comma list = **key pool** (round-robin, exhausted keys are rotated away automatically) |
| `AGENT_PROVIDER` | `anthropic` = use Claude instead of Ollama |
| `ANTHROPIC_API_KEY` | key for `AGENT_PROVIDER=anthropic` |
| `AGENT_MAX_TURNS` | max tool-loop turns per user message (default 20) |
| `AGENT_WORKDIR` | sandbox dir for shell/file tools (default `./workspace`) |

### Approvals & tools

| Key | Meaning |
|---|---|
| `APPROVAL_MODE` | `ask` (default: dangerous commands ask in chat) / `approve_all` (never ask) / `deny_all` |
| `APPROVAL_COMMANDS` | extra command regexes to treat as destructive |
| `AGENT_ALLOW_ELEVATED` | `1` (default) exposes `run_shell_elevated` (sudo); every use asks |
| `SHOW_TOOLS_USED` / `SHOW_COMMANDS_USED` | print a modest "-# Commands executed" summary after replies |

### Personalization

`AGENT_NAME` (display name), `AGENT_GENDER`, `AGENT_PRONOUNS`, `AGENT_CASUAL_TONE`,
`AGENT_SYSTEM_PROMPT` (full prompt override; empty = built-in default).

### Subagents

`SUBAGENTS` (`on` default), `SUBAGENT_MODEL` (smaller model for the workers?),
`SUBAGENTS ` max parallelism & turns: `SUBAGENT_MAX_PARALLEL` (3), `SUBAGENT_MAX_TURNS` (12), `SUBAGENT_TIMEOUT` (600s).

### Integrations

| Key | Meaning |
|---|---|
| `INTEGRATIONS` | comma/semicolon list of integration names, or `all` — see §12 |
| `PY_COMPAT` | `auto` (default) / `on` / `off` — bridge for legacy python `integrations/*.py` |
| `INTEGRATION_TIMEOUT` | max seconds per python-compat tool call (180) |

### Web console

| Key | Meaning |
|---|---|
| `SYSTEMC_WEBSITE` | `1` (default) starts the local web console |
| `WEBUI_PORT` | port, default 7842 |
| `SYSTEMC_API_TOKEN` | if set: enables the protected `/api/v1/*` endpoints; these are the endpoints the desktop panel/profiles use. If empty, `/api/v1/*` returns 503 (tools routes stay open on localhost) |

### Profiles

`SYSTEMC_PROFILE_ID` picks a running profile when started with `--profile <id>`.
`SINGLE_PROFILE`, profiles.json lives in `.profiles/profiles.json`. Per-profile env
overlays live at `.profiles/<id>.env`; per-profile pip packages go into the profile's
own `DEPENDENCIES=` key (Pulled from the Profiles or Dependencies tab).

### Workspace & account (Setup tab)

| Key | Meaning |
|---|---|
| `AGENT_WORKDIR` | sandbox dir for shell/file tools (Setup tab's workspace field) |
| `AGENT_RUN_AS_USER` | run instances of profiles as this user (`runuser` as root, `sudo -n` otherwise); empty = panel's own account |
| `AGENT_ACCOUNT_MODE` | `own` / `other` — set by the Save config button |
| `AGENT_ALLOW_ELEVATED` | `1` exposes `run_shell_elevated` under the run-as account (an approval still comes before every elevated command) |

### Backends — BACKEND=java|python

There are two interchangeable backends of the same agent in this repo, and the
whole "backend" concept is just the question *do you want java or python*:

- chosen at setup: the installer and the first `systemc run` with no `.env` ask
  once in the terminal (default **java**);
- switchable any time: `systemc config set backend python` downloads the newest
  release carrying a python build (`systemc-*.zip`, sha256-verified), unpacks it
  under `backend/python/`, pulls its pip deps and sets `BACKEND=python`;
  `systemc config set backend java` flips back — the jar stays installed either
  way;
- `systemc run` dispatches by `BACKEND`: java → this engine's jar, python →
  `backend/python/main.py` (whose `.env` is a symlink to the shared install
  `.env`, so tokens/config are set once and work under both);
- `systemc panel` does the same dispatch: java → the Swing control panel, python
  → the tkinter control panel (`backend/python/control_panel.py`), so the app-menu
  shortcut keeps working after a backend switch.
- BACKEND lives in `.env`, so the Swing panel, TUI and web API all apply it on
  the next start.

| Key | Meaning |
|---|---|
| `BACKEND` | `java` (default) or `python` — what `systemc run` starts |

### Updates

| Key | Meaning |
|---|---|
| `UPDATE_REPO` | GitHub repo to track (default `TheRealHyperno/systemcontrol-agent`) |
| `UPDATE_PRERELEASE` | `1` → also offer the newest published release even when it's a pre-release (still by publication date, never by tag number) |

Both are settable from the terminal with `systemc config set UPDATE_REPO …`.


## 6. Architecture

One process, four cooperating layers:

```
 ┌─ discord bot(JDA) ──┐                        ┌─ Swing control panel (separate process) ─┐
 │                    ├── agent loop (thread)  │   tabs: Overview/Sessions/Approvals/     │
 └─ stoat bot (ws) ───┘        │                │         Profiles/Personalize/Integrations│
                                │                └──── .env + history dirs shared with run ─┘
        web console + API (localhost) │
        (com.sun.net.httpserver)     │
                                     ▼
    providers  (Ollama local/cloud — Anthropic cloud)
        └ tool loop: tool-call → run → feed result JSON back → repeat
```

- `systemc.Main` loads `.env`, builds one shared Agent, starts the enabled bots as
  daemon threads and the web server, then waits.
- The **models** layer (`Providers`) normalizes Ollama- and Anthropic-style APIs
  behind one `step(history, tools)` interface, translating tool schemas per provider
  Anthropic gets them as written, Ollama/OpenAI-style gets each one wrapped as a
  function whose parameters is your input schema. Write plain JSON Schema; it works
  on both.
- **State** (same layout as python): `.history/` (chat sessions), `.memory/`
  (long-term memory as date-stamped notes), `.approvals/` (pending destructive-command
  requests), `.profiles/` (per-profile envs), `workspace/` (the tools' default dir).

## 7. The agent loop and tools

Built-in tools (exposed to the model on every turn):

| Tool | Notes |
|---|---|
| `run_shell` | PTY shell (GNU `script -qfec`, piped fallback). Long timeouts, real exit codes. Destructive commands (regex-gated) go through the approval gate |
| `run_shell_elevated` | sudo; only exists when allowed; **always asks** |
| `run_python` | runs a snippet in `python3 -` pipes; isolated process |
| `read_file` / `write_file` / `list_dir` | sandboxed by `AGENT_WORKDIR` paths, but not forcibly locked inside it (write_file has a delete/overwrite gate too) |
| `web_search` | DuckDuckGo lite HTML, first N results |
| `remember` | append a fact to long-term memory |
| `send_message`, `send_file` | reply proactively / attach |
| `fetch_channel_messages` | read recent channel history |
| `reset_session` | clear a conversation's history (asks approval for other sessions) |
| `spawn_subagent`, `spawn_subagents` | workers with a fixed tool subset (no integrations, no messaging) — see §9 |

Loop per user message (defaults, `AGENT_MAX_TURNS=20`):

1. history → model with all tool schemas
2. no tool call → text goes to the chat, done
3. tool call(s) → executed one at a time, results JSON into history, loop

Anything crashing produces an `{"error": …}` result for the model rather than dying.

## 8. Approvals (the safety gate)

Some commands trip a regex (delete/overwrite formats, `rm -rf`, `sudo`, shutdown, etc.,
plus `APPROVAL_COMMANDS` extras). When that happens:

1. a request file is written to `.approvals/`;
2. the **chat** gets a "y/n" prompt (y = run, n = skip);
3. the Swing panel's Approvals tab (and the web API) can also resolve the same
   pending request from your desktop;
4. timeout (default 60s, elevated 90s) counts as no.

`APPROVAL_MODE=approve_all` skips step 1–3 (dangerous), `deny_all` denies everything.

## 9. Sessions, memory and subagents

- **Sessions**: one history file per `platform:channelId` under `.history/`, JSON,
  with the system prompt and all tool traffic. `reset_session` clears one (or another
  one you approve), and the bot's memory panels show usage counts.
- **Memory**: `remember` appends a line to `.memory/YYYY-MM-DD.md`; every turn the
  whole memory is loaded into the system prompt as context. There is no other database.
- **Subagents**: `spawn_subagent(task, name)` delegates one self-contained job to a
  fresh agent with shell/python/files/web (`run_shell` only after approval,
  serialized through a lock); `spawn_subagents(tasks[])` runs up to 5 in parallel
  with its own pool and per-task timeouts, returning one report per task.

## 10. Chat platforms: Discord and Stoat

- **Discord** runs on JDA; works in servers and DMs, honors `ALLOWED_CHANNEL_IDS`, and
  sends/receives files (the bot streams long replies).
- **Stoat** uses a small built-in WebSocket client (the API endpoints are env-tunable
  for forks: `STOAT_API_BASE`, `STOAT_WS_BASE`, `STOAT_AUTUMN_BASE`) honoring
  `STOAT_ALLOWED_CHANNEL_IDS`, and supports the same tool surface, incl. file
  downloads/attachments and y/n approvals — the Stoat bot is feature-equal to the
  Discord one in this port's parity work.
- Both bots wake on the wake word (or mention/everything per `BOT_MODE`), gate
  destructive commands, and use the same Agent.

## 11. The web console and HTTP API

Locally on `127.0.0.1:WEBUI_PORT` (no TLS on purpose — it is localhost-only by default):

| Route | What |
|---|---|
| `/` | the rounded web chat console (single-page, local) |
| `/tools/run_shell` etc. | JSON tool endpoints: `{"command":…}` → tool result |
| `/ollama/chat` | streaming chat proxy (for the console's own playground) |
| `/config` | serve tool/`workdir` info |
| `/api/v1/*` | **bearer-token** (`SYSTEMC_API_TOKEN`) protected administrative API: `ping`, `approvals` (GET list, POST resolve), `sessions` + `sessions/reset`, `memory`, and the integrations endpoints of §13 |

## 12. Integrations (extending the agent)

An integration gives the model **extra tools**. Two kinds are supported:

### 12.1 Java SPI (the primary way)

One class implements `systemc.integrations.Integration`, registered in
`META-INF/services/systemc.integrations.Integration` inside a jar dropped into
`integrations/` next to `.env` (or you can file-pick "Install from .jar…" in the
panel). It can expose:

```java
name()             // "myexample" (unique; also the settings key prefix)
description()      // one line, shown in menus
prompt()           // optional; becomes "## Integration: myexample" in the system prompt
available(ctx)     // optional per-turn gate (author/platform aware)
tools()            // plain JSON-Schema definitions: {name, description, input_schema}
handle(ctx, tool, args)   // one call; return anything JSON-serializable
panel()            // optional settings-menu descriptor (see its own section)
init()             // optional one-time hook; throw to refuse load
```

- `ctx` gives `platform`, `channelId`, `authorId/Name`, `send(text)`,
  `sendFile(path,caption)`, `requestApproval(text, timeout)`.
- The engine drops name collisions (built-ins win, then env order), malformed schemas
  and integrations failing `init()` — logging exactly which.
- Subagents **never** get tools: only the main loop does.

Full walkthrough with a working class in `INTEGRATIONS.md` (same site, this repo).

### 12.2 Python-compat (backwards compatibility mode)

`integrations/*.py` from the python port keep working, via a tiny born-per-call
python bridge (`PY_COMPAT=auto/on/off`, default auto). Module contract:

```python
NAME = "myexample"
TOOLS = [{"name": ..., "description": ..., "input_schema": {...}}]
PROMPT = "optional advice for the model"
PANEL = {..same descriptor shape..}
def handle(ctx, tool, args):   # ctx.send/request_approval/send_file approximated
```

Honest limitations: one-shot process per call (no in-memory state — touch the fs
instead), module-level imports run at discovery (broken files are shown as FAILED
in menus, not fatal), `requestApproval` auto-yes at the python side (Java enforces
the real flow), and each call is subject to `INTEGRATION_TIMEOUT` seconds. Fine for
small/legacy tools; write new ones in Java.

## 13. Integrations menus (all three UIs)

Every configuration surface reads the **same engine**, so the menus cannot drift:
the source of truth is the integration's `panel()` descriptor (fields; optional
`title`/`icon`/`submenu`). Values are stored in `.env` under each field's own
key (convention: `DICE_MAX_PER_ROLL`, `NOTES_ANNOUNCE`, `HELLOBOT_PREFIX`, …) and
apply at the next bot start.

| Surface | Menu |
|---|---|
| **Swing panel** → *Integrations* tab | list + `✔ enabled/disabled` + tool count + source (java/python) + load errors. Buttons: Enable/disable · Settings… (autogenerates the dialog: checkbox for bool, combo for choice, password field for secret, muted description under each field) · Install from .jar… · Open folder · Reload |
| **TUI** → *Integrations* | same list; Enter toggles and opens the integration's settings page (rows per field; secrets masked as `●●●`; choice fields show numbered options; Enter-on-empty keeps the current value). `q` walks back one level |
| **Web API** | `GET /api/v1/integrations` (name, source, enabled, tools, `menu_title`, fields, settings) · `POST /api/v1/integrations/enable {"name","enabled"}` · `PUT /api/v1/integrations/settings {"name","settings":{KEY:value}}` |

Also see `INTEGRATIONS.md` section "Menus" for the same with screenshots-like
descriptions, and the bundled `dice`/`notes` as live examples of the whole shape.

## 14. Starting at login / boot (systemd)

The bots can be handed to systemd as a **user unit** — no root needed:

```bash
systemc service install [--profile <id>]     # write + enable systemc[-<id>].service at login
systemc service status                       # installed/enabled/active
systemc service stop | restart
systemc service uninstall                    # disable + remove the unit
systemc service linger                       # systemd starts it at TRUE boot, pre-login
```

- unit files: `~/.config/systemd/user/systemc.service` (or `…/systemc-<id>.service`),
- `journalctl --user -u systemc.service -f` for logs,
- the python version has the equivalents
  (`python main.py --install-service [--profile id] [--linger]`),
- installing requires a `.env` with at least one bot token (pre-flight check).

## 15. The control surfaces

| Surface | For | Highlights |
|---|---|---|
| `systemc run` | servers/SSH | the bot process + web console |
| Swing panel | desktop | 10 tabs, python-panel parity: **Overview** (quick actions + panel log) · **Setup** (tokens, channels, bot modes, wake words, Ollama local/cloud + ordered key pool, model picker with *Fetch available models*, workspace dir + run-as-user with `Set up / Verify directory` and `Allow panel to run as user`, *Save config to .env*) · **Profiles** (cards with model/platform/deps summary; New / Import archive / Export archive / Migrate .env dialogs with the secrets warning; per-profile Start / Stop / Pull Dependencies / Edit .env / Delete) · **Personalize** · **Dependencies** (java/ollama/python3/pip/tar health + per-profile `DEPENDENCIES=` pip packages with Pull) · **Run & Logs** (Start/Stop per instance, auto-restart-on-crash checkbox, Open web UI, live log tail of the selected instance) · **Approvals** · **Integrations** · **Sessions** · **Updates** (Check for updates / Update now / Roll back / Show backups, pre-release checkbox, UPDATERepo field); branded with the SystemC logo/favicon; macOS-ish FlatLaf theme |
| TUI | SSH/no-DE | bots / agent / console / subagents / **integrations** / startup / `.env` in `$EDITOR` |
| web console | everyone | chat with the agent in the browser; same tool endpoints |
| `systemc update` | servers/SSH | CLI self-update: `--check` / (default: install) / `--rollback`, `--repo owner/name`, `--pre-release` — same engine as the Updates tab |

## 16. Python version: parity and differences

The `main` branch python version is the original; this Java port is at parity for
agents, bots, integrations APIs and state files. The differences that matter:

| Topic | Python | Java |
|---|---|---|
| Integrations code | `.py` files, rich ctx | `.jar` SPI (first-class) + simple `.py` compat bridge |
| Control panel | Tk desktop + FastAPI webui (`webui_server.py`) | Swing + built-in web console in one jar |
| Approvals | same file layout | same; plus chat y/n in both bots |
| Extras (upstream, either side) | `voice_call`, `import_openclaw`, JS experimental setups removed upstream | subagents pool, PTY via `script(1)`, built-in integrations engine/menus |
| systemd/TUI | supported (main) | supported |
| Packaging | source + `install.sh` | release tarball + one-liner + desktop entry |

## 17. Troubleshooting

| Symptom | Fix |
|---|---|
| `[integrations] … is in INTEGRATIONS but no such integration exists` | typo, or the jar does not declare `name()` / is not in `integrations/`, or it failed `init()` (the menus and the startup log show the exact error) |
| no Stoat connection | check `STOAT_BOT_TOKEN`, or override `STOAT_API_BASE` |
| Discord `Improper token` | regenerate token in the developer portal |
| `systemc service install` says enable failed | systemd user session not running for this login — `loginctl show-user $USER` |
| web API returns 503 | set `SYSTEMC_API_TOKEN` in `.env` (restart) |
| tool calls time out on python integrations | raise `INTEGRATION_TIMEOUT`, or move the work to a background file-based tool |
| web console unreachable | only binds 127.0.0.1; SSH-tunnel if remote (`ssh -NfL 7842:127.0.0.1:7842 host`) |

---

*Generated for the Java port — docs live in the `java` branch: `README.md`,
`INTEGRATIONS.md`, and this file (`DOCS.md`).*
