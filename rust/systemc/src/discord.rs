//! Discord gateway client (bot.py port).
//!
//! Minimal but real: connect over WSS, identify with the needed intents,
//! heartbeat on the server's schedule, dispatch MESSAGE_CREATE to the agent
//! and reconnect on any drop. Modes (BOT_MODE): WAKEWORD | MENTION |
//! EVERYTHING, ALLOWED_CHANNEL_IDS filter, `!command` shell shortcut and
//! `/reset` control command all behave like the Python version.

use crate::agent::Agent;
use crate::chat_platform::{download_attachment, Author, ChannelAdapter, DiscordRest};
use crate::config::getenv;
use crate::providers::Message;
use crate::sessions;
use crate::tools;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::sync::Arc;
use tokio_tungstenite::{connect_async, tungstenite::Message as WsMessage};

const GATEWAY_URL: &str = "wss://gateway.discord.gg/?v=10&encoding=json";

// intents: GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT.
// Approvals poll reactions over REST, so no reaction events needed.
const INTENTS: u64 = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15);

pub fn bot_mode() -> &'static str {
    match getenv("BOT_MODE").unwrap_or_else(|| "WAKEWORD".into()).to_ascii_uppercase().as_str() {
        "EVERYTHING" => "EVERYTHING",
        "MENTION" => "MENTION",
        _ => "WAKEWORD",
    }
}

fn wake_word() -> String {
    getenv("WAKE_WORD").unwrap_or_else(|| "hey claw".into()).to_ascii_lowercase()
}

