# SystemC Drovex — Java port

A Java/JavaScript port of TheRealHyperno/systemcontrol-agent (Python). The agent,
Discord bot, Stoat bot, HTTP server and control panel are all Java now (port 7842
web API and TUI behaviour are compatible with the original); the local web console
`webui.html` is the HTML/CSS/JS part.

## Build

```bash
mvn -q package -DskipTests     # -> target/systemc-java.jar (fat jar)
```

## Run

```bash
cp example.env .env            # then edit
java -jar target/systemc-java.jar
java -cp target/systemc-java.jar systemc.ControlPanel   # Swing control panel (desktop)
systemc tui                                            # TUI control panel (SSH / no desktop)
java -cp target/systemc-java.jar systemc.Providers models   # list Ollama models
systemc service install        # start the bots at login/boot (systemd user unit)
systemc update --check         # check GitHub's Latest release for a newer version
systemc config                 # backend (java|python) / version / repo — set with: config set KEY VALUE
```

The Swing control panel does full first-run setup: the **Setup** tab writes your
Discord/Stoat tokens, allowed channels, bot modes and wake words, the Ollama
source (local/cloud) with an ordered key pool and model picker, and the
workspace/account mode (run as another user via `runuser`, ACL grants, elevated
permission) into `.env`. **Profiles** manages multi-instance profiles
(New / Import archive / Export archive / Migrate .env, per-profile Start and
Pull Dependencies). **Run & Logs** starts/stops instances with auto-restart and
tails their logs. **Dependencies** checks the environment (java/ollama/pip/tar)
and pulls per-profile pip packages. **Updates** checks GitHub releases, installs
(v1.0.6 adds `systemc update` as the CLI equivalent), and rolls back from
backups. The **backend** choice is simply "do you want java or python" — asked
at setup, switched with `systemc config set backend python|java`.

Integrations (extra agent tools as Java SPI jars or python-compat `.py` files,
with settings menus in the Swing panel / TUI / web API) — see
[INTEGRATIONS.md](INTEGRATIONS.md). Full documentation of everything (install,
config keys, architecture, tools, approvals, sessions, systemd, troubleshooting)
is in [DOCS.md](DOCS.md).

The TUI control panel configures the same .env keys as the Swing panel — it runs
on any TTY or over SSH with no desktop environment (arrow keys + Enter; q backs out).

## One-liner install / release process

Build the distributable (dist.sh → `target/systemc-java-linux-x64.tar.gz` + `.sha256`):

```bash
./dist/dist.sh
```

To serve a `curl | bash` installer from project's GitHub Pages + releases:

1. `git subtree push --prefix dist origin gh-pages` (or copy `dist/install.sh` into a `gh-pages` branch
   as `install.sh`) — then the URL is `https://<user>.github.io/<repo>/install.sh`.
2. `cd dist && gh release create v1.0.x systemc-java-linux-x64.tar.gz systemc-java-linux-x64.tar.gz.sha256 --notes '...'`
   from the repo root: `gh release upload v1.0.x target/systemc-java-linux-x64.tar.gz*`.
3. Users run:
   `curl -fsSL https://<user>.github.io/<repo>/install.sh | bash`
   which installs into `~/.systemc`, links `systemc` into `~/.local/bin` and verifies sha256.
   Overrides: `PREFIX=... VERSION=v1.0.x REPO=user/repo` (and `DISTRIBUTION_URL=...` for testing).

## .env keys (same names as the Python version)

| Key | Meaning |
|---|---|
| `DISCORD_BOT_TOKEN` / `STOAT_BOT_TOKEN` | bots (each optional) |
| `ALLOWED_CHANNEL_IDS` / `STOAT_ALLOWED_CHANNEL_IDS` | restrict channels |
| `BOT_MODE` / `STOAT_BOT_MODE` | `WAKEWORD` (default) / `MENTION` / `EVERYTHING` |
| `WAKE_WORD` / `STOAT_WAKE_WORD` | default "hey claw" |
| `AGENT_MODEL`, `AGENT_PROVIDER` | `ollama` (default) or `anthropic` |
| `OLLAMA_HOST`, `OLLAMA_API_KEY(S)` | local ollama / Ollama Cloud keys (pool) |
| `ANTHROPIC_API_KEY` | for `AGENT_PROVIDER=anthropic` |
| `AGENT_WORKDIR` | workspace (default `./workspace`) |
| `AGENT_ALLOW_ELEVATED` | offer run_shell_elevated (sudo, approval-gated) |
| `APPROVAL_MODE`, `APPROVAL_COMMANDS` | ask / approve_all / deny_all + token list |
| `AGENT_*` personalization | name, gender, pronouns, system prompt, casual tone |
| `SUBAGENTS`, `SUBAGENT_MODEL`, `SUBAGENT_MAX_*` | subagent knobs |
| `SYSTEMC_WEBSITE`, `WEBUI_PORT` | local web UI (default on, port 7842) |
| `SYSTEMC_API_TOKEN` | bearer token for `/api/v1/*` |
| `SYSTEMC_PROFILE_ID` | multi-instance profiles |
| `STOAT_API_BASE` / `STOAT_WS_BASE` / `STOAT_AUTUMN_BASE` | Stoat endpoints (ideal if the instance differs) |

## State layout (unchanged)

```
.history/<id>.json (+ .reset, .label)   per-channel conversations
.memory/MEMORY.md + .memory/daily/*.md  long-term + daily memory
.approvals/*.json                       approval queue (control panel resolves)
.profiles/profiles.json                 multi-instance profiles
workspace/                              agent's shell/file workdir
```

## Deltas vs the Python original

- **Stoat improvements**: the Java stoat client gets `!command` shell shortcut,
  `/reset`, attachment download and in-chat y/n approvals (the Python one only
  had wake/mention routing).
- Shell runs on a real PTY via GNU `script -qfec` when installed; otherwise a
  piped fallback (identical result shape).
- Not ported: the auto-updater (updater.py/profile_transfer.py), the integrations
  plugin API and OpenClaw import. Everything else has a counterpart.
- The panel is a FlatLaf "macOS Light" Swing app instead of tkinter.
