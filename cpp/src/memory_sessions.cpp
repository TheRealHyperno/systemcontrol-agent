// memory_sessions.cpp
#include "memory_sessions.h"
#include "env.h"
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <sstream>

namespace fs = std::filesystem;
using nlohmann::json;

namespace sysc {
namespace {
fs::path memory_dir() {
    std::error_code ec;
    fs::path base;
    auto profile = getenv2("SYSTEMC_PROFILE_ID");
    if (profile && !profile->empty() && *profile != "default")
        base = fs::path(app_dir()) / ".memory" / "profiles" / *profile;
    else
        base = fs::path(app_dir()) / ".memory";
    fs::create_directories(base / "daily", ec);
    return base;
}

std::string today_iso(int offset_days = 0) {
    auto now = std::chrono::system_clock::now() + std::chrono::hours(24 * offset_days);
    std::time_t t = std::chrono::system_clock::to_time_t(now);
    std::tm tm{};
    localtime_r(&t, &tm);
    char buf[16];
    std::strftime(buf, sizeof buf, "%Y-%m-%d", &tm);
    return buf;
}

std::string read_file_capped(const fs::path& p, size_t cap) {
    std::ifstream f(p);
    if (!f) return "";
    std::stringstream ss;
    ss << f.rdbuf();
    auto text = ss.str();
    // trim
    auto s = text.find_first_not_of(" \t\r\n");
    auto e3 = text.find_last_not_of(" \t\r\n");
    if (s == std::string::npos) return "";
    text = text.substr(s, e3 == std::string::npos ? std::string::npos : e3 - s + 1);
    if (text.size() > cap) text = "...[truncated, older lines cut]...\n" + text.substr(text.size() - cap);
    return text;
}

void append_to_file(const fs::path& p, const std::string& text) {
    std::error_code ec;
    fs::create_directories(p.parent_path(), ec);
    std::ofstream f(p, std::ios::app);
    if (f) f << text << "\n";
}

// one-line entry capped at 500 chars, newlines folded to spaces
std::string trim_entry(std::string t) {
    std::string out;
    for (char c : t) out.push_back(c == '\n' ? ' ' : c);
    return out.size() > 500 ? out.substr(0, 500) + "…" : out;
}

bool write_atomic(const fs::path& p, const std::string& text) {
    std::error_code ec;
    fs::create_directories(p.parent_path(), ec);
    fs::path tmp = p.parent_path() / ("." + p.filename().string() + ".tmp");
    {
        std::ofstream f(tmp);
        if (!f) return false;
        f << text;
    }
    std::error_code rc;
    fs::rename(tmp, p, rc);
    if (rc) { fs::remove(tmp, ec); return false; }
    return true;
}

std::string read_text(const fs::path& p) {
    std::ifstream f(p);
    if (!f) return "";
    std::stringstream ss;
    ss << f.rdbuf();
    auto s = ss.str();
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r' || s.back() == ' ')) s.pop_back();
    return s;
}
}  // namespace

// ── memory ─────────────────────────────────────────────────────────────────
std::string memory_load_context() {
    auto long_term = read_file_capped(memory_dir() / "MEMORY.md", 4000);
    auto yesterday = read_file_capped(memory_dir() / "daily" / (today_iso(-1) + ".md"), 4000);
    auto today = read_file_capped(memory_dir() / "daily" / (today_iso() + ".md"), 4000);
    if (long_term.empty() && yesterday.empty() && today.empty()) return "";
    std::ostringstream o;
    o << "## Memory\nNotes carried over from previous sessions/days. Use them for context; "
         "don't recite them back verbatim unless asked.";
    if (!long_term.empty()) o << "\n\n### Long-term notes\n" << long_term;
    if (!yesterday.empty()) o << "\n\n### Yesterday's log (" << today_iso(-1) << ")\n" << yesterday;
    if (!today.empty()) o << "\n\n### Today's log (" << today_iso() << ")\n" << today;
    return o.str();
}

void memory_log_turn(const std::string& channel_label, const std::string& user_text, const std::string& assistant_text) {
    std::time_t t = std::chrono::system_clock::to_time_t(std::chrono::system_clock::now());
    std::tm tm{};
    localtime_r(&t, &tm);
    char ts[8];
    std::strftime(ts, sizeof ts, "%H:%M", &tm);
    std::ostringstream o;
    o << "\n## " << ts << " — #" << channel_label << "\n- user: " << trim_entry(user_text);
    if (!assistant_text.empty()) o << "\n- agent: " << trim_entry(assistant_text);
    append_to_file(memory_dir() / "daily" / (today_iso() + ".md"), o.str());
}

nlohmann::json memory_remember(const std::string& fact) {
    auto f = fact;
    while (!f.empty() && (f.front() == ' ' || f.front() == '\n')) f.erase(f.begin());
    while (!f.empty() && (f.back() == ' ' || f.back() == '\n')) f.pop_back();
    if (f.empty()) return json{{"error", "empty fact — nothing to remember"}};
    append_to_file(memory_dir() / "MEMORY.md", "- [" + today_iso() + "] " + f);
    return json{{"remembered", f}};
}

std::string memory_long_term_path() { return (memory_dir() / "MEMORY.md").string(); }
std::string memory_daily_path(const std::string& date_iso) { return (memory_dir() / "daily" / (date_iso + ".md")).string(); }

// ── sessions ───────────────────────────────────────────────────────────────
fs::path history_dir() {
    std::error_code ec;
    fs::path base;
    auto profile = getenv2("SYSTEMC_PROFILE_ID");
    if (profile && !profile->empty() && *profile != "default")
        base = fs::path(app_dir()) / ".history" / "profiles" / *profile;
    else
        base = fs::path(app_dir()) / ".history";
    fs::create_directories(base, ec);
    return base;
}

std::string sessions_history_dir() { return history_dir().string(); }
std::string sessions_make_key(const std::string& platform, const std::string& channel_id) {
    return platform + ":" + channel_id;
}
bool sessions_parse_key(const std::string& key, std::string* platform, std::string* cid) {
    auto colon = key.find(':');
    if (colon == std::string::npos) return false;
    auto p = key.substr(0, colon);
    auto c = key.substr(colon + 1);
    if (p != "discord" && p != "stoat" && p != "cli") return false;
    if (c.empty() || c.size() > 64) return false;
    for (char ch : c)
        if (!isalnum((unsigned char)ch) && ch != '_' && ch != '-') return false;
    if (platform) *platform = p;
    if (cid) *cid = c;
    return true;
}

static fs::path key_path(const std::string& key, const std::string& ext) {
    std::string p, c;
    if (!sessions_parse_key(key, &p, &c)) return {};
    auto stem = p == "discord" ? c : (p + "_" + c);
    return history_dir() / (stem + ext);
}

nlohmann::json sessions_load(const std::string& key) {
    auto p = key_path(key, ".json");
    if (p.empty()) return json::array();
    std::ifstream f(p);
    if (!f) return json::array();
    try {
        auto data = json::parse(f);
        return data.is_array() ? data : json::array();
    } catch (...) { return json::array(); }
}

bool sessions_save(const std::string& key, const json& history) {
    auto p = key_path(key, ".json");
    if (p.empty()) return false;
    return write_atomic(p, history.dump());
}

long sessions_stamp(const std::string& key) {
    auto p = key_path(key, ".reset");
    if (p.empty()) return 0;
    try { return std::stol(read_text(p)); } catch (...) { return 0; }
}

nlohmann::json sessions_reset(const std::string& key) {
    std::string p, c;
    if (!sessions_parse_key(key, &p, &c)) return json{{"error", "not a valid session key: " + key}};
    // count user turns in the saved history
    long cleared = 0;
    for (auto& m : sessions_load(key))
        if (m.value("role", "") == "user") ++cleared;
    // marker first, then empty the file (same order as python, so a running
    // turn notices the reset and throws its result away)
    auto now = std::chrono::system_clock::now().time_since_epoch();
    write_atomic(key_path(key, ".reset"), std::to_string(std::chrono::duration_cast<std::chrono::nanoseconds>(now).count()));
    bool emptied = sessions_save(key, json::array());
    return json{{"key", key}, {"cleared_turns", cleared}, {"ok", emptied}};
}

nlohmann::json sessions_list() {
    json out = json::array();
    std::error_code ec;
    for (auto& entry : fs::directory_iterator(history_dir(), ec)) {
        auto name = entry.path().filename().string();
        if (name.empty() || name[0] == '.' || name.rfind(".json") != name.size() - 5) continue;
        auto stem = name.substr(0, name.size() - 5);
        std::string key;
        if (stem.rfind("stoat_", 0) == 0) key = "stoat:" + stem.substr(6);
        else if (stem.find_first_not_of("0123456789") == std::string::npos) key = "discord:" + stem;
        else continue;
        std::string platform, cid;
        if (!sessions_parse_key(key, &platform, &cid)) continue;
        auto hist = sessions_load(key);
        long turns = 0;
        std::string preview;
        for (auto& m : hist)
            if (m.value("role", "") == "user") {
                ++turns;
                if (preview.empty() && m.contains("content")) {
                    auto c = m.value("content", "");
                    if (c.rfind("[from: ", 0) == 0) { auto e = c.find("] "); if (e != std::string::npos) c = c.substr(e + 2); }
                    if (c.size() > 70) c = c.substr(0, 69) + "…";
                    preview = c;
                }
            }
        auto mtime = fs::last_write_time(entry.path());
        long last_active = std::chrono::duration_cast<std::chrono::milliseconds>(mtime.time_since_epoch()).count();
        out.push_back(json{{"key", key}, {"platform", platform}, {"channel_id", cid},
                           {"label", read_text(key_path(key, ".label"))},
                           {"turns", turns}, {"last_active", (double)last_active / 1000.0}, {"preview", preview}});
    }
    std::sort(out.begin(), out.end(), [](const json& a, const json& b) { return a["last_active"] > b["last_active"]; });
    return out;
}

void sessions_set_label(const std::string& key, const std::string& label) {
    if (label.empty()) return;
    write_atomic(key_path(key, ".label"), label);
}

std::string sessions_resolve(const std::string& ref, const std::string& current, bool* ok) {
    auto lower = [](std::string s) { for (auto& c : s) c = (char)tolower(c); return s; };
    auto r = ref;
    while (!r.empty() && (r.front() == ' ' || r.front() == '\n')) r.erase(r.begin());
    while (!r.empty() && (r.back() == ' ' || r.back() == '\n')) r.pop_back();
    if (ok) *ok = false;
    std::string l = lower(r);
    if (l.empty() || l == "current" || l == "this" || l == "here") {
        if (!current.empty()) { if (ok) *ok = true; return current; }
        return "can't tell which session is the current one";
    }
    if (sessions_parse_key(r, nullptr, nullptr)) { if (ok) *ok = true; return r; }
    std::vector<std::string> matches;
    for (auto& s : sessions_list())
        if (s["channel_id"] == r) matches.push_back(s["key"]);
    if (matches.empty()) return "no saved session matches \"" + r + "\"";
    if (matches.size() == 1) { if (ok) *ok = true; return matches[0]; }
    std::string all;
    for (size_t i = 0; i < matches.size(); ++i) { if (i) all += ", "; all += matches[i]; }
    return "\"" + r + "\" matches several sessions (" + all + ") — use the full key";
}

nlohmann::json sessions_tool_schema() {
    json props = json::object();
    json s = json::object();
    s["type"] = "string";
    props["session"] = s;
    json input_schema = json::object();
    input_schema["type"] = "object";
    input_schema["properties"] = props;
    json out = json::object();
    out["name"] = "reset_session";
    out["description"] = "Clear a conversation session so it starts completely fresh: the saved chat history for a channel is wiped. Long-term memory (remember / MEMORY.md) is NOT affected. With no arguments it resets the CURRENT channel's session. You can also pass another session's key (e.g. 'discord:123456789'); that asks the owner for approval first. Only when someone asks for a fresh start or a reset.";
    out["input_schema"] = input_schema;
    return out;
}
}  // namespace sysc
