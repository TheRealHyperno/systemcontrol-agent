//! Chat platform abstraction (chat_platform.py port) plus the Discord REST
//! half: sending messages, typing, attachments and the reaction-based
//! approval gate. Concrete enum instead of a trait object — the Rust port
//! supports Discord and a local CLI channel.

use serde_json::{json, Value};
use std::time::Duration;

pub const MAX_MESSAGE_LEN: usize = 2000;

#[derive(Clone, Debug)]
pub struct Author {
    pub id: String,
    pub name: String,
}

/// "[from: Name (@username, id N)]:" tag prefixed onto each user prompt.
/// It is boilerplate the agent gets — never part of what the person typed.
pub fn sender_tag(author: &Author) -> String {
    format!("[from: {} (@{}, id {})]:", author.name, author.name, author.id)
}

#[derive(Clone)]
pub enum ChannelAdapter {
    Discord(DiscordRest),
    Cli,
}

impl ChannelAdapter {
    pub fn platform(&self) -> &'static str {
        match self {
            ChannelAdapter::Discord(_) => "discord",
            ChannelAdapter::Cli => "cli",
        }
    }
    pub fn id(&self) -> String {
        match self {
            ChannelAdapter::Discord(d) => d.channel_id.clone(),
            ChannelAdapter::Cli => "cli".into(),
        }
    }
    pub fn label(&self) -> String {
        match self {
            ChannelAdapter::Discord(d) => d.label.clone(),
            ChannelAdapter::Cli => "terminal".into(),
        }
    }

    pub async fn send(&self, content: &str) {
        match self {
            ChannelAdapter::Discord(d) => d.send_message(content).await,
            ChannelAdapter::Cli => println!("\x1b[1;36m{}\x1b[0m", content),
        }
    }

    pub async fn send_file(&self, path: &str, caption: &str) -> Value {
        match self {
            ChannelAdapter::Discord(d) => d.send_file(path, caption).await,
            ChannelAdapter::Cli => {
                println!("\x1b[1;36m[file] {path} — {caption}\x1b[0m");
                json!({"sent": true})
            }
        }
    }

    pub async fn typing(&self) {
        if let ChannelAdapter::Discord(d) = self {
            d.typing().await;
        }
    }

    /// Post the command and poll for ✅ / ❌ reactions. True only on approval.
    pub async fn request_approval(&self, command: &str, elevated: bool, timeout_secs: u64) -> bool {
        match self {
            ChannelAdapter::Discord(d) => d.request_approval(command, elevated, timeout_secs).await,
            ChannelAdapter::Cli => {
                eprintln!("\n⚠️  APPROVAL REQUESTED{}:\n{}",
                    if elevated { " [ELEVATED / sudo]" } else { "" }, command);
                eprint!("Type 'ok' to approve, anything else to deny: ");
                use std::io::Write;
                let _ = std::io::stderr().flush();
                let mut line = String::new();
                if std::io::stdin().read_line(&mut line).is_ok() && line.trim().eq_ignore_ascii_case("ok") {
                    true
                } else {
                    false
                }
            }
        }
    }

    pub async fn history(&self, limit: usize) -> Vec<Value> {
        match self {
            ChannelAdapter::Discord(d) => d.fetch_history(limit).await,
            ChannelAdapter::Cli => vec![],
        }
    }
}

/// Discord REST client for one channel.
#[derive(Clone)]
pub struct DiscordRest {
    pub token: String,
    pub channel_id: String,
    pub app_id: String, // the bot's user id, needed to exclude its own reactions
    pub label: String,
}

impl DiscordRest {
    fn http(&self) -> reqwest::Client {
        reqwest::Client::builder()
            .timeout(Duration::from_secs(30))
            .user_agent("systemc-rs/1.0 (github.com/TheRealHyperno)")
            .build()
            .unwrap_or_default()
    }

    fn base(&self) -> String {
        "https://discord.com/api/v10".into()
    }

    async fn api(&self, method: reqwest::Method, path: &str, body: Option<Value>) -> Result<Value, String> {
        let url = format!("{}{}", self.base(), path);
        let mut req = self.http().request(method, &url).bearer_auth(&self.token);
        if let Some(b) = body {
            req = req.json(&b);
        }
        let resp = req.send().await.map_err(|e| format!("discord api: {e}"))?;
        let code = resp.status();
        let text = resp.text().await.unwrap_or_default();
        if code.is_success() {
            Ok(serde_json::from_str(&text).unwrap_or(Value::Null))
        } else {
            Err(format!("discord HTTP {}: {}", code.as_u16(), text.chars().take(300).collect::<String>()))
        }
    }

    pub async fn send_message(&self, content: &str) {
        // split at 2000 chars, preferring to break on newlines
        for chunk in chunk_message(content) {
            let r = self.api(reqwest::Method::POST, &format!("/channels/{}/messages", self.channel_id), Some(json!({"content": chunk}))).await;
            if let Err(e) = r {
                eprintln!("[discord] send failed: {e}");
                tokio::time::sleep(Duration::from_secs(2)).await;
            }
        }
    }