fn allowed_channels() -> Vec<String> {
    getenv("ALLOWED_CHANNEL_IDS").unwrap_or_default()
        .split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

pub struct DiscordBot {
    token: String,
    rest: reqwest::Client,
}

struct FrameState {
    heartbeat_ms: u64,
    last_seq: Option<u64>,
    identified: bool,
    me_id: String,
}

impl DiscordBot {
    pub fn new(token: String) -> Self {
        DiscordBot {
            token,
            rest: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(60))
                .user_agent("systemc-rs/1.0 (github.com/TheRealHyperno)")
                .build()
                .unwrap_or_default(),
        }
    }

    pub async fn run(&self, agent: Arc<Agent>) {
        let mode = bot_mode();
        let wake = wake_word();
        let allowed = allowed_channels();
        println!("  mode:      {mode}");
        if mode == "WAKEWORD" {
            println!("  wake word: \"{wake}\"");
        }
        if allowed.is_empty() {
            println!("  WARNING: no ALLOWED_CHANNEL_IDS set — responding everywhere");
        } else {
            println!("  channels:  {}", allowed.join(", "));
        }

        loop {
            match self.gateway_session(&agent, &allowed, &mode, &wake).await {
                Ok(()) => eprintln!("[discord] gateway closed cleanly — reconnecting in 5s"),
                Err(e) => eprintln!("[discord] gateway error: {e} — reconnecting in 5s"),
            }
            tokio::time::sleep(std::time::Duration::from_secs(5)).await;
        }
    }

    async fn send_ws(&self, write: &mut (impl futures_util::Sink<WsMessage, Error = tokio_tungstenite::tungstenite::Error> + Unpin), v: Value) -> Result<(), String> {
        write
            .send(WsMessage::Text(v.to_string()))
            .await
            .map_err(|e| format!("ws send: {e}"))
    }

    async fn gateway_session(&self, agent: &Arc<Agent>, allowed: &[String], mode: &str, wake: &str) -> Result<(), String> {
        let (ws, _) = connect_async(GATEWAY_URL).await.map_err(|e| format!("connect: {e}"))?;
        let (mut write, mut read) = ws.split();
        let mut st = FrameState { heartbeat_ms: 41_250, last_seq: None, identified: false, me_id: String::new() };

        loop {
            let sleep = tokio::time::sleep(std::time::Duration::from_millis(st.heartbeat_ms));
            tokio::pin!(sleep);
            tokio::select! {
                _ = &mut sleep => {
                    self.send_ws(&mut write, json!({"op": 1, "d": st.last_seq})).await?;
                }
                msg = read.next() => {
                    let raw = match msg.ok_or("gateway closed")? {
                        Ok(m) => m,
                        Err(e) => return Err(format!("websocket: {e}")),
                    };
                    if let WsMessage::Text(ref t) = raw {
                        let v = match serde_json::from_str::<Value>(&t.to_string()) { Ok(v) => v, Err(_) => continue };
                        self.process_frame(v, agent, &mut write, allowed, mode, wake, &mut st).await?;
                    }
                }
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    async fn process_frame(
        &self,
        v: Value,
        agent: &Arc<Agent>,
        write: &mut (impl futures_util::Sink<WsMessage, Error = tokio_tungstenite::tungstenite::Error> + Unpin),
        allowed: &[String],
        mode: &str,
        wake: &str,
        st: &mut FrameState,
    ) -> Result<(), String> {
        match v["op"].as_i64() {
            Some(10) => { // HELLO
                st.heartbeat_ms = v["d"]["heartbeat_interval"].as_u64().unwrap_or(41_250);
                if st.identified {
                    self.send_ws(write, json!({"op": 6, "d": {"token": self.token, "seq": st.last_seq}})).await?;
                } else {
                    self.send_ws(write, json!({
                        "op": 2,
                        "d": {
                            "token": self.token,
                            "intents": INTENTS,
                            "properties": {"os": "linux", "browser": "systemc-rs", "device": "systemc-rs"},
                        }
                    })).await?;
                    st.identified = true;
                }
            }
            Some(11) => {} // heartbeat ACK
            Some(0) => {
                // dispatch
                if let Some(seq) = v["s"].as_u64() {
                    st.last_seq = Some(seq);
                }
                let t = v["t"].as_str().unwrap_or_default().to_string();
                if t == "READY" {
                    st.me_id = v["d"]["user"]["id"].as_str().unwrap_or_default().to_string();
                    let username = v["d"]["user"]["username"].as_str().unwrap_or_default();
                    println!("✓ logged in as {username} (id {})", st.me_id);
                }
                if t == "MESSAGE_CREATE" {
                    self.dispatch_message(agent, &v["d"], &st.me_id, allowed, mode, wake).await;
                }
            }
            Some(7) | Some(9) => {
                return Err("server asked for reconnect".into());
            }
            _ => {}
        }
        Ok(())
    }

    async fn dispatch_message(&self, agent: &Arc<Agent>, d: &Value, me_id: &str, allowed: &[String], mode: &str, wake: &str) {
        let content = d["content"].as_str().unwrap_or_default().to_string();
        if content.is_empty() {
            return; // attachments-only etc.
        }
        let author_bot = d["author"].get("bot").map(|b| b.as_bool().unwrap_or(false)).unwrap_or(false);
        if d["author"]["id"].as_str() == Some(me_id) || author_bot {
            return;
        }
        let channel_id = d["channel_id"].as_str().unwrap_or_default().to_string();
        if !allowed.is_empty() && !allowed.iter().any(|c| c == &channel_id) {
            return;
        }
        let author = Author {
            id: d["author"]["id"].as_str().unwrap_or_default().to_string(),
            name: d["author"]["global_name"].as_str().or(d["author"]["username"].as_str()).unwrap_or_default().to_string(),
        };
        let rest = DiscordRest {
            token: self.token.clone(),
            channel_id: channel_id.clone(),
            app_id: me_id.to_string(),
            label: label_for(d, &author),
        };

        // ── !command shortcut: bypass the LLM, run shell directly ────────
        if content.starts_with('!') && content.len() > 1 {
            let command = content[1..].trim().to_string();
            rest.typing().await;
            if tools::is_destructive(&command) {
                if !rest.request_approval(&command, false, 60).await {
                    return;
                }
            }
            let result = tools::run_shell(&command, false).await;
            let stdout = result["stdout"].as_str().unwrap_or_default().trim().to_string();
            let exit = result["exit_code"].as_i64().unwrap_or(-999);
            let out = if stdout.is_empty() { "(no output)".to_string() } else { tail(&stdout, 1900) };
            rest.send_message(&format!("```\n{out}\n```\nexit: `{exit}`")).await;
            return;
        }

        // ── /reset: clear this channel's conversation context ────────────
        let stripped = content
            .replace(&format!("<@{me_id}>"), "")
            .replace(&format!("<@!{me_id}>"), "")
            .trim()
            .to_string();
        if stripped.eq_ignore_ascii_case("/reset") {
            if sessions::reset(&sessions::make_key("discord", &channel_id)).is_ok() {
                rest.send_message("🧹 Context cleared for this channel. Long-term memory is untouched.").await;
            } else {
                rest.send_message("⚠️ couldn't clear the session.").await;
            }
            return;
        }

        // ── should the agent respond? ─────────────────────────────────────
        let mentioned = content.contains(&format!("<@{me_id}>")) || content.contains(&format!("<@!{me_id}>"));
        let responds = match mode {
            "EVERYTHING" => true,
            "MENTION" => mentioned,
            _ => mentioned || content.to_ascii_lowercase().starts_with(wake),
        };
        if !responds {
            return;
        }

        // strip the wake word off the front (mention already stripped by /reset pass)
        let mut prompt = stripped;
        if prompt.to_ascii_lowercase().starts_with(wake) {
            prompt = prompt[wake.len()..].trim().to_string();
        }
        if prompt.is_empty() {
            prompt = content.clone();
        }

        rest.typing().await;

        // attachments auto-download into the workspace
        let mut saved: Vec<String> = Vec::new();
        if let Some(atts) = d["attachments"].as_array() {
            for att in atts.clone() {
                if let (Some(url), Some(name)) = (att["url"].as_str(), att["filename"].as_str()) {
                    match download_attachment(&self.rest, url, name, &tools::workdir()).await {
                        Ok(p) => saved.push(p),
                        Err(e) => eprintln!("[discord] attachment download failed: {e}"),
                    }
                }
            }
        }
        if !saved.is_empty() {
            prompt.push_str(&format!("\n\n[Attached files saved to workspace: {}]", saved.join(", ")));
        }

        let key = sessions::make_key("discord", &channel_id);
        sessions::begin(&key); // refresh the in-process cache
        let mut history: Vec<Message> = sessions::load_history(&key);
        let channel = ChannelAdapter::Discord(rest.clone());
        if let Err(e) = agent.run_turn(channel, &prompt, &mut history, Some(&author)).await {
            eprintln!("[bot] agent error: {e}");
            rest.send_message(&format!("⚠️ {e}")).await;
            return;
        }

        // trim to the last 60 messages, snapped forward to a "user" boundary
        // (a flat slice can cut a tool message loose from its assistant
        // tool-call — providers reject that as an orphaned tool result;
        // "user" messages are always safe turn boundaries)
        let start = history.len().saturating_sub(60);
        let mut t: Vec<Message> = history[start..].to_vec();
        while !t.is_empty() && t[0].role != "user" {
            t.remove(0);
        }
        sessions::save_history(&key, &t);
        sessions::set_label(&key, &rest.label);
    }
}

fn label_for(d: &Value, author: &Author) -> String {
    match d["guild_id"].as_str() {
        Some(_) => {
            let ch = d["channel_name"].as_str().unwrap_or_default();
            if ch.is_empty() { "#".to_string() } else { format!("#{ch}") }
        }
        None => format!("DM with {}", author.name),
    }
}

fn tail(s: &str, n: usize) -> String {
    if s.chars().count() <= n { s.to_string() } else { s.chars().skip(s.chars().count() - n).collect() }
}
