# SystemC Drovex — Multi-Bot Profiles & Wiring Architecture Guide

This document details the multi-instance bot profile system for SystemC Drovex, explaining how multiple bots run concurrently (e.g. 3 different bots simultaneously), how per-agent dependencies are pulled, how migration from the legacy `.env` format works, and how all components are wired together.

---

## 1. Executive Overview

Historically, SystemC stored its configuration in a single, flat `.env` file (`DISCORD_BOT_TOKEN`, `AGENT_MODEL`, `OLLAMA_API_KEY`, etc.). This limited the user to running only one bot instance at a time with a single persona, shared memory, and a single set of API keys.

The **Profiles Architecture** converts SystemC into a multi-tenant bot orchestrator:
- **Multiple Concurrent Bot Instances**: Run 3 (or more) bots simultaneously (e.g. `DevBot`, `ModClaw`, `ResearchStoat`).
- **Independent LLM & API Keys**: Each profile configures its own provider (`ollama` local/cloud, `anthropic`), model, primary API key, and fallback API keys.
- **Isolated Workspaces & Memory**: Each profile (except `default`, which keeps the original `workspace/`, `.memory/` and `.history/` folders so migrated installs keep their data) writes to its own isolated workspace (`workspace/profiles/<id>/`), conversation history (`.history/profiles/<id>/`), and long-term memory facts (`.memory/profiles/<id>/MEMORY.md`), preventing cross-agent contamination.
- **Per-Agent Dependency Pulling**: Each bot profile defines custom dependencies (e.g., `["pandas", "requests", "beautifulsoup4"]`). When the agent is launched (or via the UI), SystemC automatically pulls and installs these packages for that agent.
- **Seamless Migration**: Automatic migration tool parses old single-bot `.env` files into a valid profile while creating a safe backup (`.env.legacy.backup`).
- **Claude-Style Sidebar Navigation**: Replaces cramped top tabs with a modern, collapsible left navigation sidebar featuring live status indicators, profile badges, and quick actions.

---

## 2. Directory & Storage Structure

```
SystemC_Drovex/
├── .profiles/
│   ├── profiles.json          # Master registry: list of profile IDs & active profile
│   ├── default.json           # Profile: SystemC Prime
│   ├── dev_assistant.json     # Profile: DevBot (Claude 3.7 / Ollama Cloud)
│   └── moderation_bot.json    # Profile: ModClaw (Discord Admin, Stoat)
├── .history/
│   └── profiles/
│       ├── default/           # Chat history isolated to Profile "default"
│       ├── dev_assistant/     # Chat history isolated to Profile "dev_assistant"
│       └── moderation_bot/    # Chat history isolated to Profile "moderation_bot"
├── .memory/
│   └── profiles/
│       ├── default/           # Long-term MEMORY.md & daily logs for default bot
│       ├── dev_assistant/     # Long-term MEMORY.md & daily logs for dev bot
│       └── moderation_bot/    # Long-term MEMORY.md & daily logs for mod bot
├── workspace/
│   └── profiles/
│       ├── default/           # Filesystem & shell sandbox for default bot
│       ├── dev_assistant/     # Filesystem & shell sandbox for dev bot
│       └── moderation_bot/    # Filesystem & shell sandbox for mod bot
├── profiles.py                # Core profile model, registry, dependency puller, supervisor
├── profiles_api.py            # FastAPI REST API router (/api/v1/profiles)
├── main.py                    # Process entry point supporting `--profile <id>`
├── bot.py                     # Discord bot adapter
├── stoat_bot.py               # Stoat bot adapter
├── webui_server.py            # Local web server mounting /api/v1/profiles & tool execution
├── control_panel.py           # Tkinter GUI with Claude-style sidebar and Profiles tab
└── WIRING_DOCUMENTATION.md    # This file
```

---

## 3. Profile Schema (`.profiles/<id>.json`)

Each profile is serialized into a clean JSON structure:

