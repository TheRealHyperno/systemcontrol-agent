//! Tool implementations, ported from tools.py.
//! Shell exec without a PTY is fine for the Rust port: piped stdout captures
//! everything the model needs and avoids event-loop overhead entirely.
//! Shape stays identical: {"stdout", "exit_code", "cwd"}.

use crate::config::{get_bool, getenv};
use crate::version::SYSTEMC_DISPLAY;
use regex::Regex;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::Duration;

pub const SHELL_TIMEOUT_SECS: u64 = 60;
pub const OUTPUT_CAP: usize = 6000;
pub const READ_CAP: u64 = 8000;

pub fn workdir() -> PathBuf {
    crate::config::workdir()
}

// ── Destructive command detection ──────────────────────────────────────────
pub const DEFAULT_APPROVAL_COMMANDS: &[&str] = &[
    "rm", "rmdir", "shred", "mkfs", "fdisk", "parted", "wipefs", "dd", "shutdown", "reboot",
    "halt", "poweroff", "sudo", "su", "pip", "pip3", "apt", "apt-get", "dpkg", "snap", "flatpak",
    "npm", "yarn", "pnpm", "dnf", "yum", "pacman", "systemctl", "service", "chown", "chmod",
    "iptables", "ip6tables", "ufw", "nft", "kill", "killall", "pkill", "del", "erase", "format",
    "remove-item",
];

pub fn approval_commands() -> Vec<String> {
    let raw = getenv("APPROVAL_COMMANDS").unwrap_or_default();
    if !raw.trim().is_empty() {
        let mut out = Vec::new();
        for x in raw.split(|c| c == ',' || c == ';') {
            let x = x.trim().to_ascii_lowercase();
            if !x.is_empty() && !out.contains(&x) {
                out.push(x);
            }
        }
        if !out.is_empty() {
            return out;
        }
    }
    DEFAULT_APPROVAL_COMMANDS.iter().map(|s| s.to_string()).collect()
}

/// (?:^|[|;&`(]\s*)(?:rm|rmdir|...)\b  — token at the start of a pipeline segment,
/// not "echo rm done". Plus pipe-to-shell anywhere.
pub fn is_destructive(command: &str) -> bool {
    static RE: OnceLock<Option<Regex>> = OnceLock::new();
    let re = RE.get_or_init(|| {
        let cmds = approval_commands();
        if cmds.is_empty() {
            None
        } else {
            let alt = cmds.iter().map(|c| regex::escape(c)).collect::<Vec<_>>().join("|");
            Regex::new(&format!(
                r"(?imu)(?:^|[|;&`(]\s*)(?:{alt})\b"
            ))
            .ok()
        }
    });
    static PIPE_SHELL: OnceLock<Regex> = OnceLock::new();
    let pipe = PIPE_SHELL.get_or_init(|| Regex::new(r"(?i)\|\s*(ba)?sh\b|\|\s*(iex|invoke-expression)\b").unwrap());
    re.as_ref().map(|r| r.is_match(command)).unwrap_or(false) || pipe.is_match(command)
}

pub fn approval_mode() -> &'static str {
    let v = getenv("APPROVAL_MODE").unwrap_or_else(|| "ask".into()).to_ascii_lowercase();
    match v.as_str() {
        "approve_all" | "approveall" | "approve" => "approve_all",
        "deny_all" | "denyall" | "deny" => "deny_all",
        _ => "ask",
    }
}

