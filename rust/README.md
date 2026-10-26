# SystemC — Rust rewrite

**python not fast enough so we using rust now.**

This is `the` Rust port of SystemC's core: one compiled binary, no pip, none of
the Python runtime overhead. The Python version stays on `main` unchanged; this
tree is self-contained.

## Build

```sh
cargo build --release
# binary: target/release/systemc
```

## What's in the port

| Python | Rust |
| --- | --- |
| `main.py` / `bot.py` (Discord bot, gateway, `!cmd`, `/reset`) | `systemc run` (`src/discord.rs`, native WSS gateway) |
| `agent.py` (tool-call loop, subagents) | `systemc run --chat` / `systemc chat` (`src/agent.rs`, tokio tasks) |
| `providers.py` (Ollama local/cloud + key pool, Anthropic) | `src/providers.rs` (same API-key cooldown/fallback logic) |
| `tools.py` (shell/python/files/web_search, approval gate) | `src/tools.rs` |
| `memory.py` (`.memory/` daily log + MEMORY.md) | `src/memory.rs` |
| `sessions.py` (`.history/` per-channel JSON, cross-process `/reset`) | `src/sessions.rs` |
| `webui_server.py` + control panel API | `systemc panel` (`src/panel.rs`, embedded axum UI on :7842) |
| `systemd_service.py` | `systemc service install/uninstall/status` (`src/service.rs`) |
| `setup wizard` | `systemc chat` for a zero-Discord smoke test |

.env keys are the same as the Python version (`DISCORD_BOT_TOKEN`,
`ALLOWED_CHANNEL_IDS`, `BOT_MODE`, `WAKE_WORD`, `OLLAMA_API_KEY(S)`,
`OLLAMA_HOST`, `AGENT_MODEL`, `AGENT_PROVIDER`, `AGENT_WORKDIR`, ...).
Set `SYSTEMC_HOME` to point the binary at a specific .env/workspace.

## Run

```sh
systemc chat                 # terminal agent, no Discord needed
systemc run                  # Discord bot
systemc panel                # control panel web app → http://localhost:7842
systemc service install      # systemd --user unit, started immediately
systemc service install --linger   # ...and at boot without login
systemc service install --system   # system-wide unit (needs root)
systemc models               # list models on the configured Ollama host
```

## systemd

`systemc service install [--profile <id>] [--linger] [--system]` writes a unit
file (`systemc.service` / `systemc-<profile>.service`), runs
`systemctl [--user] daemon-reload` and `enable --now`. Logs:

```sh
journalctl --user -f -u systemc
```

## Notes / differences from the Python version

- Discord gateway: MESSAGE_CONTENT intent is **privileged** — enable it in the
  Discord bot settings, same as before.
- Approvals (destructive/elevated commands, foreign-session resets) post to
  Discord and poll ✅/❌ reactions, then auto-deny on timeout.
- Shell runs piped (no PTY emulation) — same `{stdout, exit_code, cwd}` shape.
- Integrations/plugins, profiles and the Stoat bot are **not ported yet** —
  those stay Python-only until they get rewritten here.
