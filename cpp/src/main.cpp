// systemc (cpp edition) — the C++ rewrite of SystemC's Python core.
//
//   systemc run                     start the Discord agent (+ /reset, !cmd)
//   systemc panel [--port N]        the web control panel app (localhost:7842)
//   systemc chat                    local terminal agent (no Discord needed)
//   systemc models                  list models on the configured Ollama host
//   systemc service install|uninstall|status [--profile] [--linger] [--system]
//
// Config comes from .env (SYSTEMC_HOME / cwd), same keys as the Python version.

#include "agent.h"
#include "chat_platform.h"
#include "discord.h"
#include "env.h"
#include "memory_sessions.h"
#include "panel.h"
#include "providers.h"
#include "service.h"
#include "tools.h"

#include <algorithm>
#include <iostream>
#include <thread>

using nlohmann::json;

namespace sysc {
static void run_cmd();
static void chat_cmd();
static void models_cmd();
static void panel_cmd(const std::vector<std::string>& args);
static void service_cmd(const std::string& action, const std::vector<std::string>& args);

static void load_config() {
    load_dotenv();
    std::cout << banner() << "\n";
}

static void help() {
    std::cout << banner() << "\n"
              << "\nsubcommands:\n"
              << "  run                      start the Discord agent (needs DISCORD_BOT_TOKEN in .env)\n"
              << "  panel [--port N]         start the web control panel app (default port 7842)\n"
              << "  chat                     terminal agent: talk to the agent in this shell\n"
              << "  models                   list model IDs the configured Ollama host offers\n"
              << "  service install|uninstall|status [--profile <id>] [--linger] [--system]\n";
}

static void service_cmd(const std::string& action, const std::vector<std::string>& args) {
    load_config();
    if (action == "install") {
        service_install(parse_service_args(args, action));
    } else if (action == "uninstall") {
        auto o = parse_service_args(args, action);
        service_uninstall(o.profile, o.system);
    } else if (action == "status") {
        std::cout << service_status() << "\n";
    } else {
        std::cerr << "unknown service action: " << action << "\n";
    }
}

static void run_cmd() {
    load_config();
    if (!has_discord_token()) {
        std::cerr << "No DISCORD_BOT_TOKEN in .env — create a bot token (https://discord.com/developers/applications),\n"
                     " put it in .env/$SYSTEMC_HOME, and enable the Message Content intent for the bot.\n";
        std::exit(1);
    }
    try {
        Agent agent;
        DiscordBot bot(getenv3("DISCORD_BOT_TOKEN", ""));
        bot.run(agent);
    } catch (const std::exception& e) {
        std::cerr << "[agent] " << e.what() << "\n";
        std::exit(1);
    }
}

static void chat_cmd() {
    load_config();
    Agent agent;
    std::vector<Message> history;
    std::cerr << "(terminal agent — type a message, ctrl-d to exit)\n";
    std::string line;
    while (true) {
        std::cerr << "you \u25b8 " << std::flush;
        if (!std::getline(std::cin, line)) break;
        auto text = line;
        while (!text.empty() && (text.back() == ' ' || text.back() == '\r')) text.pop_back();
        if (text.empty()) continue;
        Channel channel;  // Cli
        try {
            agent.run_turn(channel, text, &history, nullptr);
        } catch (const std::exception& e) {
            std::cerr << "[agent] " << e.what() << "\n";
        }
    }
}

static void models_cmd() {
    load_config();
    auto cfg = ollama_settings();
    std::cout << "[" << cfg.mode << "] " << cfg.host << "\n";
    auto keys = cfg.api_keys;
    keys.push_back("");
    for (auto& key : keys) {
        try {
            for (auto& n : list_ollama_models(cfg.host, key)) std::cout << "  " << n << "\n";
            return;
        } catch (const std::exception& e) {
            std::cerr << "  key failed: " << e.what() << "\n";
        }
    }
}

static void panel_cmd(const std::vector<std::string>& args) {
    load_config();
    int port = 7842;
    for (size_t i = 0; i < args.size(); ++i)
        if (args[i] == "--port" && i + 1 < args.size()) port = std::atoi(args[i + 1].c_str());
    panel_serve(port);
}
}  // namespace sysc

int main(int argc, char** argv) {
    std::vector<std::string> args(argv + 1, argv + argc);
    auto head = args.empty() ? "run" : args.front();
    std::vector<std::string> rest(args.begin() + 1, args.end());

    if (head == "--install-service" || head == "--uninstall-service") {
        sysc::service_cmd(head == "--install-service" ? "install" : "uninstall", rest);
        return 0;
    }
    std::string action = rest.empty() ? "" : rest.front();
    std::vector<std::string> rest2(rest.size() > 1 ? rest.size() - 1 : 0);
    if (rest.size() > 1) std::copy(rest.begin() + 1, rest.end(), rest2.begin());

    if (head == "run") sysc::run_cmd();
    else if (head == "panel" || head == "webui") sysc::panel_cmd(rest);
    else if (head == "chat") sysc::chat_cmd();
    else if (head == "models") sysc::models_cmd();
    else if (head == "service") sysc::service_cmd(rest.empty() ? "status" : rest.front(), rest2);
    else if (head == "version" || head == "--version" || head == "-V") std::cout << sysc::banner() << "\n";
    else if (head == "--tui") {
        std::cerr << "--tui removed: the C++ control panel is `systemc panel`\n";
        return 1;
    } else if (head == "help" || head == "--help" || head == "-h") sysc::help();
    else {
        std::cerr << "unknown subcommand: " << head << "\n";
        sysc::help();
        return 1;
    }
    return 0;
}
