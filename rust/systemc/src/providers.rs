//! The LLM backends, ported from providers.py. One interface for the agent loop:
//!
//!   user message:      {"role": "user", "content": "text"}
//!   assistant message: {"role": "assistant", "content": text|null, "tool_calls": [...]}
//!   tool result:       {"role": "tool", "tool_call_id", "name", "content": text}
//!
//! Ollama backend talks to a local server or Ollama Cloud with an ordered key
//! pool (a failed key is parked on a cooldown and the next one takes over).

use crate::config::getenv;
use serde_json::{json, Value};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

pub const OLLAMA_LOCAL_HOST: &str = "http://localhost:11434";
pub const OLLAMA_CLOUD_HOST: &str = "https://ollama.com";

// ── messages ────────────────────────────────────────────────────────────────
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub input: Value,
}

#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
pub struct Message {
    pub role: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub content: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub tool_calls: Vec<ToolCall>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
}

impl Message {
    pub fn user(text: impl Into<String>) -> Self {
        Message { role: "user".into(), content: Some(text.into()), tool_calls: vec![], tool_call_id: None, name: None }
    }
    pub fn tool_result(call_id: &str, name: &str, content: String) -> Self {
        Message { role: "tool".into(), content: Some(content), tool_calls: vec![], tool_call_id: Some(call_id.into()), name: Some(name.into()) }
    }
}

// ── Ollama settings ─────────────────────────────────────────────────────────
pub fn collect_api_keys() -> Vec<String> {
    let mut keys = Vec::new();
    for chunk in [getenv("OLLAMA_API_KEY").unwrap_or_default(), getenv("OLLAMA_API_KEYS").unwrap_or_default()] {
        for k in chunk.split(|c: char| c == ',' || c.is_whitespace() || c == ';') {
            if !k.is_empty() && !keys.iter().any(|x| x == k) {
                keys.push(k.to_string());
            }
        }
    }
    keys
}

pub struct OllamaSettings {
    pub mode: &'static str,
    pub host: String,
    pub api_keys: Vec<String>,
}

pub fn ollama_settings() -> OllamaSettings {
    let api_keys = collect_api_keys();
    let host_raw = getenv("OLLAMA_HOST").unwrap_or_default().trim().trim_end_matches('/').to_string();
    let mode_env = getenv("OLLAMA_MODE").unwrap_or_default().trim().to_ascii_lowercase();
    let mode_is_cloud = match mode_env.as_str() {
        "cloud" => Some(true),
        "local" => Some(false),
        _ => None,
    };
    let cloud = mode_is_cloud.unwrap_or_else(|| !api_keys.is_empty() && (host_raw.is_empty() || host_raw.contains("ollama.com")));
    if cloud {
        let host = if !host_raw.is_empty() && !host_raw.contains("localhost") && !host_raw.contains("127.0.0.1") {
            host_raw
        } else {
            OLLAMA_CLOUD_HOST.to_string()
        };
        OllamaSettings { mode: "cloud", host, api_keys }
    } else {
        let host = if host_raw.is_empty() { OLLAMA_LOCAL_HOST.to_string() } else { host_raw };
        OllamaSettings { mode: "local", host, api_keys: vec![] } // never send a cloud key to a local server
    }
}

// ── API key pool: use keys in order, fall back when one fails ───────────────
fn cooldown_for(code: u16) -> Duration {
    match code {
        429 => Duration::from_secs(15 * 60), // usage / rate limit hit
        402 => Duration::from_secs(60 * 60), // no usage included / out of credits
        401 | 403 => Duration::from_secs(24 * 3600),
        _ => Duration::from_secs(900),
    }
}

fn reason(code: u16) -> String {
    match code {
        429 => "hit its usage limit (429)".into(),
        402 => "has no usage left for this model (402)".into(),
        401 | 403 => format!("was rejected ({code})"),
        c => format!("failed (HTTP {c})"),
    }
}

