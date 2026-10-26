// tools.h — port of tools.py / tools.rs: shell exec, file tools, gate, search
#pragma once
#include <nlohmann/json.hpp>
#include <optional>
#include <string>
#include <vector>
#include <future>

namespace sysc {
nlohmann::json run_shell(const std::string& command, bool elevated_prefix);
nlohmann::json run_python(const std::string& code);
nlohmann::json read_file(const std::string& path);
nlohmann::json write_file(const std::string& path, const std::string& content);
nlohmann::json list_dir(const std::string& path);
// async via std::async so the agent loop can await multiple calls later
std::future<nlohmann::json> web_search(const std::string& query, int max_results);

bool is_destructive(const std::string& command);
bool elevated_enabled();
bool show_commands_used();
bool show_tools_used();
// tool schemas the model sees
nlohmann::json local_tool_schemas();
nlohmann::json web_tool_schemas();

// URL-encode for form bodies
std::string url_encode(const std::string& s);
std::future<nlohmann::json> web_search_async(const std::string& q);

// shared tool-schema builders (explicit construction — the nested
// initializer-list pattern is ambiguous deep in nlohmann's json)
nlohmann::json prop_str();
nlohmann::json prop_int(int def = 0);
nlohmann::json mk_tool(const std::string& name, const std::string& description,
                       nlohmann::json properties, nlohmann::json required = nlohmann::json::array());
}  // namespace sysc