pub fn show_commands_used() -> bool {
    get_bool("SHOW_COMMANDS_USED")
}
pub fn show_tools_used() -> bool {
    get_bool("SHOW_TOOLS_USED")
}
pub fn elevated_enabled() -> bool {
    if let Some(v) = getenv("AGENT_ALLOW_ELEVATED") {
        return matches!(v.to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on");
    }
    getenv("AGENT_RUN_AS_USER").is_none()
}

fn clean_text(raw: String) -> String {
    static ANSI: OnceLock<Regex> = OnceLock::new();
    static ANSI_OSC: OnceLock<Regex> = OnceLock::new();
    let ansi = ANSI.get_or_init(|| Regex::new(r"\x1b\[[0-9;]*[A-Za-z]").unwrap());
    let osc = ANSI_OSC.get_or_init(|| Regex::new(r"\x1b\][^\x07]*\x07").unwrap());
    let mut text = osc.replace_all(&raw, "").to_string();
    text = ansi.replace_all(&text, "").replace("\r\n", "\n").replace('\r', "\n");
    if text.len() > OUTPUT_CAP {
        let half = OUTPUT_CAP / 2;
        let start = String::from_utf8_lossy(&text.as_bytes()[..text.len().min(half)]).to_string();
        let end = String::from_utf8_lossy(
            &text.as_bytes()[text.len().saturating_sub(half)..],
        )
        .to_string();
        text = format!("{start}\n... [truncated] ...\n{end}");
    }
    text.trim().to_string()
}

fn run_cmd_args(command: &str) -> Vec<String> {
    let run_as = getenv("AGENT_RUN_AS_USER");
    let current_user = std::env::var("USER").unwrap_or_default();
    if let Some(user) = run_as.filter(|u| !u.is_empty() && *u != current_user) {
        if getuid() == 0 {
            return vec!["runuser".into(), "-u".into(), user, "--".into(), "/bin/bash".into(), "-c".into(), command.into()];
        }
        return vec!["sudo".into(), "-n".into(), "-u".into(), user, "-H".into(), "--".into(), "/bin/bash".into(), "-c".into(), command.into()];
    }
    vec!["/bin/bash".into(), "-c".into(), command.into()]
}

fn getuid() -> u32 {
    // std doesn't expose getuid; this path is rare and `id -u` is infinitely
    // cheaper than pulling an FFI crate into the build.
    if let Ok(out) = std::process::Command::new("id").arg("-u").output() {
        if let Ok(s) = String::from_utf8(out.stdout) {
            if let Ok(n) = s.trim().parse::<u32>() {
                return n;
            }
        }
    }
    1000
}

async fn read_to_vec(mut pipe: impl tokio::io::AsyncRead + Unpin + Send + 'static) -> Vec<u8> {
    let mut v = Vec::new();
    let _ = tokio::io::AsyncReadExt::read_to_end(&mut pipe, &mut v).await;
    v
}

/// Run a shell command in WORKDIR. Returns {stdout, exit_code, cwd}.
pub async fn run_shell(command: &str, elevated_prefix: bool) -> Value {
    let wd = workdir();
    let full = if elevated_prefix { format!("sudo -n {command}") } else { command.to_string() };
    let args = run_cmd_args(&full);
    let mut cmd = tokio::process::Command::new(&args[0]);
    cmd.args(&args[1..]);
    cmd.current_dir(&wd);
    cmd.env("HOME", &wd).env("PWD", &wd);
    cmd.stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::piped()).kill_on_drop(true);

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => return json!({"stdout": "", "exit_code": -1, "error": format!("spawn failed: {e}"), "cwd": wd.display().to_string()}),
    };
    let out_handle = child.stdout.take().map(read_to_vec);
    let err_handle = child.stderr.take().map(read_to_vec);

    let timeout_fut = tokio::time::timeout(
        Duration::from_secs(SHELL_TIMEOUT_SECS),
        async { (out_handle, err_handle) },
    )
    .await;

    let (out_bytes, err_bytes, timed_out) = match timeout_fut {
        // handlers are JoinHandles: just await them wherever the timeout lands
        Ok((oh, eh)) => {
            let out = match oh { Some(h) => h.await, None => Vec::new() };
            let err = match eh { Some(h) => h.await, None => Vec::new() };
            (out, err, false)
        }
        Err(_) => {
            let _ = child.start_kill();
            (Vec::new(), Vec::new(), true)
        }
    };

    let exit_code = child.wait().await.map(|s| s.code().unwrap_or(-1)).unwrap_or(-1);
    if timed_out {
        return json!({
            "stdout": clean_text(String::from_utf8_lossy(&out_bytes).to_string()),
            "exit_code": -1,
            "error": format!("timed out after {}s", SHELL_TIMEOUT_SECS),
            "cwd": wd.display().to_string(),
        });
    }
    let mut text = String::from_utf8_lossy(&out_bytes).to_string();
    let err = String::from_utf8_lossy(&err_bytes).to_string();
    if !err.trim().is_empty() {
        text.push('\n');
        text.push_str(&err);
    }
    json!({"stdout": clean_text(text), "exit_code": exit_code, "cwd": wd.display().to_string()})
}

pub async fn run_shell_elevated(command: &str) -> Value {
    if !elevated_enabled() {
        return json!({
            "stdout": "", "exit_code": -1,
            "error": "Elevated commands are turned off on this machine (AGENT_ALLOW_ELEVATED).",
            "cwd": workdir().display().to_string(),
        });
    }
    run_shell(command, true).await
}

