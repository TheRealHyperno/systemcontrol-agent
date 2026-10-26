//! The control panel app (`systemc panel`) — an axum web server that replaces
//! the Python FastAPI webui_server / control panel web parts. Runs on
//! localhost:7842 and exposes:
//!
//!   GET  /                      the panel UI (single embedded HTML page)
//!   GET  /config                model, provider, workdir, version
//!   GET  /sessions              list saved sessions
//!   POST /sessions/reset        {key} — clear one channel's history
//!   GET  /memory                long-term + today/yesterday logs
//!   POST /memory/remember       {fact}
//!   POST /tools/*               run_shell, run_shell_elevated, read_file,
//!                               write_file, list_dir, run_python, web_search
//!   POST /ollama/chat           streaming proxy so the browser UI can chat
//!                               (avoids CORS and keeps tool interception local)
//!   POST /service/install       {animate: systemd --user install}
//!   POST /service/install       {linger: loginctl enable-linger}
//!   POST /service/uninstall
//!   GET  /api/status

use crate::memory;
use crate::sessions;
use crate::tools;
use crate::version::{SYSTEMC_DISPLAY, SYSTEMC_VERSION};

use axum::http::StatusCode;
use axum::response::{Html, IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{json, Value};

pub struct PanelState {
    pub port: u16,
}

pub async fn serve(port: u16) -> Result<(), String> {
    let state = std::sync::Arc::new(PanelState { port });
    let app = Router::new()
        .route("/", get(ui))
        .route("/config", get(config))
        .route("/sessions", get(list_sessions))
        .route("/sessions/reset", post(reset_session))
        .route("/memory", get(get_memory))
        .route("/memory/remember", post(remember_fact))
        .route("/api/status", get(status))
        .route("/tools/run_shell", post(run_shell_ep))
        .route("/tools/run_shell_elevated", post(run_shell_elevated_ep))
        .route("/tools/read_file", post(read_file_ep))
        .route("/tools/write_file", post(write_file_ep))
        .route("/tools/list_dir", post(list_dir_ep))
        .route("/tools/run_python", post(run_python_ep))
        .route("/tools/web_search", post(web_search_ep))
        .route("/ollama/chat", post(ollama_chat))
        .route("/service/status", get(service_status))
        .route("/service/install", post(service_install))
        .route("/service/uninstall", post(service_uninstall))
        .with_state(state);
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", port))
        .await
        .map_err(|e| format!("couldn't bind port {port}: {e}"))?;
    println!("╔════════════════════════════════════╗");
    println!("║   systemc control panel (rust)     ║");
    println!("╚════════════════════════════════════╝");
    println!("  open → http://localhost:{port}");
    println!("  release → {SYSTEMC_DISPLAY}");
    println!();
    axum::serve(listener, app).await.map_err(|e| e.to_string())
}

async fn ui() -> Html<&'static str> {
    Html(include_str!("panel_ui.html"))
}

fn env_of() -> Value {
    json!({
        "release": SYSTEMC_DISPLAY,
        "model": crate::providers::describe().0,
        "provider": crate::config::getenv("AGENT_PROVIDER").unwrap_or_else(|| "ollama".into()),
        "workdir": tools::workdir().display().to_string(),
        "version": SYSTEMC_VERSION,
    })
}

async fn config() -> Json<Value> {
    Json(env_of())
}

async fn status() -> Json<Value> {
    let mut out = env_of();
    out["discord"] = json!(crate::config::has_discord_token());
    out["history_dir"] = json!(sessions::history_dir().display().to_string());
    out["memory_dir"] = json!(memory::load_context().len()); // cheap liveness probe
    Json(out)
}

async fn list_sessions() -> Json<Vec<Value>> {
    Json(sessions::list_sessions())
}

async fn reset_session(Json(body): Json<Value>) -> Response {
    let key = body.get("key").and_then(|v| v.as_str()).unwrap_or_default();
    match sessions::reset(key) {
        Ok(res) => (StatusCode::OK, Json(res)).into_response(),
        Err(e) => (StatusCode::BAD_REQUEST, Json(json!({"error": e}))).into_response(),
    }
}