```json
{
  "id": "dev_assistant",
  "name": "Dev Assistant Bot",
  "description": "Coding agent with python and data science tools",
  "icon": "⚡",
  "color": "#38bdf8",
  "enabled": true,

  "discord_enabled": true,
  "discord_token": "DISCORD_BOT_TOKEN_HERE",
  "discord_channels": "123456789012345678,987654321098765432",
  "discord_bot_mode": "WAKEWORD",
  "discord_wake_word": "hey dev",

  "stoat_enabled": false,
  "stoat_token": "",
  "stoat_channels": "",
  "stoat_bot_mode": "WAKEWORD",
  "stoat_wake_word": "hey dev",

  "agent_provider": "ollama",
  "agent_model": "gpt-oss:120b",
  "ollama_mode": "cloud",
  "ollama_host": "https://ollama.com",
  "api_key": "sk-primary-key-here",
  "api_keys": ["sk-fallback-key-1", "sk-fallback-key-2"],
  "anthropic_api_key": "",

  "agent_account_mode": "home_agent",
  "agent_workdir": "workspace/profiles/dev_assistant",
  "agent_run_as_user": "",
  "agent_allow_elevated": "0",

  "subagents_enabled": true,
  "subagent_model": "qwen2.5:7b",
  "subagent_max_parallel": 3,

  "integrations": ["discord_admin"],

  "dependencies": [
    "httpx",
    "pandas",
    "matplotlib",
    "beautifulsoup4"
  ],

  "auto_restart": true
}
```

---

## 4. How Multi-Instance Execution is Wired Up

### 4.1 Process Isolation & Spawning

When you start an instance from the Web UI or Control Panel:

1. **Supervisor Call**: `supervisor.start_instance(profile_id)`
2. **Environment Assembly**:
   - The profile's `to_env_dict()` builds a targeted dictionary of environment variables.
   - Sets `SYSTEMC_PROFILE_ID = profile_id`.
   - Injects that profile's specific `DISCORD_BOT_TOKEN`, `STOAT_BOT_TOKEN`, `AGENT_MODEL`, `OLLAMA_API_KEY`, etc.
3. **Subprocess Spawn**:
   - Spawns: `python main.py --profile <profile_id>` with the dedicated environment.
   - Popen streams `stdout` and `stderr` asynchronously into a dedicated `queue.Queue` per profile.
4. **Isolated Namespace**:
   - `sessions.py`: `get_history_dir()` redirects storage to `.history/profiles/<profile_id>/`.
   - `memory.py`: `_get_memory_dir()` redirects storage to `.memory/profiles/<profile_id>/`.
   - `tools.py`: `WORKDIR` redirects file tools & shell execution to `workspace/profiles/<profile_id>/`.
5. **Crash Protection & Supervision**:
   - If an instance crashes with non-zero exit code, `supervisor` checks `profile.auto_restart` and restarts after a 3-second backoff.
   - Intentional stops (`supervisor.stop_instance`) cleanly terminate the process tree.

### 4.2 Running 3 Bots Concurrently via CLI or Python

**Via Terminal:**
```bash
# Terminal 1: Start Bot 1 (e.g. General Assistant)
python main.py --profile default

# Terminal 2: Start Bot 2 (e.g. Coding Agent with Python packages)
python main.py --profile dev_assistant

# Terminal 3: Start Bot 3 (e.g. Moderation Bot with Discord Admin)
python main.py --profile moderation_bot
```

**Via Python Script (`supervisor`):**
```python
from profiles import manager, supervisor

# Launch all 3 bots concurrently
supervisor.start_instance("default")
supervisor.start_instance("dev_assistant")
supervisor.start_instance("moderation_bot")

# Check status
overview = supervisor.get_status_overview()
for bot in overview:
    print(f"[{bot['name']}] Running: {bot['running']} (PID: {bot['pid']})")
```

---

## 5. Per-Agent Dependency Pulling

Different agents require different Python libraries to execute tools (e.g., a data analyst bot needs `pandas` and `matplotlib`, while a web-scraping bot needs `beautifulsoup4` and `playwright`).

### How It Works:
1. Each profile declares a `dependencies: [...]` list.
2. In the UI or via CLI:
   ```bash
   python profiles.py install-deps dev_assistant
   ```
3. Or via REST API:
   ```http
   POST /api/v1/profiles/dev_assistant/install_deps
   ```
4. `manager.pull_dependencies(profile_id, log_queue)` executes:
   ```bash
   pip install <dependencies...>
   ```
   Streaming live logs directly into the supervisor log queue and returning the output status.
5. In addition, `supervisor.start_instance()` automatically verifies and installs missing profile dependencies prior to booting the bot process if configured!

