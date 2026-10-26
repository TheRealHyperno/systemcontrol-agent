//! systemc — the Rust rewrite of SystemC's Python core.
//!
//!   systemc run                     start the Discord agent (+ /reset, !cmd)
//!   systemc panel [--port N]        the web control panel app (localhost:7842)
//!   systemc chat                    local terminal agent (no Discord needed)
//!   systemc models                  list models on the configured Ollama host
//!   systemc service install|uninstall|status [--profile] [--linger] [--system]
//!
//! Config comes from .env (SYSTEMC_HOME / cwd), same keys as the Python version.

mod agent;
mod chat_platform;
mod config;
mod discord;
mod memory;
mod panel;
mod prompt;
mod providers;
mod sessions;
mod service;
mod tools;
mod version;

use chat_platform::ChannelAdapter;
use std::sync::Arc;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let head = args.first().map(|s| s.as_str()).unwrap_or("run");

    // parity with python main.py: --install-service / --uninstall-service flags
    if head == "--install-service" || head == "--uninstall-service" {
        service_cmd(
            if head == "--install-service" { "install" } else { "uninstall" },
            &args[1..],
        );
        return;
    }

    match head {
        "run" => run(&args[1..]),
        "panel" | "webui" => panel_cmd(&args[1..]),
        "chat" => chat_cmd().expect("chat failed"),
        "models" => models_cmd(),
        "service" => service_cmd(args.get(1).map(|s| s.as_str()).unwrap_or("status"), &args[2..]),
        "version" | "--version" | "-V" => println!("{}", version::banner()),
        "--tui" => {
            eprintln!("--tui removed: the Rust control panel is `systemc panel`");
            std::process::exit(1);
        }
        "help" | "--help" | "-h" => help(),
        other => {
            eprintln!("unknown subcommand: {other}");
            help();
            std::process::exit(1);
        }
    }
}

fn help() {
    println!("{}", version::banner());
    println!(
        r#"
subcommands:
  run                      start the Discord agent (needs DISCORD_BOT_TOKEN in .env)
  panel [--port N]         start the web control panel app (default port 7842)
  chat                     terminal agent: talk to the agent in this shell
  models                   list model IDs the configured Ollama host offers
  service install|uninstall|status [--profile <id>] [--linger] [--system]
"#
    );
}

fn load_config() {
    config::load_dotenv();
    println!("{}", version::banner());
}

fn run(_args: &[String]) {
    load_config();
    if !config::has_discord_token() {
        eprintln!(
            "No DISCORD_BOT_TOKEN in .env — create a bot token (https://discord.com/developers/applications),\n\
             put it in .env/$SYSTEMC_HOME, and enable the Message Content intent for the bot."
        );
        std::process::exit(1);
    }
    let agent = match agent::Agent::new() {
        Ok(a) => Arc::new(a),
        Err(e) => {
            eprintln!("[agent] {e}");
            std::process::exit(1);
        }
    };
    let token = config::getenv("DISCORD_BOT_TOKEN").unwrap();
    let bot = discord::DiscordBot::new(token);
    let rt = tokio::runtime::Runtime::new().expect("tokio runtime");
    rt.block_on(async move {
        println!("Starting Discord bot...");
        bot.run(agent).await;
    });
}

fn panel_cmd(args: &[String]) {
    load_config();
    let mut port = 7842u16;
    let mut it = args.iter();
    while let Some(a) = it.next() {
        if a == "--port" {
            if let Some(v) = it.next() {
                port = v.parse().unwrap_or(7842);
            }
        }
    }
    let rt = tokio::runtime::Runtime::new().expect("tokio runtime");
    rt.block_on(async move {
        if let Err(e) = panel::serve(port).await {
            eprintln!("[panel] {e}");
            std::process::exit(1);
        }
    });
}

fn chat_cmd() -> Result<(), String> {
    use std::io::Write;
    load_config();
    let agent = agent::Agent::new()?;
    let rt = tokio::runtime::Runtime::new().expect("tokio runtime");
    let mut history: Vec<providers::Message> = Vec::new();
    eprintln!("(terminal agent — type a message, ctrl-d to exit)");
    loop {
        eprint!("you ▸ ");
        let _ = std::io::stderr().flush();
        let mut line = String::new();
        if std::io::stdin().read_line(&mut line).is_err() {
            break;
        }
        let text = line.trim().to_string();
        if text.is_empty() {
            continue;
        }
        let agent_ref = &agent;
        let hist = &mut history;
        rt.block_on(async {
            if let Err(e) = agent_ref
                .run_turn(ChannelAdapter::Cli, &text, hist, None)
                .await
            {
                eprintln!("[agent] {e}");
            }
        });
    }
    Ok(())
}

fn models_cmd() {
    load_config();
    let cfg = providers::ollama_settings();
    println!("[{}] {}", cfg.mode, cfg.host);
    let rt = tokio::runtime::Runtime::new().expect("tokio runtime");
    rt.block_on(async {
        let mut keys: Vec<String> = cfg.api_keys.clone();
        keys.push(String::new());
        for key in keys {
            match providers::list_ollama_models(&cfg.host, &key).await {
                Ok(names) => {
                    for n in names {
                        println!("  {n}");
                    }
                    break;
                }
                Err(e) => eprintln!("  key failed: {e}"),
            }
        }
    });
}

fn service_cmd(action: &str, args: &[String]) {
    let (profile, linger, system) = {
        let o = service::parse_service_args(args, action);
        (o.profile, o.linger, o.system)
    };
    load_config();
    match action {
        "install" => {
            service::install(profile, linger, system);
        }
        "uninstall" => {
            service::uninstall(profile, system);
        }
        "status" => println!("{}", service::status()),
        other => {
            eprintln!("unknown service action: {other}");
        }
    }
}