    pub async fn send_file(&self, path: &str, caption: &str) -> Value {
        let data = match tokio::fs::read(path).await {
            Ok(d) => d,
            Err(e) => {
                eprintln!("[discord] couldn't read file for upload: {e}");
                return json!({"error": e.to_string()});
            }
        };
        let filename = std::path::Path::new(path)
            .file_name()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_else(|| "file.bin".into());
        let form = reqwest::multipart::Form::new()
            .text("payload_json", serde_json::to_string(&json!({"content": caption })).unwrap_or_default())
            .part("files[0]", reqwest::multipart::Part::bytes(data).file_name(filename));
        let url = format!("{}/channels/{}/messages", self.base(), self.channel_id);
        let resp = self.http().post(&url).bearer_auth(&self.token).multipart(form).send().await;
        match resp {
            Ok(r) => {
                let ok = r.status().is_success();
                let body: Value = r.json().await.unwrap_or(Value::Null);
                if ok { json!({"sent": true, "message": body["id"]}) } else { json!({"error": format!("HTTP {}", body)})}
            }
            Err(e) => json!({"error": e.to_string()}),
        }
    }

    pub async fn typing(&self) {
        let _ = self.api(reqwest::Method::POST, &format!("/channels/{}/typing", self.channel_id), None).await;
    }

    /// Most recent messages, oldest first. {id, author, content, timestamp}
    pub async fn fetch_history(&self, limit: usize) -> Vec<Value> {
        let list = self
            .api(reqwest::Method::GET, &format!("/channels/{}/messages?limit={}", self.channel_id, limit.clamp(1, 100)), None)
            .await
            .unwrap_or(Value::Null);
        let arr = list.as_array().cloned().unwrap_or_default();
        arr.iter().rev()
            .map(|m| json!({
                "id": m["id"],
                "author": m["author"]["username"],
                "content": m["content"],
                "timestamp": m["timestamp"],
            }))
            .collect()
    }

    /// Post the command and poll for ✅ / ❌ reactions (non-bot users only,
    /// via `type=1`). Only an explicit ✅ approves.
    pub async fn request_approval(&self, command: &str, elevated: bool, timeout_secs: u64) -> bool {
        let title = if elevated { "⚠️ ELEVATED (sudo) — approve this command?" } else { "⚠️ Approve this command?" };
        let body = format!(
            "{title}\n```{}```\nReact ✅ to approve or ❌ to deny. Auto-deny in {timeout_secs}s.",
            trim(command, 1500)
        );
        let msg = match self
            .api(reqwest::Method::POST, &format!("/channels/{}/messages", self.channel_id), Some(json!({"content": body})))
            .await
        {
            Ok(m) => m,
            Err(e) => {
                eprintln!("[approval] couldn't post approval request: {e}");
                return false;
            }
        };
        let mid = msg["id"].as_str().unwrap_or_default().to_string();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(timeout_secs);
        let yes = emoji_path("✅");
        let no = emoji_path("❌");
        while tokio::time::Instant::now() < deadline {
            tokio::time::sleep(Duration::from_secs(2)).await;
            let no_hit = self.has_reaction(&mid, &no).await;
            if no_hit {
                // someone said no: stop asking right away
                let _ = self.api(
                    reqwest::Method::PATCH,
                    &format!("/channels/{}/messages/{}", self.channel_id, mid),
                    Some(json!({"content": "❌ Denied."})),
                ).await;
                return false;
            }
            if self.has_reaction(&mid, &yes).await {
                return true;
            }
        }
        let _ = self.api(reqwest::Method::DELETE, &format!("/channels/{}/messages/{}", self.channel_id, mid), None).await;
        false
    }

    /// Any non-bot user reaction with this emoji on the message?
    async fn has_reaction(&self, mid: &str, emoji: &str) -> bool {
        let url = format!(
            "{}/channels/{}/messages/{}/reactions/{}?type=1&limit=100",
            self.base(),
            self.channel_id,
            mid,
            emoji
        );
        match self.http().get(&url).bearer_auth(&self.token).send().await {
            Ok(r) => {
                let users: Vec<Value> = r.json().await.unwrap_or_default();
                users.iter().any(|u| u["id"].as_str() != Some(self.app_id.as_str()))
            }
            Err(_) => false,
        }
    }
}

/// "encoding=✅" → the URL path Discord expects for the reactions endpoint.
fn emoji_path(e: &str) -> String {
    percent_encoding::utf8_percent_encode(e, percent_encoding::NON_ALPHANUMERIC).to_string()
}

fn trim(s: &str, n: usize) -> String {
    if s.chars().count() <= n { s.to_string() } else { s.chars().take(n).collect::<String>() + "…" }
}

/// Split into Discord-sized chunks, preferring newline breaks.
pub fn chunk_message(content: &str) -> Vec<String> {
    let mut chunks = Vec::new();
    let mut rest = content;
    while rest.chars().count() > MAX_MESSAGE_LEN {
        let cut = rest.char_indices()
            .map(|(i, _)| i)
            .filter(|i| *i <= MAX_MESSAGE_LEN)
            .map(|i| (i, rest[..i].rfind('\n')))
            .last()
            .map(|(i, nl)| nl.unwrap_or(i))
            .unwrap_or(MAX_MESSAGE_LEN);
        let (head, tail) = rest.split_at(cut);
        chunks.push(head.trim_end().to_string());
        rest = &tail[tail.starts_with('\n') as usize..];
    }
    chunks.push(rest.to_string());
    chunks
}

/// Download an attachment into the workspace; returns the saved relative path.
pub async fn download_attachment(client: &reqwest::Client, url: &str, filename: &str, workdir: &std::path::Path) -> Result<String, String> {
    let resp = client.get(url).send().await.map_err(|e| e.to_string())?;
    let bytes = resp.bytes().await.map_err(|e| e.to_string())?;
    let safe = filename.replace('/', "_").replace("..", "_");
    let dest = workdir.join(&safe);
    tokio::fs::write(&dest, &bytes).await.map_err(|e| e.to_string())?;
    Ok(safe)
}
