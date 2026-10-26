# systemc (C++ edition)

The C++ rewrite of SystemC's Python core — codenamed **Aurelia-CPP (beta)**.
Same agent, same .env keys, same `.memory/` and `.history/` data formats as the
Python version, compiled down to one static-ish binary against OpenSSL.

```
systemc run                     start the Discord agent (+ /reset, !command)
systemc panel [--port N]        web control panel app (default port 7842)
systemc chat                    local terminal agent (no Discord needed)
systemc models                  list models on the configured Ollama host
systemc service install|uninstall|status [--profile] [--linger] [--system]
```

## What's ported
- agent loop with tools (shell / sudo / python / files / web_search / remember /
  reset_session / send_message / send_file / fetch_channel_messages),
  destructive-command approval gate, subagents (parallel via std::async)
- providers: Ollama local + Ollama Cloud (priority API-key pool with
  429/402/401/403 cooldowns) and Anthropic
- Discord bot: WSS gateway on raw OpenSSL (no websocket dep), REST send/typing/
  attachments/react-approval, WAKEWORD / MENTION / EVERYTHING modes,
  ALLOWED_CHANNEL_IDS, `!cmd` shortcut, `/reset`
- control panel web app (single embedded HTML page + JSON API): sessions,
  memory, tool runner, chat proxy, service install
- systemd support: user/system unit writer + enable/linger

## Build

```bash
cmake -B build && cmake --build build
# or directly:
g++ -std=c++20 -O2 -Isrc -Ivendor src/*.cpp -lssl -lcrypto -lpthread -o systemc
```

No libcurl needed — HTTPS and the Discord gateway are built directly on
OpenSSL. Same `.env` keys as the Python/Rust versions (AGENT_PROVIDER,
AGENT_MODEL, OLLAMA_HOST/OLLAMA_API_KEY(S), DISCORD_BOT_TOKEN, ...).
