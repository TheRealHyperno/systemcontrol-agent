// discord.cpp — the Discord half: WSS gateway + REST pieces (port of discord.rs)
#include "discord.h"
#include "chat_platform.h"
#include "env.h"
#include "http.h"
#include "memory_sessions.h"
#include "providers.h"
#include "tools.h"
#include <algorithm>
#include <chrono>
#include <iostream>
#include <thread>

using nlohmann::json;

namespace sysc {

namespace {
const char* GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json";

// GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT
constexpr long long INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15);

std::string lower(std::string s) {
    for (auto& c : s) c = (char)tolower((unsigned char)c);
    return s;
}

std::string label_for(const json& d, const Author& author) {
    if (d.contains("guild_id"))
        return "#" + d.value("channel_name", std::string());
    return "DM with " + author.name;
}

std::string tail(const std::string& s, size_t n) {
    return s.size() <= n ? s : s.substr(s.size() - n);
}
}  // namespace

void DiscordBot::run(Agent& agent) {
    mode_ = lower(getenv3("BOT_MODE", "WAKEWORD"));
    if (mode_ != "everything" && mode_ != "mention") mode_ = "wakeword";
    wake_ = lower(getenv3("WAKE_WORD", "hey claw"));
    {
        auto v = getenv3("ALLOWED_CHANNEL_IDS", "");
        std::string cur;
        for (char c : v) {
            if (c == ',') {
                if (!cur.empty()) { allowed_.push_back(cur); cur.clear(); }
            } else if (c != ' ') cur += c;
        }
        if (!cur.empty()) allowed_.push_back(cur);
    }

    std::cout << "Starting Discord bot...\n"
              << "  mode:      " << mode_ << "\n";
    if (mode_ == "wakeword") std::cout << "  wake word: \"" << wake_ << "\"\n";
    if (allowed_.empty()) {
        std::cout << "  WARNING: no ALLOWED_CHANNEL_IDS set — responding everywhere\n";
    } else {
        std::string all;
        for (size_t i = 0; i < allowed_.size(); ++i) { if (i) all += ", "; all += allowed_[i]; }
        std::cout << "  channels:  " << all << "\n";
    }

    while (true) {
        try {
            gateway_session(agent);
            std::cerr << "[discord] gateway closed cleanly — reconnecting in 5s\n";
        } catch (const std::exception& e) {
            std::cerr << "[discord] gateway error: " << e.what() << " — reconnecting in 5s\n";
        }
        std::this_thread::sleep_for(std::chrono::seconds(5));
    }
}

void DiscordBot::gateway_session(Agent& agent) {
    WebSocket ws;
    if (!ws.connect(GATEWAY_URL)) throw std::runtime_error("connect: websocket failed");

    long long heartbeat_ms = 41250;
    bool identified = false;
    std::string me_id;
    long long last_seq = -1;

    auto send_kv = [&](const json& v) {
        if (!ws.send_text(v.dump())) throw std::runtime_error("ws send failed");
    };

    for (;;) {
        // wait up to the heartbeat interval, then send op 1
        auto opt = ws.read_message(heartbeat_ms);
        if (!opt) {  // timeout → heartbeat
            send_kv(json{{"op", 1}, {"d", last_seq >= 0 ? json(last_seq) : json(nullptr)}});
            continue;
        }
        if (opt->empty()) throw std::runtime_error("gateway closed");
        json v;
        try { v = json::parse(*opt); } catch (...) { continue; }

        auto op = v.value("op", -1);
        if (op == 10) {  // HELLO
            heartbeat_ms = v["d"].value("heartbeat_interval", (long long)41250);
            if (identified && last_seq >= 0) {
                send_kv(json{{"op", 6}, {"d", json{{"token", token_}, {"seq", last_seq}}}});
            } else {
                send_kv(json{{"op", 2},
                             {"d", json{{"token", token_},
                                        {"intents", INTENTS},
                                        {"properties", json{{"os", "linux"},
                                                            {"browser", "systemc-cpp"},
                                                            {"device", "systemc-cpp"}}}}}});
                identified = true;
            }
        } else if (op == 0) {  // dispatch
            if (v.contains("s") && v["s"].is_number()) last_seq = v["s"].get<long long>();
            auto t = v.value("t", "");
            if (t == "READY") {
                me_id = v["d"]["user"].value("id", "");
                std::cout << "✓ logged in as " << v["d"]["user"].value("username", "")
                          << " (id " << me_id << ")\n";
            } else if (t == "MESSAGE_CREATE") {
                // one thread per message so heartbeats keep flowing during turns
                std::thread([this, &agent, d = v["d"], me_id]() mutable { dispatch(d, me_id, agent); }).detach();
            }
        } else if (op == 7 || op == 9) {
            throw std::runtime_error("server asked for reconnect");
        }
        // op 11 heartbeat ACK; other ops ignored
    }
}

