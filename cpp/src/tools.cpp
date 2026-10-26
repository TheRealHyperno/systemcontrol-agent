// tools.cpp
#include "tools.h"
#include "env.h"
#include "http.h"
#include <cstdio>
#include <cstdlib>
#include <unistd.h>
#include <sys/select.h>
#include <sys/wait.h>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <thread>

namespace fs = std::filesystem;
using nlohmann::json;

namespace sysc {
namespace {
const size_t OUTPUT_CAP = 6000;
const long SHELL_TIMEOUT = 60;

std::string strip_ansi(const std::string& raw) {
    std::string out;
    for (size_t i = 0; i < raw.size(); ++i) {
        if (raw[i] == 0x1b) {
            // skip CSI and OSC sequences
            if (i + 1 < raw.size() && raw[i + 1] == '[') {
                i += 2;
                while (i < raw.size() && !isalpha((unsigned char)raw[i])) ++i;
                continue;
            }
            if (i + 1 < raw.size() && raw[i + 1] == ']') {
                i += 2;
                while (i + 1 < raw.size() && !(raw[i] == 0x07)) ++i;
                continue;
            }
            continue;
        }
        if (raw[i] != '\r') out.push_back(raw[i]);
        if (i + 1 < raw.size() && raw[i] == '\r' && raw[i + 1] == '\n') { out.push_back('\n'); ++i; continue; }
        if (raw[i] == '\r') out.push_back('\n');
    }
    return out;
}

std::string cap(const std::string& text) {
    auto t = strip_ansi(text);
    // collapse \r\n handled above; trim
    auto trim = [](std::string s) {
        while (!s.empty() && (s.back() == '\n' || s.back() == ' ')) s.pop_back();
        return s;
    };
    t = trim(t);
    if (t.size() > OUTPUT_CAP) {
        auto start = t.substr(0, OUTPUT_CAP / 2);
        auto end = t.substr(t.size() - OUTPUT_CAP / 2);
        return start + "\n... [truncated] ...\n" + end;
    }
    return t;
}
}  // namespace

nlohmann::json run_shell(const std::string& command, bool elevated_prefix) {
    auto wd = workdir();
    std::string full = elevated_prefix ? "sudo -n " + command : command;
    std::string err;
    // fopen+popen style: capture combined stdout/stderr, with a timeout via kill on thread
    std::string cmd = "cd " + wd + " && HOME=" + wd + " PWD=" + wd + " " + full + " 2>&1";
    FILE* pipe = ::popen(cmd.c_str(), "r");
    if (!pipe) return json{{"stdout", ""}, {"exit_code", -1}, {"error", "popen failed"}, {"cwd", wd}};
    std::ostringstream ss;
    char buf[8192];
    bool timed_out = false;
    auto start = std::chrono::steady_clock::now();
    // non-blocking-ish: popen is blocking; emulate timeout by reading chunks
    int fd = ::fileno(pipe);
    fd_set fds;
    while (true) {
        FD_ZERO(&fds);
        FD_SET(fd, &fds);
        timeval tv{0, 200000};
        int r = ::select(fd + 1, &fds, nullptr, nullptr, &tv);
        if (r > 0) {
            ssize_t n = ::read(fd, buf, sizeof buf);
            if (n <= 0) break;
            ss.write(buf, n);
            continue;
        }
        if (std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count() > SHELL_TIMEOUT) {
            timed_out = true;
            break;
        }
        // check EOF: blocking read drained but child may still be alive
        // try a non-blocking peek then verify process still running by attempting read with tiny timeout
        timeval tv2{0, 1000};
        FD_ZERO(&fds);
        FD_SET(fd, &fds);
        int r2 = ::select(fd + 1, &fds, nullptr, nullptr, &tv2);
        if (r2 <= 0) continue;  // nothing yet, keep looping until timeout
        ssize_t n2 = ::read(fd, buf, 1);
        if (n2 <= 0 && !timed_out) break;
        if (n2 > 0) ss.write(buf, 1);
    }
    int exit_code = ::pclose(pipe);
    exit_code = exit_code == -1 ? -1 : exit_code;
    if (timed_out) {
        return json{{"stdout", cap(ss.str())},
                    {"exit_code", -1},
                    {"error", "timed out after " + std::to_string(SHELL_TIMEOUT) + "s"},
                    {"cwd", wd}};
    }
    // pclose returns wait status; real exit code
    if (WIFEXITED(exit_code)) exit_code = WEXITSTATUS(exit_code);
    return json{{"stdout", cap(ss.str())}, {"exit_code", exit_code}, {"cwd", wd}};
}

nlohmann::json run_python(const std::string& code) {
    auto wd = workdir();
    auto path = fs::path(wd) / (".systemc_run_" + std::to_string(::rand()) + ".py");
    {
        std::ofstream f(path);
        f << code;
    }
    auto py = getenv3("SYSTEMC_PYTHON", "python3");
    auto out = run_shell("'" + py + "' '" + path.string() + "'", false);
    std::error_code ec;
    fs::remove(path, ec);
    return out;
}

static std::string resolve_path(const std::string& p) {
    fs::path path(p);
    return path.is_absolute() ? path.string() : (fs::path(workdir()) / path).string();
}

nlohmann::json read_file(const std::string& path) {
    auto full = resolve_path(path);
    std::ifstream f(full, std::ios::binary);
    if (!f) return json{{"error", "couldn't read file"}};
    std::string content((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    bool truncated = content.size() > 8000;
    if (truncated) content.resize(8000);
    return json{{"content", content}, {"path", full}, {"truncated", truncated}};
}

nlohmann::json write_file(const std::string& path, const std::string& content) {
    auto full = fs::path(resolve_path(path));
    std::error_code ec;
    fs::create_directories(full.parent_path(), ec);
    std::ofstream f(full, std::ios::binary);
    if (!f) return json{{"error", "couldn't write file"}};
    f << content;
    return json{{"written", content.size()}, {"path", full.string()}};
}

nlohmann::json list_dir(const std::string& path) {
    auto full = path.empty() ? workdir() : resolve_path(path);
    json entries = json::array();
    std::error_code ec;
    for (auto& e : fs::directory_iterator(full, ec)) {
        std::error_code mec;
        auto md = e.status();
        json item{{"name", e.path().filename().string()},
                  {"type", fs::is_directory(md) ? "dir" : "file"}};
        if (fs::is_regular_file(md)) item["size"] = fs::file_size(e.path(), mec);
        entries.push_back(item);
    }
    if (ec) return json{{"error", "couldn't list directory"}};
    return json{{"path", full}, {"entries", entries}};
}

bool elevated_enabled() {
    auto v = getenv2("AGENT_ALLOW_ELEVATED");
    if (v) return *v == "1" || *v == "true" || *v == "yes" || *v == "on";
    return !getenv2("AGENT_RUN_AS_USER").has_value();
}
bool show_commands_used() { return get_bool("SHOW_COMMANDS_USED"); }
bool show_tools_used() { return get_bool("SHOW_TOOLS_USED"); }

bool is_destructive(const std::string& command) {
    static const std::vector<std::string> cmds = {
        "rm","rmdir","shred","mkfs","fdisk","parted","wipefs","dd","shutdown","reboot",
        "halt","poweroff","sudo","su","pip","pip3","apt","apt-get","dpkg","snap","flatpak",
        "npm","yarn","pnpm","dnf","yum","pacman","systemctl","service","chown","chmod",
        "iptables","ip6tables","ufw","nft","kill","killall","pkill","del","erase","format","remove-item"};
    // token must start a pipeline segment: start of string or after |;&`(
    for (const auto& c : cmds) {
        size_t pos = 0;
        while ((pos = command.find(c, pos)) != std::string::npos) {
            bool boundary_start = (pos == 0) ||
                std::string("|;&`(").find(command[pos - 1]) != std::string::npos;
            if (boundary_start) {
                auto after = pos + c.size();
                bool space_after = after >= command.size() || command[after] == ' ' || command[after] == '/';
                if (space_after) return true;
            }
            ++pos;
        }
    }
    auto pipe_to_sh = [](const std::string& s) {
        return s.find("| sh") != std::string::npos || s.find("|sh") != std::string::npos ||
               s.find("| bash") != std::string::npos || s.find("|iex") != std::string::npos;
    };
    return pipe_to_sh(command);
}

json prop_str() {
    json p = json::object();
    p["type"] = "string";
    return p;
}

json prop_int(int def) {
    json p = json::object();
    p["type"] = "integer";
    if (def) p["default"] = def;
    return p;
}

json mk_tool(const std::string& name, const std::string& description, json properties, json required) {
    json input_schema = json::object();
    input_schema["type"] = "object";
    input_schema["properties"] = properties;
    if (!required.empty()) input_schema["required"] = required;
    json o = json::object();
    o["name"] = name;
    o["description"] = description;
    o["input_schema"] = input_schema;
    return o;
}

nlohmann::json local_tool_schemas() {
    json props;
    props["command"] = prop_str();
    json out = json::array();
    out.push_back(mk_tool("run_shell",
        "Run a shell command in the workspace. Returns {stdout, exit_code, cwd}. Destructive commands need user approval — don't retry when denied.",
        props, json::array({"command"})));
    out.push_back(mk_tool("run_shell_elevated",
        "Run a shell command with sudo (root). ALWAYS requires the owner's approval.",
        props, json::array({"command"})));
    json p2;
    p2["code"] = prop_str();
    out.push_back(mk_tool("run_python",
        "Run a Python snippet in the workspace with the system python3. Prefer over run_shell for anything beyond a one-liner.",
        p2, json::array({"code"})));
    json p3;
    p3["path"] = prop_str();
    out.push_back(mk_tool("read_file",
        "Read a file (workspace-relative or absolute), first 8000 chars.",
        p3, json::array({"path"})));
    json p4;
    p4["path"] = prop_str();
    p4["content"] = prop_str();
    out.push_back(mk_tool("write_file",
        "Write a file (creates parent dirs).",
        p4, json::array({"path", "content"})));
    out.push_back(mk_tool("list_dir",
        "List a directory (defaults to workspace root).",
        p3));
    json p5;
    p5["fact"] = prop_str();
    out.push_back(mk_tool("remember",
        "Save a short, durable fact to long-term memory (MEMORY.md). Use when the user asks to remember something or shares a stable preference/fact.",
        p5, json::array({"fact"})));
    return out;
}

nlohmann::json web_tool_schemas() {
    json out = json::array();
    json props;
    props["query"] = prop_str();
    props["max_results"] = prop_int();
    out.push_back(mk_tool("web_search",
        "Search the web via DuckDuckGo (no API key). Returns title/url/snippet results.",
        props, json::array({"query"})));
    return out;
}

std::string url_encode(const std::string& s) {
    std::ostringstream o;
    o << std::hex << std::uppercase << std::setfill('0');
    for (unsigned char c : s) {
        if (isalnum(c) || c == '-' || c == '_' || c == '.' || c == '~') o << (char)c;
        else o << '%' << std::setw(2) << (int)c;
    }
    return o.str();
}

std::future<json> web_search(const std::string& query, int max_results) {
    return std::async(std::launch::async, [query, max_results]() -> json {
        auto body = "q=" + url_encode(query);
        std::map<std::string, std::string> headers;
        headers["Content-Type"] = "application/x-www-form-urlencoded";
        headers["User-Agent"] = "Mozilla/5.0 (compatible; systemc-agent-cpp/1.0)";
        auto resp = http_request("POST", "https://html.duckduckgo.com/html/", body, {}, headers, 15);
        if (resp.status == 0) return json{{"error", "search failed: " + resp.body.substr(0, 200)}};
        // regex-free naive scan for result blocks
        json results = json::array();
        size_t pos = 0;
        while ((int)results.size() < max_results) {
            auto link = resp.body.find("result__a", pos);
            if (link == std::string::npos) break;
            // the <a> tag may carry href before or after class=
            auto tag_start = resp.body.rfind('<', link == 0 ? 0 : link - 1);
            if (tag_start == std::string::npos) break;
            auto tag_end = resp.body.find('>', link);
            if (tag_end == std::string::npos) break;
            std::string tag = resp.body.substr(tag_start, tag_end - tag_start);
            auto href0 = tag.find("href=\"");
            if (href0 == std::string::npos) { pos = tag_end; continue; }
            auto href1 = tag.find('"', href0 + 6);
            std::string href = tag.substr(href0 + 6, href1 - href0 - 6);
            auto text0 = tag_end;
            auto text1 = resp.body.find("</a>", text0);
            std::string title = resp.body.substr(text0 + 1, text1 - text0 - 1);
            // snippet within the window up to the next result
            auto next = resp.body.find("result__a", text1);
            auto snip = resp.body.find("result__snippet", text1);
            std::string snippet;
            if (snip != std::string::npos && snip < next) {
                auto s0 = resp.body.find(">", snip);
                auto s1 = resp.body.find("</a>", s0);
                snippet = resp.body.substr(s0 + 1, s1 - s0 - 1);
            }
            // unwrap the ddg redirect (uddg=<percent-encoded target>)
            if (href.find("uddg=") != std::string::npos) {
                auto u = href.find("uddg=") + 5;
                auto e = href.find('&', u);
                std::string enc = href.substr(u, e == std::string::npos ? std::string::npos : e - u);
                std::string dec;
                for (size_t i = 0; i < enc.size(); ++i) {
                    if (enc[i] == '%' && i + 2 < enc.size()) {
                        dec.push_back((char)std::strtol(enc.substr(i + 1, 2).c_str(), nullptr, 16));
                        i += 2;
                    } else dec.push_back(enc[i]);
                }
                href = dec;
            } else if (href.rfind("//", 0) == 0) href = "https:" + href;
            results.push_back(json{{"title", title}, {"url", href}, {"snippet", snippet}});
            pos = text1;
        }
        if (results.empty()) return json{{"results", json::array()}, {"note", "no results parsed — markup may have changed"}};
        return json{{"results", results}};
    });
}
}  // namespace sysc
