//! Lightweight file-based memory, ported from memory.py.
//!   .memory/daily/YYYY-MM-DD.md — append-only daily log (today+yesterday are
//!                                  re-injected into the prompt every turn)
//!   .memory/MEMORY.md           — curated long-term facts (the `remember` tool)

use crate::config::{app_dir, getenv};
use chrono::{Duration, Local};
use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::OnceLock;

pub const MAX_DAILY_CHARS: usize = 4000;
pub const MAX_LONG_TERM_CHARS: usize = 4000;
pub const MAX_ENTRY_CHARS: usize = 500;

fn memory_dir() -> &'static PathBuf {
    static DIR: OnceLock<PathBuf> = OnceLock::new();
    DIR.get_or_init(|| {
        let base = match getenv("SYSTEMC_PROFILE_ID").filter(|p| !p.is_empty() && p != "default") {
            Some(profile) => app_dir().join(".memory").join("profiles").join(profile),
            None => app_dir().join(".memory"),
        };
        let _ = std::fs::create_dir_all(base.join("daily"));
        base
    })
}

pub fn daily_path(date: chrono::NaiveDate) -> PathBuf {
    memory_dir().join("daily").join(format!("{}.md", date.format("%Y-%m-%d")))
}

pub fn long_term_path() -> PathBuf {
    memory_dir().join("MEMORY.md")
}

fn read_capped(path: &PathBuf, cap: usize) -> String {
    let Ok(text) = std::fs::read_to_string(path) else { return String::new() };
    let text = text.trim().to_string();
    if text.len() > cap {
        format!("...[truncated, older lines cut]...\n{}", trim_bytes(&text, cap))
    } else {
        text
    }
}

fn trim_bytes(text: &str, cap: usize) -> String {
    // cut on a char boundary, not a byte boundary
    if text.len() <= cap {
        return text.to_string();
    }
    let mut end = cap;
    while end > 0 && !text.is_char_boundary(end) {
        end -= 1;
    }
    text[end..].to_string()
}

/// Markdown block combining MEMORY.md + yesterday's + today's daily log,
/// appended to the system prompt each turn. "" if nothing recorded yet.
pub fn load_context() -> String {
    let today = Local::now().date_naive();
    let yesterday = today - Duration::days(1);
    let long_term = read_capped(&long_term_path(), MAX_LONG_TERM_CHARS);
    let yesterday_log = read_capped(&daily_path(yesterday), MAX_DAILY_CHARS);
    let today_log = read_capped(&daily_path(today), MAX_DAILY_CHARS);
    if long_term.is_empty() && yesterday_log.is_empty() && today_log.is_empty() {
        return String::new();
    }
    let mut parts = vec![
        "## Memory\nNotes carried over from previous sessions/days. Use them for context; don't recite them back verbatim unless asked.".to_string(),
    ];
    if !long_term.is_empty() {
        parts.push(format!("### Long-term notes\n{long_term}"));
    }
    if !yesterday_log.is_empty() {
        parts.push(format!("### Yesterday's log ({})\n{}", yesterday.format("%Y-%m-%d"), yesterday_log));
    }
    if !today_log.is_empty() {
        parts.push(format!("### Today's log ({})\n{}", today.format("%Y-%m-%d"), today_log));
    }
    parts.join("\n\n")
}

fn trim_entry(t: &str) -> String {
    let t = t.replace('\n', " ").trim().to_string();
    if t.len() > MAX_ENTRY_CHARS { format!("{}…", trim_bytes(&t, MAX_ENTRY_CHARS)) } else { t }
}

/// Append a short entry to today's daily log. Best-effort, never panics.
pub fn log_turn(channel_label: &str, user_text: &str, assistant_text: Option<&str>) {
    let ts = Local::now().format("%H:%M");
    let mut lines = vec![
        format!("\n## {} — #{channel_label}", ts),
        format!("- user: {}", trim_entry(user_text)),
    ];
    if let Some(a) = assistant_text.filter(|t| !t.is_empty()) {
        lines.push(format!("- agent: {}", trim_entry(a)));
    }
    let path = daily_path(Local::now().date_naive());
    append_line(&path, &lines.join("\n"));
}

fn append_line(path: &PathBuf, text: &str) {
    use std::io::Write;
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = f.write_all(text.as_bytes());
        let _ = f.write_all(b"\n");
    }
}

/// Append a curated fact to MEMORY.md. Called via the `remember` tool.
pub fn remember(fact: &str) -> Value {
    let fact = fact.trim();
    if fact.is_empty() {
        return json!({"error": "empty fact — nothing to remember"});
    }
    let date = Local::now().format("%Y-%m-%d");
    let path = long_term_path();
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let entry = format!("- [{date}] {fact}\n");
    let result = append_result(&path, &entry);
    match result {
        Ok(()) => json!({"remembered": fact}),
        Err(e) => json!({"error": e}),
    }
}

fn append_result(path: &PathBuf, text: &str) -> Result<(), String> {
    use std::io::Write;
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
        f.write_all(text.as_bytes()).map_err(|e| e.to_string())?;
        Ok(())
    } else {
        Err("couldn't open memory file".into())
    }
}