fn mask(key: &str) -> String {
    if key.len() > 4 { format!("…{}", &key[key.len() - 4..]) } else { "…".into() }
}

struct PoolState {
    until: Vec<Option<(Instant, u16)>>,
    notices: Vec<String>,
}

pub struct KeyPool {
    keys: Vec<String>,
    state: Mutex<PoolState>,
}

impl KeyPool {
    pub fn new(keys: Vec<String>) -> Self {
        let n = keys.len();
        KeyPool { keys, state: Mutex::new(PoolState { until: vec![None; n], notices: vec![] }) }
    }

    fn idx_of(&self, key: &str) -> usize {
        self.keys.iter().position(|k| k == key).unwrap_or(0)
    }

    fn free(&self) -> Option<usize> {
        let st = self.state.lock().unwrap();
        let now = Instant::now();
        st.until.iter().position(|u| match u { Some((at, _)) => *at <= now, None => true })
    }

    fn mark_ok(&self, idx: usize) {
        self.state.lock().unwrap().until[idx] = None;
    }

    fn mark_bad(&self, idx: usize, code: u16) -> String {
        let mut st = self.state.lock().unwrap();
        let now = Instant::now();
        if let Some((at, _)) = st.until[idx] {
            if at > now {
                return String::new(); // another thread already parked this key
            }
        }
        st.until[idx] = Some((now + cooldown_for(code), code));
        let nxt = st.until.iter().enumerate()
            .find(|(j, u)| *j != idx && matches!(u, Some((at, _)) if *at <= now) || (u.is_none() && *j != idx))
            .map(|(j, _)| j);
        let msg = match nxt {
            Some(n) => format!(
                "🔑 API key #{} ({}) {} — switching to key #{} ({}).",
                idx + 1, mask(&self.keys[idx]), reason(code), n + 1, mask(&self.keys[n])),
            None => format!(
                "🔑 API key #{} ({}) {}. No other keys available.",
                idx + 1, mask(&self.keys[idx]), reason(code)),
        };
        st.notices.push(msg.clone());
        msg
    }

    fn drain_notices(&self) -> Vec<String> {
        let mut st = self.state.lock().unwrap();
        std::mem::take(&mut st.notices)
    }

    fn exhausted_message(&self, host: &str, model: &str) -> String {
        if self.keys.len() == 1 {
            return explain_key_failure(-1, host, model);
        }
        let mut parts = Vec::new();
        for (i, k) in self.keys.iter().enumerate() {
            if let Some((at, code)) = self.state.lock().unwrap().until[i] {
                let left = at.saturating_duration_since(Instant::now()).as_secs() / 60 + 1;
                parts.push(format!("#{} ({}), HTTP {}, retry in ~{} min", i + 1, mask(k), code, left));
            }
        }
        format!(
            "All {} Ollama API keys are unavailable right now: {}. Add credits or another key at https://ollama.com/settings/keys, or pick a model included in your plan ('{model}' may not be).",
            self.keys.len(), parts.join("; ")
        )
    }
}

fn explain_key_failure(_code: i32, host: &str, model: &str) -> String {
    format!(
        "Ollama request failed against {host}. '{model}' may not be available: check your key at https://ollama.com/settings/keys or pick another model."
    )
}

static POOL: OnceLock<LazyKeyPool> = OnceLock::new();
struct LazyKeyPool(KeyPool);

fn get_pool(keys: &[String]) -> &'static LazyKeyPool {
    POOL.get_or_init(|| LazyKeyPool(KeyPool::new(keys.to_vec())))
}

pub fn drain_key_notices() -> Vec<String> {
    match POOL.get() {
        Some(p) => p.0.drain_notices(),
        None => vec![],
    }
}

fn http() -> &'static reqwest::Client {
    static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .timeout(Duration::from_secs(240))
            .build()
            .expect("failed to build HTTP client")
    })
}

