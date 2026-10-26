// chat_platform.cpp
#include "chat_platform.h"
#include "env.h"
#include "http.h"
#include "tools.h"
#include <algorithm>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <thread>

using nlohmann::json;
namespace fs = std::filesystem;

namespace sysc {

std::string sender_tag(const Author& a) {
    return "[from: " + a.name + " (@" + a.name + ", id " + a.id + ")]:";
}

namespace {
std::string rest_base() { return "https://discord.com/api/v10"; }

void sleep_s(int s) { std::this_thread::sleep_for(std::chrono::seconds(s)); }

std::string clip(const std::string& s, size_t n, bool tail_dots = false) {
    return s.size() <= n ? s : s.substr(0, n) + (tail_dots ? "" : "…");
}
}  // namespace

json DiscordRest::api(const std::string& method, const std::string& path, const json& body) const {
    HttpResponse r = http_request(method, rest_base() + path,
                                  body.is_null() ? "" : body.dump(), token, {}, 30);
    if (r.status >= 200 && r.status < 300) {
        try { return json::parse(r.body); } catch (...) { return json(); }
    }
    return json{{"__error__", "discord HTTP " + std::to_string(r.status) + ": " + r.body.substr(0, 300)}};
}

// split at 2000 chars, preferring newline breaks
std::vector<std::string> chunk_message(const std::string& content) {
    std::vector<std::string> chunks;
    std::string rest = content;
    while (rest.size() > MAX_MESSAGE_LEN) {
        size_t cut = MAX_MESSAGE_LEN;
        auto nl = rest.rfind('\n', MAX_MESSAGE_LEN);
        if (nl != std::string::npos) cut = nl;
        chunks.push_back(rest.substr(0, cut));
        while (cut < rest.size() && rest[cut] == '\n') ++cut;
        rest = rest.substr(cut);
    }
    chunks.push_back(rest);
    return chunks;
}

void DiscordRest::send_message(const std::string& content) const {
    for (auto& chunk : chunk_message(content)) {
        auto r = api("POST", "/channels/" + channel_id + "/messages", json{{"content", chunk}});
        if (r.contains("__error__")) {
            std::cerr << "[discord] send failed: " << r["__error__"].get<std::string>() << "\n";
            sleep_s(2);
        }
    }
}

json DiscordRest::send_file(const std::string& path, const std::string& caption) const {
    std::error_code ec;
    if (!fs::exists(path, ec)) return json{{"error", "couldn't read file for upload: " + path}};
    std::string name = fs::path(path).filename().string();
    if (name.empty()) name = "file.bin";
    auto payload = json{{"content", caption}}.dump();
    auto r = http_upload(rest_base() + "/channels/" + channel_id + "/messages",
                         token, "payload_json", payload, path, name);
    return r.status < 300 ? json{{"sent", true}} : json{{"error", "HTTP " + std::to_string(r.status) + ": " + r.body.substr(0, 200)}};
}

void DiscordRest::typing() const {
    api("POST", "/channels/" + channel_id + "/typing", json());
}

json DiscordRest::fetch_history(int limit) const {
    auto list = api("GET", "/channels/" + channel_id + "/messages?limit=" + std::to_string(std::max(1, std::min(limit, 100))), json());
    json out = json::array();
    auto arr = list.is_array() ? list : json::array();
    for (auto it = arr.rbegin(); it != arr.rend(); ++it)
        out.push_back(json{{"id", (*it).value("id", "")},
                           {"author", (*it)["author"].value("username", "")},
                           {"content", (*it).value("content", "")},
                           {"timestamp", (*it).value("timestamp", "")}});
    return out;
}

bool DiscordRest::has_reaction(const std::string& mid, const std::string& emoji) const {
    auto url = rest_base() + "/channels/" + channel_id + "/messages/" + mid +
               "/reactions/" + url_encode(emoji) + "?type=1&limit=100";
    auto r = http_request("GET", url, "", token, {}, 20);
    if (r.status != 200) return false;
    try {
        for (auto& u : json::parse(r.body))
            if (u.value("id", "") != app_id) return true;  // non-bot user reacted
    } catch (...) {}
    return false;
}

bool DiscordRest::request_approval(const std::string& command, bool elevated, int timeout_secs) const {
    auto title = elevated ? std::string("⚠️ ELEVATED (sudo) — approve this command?") : std::string("⚠️ Approve this command?");
    auto body = title + "\n```" + clip(command, 1500) + "```\nReact ✅ to approve or ❌ to deny. Auto-deny in "
              + std::to_string(timeout_secs) + "s.";
    auto msg = api("POST", "/channels/" + channel_id + "/messages", json{{"content", body}});
    if (msg.contains("__error__")) return false;
    auto mid = msg.value("id", "");
    auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(timeout_secs);
    while (std::chrono::steady_clock::now() < deadline) {
        sleep_s(2);
        if (has_reaction(mid, "❌")) {
            api("PATCH", "/channels/" + channel_id + "/messages/" + mid, json{{"content", "❌ Denied."}});
            return false;
        }
        if (has_reaction(mid, "✅")) return true;
    }
    api("DELETE", "/channels/" + channel_id + "/messages/" + mid, json());
    return false;
}

// ── ChannelAdapter ──────────────────────────────────────────────────────────
void Channel::send(const std::string& content) const {
    if (kind == Cli) {
        std::cout << "\x1b[1;36m" << content << "\x1b[0m\n";
    } else {
        rest.send_message(content);
    }
}

json Channel::send_file(const std::string& path, const std::string& caption) const {
    if (kind == Cli) {
        std::cout << "\x1b[1;36m[file] " << path << " — " << caption << "\x1b[0m\n";
        return json{{"sent", true}};
    }
    return rest.send_file(path, caption);
}

void Channel::typing() const {
    if (kind == Discord) rest.typing();
}

bool Channel::request_approval(const std::string& command, bool elevated, int timeout_secs) const {
    if (kind == Discord) return rest.request_approval(command, elevated, timeout_secs);
    std::cerr << "\n⚠️  APPROVAL REQUESTED" << (elevated ? " [ELEVATED / sudo]" : "") << ":\n"
              << command << "\nType 'ok' to approve, anything else to deny: " << std::flush;
    std::string line;
    std::getline(std::cin, line);
    for (auto& c : line) c = (char)tolower((unsigned char)c);
    return line == "ok";
}

json Channel::history(int limit) const {
    if (kind == Discord) return rest.fetch_history(limit);
    return json::array();
}

std::string download_attachment(const std::string& url, const std::string& filename, bool* ok) {
    auto safe = filename;
    auto fix = [](std::string& s, const std::string& bad, const std::string& good) {
        size_t p; while ((p = s.find(bad)) != std::string::npos) s.replace(p, bad.size(), good); };
    fix(safe, "/", "_");
    fix(safe, "..", "_");
    auto r = http_request("GET", url, "", "", {}, 60);
    if (ok) *ok = r.status == 200 && !r.body.empty();
    if (ok && *ok) {
        auto dest = fs::path(workdir()) / safe;
        std::ofstream f(dest, std::ios::binary);
        f << r.body;
        return safe;
    }
    return "";
}
}  // namespace sysc
