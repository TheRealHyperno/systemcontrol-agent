// env.cpp
#include "env.h"
#include <cstdlib>
#include <unistd.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>

namespace fs = std::filesystem;

namespace sysc {
namespace {
std::map<std::string, std::string>& settings() {
    static std::map<std::string, std::string> m;
    return m;
}

std::string unquote(std::string v) {
    while (!v.empty() && (v.front() == ' ' || v.front() == '\t')) v.erase(v.begin());
    while (!v.empty() && (v.back() == ' ' || v.back() == '\t')) v.pop_back();
    if (v.size() >= 2 && (v.front() == '"' || v.front() == '\'') && v.back() == v.front())
        return v.substr(1, v.size() - 2);
    return v;
}
}  // namespace

bool load_dotenv() {
    auto& m = settings();
    auto path = fs::path(app_dir()) / ".env";
    std::ifstream f(path);
    if (!f) return false;
    bool found = false;
    std::string line;
    while (std::getline(f, line)) {
        auto t = unquote(line);
        if (t.empty() || t[0] == '#') continue;
        auto eq = t.find('=');
        if (eq == std::string::npos) continue;
        auto key = unquote(t.substr(0, eq));
        auto val = unquote(t.substr(eq + 1));
        if (key.empty()) continue;
        m[key] = val;  // process env always wins: getenv2 checks env first
        found = true;
    }
    return found;
}

std::string app_dir() {
    if (const char* home = std::getenv("SYSTEMC_HOME"); home && *home) return home;
    char buf[4096];
    if (::getcwd(buf, sizeof buf)) return buf;
    return ".";
}

std::string workdir() {
    std::string base;
    auto env_wd = getenv2("AGENT_WORKDIR");
    base = env_wd.value_or((fs::path(app_dir()) / "workspace").string());
    std::error_code ec;
    if (!fs::exists(base)) {
        std::string fallback = (fs::path(app_dir()) / "workspace").string();
        std::string fallback2 = (fs::path(::getenv("HOME") ? ::getenv("HOME") : ".") / "systemc_workspace").string();
        for (const auto& cand : {base, fallback, fallback2}) {
            if (fs::create_directories(cand, ec) || (fs::exists(cand) && !ec)) return cand;
            ec.clear();
        }
        return "/tmp/systemc-workspace";
    }
    return base;
}

std::optional<std::string> getenv2(const std::string& key) {
    if (const char* v = std::getenv(key.c_str()); v && *v) return std::string(v);
    if (auto it = settings().find(key); it != settings().end() && !it->second.empty()) return it->second;
    return std::nullopt;
}

std::string getenv3(const std::string& key, const std::string& fallback) {
    auto v = getenv2(key);
    return v ? *v : fallback;
}

bool get_bool(const std::string& key) {
    auto v = getenv2(key);
    if (!v) return false;
    for (auto& c : *v) c = (char)tolower((unsigned char)c);
    return *v == "1" || *v == "true" || *v == "yes" || *v == "on";
}

bool has_discord_token() {
    return getenv2("DISCORD_BOT_TOKEN").has_value();
}

std::string version_display() { return "Aurelia-CPP (beta)"; }

std::string banner() {
    return "You are running a beta version of SystemC codenamed Aurelia-CPP — the C++ rewrite. "
           "Closer to the metal, still just one binary.";
}
}  // namespace sysc
