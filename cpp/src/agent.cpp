// agent.cpp — the agent loop, ported from agent.rs
#include "agent.h"
#include "env.h"
#include "memory_sessions.h"
#include "prompt.h"
#include "tools.h"
#include <algorithm>
#include <future>
#include <iostream>
#include <sstream>

using nlohmann::json;

namespace sysc {

namespace {
int env_num(const std::string& name, int def) {
    auto v = getenv2(name);
    if (!v) return def;
    try {
        int n = std::stoi(*v);
        return n >= 1 ? n : def;
    } catch (...) { return def; }
}

std::string fmt(const json& v) { return v.is_string() ? v.get<std::string>() : v.dump(); }

std::string clip(const std::string& s, size_t n) {
    return s.size() <= n ? s : s.substr(0, n) + "…";
}

Message tool_msg(const ToolCall& tc, const json& result) {
    Message m;
    m.role = "tool";
    m.content = fmt(result);
    m.tool_call_id = tc.id;
    m.tool_name = tc.name;
    return m;
}

json reset_session_tool(const Channel& channel, const std::string& current, const std::string& session_ref);
}  // namespace

int max_turns() { return env_num("AGENT_MAX_TURNS", 20); }

bool subagents_enabled() {
    auto v = getenv2("SUBAGENTS");
    if (!v) return true;
    for (auto& c : *v) c = (char)tolower((unsigned char)c);
    return *v != "off";
}

Agent::Agent() {
    Provider p("");  // resolves kind/model/keys from env
    auto prompt = build_prompt(p.model(), p.describe_source());
    if (!elevated_enabled()) {
        auto from = "If run_shell fails with \"Permission denied\", retry with run_shell_elevated\n";
        auto to = "If run_shell fails with \"Permission denied\", you may not have elevated access here: tell the user what needs doing instead of trying to work around it\n";
        auto pos = prompt.find(from);
        if (pos != std::string::npos) prompt.replace(pos, std::string(from).size(), to);
    }
    base_prompt_ = prompt;
    provider_ = p;
    provider_.set_system_prompt(prompt);
}

namespace {
json base_schemas(const Channel& channel) {
    json schemas = local_tool_schemas();
    schemas.push_back(sessions_tool_schema());
    for (auto& s : web_tool_schemas()) schemas.push_back(s);
    {
        json props = json::object();
        props["content"] = prop_str();
        schemas.push_back(mk_tool("send_message",
            "Post a message to the current channel (use only for progress notes or when the reply itself isn't the answer).",
            props, json::array({"content"})));
    }
    if (channel.kind == Channel::Discord) {
        json props = json::object();
        props["path"] = prop_str();
        props["caption"] = prop_str();
        schemas.push_back(mk_tool("send_file",
            "Upload a file from the workspace (or absolute path) as an attachment.",
            props, json::array({"path"})));
        json props2 = json::object();
        props2["limit"] = prop_int(20);
        schemas.push_back(mk_tool("fetch_channel_messages",
            "Read this channel's recent message history with reply context.", props2));
    }
    return schemas;
}

json subagent_schemas() {
    json out = json::array();
    {
        json props = json::object();
        json task = prop_str();
        task["description"] = "Complete, self-contained instructions";
        props["task"] = task;
        json name = prop_str();
        name["description"] = "Short label, e.g. 'disk-audit'";
        props["name"] = name;
        out.push_back(mk_tool("spawn_subagent",
            "Delegate ONE self-contained task to a subagent (a fresh copy of you with its own context, shell, python, files and web search). It cannot see this conversation, so include all context and say exactly what to report back. Returns its final report.",
            props, json::array({"task"})));
    }
    {
        json item_props = json::object();
        item_props["task"] = prop_str();
        item_props["name"] = prop_str();
        json item = json::object();
        item["type"] = "object";
        item["properties"] = item_props;
        item["required"] = json::array({"task"});
        json tasks_arr = json::object();
        tasks_arr["type"] = "array";
        tasks_arr["items"] = item;
        json props = json::object();
        props["tasks"] = tasks_arr;
        out.push_back(mk_tool("spawn_subagents",
            "Run up to 5 independent tasks in PARALLEL, one subagent each. Same rules as spawn_subagent: every task must be self-contained. Returns all reports.",
            props, json::array({"tasks"})));
    }
    return out;
}

void send_usage_summary(const Channel& channel,
                        const std::vector<std::string>& commands_used,
                        const std::vector<std::pair<std::string, int>>& counts) {
    if (!(show_commands_used() || show_tools_used())) return;
    std::vector<std::string> lines;
    if (show_commands_used()) {
        std::vector<std::string> uniq;
        for (auto& c : commands_used)
            if (std::find(uniq.begin(), uniq.end(), c) == uniq.end()) uniq.push_back(c);
        if (!uniq.empty()) {
            lines.push_back("-# __Commands executed__");
            for (size_t i = 0; i < uniq.size() && i < 50; ++i) lines.push_back("-# - " + uniq[i]);
        }
    }
    if (show_tools_used() && !counts.empty()) {
        lines.push_back("-# __Tools Used__");
        for (auto& [n, c] : counts) lines.push_back("-# " + n + " (" + std::to_string(c) + ")");
    }
    std::string out;
    for (size_t i = 0; i < lines.size(); ++i) {
        if (i) out += "\n";
        out += lines[i];
    }
    if (!out.empty()) channel.send(out);
}
}  // namespace

void Agent::run_turn(const Channel& channel, const std::string& user_text,
                     std::vector<Message>* history, const Author* author) {
    std::string prompt = user_text;
    if (author) prompt = sender_tag(*author) + " " + user_text;
    history->push_back(Message::user(prompt));

    // re-inject memory fresh every turn (another channel may have written it)
    auto mem_context = memory_load_context();
    std::string system_prompt = mem_context.empty() ? base_prompt_ : base_prompt_ + "\n\n" + mem_context;
    provider_.set_system_prompt(system_prompt);

    json schemas = base_schemas(channel);
    if (subagents_enabled()) {
        auto sub = subagent_schemas();
        for (auto& s : sub) schemas.push_back(s);
    }

    auto current_session = sessions_make_key(channel.platform(), channel.id());
    std::vector<std::string> commands_used;
    std::vector<std::pair<std::string, int>> counts;  // first-seen order

    std::string final_text;
    bool hit_limit = true;

    for (int turn = 0; turn < max_turns(); ++turn) {
        Message assistant;
        try {
            assistant = provider_.step(schemas, *history);
        } catch (const std::exception& e) {
            // leave the user message in history so the error is retryable
            throw;
        }
        history->push_back(assistant);

        if (assistant.tool_calls.empty()) {
            if (!assistant.content.empty()) {
                final_text = assistant.content;
                hit_limit = false;
                send_usage_summary(channel, commands_used, counts);
                channel.send(assistant.content);
            }
            break;
        }

        for (auto& tc : assistant.tool_calls) {
            // count first-seen order
            bool seen = false;
            for (auto& [n, c] : counts)
                if (n == tc.name) { ++c; seen = true; break; }
            if (!seen) counts.push_back({tc.name, 0}), counts.back().second = 1;

            json result;
            auto get = [&](const std::string& k) -> std::string {
                return tc.input.value(k, "");
            };
            if (tc.name == "run_shell") {
                auto command = get("command");
                commands_used.push_back(command);
                if (is_destructive(command) && !channel.request_approval(command, false, 60))
                    result = json{{"error", "command was denied by user"}, {"exit_code", -1}};
                else
                    result = run_shell(command, false);
            } else if (tc.name == "run_shell_elevated") {
                auto command = get("command");
                if (!elevated_enabled())
                    result = json{{"error", "elevated commands are turned off on this machine"}, {"exit_code", -1}};
                else if (!channel.request_approval("[ELEVATED / sudo]\n" + command, true, 90))
                    result = json{{"error", "elevated command was denied by user"}, {"exit_code", -1}};
                else
                    result = run_shell(command, true);
            } else if (tc.name == "run_python") {
                result = run_python(get("code"));
            } else if (tc.name == "read_file") {
                result = read_file(get("path"));
            } else if (tc.name == "write_file") {
                result = write_file(get("path"), get("content"));
            } else if (tc.name == "list_dir") {
                result = list_dir(get("path"));
            } else if (tc.name == "web_search") {
                auto max = std::clamp(tc.input.value("max_results", 5), 1, 10);
                result = web_search(get("query"), max).get();
            } else if (tc.name == "remember") {
                result = memory_remember(get("fact"));
            } else if (tc.name == "reset_session") {
                result = reset_session_tool(channel, current_session, get("session"));
            } else if (tc.name == "send_message") {
                channel.send(get("content"));
                result = json{{"sent", true}};
            } else if (tc.name == "send_file") {
                result = channel.send_file(get("path"), get("caption"));
            } else if (tc.name == "fetch_channel_messages") {
                auto limit = std::clamp(tc.input.value("limit", 20), 1, 100);
                auto msgs = channel.history(limit);
                result = json{{"messages", msgs}, {"count", msgs.is_array() ? msgs.size() : 0}};
            } else if (tc.name == "spawn_subagent") {
                result = run_subagents(channel, {{get("name"), get("task")}});
            } else if (tc.name == "spawn_subagents") {
                std::vector<std::pair<std::string, std::string>> tasks;
                if (tc.input.contains("tasks") && tc.input["tasks"].is_array())
                    for (auto& t : tc.input["tasks"])
                        tasks.push_back({t.value("name", ""), t.value("task", "")});
                result = run_subagents(channel, tasks);
            } else {
                result = json{{"error", "unknown tool: " + tc.name}};
            }
            history->push_back(tool_msg(tc, result));
        }
    }
    if (hit_limit)
        channel.send("_(hit the max tool-call limit for this turn — stopping)_");

    memory_log_turn(channel.label(), user_text, final_text);
}

// ── Subagents ────────────────────────────────────────────────────────────────
namespace {
json sub_tool_schemas() {
    json out = json::array();
    auto keep = [](const json& s) {
        auto n = s.value("name", "");
        return n == "run_shell" || n == "run_python" || n == "read_file" || n == "write_file" || n == "list_dir";
    };
    for (auto& s : local_tool_schemas())
        if (keep(s)) out.push_back(s);
    for (auto& s : web_tool_schemas()) out.push_back(s);
    return out;
}

std::string run_sub_task(const Channel& channel, const std::string& name, const std::string& task) {
    (void)channel;
    Provider provider(SUBAGENT_PROMPT);
    auto schemas = sub_tool_schemas();
    auto max_turns_n = env_num("SUBAGENT_MAX_TURNS", 12);
    auto timeout = env_num("SUBAGENT_TIMEOUT", 600);
    (void)timeout;  // each provider call carries its own HTTP timeout
    std::vector<Message> history{Message::user(task)};
    std::string last_text;

    for (int turn = 0; turn < max_turns_n; ++turn) {
        Message msg;
        try {
            msg = provider.step(schemas, history);
        } catch (const std::exception& e) {
            return json{{"name", name}, {"status", "error"}, {"report", e.what()}}.dump();
        }
        history.push_back(msg);
        if (!msg.content.empty() && msg.content.find_first_not_of(" \n\t") != std::string::npos)
            last_text = msg.content;
        if (msg.tool_calls.empty()) {
            auto empty = last_text.find_first_not_of(" \n\t") == std::string::npos;
            auto report = empty ? "(empty report)" : last_text;
            return json{{"name", name}, {"status", "ok"}, {"turns", turn + 1}, {"report", clip(report, 6000)}}.dump();
        }
        for (auto& tc : msg.tool_calls)
            history.push_back(tool_msg(tc, exec_sub_tool(channel, tc)));
    }
    auto report = last_text.empty() ? "hit the turn limit before finishing" : last_text;
    return json{{"name", name}, {"status", "incomplete"}, {"turns", max_turns_n}, {"report", clip(report, 6000)}}.dump();
}
}  // namespace

json Agent::run_subagents(const Channel& channel,
                          std::vector<std::pair<std::string, std::string>> tasks) {
    std::vector<std::pair<std::string, std::string>> clean;
    for (auto& [n, t] : tasks)
        if (!t.empty() && t.find_first_not_of(" \n\t") != std::string::npos)
            clean.push_back({n, t});
    if (clean.size() > SUB_MAX_TASKS) clean.resize(SUB_MAX_TASKS);
    if (clean.empty())
        return json{{"error", "no tasks given — pass tasks=[{\"name\": ..., \"task\": ...}, ...]"}};
    for (size_t i = 0; i < clean.size(); ++i) {
        auto trimmed = clean[i].first;
        auto first = trimmed.find_first_not_of(" \n\t");
        if (first == std::string::npos) clean[i].first = "agent-" + std::to_string(i + 1);
    }
    std::string names;
    for (size_t i = 0; i < clean.size(); ++i) {
        if (i) names += ", ";
        names += clean[i].first;
    }
    channel.send("🧩 Spawning " + std::to_string(clean.size()) + " subagent(s): " + names);

    auto parallel = (size_t)env_num("SUBAGENT_MAX_PARALLEL", 3);
    if (parallel == 0) parallel = 1;
    json results = json::array();
    // run in waves of `parallel` async tasks (same net effect as a semaphore)
    for (size_t start = 0; start < clean.size(); start += parallel) {
        std::vector<std::future<std::string>> futs;
        for (size_t i = start; i < clean.size() && i < start + parallel; ++i)
            futs.push_back(std::async(std::launch::async, run_sub_task,
                                      std::cref(channel),
                                      clean[i].first, clean[i].second));
        for (auto& f : futs) {
            auto s = f.get();
            try {
                results.push_back(json::parse(s));
            } catch (...) {
                results.push_back(json{{"report", s}});
            }
        }
    }
    return json{{"results", results}};
}

json exec_sub_tool(const Channel& channel, const ToolCall& tc) {
    auto get = [&](const std::string& k) { return tc.input.value(k, ""); };
    if (tc.name == "run_shell") {
        auto command = get("command");
        if (is_destructive(command) && !channel.request_approval("[subagent] " + command, false, 60))
            return json{{"error", "command was denied by user"}, {"exit_code", -1}};
        return run_shell(command, false);
    }
    if (tc.name == "run_python") return run_python(get("code"));
    if (tc.name == "read_file") return read_file(get("path"));
    if (tc.name == "write_file") return write_file(get("path"), get("content"));
    if (tc.name == "list_dir") return list_dir(get("path"));
    if (tc.name == "web_search") {
        auto max = std::clamp(tc.input.value("max_results", 5), 1, 10);
        return web_search(get("query"), max).get();
    }
    return json{{"error", "unknown tool (subagents have a reduced toolset): " + tc.name}};
}

namespace {
json reset_session_tool(const Channel& channel, const std::string& current, const std::string& session_ref) {
    bool ok = false;
    auto key = sessions_resolve(session_ref, current, &ok);
    if (!ok) {
        json known = json::array();
        auto list = sessions_list();
        for (size_t i = 0; i < list.size() && i < 10; ++i)
            known.push_back(list[i].value("key", ""));
        return json{{"error", key}, {"known_sessions", known}};
    }
    if (key != current) {
        json info;
        for (auto& s : sessions_list())
            if (s.value("key", "") == key) info = s;
        auto label = info.value("label", "");
        auto what = label.empty() ? key : key + " (" + label + ")";
        auto turns = info.value("turns", 0);
        if (!channel.request_approval(
                "[reset_session] Clear the conversation history of " + what + " (" + std::to_string(turns)
                    + " turn(s)). Long-term memory is not affected.",
                false, 60))
            return json{{"error", "reset of another session was denied by the owner"}, {"session", key}};
    }
    auto res = sessions_reset(key);
    res["long_term_memory"] = "untouched";
    if (key == current)
        res["note"] = "This conversation's history is cleared once your current reply is sent; the next message starts from a blank slate.";
    return res;
}
}  // namespace
}  // namespace sysc
