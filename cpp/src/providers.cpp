// providers.cpp — Ollama (local/cloud key pool) + Anthropic over exact JSON
#include "providers.h"
#include "env.h"
#include <algorithm>
#include <chrono>
#include <cctype>
#include <cstdlib>
#include <iostream>
#include <set>

using nlohmann::json;

namespace sysc {
Provider::Provider(const std::string& system_prompt) : system_prompt_(system_prompt) {
    auto kind = getenv3("AGENT_PROVIDER", "ollama");
    for (auto& c : kind) c = (char)tolower(c);
    if (kind == "anthropic") {
        kind_ = Anthropic;
        model_ = getenv3("AGENT_MODEL", "claude-sonnet-4-6");
        api_keys_ = {getenv3("ANTHROPIC_API_KEY", "")};
    } else {
        kind_ = Ollama;
        auto cfg = ollama_settings();
        host_ = cfg.host;
        api_keys_ = cfg.api_keys;
        model_ = getenv3("AGENT_MODEL", "llama3.2");
        if (!cfg.api_keys.empty())
            for (auto suf : std::set<std::string>{"-cloud", ":cloud"})
                if (model_.size() > suf.size() && model_.rfind(suf) == model_.size() - suf.size())
                    model_ = model_.substr(0, model_.size() - suf.size());
    }
}

OllamaSettings ollama_settings() {
    OllamaSettings s;
    auto keys = collect_api_keys();
    auto host = getenv2("OLLAMA_HOST").value_or("");
    while (!host.empty() && host.back() == '/') host.pop_back();
    auto mode = getenv3("OLLAMA_MODE", "");
    for (auto& c : mode) c = (char)tolower(c);
    bool cloud = (mode == "cloud")
        || (mode != "local" && !keys.empty() && (host.empty() || host.find("ollama.com") != std::string::npos));
    if (cloud) {
        s.mode = "cloud";
        if (host.find("localhost") == std::string::npos && host.find("127.0.0.1") == std::string::npos && !host.empty())
            s.host = host;
        else s.host = "https://ollama.com";
    } else {
        s.mode = "local";
        s.host = host.empty() ? "http://localhost:11434" : host;
        keys.clear();  // never send a cloud key to a local server
    }
    s.api_keys = keys;
    return s;
}

Provider::Kind Provider::kind() const { return kind_; }
std::string Provider::model() const { return model_; }
std::string Provider::describe_source() const {
    if (kind_ == Anthropic) return "Anthropic";
    return api_keys_.empty() ? "Ollama (local)" : "Ollama Cloud";
}

std::vector<std::string> collect_api_keys() {
    std::vector<std::string> keys;
    for (auto chunk : {getenv3("OLLAMA_API_KEY", ""), getenv3("OLLAMA_API_KEYS", "")}) {
        std::string cur;
        for (char c : chunk) {
            if (c == ',' || c == ';' || isspace((unsigned char)c)) {
                if (!cur.empty() && std::find(keys.begin(), keys.end(), cur) == keys.end()) keys.push_back(cur);
                cur.clear();
            } else cur += c;
        }
        if (!cur.empty() && std::find(keys.begin(), keys.end(), cur) == keys.end()) keys.push_back(cur);
    }
    return keys;
}

// Chat-format messages — same wire shape across providers (providers.py notes)
static json to_ollama_native(const Provider& p, const std::vector<Message>& history) {
    json native = json::array({json{{"role", "system"}, {"content", p.system_prompt_text()}}});
    for (auto& m : history) {
        if (m.role == "user") native.push_back(json{{"role", "user"}, {"content", m.content}});
        else if (m.role == "assistant") {
            json msg = json{{"role", "assistant"}, {"content", m.content}};
            if (!m.tool_calls.empty()) {
                json tcs = json::array();
                for (auto& tc : m.tool_calls)
                    tcs.push_back(json{{"function", json{{"name", tc.name}, {"arguments", tc.input}}}});
                msg["tool_calls"] = tcs;
            }
            native.push_back(msg);
        } else if (m.role == "tool") {
            native.push_back(json{{"role", "tool"}, {"content", m.content}, {"name", m.tool_name}});
        }
    }
    return native;
}

static json to_openai_tool_schema(const json& s) {
    return json{{"type", "function"},
                {"function", json{{"name", s["name"]},
                                  {"description", s.value("description", "")},
                                  {"parameters", s.value("input_schema", json{{"type", "object"}, {"properties", json::array()}})}}}};
}

static json to_anthropic_native(const std::vector<Message>& history) {
    json native = json::array();
    for (auto& m : history) {
        if (m.role == "user") native.push_back(json{{"role", "user"}, {"content", m.content}});
        else if (m.role == "assistant") {
            json blocks = json::array();
            if (!m.content.empty()) blocks.push_back(json{{"type", "text"}, {"text", m.content}});
            for (auto& tc : m.tool_calls) {
                blocks.push_back(json{{"type", "tool_use"}, {"id", tc.id}, {"name", tc.name}, {"input", tc.input}});
            }
            native.push_back(json{{"role", "assistant"}, {"content", blocks}});
        } else if (m.role == "tool") {
            native.push_back(json{{"role", "user"},
                {"content", json::array({json{{"type", "tool_result"}, {"tool_use_id", m.tool_call_id}, {"content", m.content}}})}});
        }
    }
    return native;
}

namespace {
key_pool<600> pool_;

std::string server_reply_text(const std::string& body, size_t cap = 300) {
    try { return json::parse(body)["error"]["message"]; } catch (...) {}
    auto s = body;
    return s.size() > cap ? s.substr(0, cap) : s;
}
}  // namespace

Message Provider::step(const std::vector<ToolSchema>& tool_schemas, const std::vector<Message>& history) const {
    if (kind_ == Anthropic) return anthropic_step(tool_schemas, history);
    return ollama_step(tool_schemas, history);
}

Message Provider::ollama_step(const std::vector<ToolSchema>& tool_schemas, const std::vector<Message>& history) const {
    // exactly like providers.py: pick the first non-cooling key; on 429/402/
    // 401/403 park it and try the next; no keys -> local server only.
    json body = json{{"model", model_}, {"messages", to_ollama_native(*this, history)}, {"stream", false}};
    if (!tool_schemas.empty()) {
        json tcs = json::array();
        for (auto& s : tool_schemas) tcs.push_back(to_openai_tool_schema(s));
        body["tools"] = tcs;
    }
    for (int turn = 0; turn < 8; ++turn) {
        std::string key;
        if (!api_keys_.empty()) {
            auto slot = pool_.acquire(api_keys_);
            if (!slot) throw std::runtime_error(pool_.exhausted_message(host_, model_));
            key = api_keys_[*slot];
        }
        HttpResponse resp;
        resp = http_request("POST", host_ + "/api/chat", body.dump(), key, {}, 240);
        if (resp.status == 429 || resp.status == 402 || resp.status == 401 || resp.status == 403) {
            if (!api_keys_.empty()) pool_.mark_bad(key, resp.status);
            continue;  // park this key; next loop iteration takes the next one
        }
        if (resp.status != 200) throw std::runtime_error("Ollama HTTP " + std::to_string(resp.status) + ": " + server_reply_text(resp.body));
        if (!api_keys_.empty()) pool_.mark_ok(key);
        try {
            auto data = json::parse(resp.body);
            auto& msg = data["message"];
            Message out;
            out.role = "assistant";
            out.content = msg.value("content", "");
            for (auto& tc : msg.value("tool_calls", json::array())) {
                ToolCall call;
                call.name = tc["function"]["name"];
                call.input = tc["function"].value("arguments", json::object());
                if (call.id.empty()) call.id = "call_" + std::to_string(::rand());
                out.tool_calls.push_back(call);
            }
            return out;
        } catch (const std::exception& e) {
            throw std::runtime_error(std::string("bad Ollama response: ") + e.what());
        }
    }
    throw std::runtime_error("all Ollama API keys are unavailable right now — check https://ollama.com/settings/keys");
}

Message Provider::anthropic_step(const std::vector<ToolSchema>& tool_schemas, const std::vector<Message>& history) const {
    json body = json{{"model", model_}, {"max_tokens", 1500}, {"system", system_prompt_text()},
                     {"tools", json::array()}, {"messages", to_anthropic_native(history)}};
    // anthropic wants tool schemas as-is ({name,description,input_schema})
    for (auto& s : tool_schemas) body["tools"].push_back(s);
    HttpResponse resp = http_request("POST", "https://api.anthropic.com/v1/messages", body.dump(),
                                     api_keys_.empty() ? "" : api_keys_[0],
                                     {{"x-api-key", api_keys_.empty() ? "" : api_keys_[0]},
                                      {"anthropic-version", "2023-06-01"}}, 120);
    if (resp.status != 200) throw std::runtime_error("Anthropic HTTP " + std::to_string(resp.status) + ": " + server_reply_text(resp.body));
    try {
        auto data = json::parse(resp.body);
        Message out;
        out.role = "assistant";
        for (auto& b : data["content"]) {
            auto t = b.value("type", "");
            if (t == "text") out.content += b.value("text", "");
            else if (t == "tool_use")
                out.tool_calls.push_back(ToolCall{b.value("id", ""), b.value("name", ""), b.value("input", json::object())});
        }
        return out;
    } catch (const std::exception& e) {
        throw std::runtime_error(std::string("bad Anthropic response: ") + e.what());
    }
}

std::vector<std::string> list_ollama_models(const std::string& host_in, const std::string& key) {
    std::string host = host_in;
    while (!host.empty() && host.back() == '/') host.pop_back();
    HttpResponse resp;
    resp.status = 0;
    // one key per attempt, exactly like main.rs's models_cmd; empty key last
    if (key.empty() && host.rfind("https://", 0) != 0 && host.find("ollama.com") == std::string::npos
        && host.rfind("http://", 0) == 0) {
        resp = http_request("GET", host + "/api/tags", "", "");
    } else {
        resp = http_request("GET", host + "/api/tags", "", key.empty() ? "" : key);
    }
    if (resp.status != 200) throw std::runtime_error("models: HTTP " + std::to_string(resp.status));
    auto data = json::parse(resp.body);
    std::vector<std::string> names;
    for (auto& m : data.value("models", json::array()))
        names.push_back(m.value("model", m.value("name", "")));
    std::sort(names.begin(), names.end());
    return names;
}
}  // namespace sysc
