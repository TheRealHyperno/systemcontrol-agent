// providers.h — LLM backends with the API-key pool (port of providers.py)
#pragma once
#include <chrono>
#include <map>
#include <memory>
#include <mutex>
#include <nlohmann/json.hpp>
#include <optional>
#include <sstream>
#include <string>
#include <vector>
#include "http.h"

namespace sysc {
struct ToolCall {
    std::string id;
    std::string name;
    nlohmann::json input;
};
struct Message {
    std::string role;
    std::string content;
    std::vector<ToolCall> tool_calls;
    std::string tool_call_id;
    std::string tool_name;

    static Message user(const std::string& text) {
        Message m;
        m.role = "user";
        m.content = text;
        return m;
    }
};
using ToolSchema = nlohmann::json;

// Priority key pool: first non-cooling key wins; a 429/402/401/403 parks it.
template <long DefaultCooldown = 600>
struct key_pool {
    std::mutex mu;
    std::map<std::string, long long> parked_until;  // key -> epoch ms
    std::vector<std::string> notices;

    static long long now_ms() {
        return std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::system_clock::now().time_since_epoch()).count();
    }

    std::optional<size_t> acquire(const std::vector<std::string>& keys) {
        std::lock_guard<std::mutex> lock(mu);
        auto now = now_ms();
        for (size_t i = 0; i < keys.size(); ++i) {
            auto it = parked_until.find(keys[i]);
            if (it == parked_until.end() || it->second <= now) return i;
        }
        return std::nullopt;
    }
    void mark_ok(const std::string& key) {
        std::lock_guard<std::mutex> lock(mu);
        parked_until.erase(key);
    }
    void mark_bad(const std::string& key, long http_code) {
        std::lock_guard<std::mutex> lock(mu);
        long cooldown_s = 900;
        if (http_code == 429) cooldown_s = 15 * 60;
        else if (http_code == 402) cooldown_s = 60 * 60;
        else if (http_code == 401 || http_code == 403) cooldown_s = 24 * 3600;
        parked_until[key] = now_ms() + cooldown_s * 1000;
    }
    std::string exhausted_message(const std::string& host, const std::string& model) {
        std::lock_guard<std::mutex> lock(mu);
        auto now = now_ms();
        std::string parts;
        for (auto& [k, until] : parked_until) {
            long mins = (until - now) / 60000 + 1;
            if (!parts.empty()) parts += "; ";
            parts += "'" + k.substr(k.size() > 4 ? k.size() - 4 : 0) + "' HTTP, retry in ~" + std::to_string(mins) + " min";
        }
        return "All Ollama API keys are unavailable right now (" + parts + "). Add credits or another key at "
               "https://ollama.com/settings/keys, or pick a model included in your plan ('" + model + "' on " + host + " may not be).";
    }
};

std::vector<std::string> collect_api_keys();

class Provider {
public:
    Provider() : Provider("") {}
    explicit Provider(const std::string& system_prompt);
    enum Kind { Ollama, Anthropic };
    Kind kind() const;
    std::string model() const;
    std::string describe_source() const;
    std::string system_prompt_text() const { return system_prompt_; }
    void set_system_prompt(const std::string& s) { system_prompt_ = s; }

    // take the current history and return exactly one assistant step
    Message step(const std::vector<ToolSchema>& tool_schemas, const std::vector<Message>& history) const;
    Message ollama_step(const std::vector<ToolSchema>& schemas, const std::vector<Message>& history) const;
    Message anthropic_step(const std::vector<ToolSchema>& schemas, const std::vector<Message>& history) const;

private:
    Kind kind_;
    std::string model_, host_;
    std::vector<std::string> api_keys_;
    std::string system_prompt_;
};

// OLLAMA_HOST / OLLAMA_MODE / OLLAMA_API_KEY(S) resolved (also used by the
// panel's chat proxy and `systemc models`)
struct OllamaSettings {
    std::string host;
    std::string mode;  // "cloud" | "local"
    std::vector<std::string> api_keys;
};
OllamaSettings ollama_settings();

std::vector<std::string> list_ollama_models(const std::string& host, const std::string& key);
}  // namespace sysc
