// memory.h / sessions.h combined + memory.cpp — ports of memory.py and sessions.py
#pragma once
#include <nlohmann/json.hpp>
#include <string>
#include <vector>

namespace sysc {
// ── memory ──────────────────────────────────────────────────────────────────
std::string memory_load_context();                       // prompt tail ("" if empty)
void memory_log_turn(const std::string& channel_label, const std::string& user_text,
                     const std::string& assistant_text /*"" = none*/);
nlohmann::json memory_remember(const std::string& fact);
std::string memory_long_term_path();
std::string memory_daily_path(const std::string& date_iso);

// ── sessions (one per chat channel, .history/<name>.json) ───────────────────
std::string sessions_history_dir();
std::string sessions_make_key(const std::string& platform, const std::string& channel_id);
bool sessions_parse_key(const std::string& key, std::string* platform, std::string* cid);
nlohmann::json sessions_load(const std::string& key);               // array
bool sessions_save(const std::string& key, const nlohmann::json& history);
long sessions_stamp(const std::string& key);
nlohmann::json sessions_reset(const std::string& key);              // {key,cleared_turns,ok}
nlohmann::json sessions_list();
std::string sessions_resolve(const std::string& ref, const std::string& current, bool* ok);
void sessions_set_label(const std::string& key, const std::string& label);
nlohmann::json sessions_tool_schema();
}  // namespace sysc
