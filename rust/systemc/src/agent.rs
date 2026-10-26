//! The agent loop, ported from agent.py. Drives the model through tool calls
//! until it stops, handling structured tool outputs and the approval gate for
//! destructive/elevated shell commands. Subagents run concurrently on separate
//! tokio tasks — faster than the Python thread version.

use crate::chat_platform::{sender_tag, Author, ChannelAdapter};
use crate::config::getenv;
use crate::memory;
use crate::prompt::{build_from_env, SUBAGENT_PROMPT};
use crate::providers::{build_provider, describe, drain_key_notices, Message, Provider, ToolCall};
use crate::sessions;
use crate::tools;
use serde_json::{json, Value};
use std::sync::Arc;

fn env_num(name: &str, default: usize) -> usize {
    getenv(name)
        .and_then(|v| v.trim().parse().ok())
        .filter(|v| *v >= 1)
        .unwrap_or(default)
}

pub fn max_turns() -> usize {
    env_num("AGENT_MAX_TURNS", 20)
}

pub fn subagents_enabled() -> bool {
    getenv("SUBAGENTS").map(|v| v.to_ascii_lowercase() != "off").unwrap_or(true)
}

pub const SUB_MAX_TASKS: usize = 5;

fn fmt(result: &Value) -> String {
    serde_json::to_string(result).unwrap_or_else(|_| result.to_string())
}

fn clip(s: &str, n: usize) -> String {
    let s = s.trim();
    if s.chars().count() <= n { s.to_string() } else { format!("{}…", s.chars().take(n).collect::<String>()) }
}

pub struct Agent {
    provider: Provider,
    base_prompt: String,
}

impl Agent {
    pub fn new() -> Result<Self, String> {
        let (model, source) = describe();
        let mut prompt = build_from_env(&model, &source);
        if !tools::elevated_enabled() {
            // Elevation is off on this box: don't advertise a tool that doesn't exist.
            prompt = prompt.replace(
                "If run_shell fails with \"Permission denied\", retry with run_shell_elevated\n",
                "If run_shell fails with \"Permission denied\", you may not have elevated access here: tell the user what needs doing instead of trying to work around it\n",
            );
        }
        Ok(Agent { provider: build_provider(prompt.clone())?, base_prompt: prompt })
    }

    fn turn_system_prompt(&self, mem_context: &str) -> String {
        if mem_context.is_empty() {
            self.base_prompt.clone()
        } else {
            format!("{}\n\n{}", self.base_prompt, mem_context)
        }
    }

    pub async fn run_turn(
        &self,
        channel: ChannelAdapter,
        user_text: &str,
        history: &mut Vec<Message>,
        author: Option<&Author>,
    ) -> Result<(), String> {
        let prompt = match author {
            Some(a) => format!("{} {}", sender_tag(a), user_text),
            None => user_text.to_string(),
        };
        history.push(Message::user(prompt));

        // Re-inject memory fresh on every turn: it can change between turns
        // (another channel's turn may have written to it).
        let mem_context = memory::load_context();
        let system_prompt = self.turn_system_prompt(&mem_context);

        let mut schemas = base_schemas(&channel);
        if subagents_enabled() {
            schemas.extend(subagent_schemas());
        }

        let channel = Arc::new(channel);
        let current_session = sessions::make_key(channel.platform(), &channel.id());
        let mut commands_used: Vec<String> = Vec::new();
        let mut counts: Vec<(String, usize)> = Vec::new(); // first-seen order preserved

        let mut final_text: Option<String> = None;
        let mut hit_limit = true;

        for _turn in 0..max_turns() {
            let assistant = self.provider.step(&system_prompt, history, &schemas).await?;
            for n in drain_key_notices() {
                if !n.is_empty() {
                    channel.send(&n).await;
                }
            }
            history.push(assistant.clone());

            if assistant.tool_calls.is_empty() {
                if let Some(text) = &assistant.content {
                    final_text = Some(text.clone());
                    hit_limit = false;
                    send_usage_summary(&channel, &commands_used, &counts).await;
                    channel.send(text).await;
                }
                break;
            }

            for tc in &assistant.tool_calls {
                let slot = match counts.iter_mut().find(|(n, _)| n == &tc.name) {
                    Some((_, c)) => c,
                    None => {
                        counts.push((tc.name.clone(), 0));
                        &mut counts.last_mut().unwrap().1
                    }
                };
                *slot += 1;

                let result = exec_tool(
                    &channel, &tc.name, &tc.input, self, &current_session,
                    &mut commands_used,
                ).await;
                history.push(Message::tool_result(&tc.id, &tc.name, fmt(&result)));
            }
        }
        if hit_limit {
            channel.send("_(hit the max tool-call limit for this turn — stopping)_").await;
        }

        memory::log_turn(&channel.label(), user_text, final_text.as_deref());
        Ok(())
    }
}