void DiscordBot::dispatch(const json& d, const std::string& me_id, Agent& agent) {
    try {
        auto content = d.value("content", "");
        if (content.empty()) return;  // attachments-only etc.

        bool author_bot = d["author"].contains("bot") ? d["author"]["bot"].get<bool>() : false;
        if (d["author"].value("id", "") == me_id || author_bot) return;

        auto channel_id = d.value("channel_id", "");
        if (!allowed_.empty() &&
            std::find(allowed_.begin(), allowed_.end(), channel_id) == allowed_.end())
            return;

        Author author{
            d["author"].value("id", ""),
            d["author"].contains("global_name") && !d["author"]["global_name"].is_null()
                ? d["author"]["global_name"].get<std::string>()
                : d["author"].value("username", ""),
        };
        DiscordRest rest{token_, channel_id, me_id, label_for(d, author)};

        // ── !command shortcut: bypass the LLM, run shell directly ──────────
        if (content.size() > 1 && content[0] == '!') {
            auto command = content.substr(1);
            rest.typing();
            if (is_destructive(command) && !rest.request_approval(command, false, 60)) return;
            auto result = run_shell(command, false);
            auto stdout_s = result.value("stdout", std::string());
            while (!stdout_s.empty() && (stdout_s.back() == '\n' || stdout_s.back() == ' ')) stdout_s.pop_back();
            auto exit = result.value("exit_code", -999);
            auto out = stdout_s.empty() ? "(no output)" : tail(stdout_s, 1900);
            rest.send_message("```\n" + out + "\n```\nexit: `" + std::to_string(exit) + "`");
            return;
        }

        // ── /reset: clear this channel's conversation context ──────────────
        std::string stripped = content;
        auto scrub = [&](const std::string& tag) {
            size_t p;
            while ((p = stripped.find(tag)) != std::string::npos) stripped.erase(p, tag.size());
        };
        scrub("<@" + me_id + ">");
        scrub("<@!" + me_id + ">");
        auto l = lower(stripped);
        auto first = l.find_first_not_of(" \n\t");
        stripped = first == std::string::npos ? "" : stripped.substr(first);
        if (lower(stripped) == "/reset") {
            if (!sessions_reset(sessions_make_key("discord", channel_id)).contains("error"))
                rest.send_message("🧹 Context cleared for this channel. Long-term memory is untouched.");
            else
                rest.send_message("⚠️ couldn't clear the session.");
            return;
        }

        // ── should the agent respond? ───────────────────────────────────────
        auto mentioned = content.find("<@" + me_id + ">") != std::string::npos
                      || content.find("<@!" + me_id + ">") != std::string::npos;
        bool responds =
            mode_ == "everything" ? true
            : mode_ == "mention" ? mentioned
            : (mentioned || lower(content).rfind(wake_, 0) == 0);
        if (!responds) return;

        // strip the wake word (mention already stripped in the /reset pass)
        auto prompt = stripped;
        if (lower(prompt).rfind(wake_, 0) == 0) {
            auto after = prompt.substr(wake_.size());
            auto f = after.find_first_not_of(" \n\t");
            prompt = f == std::string::npos ? "" : after.substr(f);
        }
        if (prompt.find_first_not_of(" \n\t") == std::string::npos) prompt = content;

        rest.typing();

        // attachments auto-download into the workspace
        std::vector<std::string> saved;
        if (d.contains("attachments") && d["attachments"].is_array()) {
            for (auto& att : d["attachments"]) {
                if (att.contains("url") && att.contains("filename")) {
                    bool ok = false;
                    auto p = download_attachment(att.value("url", ""), att.value("filename", ""), &ok);
                    if (ok) saved.push_back(p);
                }
            }
        }
        if (!saved.empty()) {
            std::string list;
            for (size_t i = 0; i < saved.size(); ++i) { if (i) list += ", "; list += saved[i]; }
            prompt += "\n\n[Attached files saved to workspace: " + list + "]";
        }

        auto key = sessions_make_key("discord", channel_id);
        auto history = sessions_load(key);
        std::vector<Message> hist;
        for (auto& m : history) {
            Message msg;
            msg.role = m.value("role", "");
            msg.content = m.value("content", "");
            if (m.contains("tool_call_id")) msg.tool_call_id = m.value("tool_call_id", "");
            if (m.contains("name")) msg.tool_name = m.value("name", "");
            if (m.contains("tool_calls") && m["tool_calls"].is_array())
                for (auto& tc : m["tool_calls"])
                    msg.tool_calls.push_back(ToolCall{tc.value("id", ""), tc.value("name", ""),
                                                      tc.value("input", json::object())});
            hist.push_back(msg);
        }
        Channel channel;
        channel.kind = Channel::Discord;
        channel.rest = rest;
        agent.run_turn(channel, prompt, &hist, &author);

        // trim to the last 60 messages, snapped forward to a "user" boundary
        // (a flat slice can orphan a tool result if it cuts mid-turn)
        size_t start = hist.size() > 60 ? hist.size() - 60 : 0;
        std::vector<Message> t(hist.begin() + start, hist.end());
        while (!t.empty() && t.front().role != "user") t.erase(t.begin());
        json out = json::array();
        for (auto& m : t) {
            json jm = (m.role == "user" || m.role == "assistant")
                          ? json{{"role", m.role}, {"content", m.content}}
                          : json{{"role", m.role}, {"content", m.content},
                                 {"tool_call_id", m.tool_call_id}, {"name", m.tool_name}};
            if (m.role == "assistant" && !m.tool_calls.empty()) {
                json tcs = json::array();
                for (auto& tc : m.tool_calls)
                    tcs.push_back(json{{"id", tc.id}, {"name", tc.name}, {"input", tc.input}});
                jm["tool_calls"] = tcs;
            }
            out.push_back(jm);
        }
        sessions_save(key, out);
        sessions_set_label(key, rest.label);
    } catch (const std::exception& e) {
        std::cerr << "[discord] dispatch error: " << e.what() << "\n";
    }
}
}  // namespace sysc