pub async fn list_ollama_models(host: &str, api_key: &str) -> Result<Vec<String>, String> {
    let host = if host.is_empty() { OLLAMA_CLOUD_HOST } else { host.trim_end_matches('/') }.to_string();
    let mut req = http().get(format!("{host}/api/tags"));
    if !api_key.is_empty() {
        req = req.bearer_auth(api_key);
    }
    let resp = req.send().await.map_err(|e| format!("Couldn't reach {host}: {e}"))?;
    let code = resp.status().as_u16();
    if code == 401 || code == 403 {
        return Err(format!("{host} rejected the API key (HTTP {code}). Create one at https://ollama.com/settings/keys"));
    }
    if !resp.status().is_success() {
        return Err(format!("{host} returned HTTP {code}"));
    }
    let data: Value = resp.json().await.map_err(|e| format!("bad response: {e}"))?;
    let mut names: Vec<String> = data["models"].as_array().map(|a| {
        a.iter().filter_map(|m| {
            m["model"].as_str().or(m["name"].as_str()).map(String::from)
        }).filter(|s| !s.is_empty()).collect()
    }).unwrap_or_default();
    names.sort();
    Ok(names)
}

fn cloud_model_id(model: &str) -> String {
    for suffix in ["-cloud", ":cloud"] {
        if let Some(stripped) = model.strip_suffix(suffix) {
            return stripped.to_string();
        }
    }
    model.to_string()
}

fn to_openai_tool_schema(schema: &Value) -> Value {
    json!({
        "type": "function",
        "function": {
            "name": schema["name"],
            "description": schema.get("description").cloned().unwrap_or(json!("")),
            "parameters": schema.get("input_schema").cloned().unwrap_or(json!({"type": "object", "properties": {}})),
        },
    })
}

// ── Providers ───────────────────────────────────────────────────────────────
pub enum Provider {
    Ollama(OllamaProvider),
    Anthropic(AnthropicProvider),
}

impl Provider {
    pub fn model(&self) -> String {
        match self {
            Provider::Ollama(p) => p.model.clone(),
            Provider::Anthropic(p) => p.model.clone(),
        }
    }
    pub async fn step(&self, system_prompt: &str, history: &[Message], tool_schemas: &[Value]) -> Result<Message, String> {
        match self {
            Provider::Ollama(p) => p.step(system_prompt, history, tool_schemas).await,
            Provider::Anthropic(p) => p.step(system_prompt, history, tool_schemas).await,
        }
    }
}

pub fn describe() -> (String, String) {
    // (model, source) as used by the prompt's {MODEL}/{SOURCE}
    let kind = getenv("AGENT_PROVIDER").unwrap_or_else(|| "ollama".into()).to_ascii_lowercase();
    if kind == "anthropic" {
        return (getenv("AGENT_MODEL").unwrap_or_else(|| "claude-sonnet-4-6".into()), "Anthropic".into());
    }
    let cfg = ollama_settings();
    let source = if cfg.mode == "cloud" { "Ollama Cloud" } else { "Ollama (local)" };
    (getenv("AGENT_MODEL").unwrap_or_else(|| "llama3.2".into()), source.into())
}

pub fn build_provider(system_prompt: String) -> Result<Provider, String> {
    let (model, _) = describe();
    let kind = getenv("AGENT_PROVIDER").unwrap_or_else(|| "ollama".into()).to_ascii_lowercase();
    if kind == "anthropic" {
        let key = getenv("ANTHROPIC_API_KEY")
            .ok_or("Anthropic provider needs ANTHROPIC_API_KEY in .env")?;
        let _ = system_prompt;
        return Ok(Provider::Anthropic(AnthropicProvider { model, api_key: key }));
    }
    let cfg = ollama_settings();
    if cfg.mode == "cloud" && cfg.api_keys.is_empty() {
        return Err("Ollama Cloud mode needs OLLAMA_API_KEY (or OLLAMA_API_KEYS) (create one at https://ollama.com/settings/keys)".into());
    }
    let _ = system_prompt;
    Ok(Provider::Ollama(OllamaProvider {
        model: if cfg.api_keys.is_empty() { model } else { cloud_model_id(&model) },
        host: cfg.host,
        keys: cfg.api_keys.clone(),
        system_prompt: system_prompt.into(),
    }))
}