// ── File tools ──────────────────────────────────────────────────────────────
fn resolve(path: &str) -> PathBuf {
    let p = Path::new(path);
    if p.is_absolute() {
        p.to_path_buf()
    } else {
        workdir().join(p)
    }
}

pub fn read_file(path: &str) -> Value {
    let full = resolve(path);
    match std::fs::read(&full) {
        Ok(bytes) => {
            let mut content = String::from_utf8_lossy(&bytes).to_string();
            let truncated = content.len() > READ_CAP as usize;
            if truncated {
                content = String::from_utf8_lossy(
                    &bytes[..bytes.len().min(READ_CAP as usize)],
                )
                .to_string();
            }
            json!({"content": content, "path": full.display().to_string(), "truncated": truncated})
        }
        Err(e) => json!({"error": e.to_string()}),
    }
}

pub fn write_file(path: &str, content: &str) -> Value {
    let full = resolve(path);
    if let Some(parent) = full.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    match std::fs::write(&full, content) {
        Ok(()) => json!({"written": content.len(), "path": full.display().to_string()}),
        Err(e) => json!({"error": e.to_string()}),
    }
}

pub fn list_dir(path: &str) -> Value {
    let full = if path.is_empty() { workdir() } else { resolve(path) };
    match std::fs::read_dir(&full) {
        Ok(entries) => {
            let mut list: Vec<(String, bool, Option<u64>)> = Vec::new();
            for entry in entries.flatten() {
                let name = entry.file_name().to_string_lossy().to_string();
                let md = entry.metadata().ok();
                let is_dir = md.as_ref().map(|m| m.is_dir()).unwrap_or(false);
                let size = md.filter(|m| m.is_file()).map(|m| m.len());
                list.push((name, is_dir, size));
            }
            list.sort();
            json!({
                "path": full.display().to_string(),
                "entries": list.into_iter().map(|(name, is_dir, size)| {
                    let mut e = json!({"name": name, "type": if is_dir {"dir"} else {"file"}});
                    if let Some(s) = size { e["size"] = json!(s); }
                    e
                }).collect::<Vec<_>>(),
            })
        }
        Err(e) => json!({"error": e.to_string()}),
    }
}

/// Run a Python snippet in the workspace (tools.run_python port).
pub async fn run_python(code: &str) -> Value {
    let wd = workdir();
    let path = wd.join(format!(
        ".systemc_run_{}.py",
        uuid::Uuid::new_v4().simple()
    ));
    if std::fs::write(&path, code).is_err() {
        return json!({"stdout": "", "exit_code": -1, "error": "couldn't write temp script", "cwd": wd.display().to_string()});
    }
    let python = getenv("SYSTEMC_PYTHON").unwrap_or_else(|| "python3".into());
    let out = run_shell(&format!("{} {}", shell_quote(&python), shell_quote(path.to_string_lossy().as_ref())), false).await;
    let _ = std::fs::remove_file(&path);
    out
}

pub fn shell_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', r"'\''"))
}