fn base_schemas(channel: &ChannelAdapter) -> Vec<Value> {
    let mut schemas = tools::local_tool_schemas();
    schemas.push(sessions::tool_schema());
    schemas.extend(tools::web_tool_schemas());
    schemas.push(json!({
        "name": "send_message",
        "description": "Post a message to the current channel (use only for progress notes or when the reply itself isn't the answer).",
        "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}
    }));
    if matches!(channel, ChannelAdapter::Discord(_)) {
        schemas.push(json!({
            "name": "send_file",
            "description": "Upload a file from the workspace (or absolute path) as an attachment.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "caption": {"type": "string"}}, "required": ["path"]}
        }));
        schemas.push(json!({
            "name": "fetch_channel_messages",
            "description": "Read this channel's recent message history with reply context.",
            "input_schema": {"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}, "required": []}
        }));
    }
    schemas
}

fn subagent_schemas() -> Vec<Value> {
    vec![
        json!({
            "name": "spawn_subagent",
            "description": "Delegate ONE self-contained task to a subagent (a fresh copy of you with its own context, shell, python, files and web search). It cannot see this conversation, so include all context and say exactly what to report back. Returns its final report.",
            "input_schema": {"type": "object", "properties": {
                "task": {"type": "string", "description": "Complete, self-contained instructions"},
                "name": {"type": "string", "description": "Short label, e.g. 'disk-audit'"}
            }, "required": ["task"]}
        }),
        json!({
            "name": "spawn_subagents",
            "description": "Run up to 5 independent tasks in PARALLEL, one subagent each. Same rules as spawn_subagent: every task must be self-contained. Returns all reports.",
            "input_schema": {"type": "object", "properties": {
                "tasks": {"type": "array", "items": {"type": "object", "properties": {
                    "task": {"type": "string"}, "name": {"type": "string"}
                }, "required": ["task"]}}
            }, "required": ["tasks"]}
        }),
    ]
}

async fn send_usage_summary(channel: &Arc<ChannelAdapter>, commands_used: &[String], counts: &[(String, usize)]) {
    if !(tools::show_commands_used() || tools::show_tools_used()) {
        return;
    }
    let mut lines: Vec<String> = Vec::new();
    if tools::show_commands_used() {
        let mut seen = std::collections::HashSet::new();
        let uniq: Vec<&String> = commands_used.iter().filter(|c| seen.insert(c.to_string())).collect();
        if !uniq.is_empty() {
            lines.push("-# __Commands executed__".to_string());
            for c in uniq.iter().take(50) {
                lines.push(format!("-# - {c}"));
            }
        }
    }
    if tools::show_tools_used() && !counts.is_empty() {
        lines.push("-# __Tools Used__".to_string());
        for (name, count) in counts {
            lines.push(format!("-# {name} ({count})"));
        }
    }
    if !lines.is_empty() {
        channel.send(&lines.join("\n")).await;
    }
}

/// Tool dispatch for one call.
async fn exec_tool(
    channel: &Arc<ChannelAdapter>,
    name: &str,
    args: &Value,
    agent: &Agent,
    current_session: &str,
    commands_used: &mut Vec<String>,
) -> Value {
    let get = |k: &str| args.get(k).and_then(|v| v.as_str()).unwrap_or_default().to_string();
    match name {
        "run_shell" => {
            let command = get("command");
            commands_used.push(command.clone());
            if tools::is_destructive(&command) {
                let approved = channel.request_approval(&command, false, 60).await;
                if !approved {
                    return json!({"error": "command was denied by user", "exit_code": -1});
                }
            }
            tools::run_shell(&command, false).await
        }
        "run_shell_elevated" => {
            let command = get("command");
            if !tools::elevated_enabled() {
                return json!({"error": "elevated commands are turned off on this machine", "exit_code": -1});
            }
            let approved = channel.request_approval(&format!("[ELEVATED / sudo]\n{command}"), true, 90).await;
            if !approved {
                return json!({"error": "elevated command was denied by user", "exit_code": -1});
            }
            tools::run_shell_elevated(&command).await
        }
        "run_python" => tools::run_python(&get("code")).await,
        "read_file" => tools::read_file(&get("path")),
        "write_file" => tools::write_file(&get("path"), &get("content")),
        "list_dir" => tools::list_dir(&get("path")),
        "web_search" => {
            let max = args.get("max_results").and_then(|v| v.as_i64()).unwrap_or(5).clamp(1, 10) as usize;
            tools::web_search(&get("query"), max).await
        }
        "remember" => memory::remember(&get("fact")),
        "reset_session" => reset_session_tool(channel, current_session, &get("session")).await,
        "send_message" => {
            channel.send(&get("content")).await;
            json!({"sent": true})
        }
        "send_file" => channel.send_file(&get("path"), &get("caption")).await,
        "fetch_channel_messages" => {
            let limit = args.get("limit").and_then(|v| v.as_i64()).unwrap_or(20).clamp(1, 100) as usize;
            let msgs = channel.history(limit).await;
            json!({"messages": msgs, "count": msgs.len()})
        }
        "spawn_subagent" => agent.run_subagents(channel, vec![(get("name"), get("task"))]).await,
        "spawn_subagents" => {
            let tasks: Vec<(String, String)> = args["tasks"].as_array().map(|a| {
                a.iter().map(|t| (
                    t["name"].as_str().unwrap_or_default().to_string(),
                    t["task"].as_str().unwrap_or_default().to_string(),
                )).collect()
            }).unwrap_or_default();
            agent.run_subagents(channel, tasks).await
        }
        other => json!({"error": format!("unknown tool: {other}")}),
    }
}

async fn reset_session_tool(channel: &Arc<ChannelAdapter>, current: &str, session_ref: &str) -> Value {
    let key = match sessions::resolve(session_ref, current) {
        Ok(k) => k,
        Err(e) => {
            let known: Vec<String> = sessions::list_sessions().iter().take(10)
                .filter_map(|s| s["key"].as_str().map(String::from)).collect();
            return json!({"error": e, "known_sessions": known});
        }
    };
    if key != current {
        let info = sessions::list_sessions().into_iter().find(|s| s["key"] == json!(key)).unwrap_or(json!({}));
        let label = info["label"].as_str().unwrap_or("");
        let what = if label.is_empty() { key.clone() } else { format!("{key} ({label})") };
        let turns = info["turns"].as_u64().unwrap_or(0);
        let approved = channel.request_approval(
            &format!("[reset_session] Clear the conversation history of {what} ({turns} turn(s)). Long-term memory is not affected."),
            false, 60,
        ).await;
        if !approved {
            return json!({"error": "reset of another session was denied by the owner", "session": key});
        }
    }
    match sessions::reset(&key) {
        Ok(mut res) => {
            res["long_term_memory"] = json!("untouched");
            if key == current {
                res["note"] = json!("This conversation's history is cleared once your current reply is sent; the next message starts from a blank slate.");
            }
            res
        }
        Err(e) => json!({"error": e, "session": key}),
    }
}

// ── Subagents ────────────────────────────────────────────────────────────────
impl Agent {
    fn sub_tool_schemas() -> Vec<Value> {
        let mut out: Vec<Value> = tools::local_tool_schemas()
            .into_iter()
            .filter(|s| matches!(s["name"].as_str(), Some("run_shell") | Some("run_python") | Some("read_file") | Some("write_file") | Some("list_dir")))
            .collect();
        out.extend(tools::web_tool_schemas());
        out
    }

    pub async fn run_subagents(
        &self,
        channel: &Arc<ChannelAdapter>,
        tasks: Vec<(String, String)>,
    ) -> Value {
        let mut tasks: Vec<(String, String)> = tasks.into_iter()
            .filter(|(_, t)| !t.trim().is_empty())
            .take(SUB_MAX_TASKS)
            .collect();
        if tasks.is_empty() {
            return json!({"error": "no tasks given — pass tasks=[{\"name\": ..., \"task\": ...}, ...]"});
        }
        for (i, (name, _)) in tasks.iter_mut().enumerate() {
            if name.trim().is_empty() {
                *name = format!("agent-{}", i + 1);
            }
        }
        let names: Vec<String> = tasks.iter().map(|(n, _)| n.clone()).collect();
        channel.send(&format!("🧩 Spawning {} subagent(s): {}", tasks.len(), names.join(", "))).await;

        // one approval prompt at a time (Stoat replies are plain yes/no)
        let approval_lock = Arc::new(tokio::sync::Mutex::new(()));
        let max_parallel = env_num("SUBAGENT_MAX_PARALLEL", 3);
        let sem = Arc::new(tokio::sync::Semaphore::new(max_parallel));

        let agent = self; // borrowed: subagent futures don't need to be 'static
        let futures = tasks.into_iter().map(|(name, task)| {
            let sem = sem.clone();
            let lock = approval_lock.clone();
            let channel = channel.clone();
            async move {
                let _permit = sem.acquire().await;
                run_sub_task(agent, &channel, lock, name, task).await
            }
        });

        let results = futures_util::future::join_all(futures).await;
        json!({"results": results})
    }
}

async fn run_sub_task(
    _agent: &Agent,
    channel: &Arc<ChannelAdapter>,
    approval_lock: Arc<tokio::sync::Mutex<()>>,
    name: String,
    task: String,
) -> Value {
    let provider = match build_provider(SUBAGENT_PROMPT.to_string()) {
        Ok(p) => p,
        Err(e) => return json!({"name": name, "status": "error", "report": e}),
    };
    let schemas = Agent::sub_tool_schemas();
    let max_turns = env_num("SUBAGENT_MAX_TURNS", 12);
    let timeout = env_num("SUBAGENT_TIMEOUT", 600);
    let mut history = vec![Message::user(task)];
    let mut last_text = String::new();

    for turn in 0..max_turns {
        let step = tokio::time::timeout(
            std::time::Duration::from_secs(timeout as u64),
            provider.step(SUBAGENT_PROMPT, &history, &schemas),
        ).await;
        let msg = match step {
            Ok(Ok(m)) => m,
            Ok(Err(e)) => return json!({"name": name, "status": "error", "report": e}),
            Err(_) => return json!({"name": name, "status": "timeout", "report": format!("timed out after {timeout}s")}),
        };
        let _ = drain_key_notices();
        history.push(msg.clone());
        if let Some(c) = &msg.content {
            if !c.trim().is_empty() {
                last_text = c.clone();
            }
        }
        if msg.tool_calls.is_empty() {
            let report = if last_text.trim().is_empty() { "(empty report)".to_string() } else { last_text.clone() };
            return json!({"name": name, "status": "ok", "turns": turn + 1, "report": clip(&report, 6000)});
        }
        for tc in &msg.tool_calls {
            let result = exec_sub_tool(channel, tc, &approval_lock).await;
            history.push(Message::tool_result(&tc.id, &tc.name, fmt(&result)));
        }
    }
    let report = if last_text.is_empty() { "hit the turn limit before finishing".to_string() } else { last_text };
    json!({"name": name, "status": "incomplete", "turns": max_turns, "report": clip(&report, 6000)})
}

async fn exec_sub_tool(channel: &Arc<ChannelAdapter>, tc: &ToolCall, approval_lock: &Arc<tokio::sync::Mutex<()>>) -> Value {
    let get = |k: &str| tc.input.get(k).and_then(|v| v.as_str()).unwrap_or_default().to_string();
    match tc.name.as_str() {
        "run_shell" => {
            let command = get("command");
            if tools::is_destructive(&command) {
                let _guard = approval_lock.lock().await;
                if !channel.request_approval(&format!("[subagent] {command}"), false, 60).await {
                    return json!({"error": "command was denied by user", "exit_code": -1});
                }
            }
            tools::run_shell(&command, false).await
        }
        "run_python" => tools::run_python(&get("code")).await,
        "read_file" => tools::read_file(&get("path")),
        "write_file" => tools::write_file(&get("path"), &get("content")),
        "list_dir" => tools::list_dir(&get("path")),
        "web_search" => {
            let max = tc.input.get("max_results").and_then(|v| v.as_i64()).unwrap_or(5).clamp(1, 10) as usize;
            tools::web_search(&get("query"), max).await
        }
        other => json!({"error": format!("unknown tool (subagents have a reduced toolset): {other}")}),
    }
}