pub struct OllamaProvider {
    pub model: String,
    pub host: String,
    pub keys: Vec<String>,
    pub system_prompt: String,
}

fn to_ollama_native(system_prompt: &str, history: &[Message]) -> Vec<Value> {
    let mut native = vec![json!({"role": "system", "content": system_prompt})];
    for m in history {
        match m.role.as_str() {
            "user" => native.push(json!({"role": "user", "content": m.content.clone().unwrap_or_default()})),
            "assistant" => {
                let mut msg = json!({"role": "assistant", "content": m.content.clone().unwrap_or_default()});
                if !m.tool_calls.is_empty() {
                    msg["tool_calls"] = json!(m.tool_calls.iter().map(|tc| {
                        json!({"function": {"name": tc.name, "arguments": tc.input.clone()}})
                    }).collect::<Vec<_>>());
                }
                native.push(msg);
            }
            "tool" => native.push(json!({
                "role": "tool",
                "content": m.content.clone().unwrap_or_default(),
                "name": m.name.clone().unwrap_or_default(),
            })),
            _ => {}
        }
    }
    native
}

impl OllamaProvider {
    pub fn use_pool(&self) -> bool {
        !self.keys.is_empty()
    }

    pub async fn step(&self, system_prompt: &str, history: &[Message], tool_schemas: &[Value]) -> Result<Message, String> {
        let oa_tools: Vec<Value> = tool_schemas.iter().map(to_openai_tool_schema).collect();
        let messages = to_ollama_native(system_prompt, history);

        loop {
            let key: String = if self.use_pool() {
                let pool = &get_pool(&self.keys).0;
                match pool.free() {
                    None => return Err(pool.exhausted_message(&self.host, &self.model)),
                    Some(idx) => self.keys[idx].clone(),
                }
            } else {
                String::new()
            };

            let mut body = json!({"model": self.model, "messages": messages, "stream": false});
            if !oa_tools.is_empty() {
                body["tools"] = json!(oa_tools);
            }
            let url = format!("{}/api/chat", self.host.trim_end_matches('/'));
            let mut req = http().post(url).json(&body).timeout(Duration::from_secs(240));
            if !key.is_empty() {
                req = req.bearer_auth(&key);
            }
            let resp = req.send().await;

            match resp {
                Ok(r) => {
                    let code = r.status().as_u16();
                    if self.use_pool() && [429u16, 402, 401, 403].contains(&code) {
                        let pool = &get_pool(&self.keys).0;
                        let msg = pool.mark_bad(pool.idx_of(&key), code);
                        if !msg.is_empty() {
                            eprintln!("[ollama] {msg}");
                        }

                        continue; // park this key; loop picks the next one
                    }
                    if !r.status().is_success() {
                        return Err(format!("Ollama returned HTTP {code}: {}", r.text().await.unwrap_or_default().chars().take(300).collect::<String>()));
                    }
                    let data: Value = r.json().await.map_err(|e| format!("bad Ollama response: {e}"))?;
                    if self.use_pool() {
                        let pool = &get_pool(&self.keys).0;
                        pool.mark_ok(pool.idx_of(&key));
                    }
                    let msg = &data["message"];
                    let text = msg["content"].as_str().unwrap_or_default().trim().to_string();
                    let mut tool_calls = Vec::new();
                    for tc in msg["tool_calls"].as_array().unwrap_or(&vec![]).clone() {
                        let args = match &tc["function"]["arguments"] {
                            Value::String(s) => serde_json::from_str(s).unwrap_or(json!({})),
                            other => other.clone(),
                        };
                        tool_calls.push(ToolCall {
                            id: tc.get("id").and_then(|v| v.as_str()).unwrap_or("").to_string(),
                            name: tc["function"]["name"].as_str().unwrap_or_default().to_string(),
                            input: args,
                        });
                    }
                    for c in tool_calls.iter_mut() {
                        if c.id.is_empty() {
                            c.id = format!("call_{}", &uuid::Uuid::new_v4().simple().to_string()[..8]);
                        }
                    }
                    let content = (!text.is_empty()).then_some(text);
                    return Ok(Message { role: "assistant".into(), content, tool_calls, tool_call_id: None, name: None });
                }
                Err(e) => {
                    if self.use_pool() {
                        let pool = &get_pool(&self.keys).0;
                        let _idx = pool.idx_of(&key);

                        continue;
                    }
                    return Err(format!("Couldn't connect to Ollama at {}: {e}", self.host));
                }
            }
        }
    }
}

