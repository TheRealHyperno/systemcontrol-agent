//! Config: .env loading and shared settings, ported from main.py + tools.py.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

pub static SETTINGS: OnceLock<Settings> = OnceLock::new();

pub struct Settings {
    map: HashMap<String, String>,
}

impl Settings {
    fn get(&self, key: &str) -> Option<String> {
        self.map.get(key).cloned()
    }
}

/// Environment lookup: real env vars first, then .env. (Python: os.environ)
pub fn getenv(key: &str) -> Option<String> {
    if let Ok(v) = std::env::var(key) {
        if !v.is_empty() {
            return Some(v);
        }
    }
    SETTINGS.get().and_then(|s| s.get(key)).filter(|v| !v.is_empty())
}

pub fn get_bool(key: &str) -> bool {
    getenv(key).map(|v| matches!(v.to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on"))
        .unwrap_or(false)
}

pub fn app_dir() -> PathBuf {
    if let Some(home) = getenv("SYSTEMC_HOME") {
        return PathBuf::from(home);
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

///models the agent's shell WORKDIR (tools.py: AGENT_WORKDIR / workspace/)
pub fn workdir() -> PathBuf {
    let base = if let Some(w) = getenv("AGENT_WORKDIR") {
        PathBuf::from(w)
    } else {
        app_dir().join("workspace")
    };
    if base.exists() {
        return base;
    }
    // fallback chain like tools.py
    let fallback = app_dir().join("workspace");
    let fallback2 = dirs_home().join("systemc_workspace");
    for cand in [base, fallback, fallback2] {
        if std::fs::create_dir_all(&cand).is_ok() {
            return cand;
        }
    }
    PathBuf::from("/tmp/systemc-workspace")
}

fn dirs_home() -> PathBuf {
    std::env::var("HOME").map(PathBuf::from).unwrap_or_else(|_| PathBuf::from("."))
}

fn unquote(v: &str) -> &str {
    let v = v.trim();
    if v.len() >= 2 {
        let b = v.as_bytes();
        if (b[0] == b'"' && b[v.len() - 1] == b'"') || (b[0] == b'\'' && b[v.len() - 1] == b'\'') {
            return &v[1..v.len() - 1];
        }
    }
    v
}

/// Load .env into the process-wide settings (never overrides real env vars,
/// same as Python's os.environ.setdefault).
pub fn load_dotenv() -> bool {
    let path = app_dir().join(".env");
    let map = load_env_file(&path);
    let found = !map.is_empty();
    let _ = SETTINGS.set(Settings { map });
    found
}

pub fn load_env_file(path: &Path) -> HashMap<String, String> {
    let mut map = HashMap::new();
    if let Ok(text) = std::fs::read_to_string(path) {
        for line in text.lines() {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let Some((k, v)) = line.split_once('=') else { continue };
            map.insert(k.trim().to_string(), unquote(v).to_string());
        }
    }
    map
}

pub fn has_discord_token() -> bool {
    getenv("DISCORD_BOT_TOKEN").is_some()
}