async fn get_memory() -> Json<Value> {
    use chrono::{Duration as ChronoDuration, Local};
    let today = Local::now().date_naive();
    let yesterday = today - ChronoDuration::days(1);
    let read = |p: std::path::PathBuf| std::fs::read_to_string(p).unwrap_or_default();
    Json(json!({
        "long_term": read(memory::long_term_path()),
        "today": read(memory::daily_path(today)),
        "yesterday": read(memory::daily_path(yesterday)),
        "date": today.format("%Y-%m-%d").to_string(),
    }))
}

async fn remember_fact(Json(body): Json<Value>) -> Json<Value> {
    let fact = body.get("fact").and_then(|v| v.as_str()).unwrap_or_default();
    Json(memory::remember(fact))
}

async fn run_shell_ep(Json(body): Json<Value>) -> Json<Value> {
    let command = body.get("command").and_then(|v| v.as_str()).unwrap_or_default();
    Json(tools::run_shell(command, false).await)
}

async fn run_shell_elevated_ep(Json(body): Json<Value>) -> Response {
    let command = body.get("command").and_then(|v| v.as_str()).unwrap_or_default();
    if !tools::elevated_enabled() {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"error": "elevated commands are turned off on this machine", "exit_code": -1})),
        ).into_response();
    }
    Json(tools::run_shell_elevated(command).await).into_response()
}

async fn read_file_ep(Json(body): Json<Value>) -> Json<Value> {
    let path = body.get("path").and_then(|v| v.as_str()).unwrap_or_default();
    Json(tools::read_file(path))
}

async fn write_file_ep(Json(body): Json<Value>) -> Json<Value> {
    let path = body.get("path").and_then(|v| v.as_str()).unwrap_or_default();
    let content = body.get("content").and_then(|v| v.as_str()).unwrap_or_default();
    Json(tools::write_file(path, content))
}

async fn list_dir_ep(Json(body): Json<Value>) -> Json<Value> {
    let path = body.get("path").and_then(|v| v.as_str()).unwrap_or(".");
    Json(tools::list_dir(path))
}

async fn run_python_ep(Json(body): Json<Value>) -> Json<Value> {
    let code = body.get("code").and_then(|v| v.as_str()).unwrap_or_default();
    Json(tools::run_python(code).await)
}

async fn web_search_ep(Json(body): Json<Value>) -> Json<Value> {
    let q = body.get("query").and_then(|v| v.as_str()).unwrap_or_default();
    let max = body.get("max_results").and_then(|v| v.as_i64()).unwrap_or(5).clamp(1, 10) as usize;
    Json(tools::web_search(q, max).await)
}

/// Streaming NDJSON proxy for the browser chat.
async fn ollama_chat(Json(body): Json<Value>) -> Response {
    let cfg = crate::providers::ollama_settings();
    let mut req = reqwest::Client::new()
        .post(format!("{}/api/chat", cfg.host))
        .json(&body);
    if !cfg.api_keys.is_empty() {
        req = req.bearer_auth(&cfg.api_keys[0]);
    }
    match req.send().await {
        Ok(resp) => {
            let stream = resp.bytes_stream();
            Response::builder()
                .header("content-type", "application/x-ndjson")
                .body(axum::body::Body::from_stream(stream))
                .unwrap()
        }
        Err(e) => (StatusCode::BAD_GATEWAY, Json(json!({"error": e.to_string()}))).into_response(),
    }
}

async fn service_status() -> Json<Value> {
    let out = std::process::Command::new("systemctl")
        .arg("--user").arg("is-active").arg("systemc.service")
        .output();
    let active = out.map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string()).unwrap_or_default();
    Json(json!({"active": active, "enabled_by_panel": true}))
}

async fn service_install(Json(body): Json<Value>) -> Json<Value> {
    let linger = body.get("linger").and_then(|v| v.as_bool()).unwrap_or(false);
    Json(crate::service::install(None, linger, false))
}

async fn service_uninstall() -> Json<Value> {
    Json(crate::service::uninstall(None, false))
}