pub struct AnthropicProvider {
    pub model: String,
    pub api_key: String,
}

fn to_anthropic_native(history: &[Message]) -> Vec<Value> {
    let mut native = Vec::new();
    for m in history {
        match m.role.as_str() {
            "user" => native.push(json!({"role": "user", "content": m.content.clone().unwrap_or_default()})),
            "assistant" => {
                let mut blocks = Vec::new();
                if let Some(c) = &m.content {
                    if !c.is_empty() {
                        blocks.push(json!({"type": "text", "text": c}));
                    }
                }
                for tc in &m.tool_calls {
                    blocks.push(json!({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}));
                }
                native.push(json!({"role": "assistant", "content": blocks}));
            }
            "tool" => native.push(json!({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": m.tool_call_id.clone().unwrap_or_default(), "content": m.content.clone().unwrap_or_default()}]
            })),
            _ => {}
        }
    }
    native
}

impl AnthropicProvider {
    pub async fn step(&self, system_prompt: &str, history: &[Message], tool_schemas: &[Value]) -> Result<Message, String> {
        let body = json!({
            "model": self.model,
            "max_tokens": 1500,
            "system": system_prompt,
            "tools": tool_schemas,
            "messages": to_anthropic_native(history),
        });
        #[allow(clippy::let_unit_value)]
        let resp = http()
            .post("https://api.anthropic.com/v1/messages")
            .header("x-api-key", &self.api_key)
            .header("anthropic-version", "2023-06-01")
            .json(&body)
            .send()
            .await
            .map_err(|e| format!("Anthropic request failed: {e}"))?;
        let code = resp.status().as_u16();
        let data: Value = resp.json().await.map_err(|e| format!("bad Anthropic response (HTTP {code}): {e}"))?;
        if code == 401 || code == 403 {
            return Err(format!("Anthropic rejected the API key (HTTP {code})"));
        }
        if !data["content"].is_array() {
            return Err(format!("Anthropic error: {}", data["error"]["message"].as_str().unwrap_or(&format!("HTTP {code}"))));
        }
        let mut text = String::new();
        let mut tool_calls = Vec::new();
        for b in data["content"].as_array().unwrap() {
            match b["type"].as_str() {
                Some("text") => text.push_str(b["text"].as_str().unwrap_or_default()),
                Some("tool_use") => tool_calls.push(ToolCall {
                    id: b["id"].as_str().unwrap_or_default().to_string(),
                    name: b["name"].as_str().unwrap_or_default().to_string(),
                    input: b["input"].clone(),
                }),
                _ => {}
            }
        }
        let content = (!text.trim().is_empty()).then(|| text.trim().to_string());
        Ok(Message { role: "assistant".into(), content, tool_calls, tool_call_id: None, name: None })
    }
}

// silence unused warnings for helpers kept for parity
#[allow(dead_code)]
fn _unused(_: &SETTINGS_NONE) {}
struct SETTINGS_NONE;
