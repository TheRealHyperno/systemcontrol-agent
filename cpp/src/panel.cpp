// panel.cpp — an 800-lineHTTP server + the control panel endpoints (panel.rs port)
#include "panel.h"
#include "agent.h"
#include "env.h"
#include "memory_sessions.h"
#include "panel_ui.h"
#include "providers.h"
#include "service.h"
#include "tools.h"
#include <chrono>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <thread>

#if defined(_WIN32)
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

using nlohmann::json;
namespace fs = std::filesystem;

namespace sysc {

namespace {

std::string today_iso(int offset_days = 0) {
    auto now = std::chrono::system_clock::now() + std::chrono::hours(24 * offset_days);
    std::time_t t = std::chrono::system_clock::to_time_t(now);
    std::tm tm{};
    localtime_r(&t, &tm);
    char buf[16];
    std::strftime(buf, sizeof buf, "%Y-%m-%d", &tm);
    return buf;
}

std::string read_text(const fs::path& p) {
    std::ifstream f(p);
    if (!f) return "";
    std::ostringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

json env_of() {
    Provider p("");
    return json{
        {"release", version_display()},
        {"model", p.model()},
        {"provider", getenv3("AGENT_PROVIDER", "ollama")},
        {"workdir", workdir()},
        {"version", version_display()},
    };
}

void send_response(int fd, int status, const std::string& status_text,
                   const std::string& ctype, const std::string& body) {
    std::ostringstream r;
    r << "HTTP/1.1 " << status << " " << status_text << "\r\n"
      << "Content-Type: " << ctype << "\r\n"
      << "Content-Length: " << body.size() << "\r\n"
      << (status == 101 ? "" : "Connection: close\r\n")
      << "\r\n"
      << body;
    auto s = r.str();
    size_t sent = 0;
    while (sent < s.size()) {
        int n = (int)::send(fd, s.data() + sent, s.size() - sent, 0);
        if (n <= 0) break;
        sent += (size_t)n;
    }
}

void send_json(int fd, const json& v, int status = 200) {
    send_response(fd, status, status == 200 ? "OK" : "Bad Request", "application/json",
                  v.is_object() || v.is_array() ? v.dump() : json{{"error", "internal"}}.dump());
}

// one connection's worth of routing
void handle(int fd) {
    char buf[65536];
    std::string head;
    // headers
    while (head.find("\r\n\r\n") == std::string::npos) {
        int n = ::recv(fd, buf, sizeof buf, 0);
        if (n <= 0) { ::close(fd); return; }
        head.append(buf, buf + n);
    }
    auto sep = head.find("\r\n\r\n");
    std::string body = head.substr(sep + 4);
    // content-length framing
    std::string lower;
    for (char c : head.substr(0, sep)) lower.push_back((char)tolower(c));
    auto pos = lower.find("content-length:");
    if (pos != std::string::npos) {
        auto cl = std::strtoull(head.c_str() + pos + 15, nullptr, 10);
        while (body.size() < cl) {
            int n = ::recv(fd, buf, sizeof buf, 0);
            if (n <= 0) break;
            body.append(buf, buf + n);
        }
        body = body.substr(0, cl);
    }

    std::istringstream first(head.substr(0, head.find("\r\n")));
    std::string method, path, proto;
    first >> method >> path >> proto;
    auto qpos = path.find('?');
    std::string query = qpos == std::string::npos ? "" : path.substr(qpos + 1);
    if (qpos != std::string::npos) path = path.substr(0, qpos);

    json req;
    try { req = json::parse(body); } catch (...) { req = json::object(); }
    auto str_arg = [&](const char* k, const char* fallback = "") {
        return req.value(k, std::string(fallback));
    };

    if (method == "GET" && (path == "/" || path == "/panel")) {
        send_response(fd, 200, "OK", "text/html; charset=utf-8", PANEL_UI_HTML);
    } else if (method == "GET" && path == "/config") {
        send_json(fd, env_of());
    } else if (method == "GET" && path == "/api/status") {
        auto out = env_of();
        out["discord"] = has_discord_token();
        out["history_dir"] = sessions_history_dir();
        out["memory_dir"] = (int)memory_load_context().size();  // cheap liveness probe
        send_json(fd, out);
    } else if (method == "GET" && path == "/sessions") {
        send_json(fd, sessions_list());
    } else if (method == "POST" && path == "/sessions/reset") {
        auto res = sessions_reset(str_arg("key", ""));
        send_json(fd, res, res.contains("error") ? 400 : 200);
    } else if (method == "GET" && path == "/memory") {
        send_json(fd, json{
                          {"long_term", read_text(memory_long_term_path())},
                          {"today", read_text(memory_daily_path(today_iso()))},
                          {"yesterday", read_text(memory_daily_path(today_iso(-1)))},
                          {"date", today_iso()},
                      });
    } else if (method == "POST" && path == "/memory/remember") {
        send_json(fd, memory_remember(str_arg("fact", "")));
    } else if (method == "POST" && path == "/tools/run_shell") {
        send_json(fd, run_shell(str_arg("command", ""), false));
    } else if (method == "POST" && path == "/tools/run_shell_elevated") {
        if (!elevated_enabled()) {
            send_json(fd, json{{"error", "elevated commands are turned off on this machine"}, {"exit_code", -1}}, 403);
        } else {
            send_json(fd, run_shell(str_arg("command", ""), true));
        }
    } else if (method == "POST" && path == "/tools/read_file") {
        send_json(fd, read_file(str_arg("path", "")));
    } else if (method == "POST" && path == "/tools/write_file") {
        send_json(fd, write_file(str_arg("path", ""), str_arg("content", "")));
    } else if (method == "POST" && path == "/tools/list_dir") {
        send_json(fd, list_dir(req.value("path", ".")));
    } else if (method == "POST" && path == "/tools/run_python") {
        send_json(fd, run_python(str_arg("code", "")));
    } else if (method == "POST" && path == "/tools/web_search") {
        auto max = std::clamp(req.value("max_results", 5), 1, 10);
        send_json(fd, web_search(str_arg("query", ""), max).get());
    } else if (method == "POST" && path == "/ollama/chat") {
        // streaming NDJSON proxy so the browser UI can chat (no CORS, tools stay local)
        std::string host = getenv3("OLLAMA_HOST", "http://localhost:11434");
        while (!host.empty() && host.back() == '/') host.pop_back();
        auto keys = collect_api_keys();
        auto r = http_request("POST", host + "/api/chat", body,
                              keys.empty() ? "" : keys[0], {}, 600);
        if (r.status == 0) {
            send_json(fd, json{{"error", r.body}}, 502);
        } else {
            // the upstream body arrives as NDJSON (stream:true) or one JSON line
            send_response(fd, 200, "OK", "application/x-ndjson", r.body);
        }
    } else if (method == "GET" && path == "/service/status") {
        std::string active;
        std::string cmd = "systemctl --user is-active systemc.service 2>&1";
        FILE* p = ::popen(cmd.c_str(), "r");
        char line[256];
        if (p) {
            while (fgets(line, sizeof line, p)) active += line;
            ::pclose(p);
        }
        while (!active.empty() && (active.back() == '\n' || active.back() == ' ')) active.pop_back();
        send_json(fd, json{{"active", active}, {"enabled_by_panel", true}});
    } else if (method == "POST" && path == "/service/install") {
        ServiceOpts o;
        o.linger = req.value("linger", false);
        send_json(fd, service_install(o));
    } else if (method == "POST" && path == "/service/uninstall") {
        send_json(fd, service_uninstall("", false));
    } else {
        send_json(fd, json{{"error", "not found"}, {"path", path}}, 404);
    }
    ::close(fd);
}
}  // namespace

void panel_serve(int port) {
    int sfd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (sfd < 0) {
        std::cerr << "[panel] socket failed\n";
        std::exit(1);
    }
    int one = 1;
    ::setsockopt(sfd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons((unsigned short)port);
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (::bind(sfd, (sockaddr*)&addr, sizeof addr) != 0) {
        std::cerr << "[panel] couldn't bind port " << port << "\n";
        std::exit(1);
    }
    ::listen(sfd, 16);

    std::cout << "╔════════════════════════════════════╗\n"
              << "║   systemc control panel (cpp)      ║\n"
              << "╚════════════════════════════════════╝\n"
              << "  open → http://localhost:" << port << "\n"
              << "  release → " << version_display() << "\n\n";

    while (true) {
        sockaddr_in peer{};
        socklen_t plen = sizeof peer;
        int fd = ::accept(sfd, (sockaddr*)&peer, &plen);
        if (fd < 0) continue;
        std::thread([fd]() { handle(fd); }).detach();
    }
}
}  // namespace sysc
