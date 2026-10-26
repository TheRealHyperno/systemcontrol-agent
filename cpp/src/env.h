// env.h — .env loading + config lookups (port of config.rs / main.py helpers)
#pragma once
#include <map>
#include <optional>
#include <string>

namespace sysc {
// load .env from app_dir; vars never override real environment variables
bool load_dotenv();
// app dir: $SYSTEMC_HOME or cwd
std::string app_dir();
// agent WORKDIR ($AGENT_WORKDIR or app_dir/workspace), created on demand
std::string workdir();
// env lookup: real env first, then .env
std::optional<std::string> getenv2(const std::string& key);
std::string getenv3(const std::string& key, const std::string& fallback);
bool get_bool(const std::string& key);
bool has_discord_token();
std::string version_display();
std::string banner();
}  // namespace sysc