// ── Web search (DuckDuckGo HTML endpoint, port of tools.web_search) ────────
pub async fn web_search(query: &str, max_results: usize) -> Value {
    let max_results = max_results.clamp(1, 10);
    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(15))
        .user_agent("Mozilla/5.0 (compatible; systemc-agent-rs/1.0)")
        .build()
    {
        Ok(c) => c,
        Err(e) => return json!({"error": format!("search failed: {e}")}),
    };
    let body = match client
        .post("https://html.duckduckgo.com/html/")
        .form(&[("q", query)])
        .send()
        .await
    {
        Ok(r) => match r.error_for_status() {
            Ok(r) => match r.text().await {
                Ok(t) => t,
                Err(e) => return json!({"error": format!("search failed: {e}")}),
            },
            Err(e) => return json!({"error": format!("search failed: {e}")}),
        },
        Err(e) => return json!({"error": format!("search failed: {e}")}),
    };

    static TITLE_RE: OnceLock<Regex> = OnceLock::new();
    static SNIPPET_RE: OnceLock<Regex> = OnceLock::new();
    let title_re = TITLE_RE.get_or_init(|| Regex::new(r#"<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>"#).unwrap());
    let snippet_re = SNIPPET_RE.get_or_init(|| Regex::new(r#"<a[^>]+class="result__snippet"[^>]*>([\s\S]*?)</a>"#).unwrap());

    let titles: Vec<_> = title_re.captures_iter(&body).collect();
    let mut results = Vec::new();
    for (i, m) in titles.iter().enumerate().take(max_results) {
        let raw_href = m.get(1).map(|x| x.as_str()).unwrap_or_default();
        let raw_title = m.get(2).map(|x| x.as_str()).unwrap_or_default();
        let start = m.get(0).map(|x| x.end()).unwrap_or(0);
        let end = titles
            .get(i + 1)
            .and_then(|n| n.get(0).map(|x| x.start()))
            .unwrap_or_else(|| (start + 2000).min(body.len()));
        let window = &body[start.min(end)..end];
        let raw_snippet = snippet_re
            .captures(window)
            .and_then(|sm| sm.get(1))
            .map(|x| x.as_str())
            .unwrap_or_default();
        // strip DuckDuckGo redirect wrapper
        let url = raw_href
            .split("uddg=")
            .nth(1)
            .and_then(|rest| rest.split('&').next())
            .and_then(|enc| percent_encoding::percent_decode_str(enc).decode_utf8().ok().map(|s| s.to_string()))
            .unwrap_or_else(|| strip_dd_link(raw_href));
        results.push(json!({
            "title": strip_tags(raw_title),
            "url": url,
            "snippet": strip_tags(raw_snippet),
        }));
    }
    if results.is_empty() {
        return json!({"results": [], "note": "no results parsed — DuckDuckGo may have changed its markup"});
    }
    json!({"results": results})
}

fn strip_dd_link(href: &str) -> String {
    if href.starts_with("//") { format!("https:{href}") } else { href.to_string() }
}

fn strip_tags(s: &str) -> String {
    static TAGS: OnceLock<Regex> = OnceLock::new();
    let re = TAGS.get_or_init(|| Regex::new(r"<[^>]+>").unwrap());
    decode_entities(&re.replace_all(s, "").to_string()).trim().to_string()
}

fn decode_entities(s: &str) -> String {
    use percent_encoding::percent_decode;
    // handle the common entities plus URL-encoded ones
    let s = s
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#39;", "'")
        .replace("&#x27;", "'");
    percent_decode(s.as_bytes()).decode_utf8_lossy().to_string()
}

// ── Tool schemas (what the model sees) ─────────────────────────────────────
fn schema(name: &str, description: &str, props: Value, required: &[&str]) -> Value {
    json!({
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": required,
        },
    })
}

pub fn local_tool_schemas() -> Vec<Value> {
    vec![
        schema("run_shell",
            "Run a shell command in the workspace. Returns {stdout, exit_code, cwd}. Destructive commands need user approval — don't retry when denied.",
            json!({"command": {"type": "string"}}), &["command"]),
        schema("run_shell_elevated",
            "Run a shell command with sudo (root). ALWAYS requires the owner's approval.",
            json!({"command": {"type": "string"}}), &["command"]),
        schema("run_python",
            "Run a Python snippet in the workspace with the system python3. Prefer over run_shell for anything beyond a one-liner. Returns {stdout, exit_code, cwd}.",
            json!({"code": {"type": "string"}}), &["code"]),
        schema("read_file", "Read a file (workspace-relative or absolute), first 8000 chars.",
            json!({"path": {"type": "string"}}), &["path"]),
        schema("write_file", "Write a file (creates parent dirs).",
            json!({"path": {"type": "string"}, "content": {"type": "string"}}), &["path", "content"]),
        schema("list_dir", "List a directory (defaults to workspace root).",
            json!({"path": {"type": "string"}}), &[]),
        schema("remember",
            format!("Save a short, durable fact to long-term memory (MEMORY.md). Use when the user asks you to remember something, or shares a stable preference/fact/decision — not for one-off details. Current release: {SYSTEMC_DISPLAY}.").as_str(),
            json!({"fact": {"type": "string"}}), &["fact"]),
    ]
}

pub fn web_tool_schemas() -> Vec<Value> {
    vec![schema("web_search",
        "Search the web via DuckDuckGo (no API key). Returns title/url/snippet results.",
        json!({"query": {"type": "string"}, "max_results": {"type": "integer", "default": 5}}), &["query"])]
}

pub fn elevated_schema() -> bool {
    elevated_enabled()
}

pub fn usage_banner() -> String {
    format!("[systemc {SYSTEMC_DISPLAY}] tools ready")
}
