# systemc — quickstart (Fedora + Ollama)

## Fastest path: run the installer

**Linux/macOS:**
```
./install.sh
```

**Windows:** double-click `install.bat`, or run it from a terminal.

Either one creates a venv, installs the Python deps, checks that tkinter is
available (needed for the control panel GUI), warns you if Ollama isn't on
your PATH yet, and launches the control panel automatically. Safe to re-run
any time — it reuses the existing venv and never touches your `.env`,
`.memory/`, or `.history/`.

From there, use the Setup tab to paste your Discord bot token (see "Get a
Discord bot token" below for how to get one), Dependencies to pull an
Ollama model, then Run && Logs to start the bot.

## Agent Workspace & Dedicated Folder

SystemC Farink allows you to isolate the agent's files and shell environment:
- **Dedicated agent folder (`/home/agent` on Linux, `C:\Users\agent` on Windows)**: The agent's workspace and user home (`$HOME` / `%USERPROFILE%`) are set to this directory, owned by your current user account. This prevents dotfiles, caches, and working files from cluttering your personal home directory while running under your login.
- **Run under a separate user account**: Shell commands run under a dedicated system user account (e.g. `agent`) with `/home/agent` (or `C:\Users\agent`) as its home directory.
- **Local workspace**: Defaults to `./workspace` next to the app.

If the agent directory does not exist yet (or if the separate user hasn't been created), the Control Panel and Setup Wizard will prompt for elevation (root/sudo password on Linux, or Windows UAC) to automatically create the folder, create the user if requested, and set proper permissions (`chown` on Linux, `icacls` on Windows).

## Updates tab

The **Updates** tab checks a GitHub repo's releases for a newer SystemC and installs it.

1. Enter the source as `owner/repo` (or paste the github.com link) and press **Check for updates**.
   Tick **Include pre-releases** to follow betas too. The source is saved as `UPDATE_REPO`
   (and `UPDATE_PRERELEASE`) in `.env`.
2. If a newer release exists, press **Update now**. It downloads the release zip, byte-compiles
   every `.py` file in it (a syntax error aborts before anything is touched), backs up each file
   it is about to overwrite into `.backups/`, installs, and re-runs `pip install -r requirements.txt`
   if that file changed.
3. Restart the control panel (the tab offers to do it) and the bots to run the new code.
4. **Roll back last update** restores the backup and removes any files that update added. The
   five most recent backups are kept.

Never touched by an update: `.env`, `.memory/`, `.history/`, `.approvals/`, `workspace/`, `venv/`,
`.backups/`, and any file the release doesn't contain (your own integrations are safe). Files the
release does contain are replaced, so local edits to those are overwritten; the backup keeps them.

Release format: a normal GitHub release with a `.zip` asset attached, or none (GitHub's automatic
source zip is used). If the release also has an asset named `<zip name>.sha256`, the download is
verified against it. The first check on an existing install always offers the latest release,
since the installed release isn't known yet; installing it records the tag in `.update_state.json`.

Only point this at a repo you trust: an update replaces the program's code, and this program can
run shell commands. Only https GitHub links are fetched, archives containing paths outside the
install folder or symlinks are refused, and the agent itself has no tool to trigger an update.
GitHub allows 60 unauthenticated API requests per hour per IP, which is plenty for manual checks.
The same thing is available from a terminal: `python updater.py check|update|rollback`.

## Personalization tab (name, gender, pronouns, system prompt)

Every profile has its own **Personalization** tab settings. Restart that profile's bot to apply changes.

- **Bot name** — what the agent calls itself (blank = the profile's name).
- **Gender** and **Pronouns** — plain text boxes, so anything works: `female`, `Gender Non-Conforming`,
  `he/him`, `xe/xer`. Blank means "not specified".
- **System prompt** — the box is pre-filled with the full default prompt. Edit it to fine-tune, or
  replace it entirely. A profile that still has the default stores nothing, so it keeps following the
  built-in prompt when SystemC is updated; **Reset to default prompt** gets you back there.
  **Preview** shows exactly what the model will see.

Variables you can put in the prompt (click one to insert it at the cursor):

| Variable | Becomes |
|---|---|
| `{NAME}` | the agent's name |
| `{GENDER}` / `{PRONOUNS}` | exactly what you typed (`not specified` if blank) |
| `{MODEL}` | the LLM model, e.g. `gpt-oss:120b` |
| `{SOURCE}` | `Ollama Cloud`, `Ollama (local)` or `Anthropic` |
| `{VERSION}` | the SystemC release name, e.g. `Drovex (beta)` |

Only these exact uppercase tokens are replaced; other braces in your prompt are left alone. Integration
tool descriptions and memory are appended after your prompt automatically. In `.env` / the profile these
are `AGENT_NAME`, `AGENT_GENDER`, `AGENT_PRONOUNS` and `AGENT_SYSTEM_PROMPT`. The default text lives in
`prompt_template.py`. The SystemC revision comes from `version.py` (`SYSTEMC_VERSION` / `SYSTEMC_STAGE`).

Note: the Run & Logs tab starts the single legacy bot from `.env`; profiles started from the Profiles
tab (or the API) are the ones that use Personalization and per-profile integration settings.

## Integrations are per profile

The Integrations tab has an **Editing settings for** selector: the active profile, or **Global defaults**.

- Which integrations are on, and every integration setting, can differ per profile. They are stored as
  overrides in `.profiles/<id>.integrations.env`.
- Anything a profile hasn't changed follows the Global defaults (`.env`), so shared settings only need
  setting once. Changing a setting in a profile never touches `.env` or other profiles.
- Profiles that used the older "integrations" list keep their enabled integrations until you change them.

## Subagents

The agent can hand self-contained jobs to subagents (`spawn_subagent`, or
`spawn_subagents` for up to 5 in parallel) and gets back a report from each. Handy
for researching several things at once or keeping a long investigation out of the
chat. Optional `.env` settings:

```
SUBAGENTS=off              # turn the feature off
SUBAGENT_MODEL=qwen3:8b    # use a different (e.g. cheaper) model for subagents
SUBAGENT_MAX_PARALLEL=3    # how many run at the same time
SUBAGENT_MAX_TURNS=12      # tool-call rounds per subagent
SUBAGENT_TIMEOUT=600       # seconds before a subagent is abandoned
```

Parallel subagents make parallel model calls, which can hit Ollama Cloud usage
limits faster; lower `SUBAGENT_MAX_PARALLEL` if you see 429 errors.

## Integrations tab

Tab **5. Integrations** lists the packs in `integrations/` and lets you switch each one on
or off with a checkbox. It writes `INTEGRATIONS` in `.env`; restart the bot to apply.
Every file in `integrations/` appears here, including ones you write yourself. An integration
that declares `fields` in its `PANEL` (see `integrations/README.md`) gets a working **Settings**
page automatically: inputs for text, secrets, numbers, switches, choices and lists, with Save and
Reset. Drop the file in, press **Rescan**, and the page is there. Hand-built pages are still used
where they exist; a submenu with neither shows a stub. Hand-built so far, under Discord Admin:

- **Admins**: add or remove the Discord user IDs allowed to use the admin tools
  (`DISCORD_ADMIN_USER_IDS`). The server owner is always allowed.
- **Approvals**: pick the global approval mode (`DISCORD_ADMIN_CONFIRM`) and override it per
  action with **Always ask** / **Never ask** / **Use default**
  (`DISCORD_ADMIN_ALWAYS_ASK` / `DISCORD_ADMIN_NEVER_ASK`). You're asked to confirm before
  turning approval off for an action that is destructive by default.

### HTTP API

Everything in the Integrations tab is also available over HTTP for scripts and other programs,
served by the Local web UI at `http://127.0.0.1:7842/api/v1`. Create a token in the tab's
**API token** box (Generate / Show / Copy), start the Local web UI, and send
`Authorization: Bearer <token>`. With no token the API is closed. See `API.md` for every
endpoint and for how to add endpoints to your own integration.

## Sessions tab

Tab **6. Sessions** lists every conversation session, one per channel (Discord and Stoat), with the channel name, how many turns it holds, when it was last active and a preview of the last message. Select one or more rows (Ctrl-click or Shift-click for several) and press **Reset selected** to wipe their conversation history. The next message in that channel starts from a blank slate.

- Works whether or not the bots are running. A running bot notices the reset on its next message, and a turn that is mid-flight when you reset won't write the old conversation back.
- Only the conversation is cleared. Long-term memory (`.memory/`) is untouched.
- A channel shows up here after the bot has replied in it once. Channel names are filled in by the bot, so sessions saved by an older version show their numeric ID until the next message.

The bot can do the same thing itself with the **`reset_session`** tool: with no arguments it resets the current channel (once its current reply is sent). Pass another session's key (`discord:123456789`, `stoat:01ABC...`, or just the channel ID) and it asks you for approval first, in chat or on the Approvals tab. `/reset` in a channel does the same thing as well.

## Language / Langue

Use the dropdown in the top-right corner (control panel and web UI) to switch
between International, Traditional, Canadian and AMERICAN English, and Français. To add another language, add an entry to
`TRANSLATIONS` in `i18n.py` (and `LANG_NAMES`).

## Ollama Cloud (no local Ollama needed)

In the Setup tab, choose **Ollama Cloud (API key)** and paste a key from
https://ollama.com/settings/keys. Click **Fetch available models** to list the
exact model IDs your account can use, then pick one (e.g. `gpt-oss:120b`) and
Save. Or set it by hand in `.env`:

```
OLLAMA_MODE=cloud
OLLAMA_API_KEY=your_key
AGENT_MODEL=gpt-oss:120b
```

From a terminal, `python providers.py models` prints the same list.

### Multiple API keys (automatic fallback)

Add as many keys as you like in the Setup tab (➕ Add key). They're used in order,
and if one runs out of usage or gets rejected the next one takes over; the chat
shows a short "switching to key #2" notice. Use ▲ ▼ to pick the primary. In `.env`:

```
OLLAMA_API_KEY=primary_key
OLLAMA_API_KEYS=fallback_key_1,fallback_key_2
```

A failed key is skipped for a while (15 min after a usage limit, 1 h when it has
no usage for that model, 24 h if it's rejected) and the primary is used again once
that passes. Only use keys you're entitled to — don't rotate keys to get around
a provider's limits or terms.

## Stoat

Install the library (`pip install stoat.py` — the installer already does this),
then in the Setup tab fill the Stoat section (token from stoat.chat → Settings →
My Bots). Destructive-command approvals work in Stoat by replying `yes` / `no`,
or from the Approvals tab. Note: don't name any file `platform.py` in this
folder — it would shadow Python's standard library.

## Manual setup

If you'd rather not run a script, or the installer doesn't fit your setup:

### 1. Install Python deps
You need Python 3.10+ (tkinter comes with Fedora's system Python by default).

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

(Or skip the venv — the control panel will install into whatever `python3` it's launched with.)

### 2. Launch the control panel

```
python3 control_panel.py
```

This opens one window with four tabs:

1. **Setup** — paste your Discord bot token + channel ID, pick bot mode/wake word/model, hit "Save config". Writes `.env`.
2. **Dependencies** — shows which Python packages (discord.py, fastapi, uvicorn, httpx, ollama) and which Ollama models are installed, with one-click "Install missing" / "Pull recommended model" buttons.
3. **Run && Logs** — Start/Stop the Discord bot and the local web UI, with live output streamed into the log box. Each has an "Auto-restart on crash" checkbox (on by default) — if the process dies unexpectedly it comes back up on its own after 3s; a deliberate Stop never triggers a restart.
4. **Approvals** — destructive commands (`rm`, `sudo`, `apt install`, etc.) show up here as they happen, so you can hit Approve/Deny right in the control panel instead of needing Discord open. It's the same approval gate either way — whichever you respond to first wins.

Conversation history now also persists to disk per-channel (in a `.history/` folder next to the scripts), so restarting the bot doesn't wipe its memory of a conversation.

## Memory

Separately from per-channel chat history, the bot keeps its own long-running memory in a `.memory/` folder next to the scripts — plain markdown, nothing fancy:

- `.memory/daily/YYYY-MM-DD.md` — an append-only log line gets written after every turn automatically. The bot loads today's + yesterday's file back in as context on each turn.
- `.memory/MEMORY.md` — curated long-term facts. Only written when the model calls the `remember` tool, e.g. when you say "remember that I prefer X" — so it stays small instead of filling up with noise.

Both files are shared across all channels (it's one bot, one memory), separate from the per-channel `.history/` conversation logs, and they're plain text — open and edit them by hand any time.

If a channel's chat history gets too heavy (long back-and-forth, lots of tool output piling up), type **`/reset`** in that channel (or use the Sessions tab, or ask the bot to use its `reset_session` tool) to wipe just that channel's conversation context. Memory (`.memory/`) is untouched — the bot still has its daily logs and `MEMORY.md` on the very next message, it just forgets the specific back-and-forth in that channel.

## New in v1.4.0: web search, Python exec, file attachments

Three new tools, available in both the Discord bot and the local web UI. Nothing extra to install — all three use dependencies you already have (`httpx` for search, the standard library for the rest).

- **`web_search`** — the model can search the web on its own (via DuckDuckGo, no API key needed) whenever it needs something current or outside its training data. You'll see it fire automatically when you ask about anything time-sensitive; no setup required.
- **`run_python`** — a cleaner way for the model to run Python than shelling out to `python3 -c`. Runs with the same interpreter/packages as the bot itself. Nothing to configure.
- **File attachments** — attach a file to your Discord message and it's automatically saved into the workspace (`workspace/attachments/`) before the bot even sees your message, so you can just say "summarize this" or "clean up this CSV" with a file attached. The model can also send files *back* to you (a generated chart, an edited file, a log) — no action needed on your end, it just works the next time you attach something.

One thing worth knowing: file uploads (both directions) are capped at **10MB**, which is Discord's own limit for a non-boosted server. Bigger files will get rejected with an error the bot will relay to you.

## Get a Discord bot token
1. https://discord.com/developers/applications → New Application
2. Bot tab → Reset Token → copy it
3. Same page → turn on **Message Content Intent**
4. OAuth2 → URL Generator → check `bot`, plus Send Messages / Read Message History → open the generated URL → invite it to your server
5. In Discord: enable Developer Mode (User Settings → Advanced), then right-click your target channel → Copy Channel ID

## Ollama model note
You've already got Ollama running locally — just make sure the model you pick actually
supports tool calling (`qwen2.5`, `llama3.1`, `mistral-nemo` all work; plain `llama3.2`
sometimes struggles with the shell tools). Pull one with:

```
ollama pull qwen2.5
```

or use the "Pull recommended model" button in the Dependencies tab.

## Windows support
`tools.py` now auto-detects the OS. On Linux/macOS it behaves exactly as
before (PTY-based shell exec). On Windows it falls back to a plain piped
subprocess (PowerShell if installed, otherwise `cmd.exe`) — same
`{stdout, exit_code, cwd}` shape, so nothing else in the project changed.

Known Windows limitations, since there's no real equivalent for these:
- **No elevated/admin commands** — there's no `sudo` on Windows that plays
  nicely with piped output, so `run_shell_elevated` just returns an error
  explaining that. If the bot itself needs admin rights, run the control
  panel as Administrator instead.
- Programs that specifically check for a PTY (rare outside of Unix installers)
  may behave slightly differently than on Linux/macOS.

Packaging as a standalone `.exe` (e.g. with `pyinstaller`) is a separate,
optional step on top of this — ask if you want that too.


This gives an AI agent real shell access to this machine (with a Discord approval prompt
for destructive commands like `rm`/`sudo`/`apt`). Running it in a VM or container, rather
than your daily-driver Fedora install, is the safer call — the original author's own
setup screen says the same thing.

## Running the bot as another user (e.g. `agent`)

Pick **Run under another user account** in the Setup tab. The control panel launches
the bot, web UI and Stoat bot *as that user*, so shell commands, Python snippets and
file reads/writes all happen as `agent`. The panel window itself stays in your own
desktop session, so there is no `xhost` / `XAUTHORITY` / `DISPLAY` sharing to set up.

One-time setup (asks for your sudo password once). Either click
**🔗 Allow panel to run as user** in the Setup tab, click Start and accept the prompt,
or run:

    python3 runas.py grant agent

This installs `/etc/sudoers.d/systemc-runas-agent` (lets *your* account run commands as
`agent` only, no password) and adds ACLs so your account can still edit `.env` and the
approvals queue in the app folder. Requires the `acl` package (`sudo apt install acl`).
Check status any time with `python3 runas.py status agent`.

Notes:
- If the panel's Python lives somewhere `agent` can't execute (e.g. a venv in your home
  folder), it falls back to the system `python3` and logs a warning. Install the
  dependencies for that interpreter too.
- **Allow running elevated?** (checkbox, only shown in this mode, **off by default**).
  Off: the `run_shell_elevated` tool is not offered to the model at all, the system
  prompt doesn't mention it, and `agent` has no sudo. On: the tool exists and each use
  still needs your approval in chat. Saving the Setup tab applies it at the OS level
  (asks for your sudo password once): ticking installs
  `/etc/sudoers.d/systemc-elevate-agent`, unticking deletes it. CLI equivalent:
  `python3 runas.py elevate agent on|off`. Without run-as mode, behaviour is unchanged
  (elevated is available, using your own sudo); set `AGENT_ALLOW_ELEVATED=0` in `.env`
  to disable it there too.
- To undo: `sudo rm /etc/sudoers.d/systemc-runas-agent /etc/sudoers.d/systemc-elevate-agent`.