---

## 6. Migration from Old `.env` Format

To avoid breaking existing user configurations:

### CLI Migration:
```bash
python profiles.py migrate .env
```

### Programmatic Migration:
```python
import profiles

ok, msg = profiles.manager.migrate_from_env(".env", target_id="default")
print(msg)
```

### What Happens:
1. The parser reads all existing keys from `.env`:
   - `DISCORD_BOT_TOKEN`, `ALLOWED_CHANNEL_IDS`, `BOT_MODE`, `WAKE_WORD`
   - `STOAT_BOT_TOKEN`, `STOAT_ALLOWED_CHANNEL_IDS`, `STOAT_BOT_MODE`, `STOAT_WAKE_WORD`
   - `AGENT_NAME`, `AGENT_MODEL`, `OLLAMA_MODE`, `OLLAMA_HOST`, `OLLAMA_API_KEY`, `OLLAMA_API_KEYS`
   - `AGENT_WORKDIR`, `AGENT_ACCOUNT_MODE`, `AGENT_RUN_AS_USER`, `AGENT_ALLOW_ELEVATED`
   - `INTEGRATIONS`, `SUBAGENTS`
2. Creates `.profiles/default.json` populated with these values.
3. Automatically sets `default` as the active profile in `.profiles/profiles.json`.
4. Saves a safety backup copy at `.env.legacy.backup`.

---

## 7. REST API Endpoints (`profiles_api.py`)

Mounted on the Local Web Server at `http://127.0.0.1:7842`:

| Endpoint | Method | Description |
|---|---|---|
| `/api/v1/profiles` | `GET` | List all profiles, active ID, and running statuses of all bots |
| `/api/v1/profiles` | `POST` | Create or update a bot profile |
| `/api/v1/profiles/{id}` | `GET` | Get details and status of a single profile |
| `/api/v1/profiles/{id}` | `DELETE` | Delete a profile (blocked if currently running) |
| `/api/v1/profiles/{id}/start` | `POST` | Start the bot process for this profile |
| `/api/v1/profiles/{id}/stop` | `POST` | Stop the bot process for this profile |
| `/api/v1/profiles/{id}/install_deps` | `POST` | Trigger pip install for the profile's custom dependencies |
| `/api/v1/profiles/migrate` | `POST` | Trigger migration of legacy `.env` into profiles |
| `/api/v1/profiles/{id}/logs` | `GET` | Fetch real-time streaming output logs for this bot |

---

## 8. Claude-Style Sidebar User Interface

The web application (`src/App.tsx`) and Control Panel GUI feature a vertical Claude-inspired layout:

- **Left Rail (Sidebar)**:
  - Top: SystemC Farink branding with version badge.
  - Profile Switcher Bar: Interactive tabs representing each configured bot (`🤖 Bot 1`, `⚡ Bot 2`, `🛡️ Bot 3`) with active status pings.
  - Navigation Sections:
    - 🤖 **Bot Profiles**: Instance manager (Start/Stop all, resource overview, clone profile).
    - ⚙️ **Agent Setup**: Name, provider, model selector, API key chain with fallback order.
    - 💬 **Platform Gateways**: Discord & Stoat credentials, channel filters, wake words.
    - 📦 **Agent Dependencies**: Custom pip package editor with one-click "Pull Dependencies" runner.
    - 🗂️ **Workspace & Sandbox**: Folder isolation, execution user, elevated permission toggle.
    - 🔄 **Migration Tool**: Visual diff and migration runner from `.env`.
    - 📜 **Live Logs & Approvals**: Streamed output with destructive command approval cards.
    - 📖 **Wiring Documentation**: Built-in interactive copy of this manual.
  - Bottom: Quick instance counters ("Bots Running: 2 / 3") and one-click "Download SystemC_Drovex.zip".

---

## 9. Verification & Quick Test Commands

Verify the setup inside `SystemC_Drovex`:

```bash
# 1. Check profiles list
python profiles.py list

# 2. Test migration from an existing .env
python profiles.py migrate

# 3. Pull dependencies for a profile
python profiles.py install-deps default

# 4. Run main with the profile argument
python main.py --profile default
```

Everything is fully self-contained, robustly tested, and packaged into the root zip archive for deployment.
