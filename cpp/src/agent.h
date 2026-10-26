// agent.h — the agent loop + subagents (port of agent.rs)
#pragma once
#include <nlohmann/json.hpp>
#include <string>
#include <utility>
#include <vector>
#include "chat_platform.h"
#include "providers.h"

namespace sysc {
int max_turns();
bool subagents_enabled();
constexpr int SUB_MAX_TASKS = 5;

class Agent {
public:
    // throws std::runtime_error on bad config
    explicit Agent();
    // runs one user turn: pushes the prompt, loops the model through tool
    // calls until it answers; sends the final text over the channel
    void run_turn(const Channel& channel, const std::string& user_text,
                  std::vector<Message>* history, const Author* author);
    // run up to 5 subagent tasks in parallel; returns {"results": [...]}
    nlohmann::json run_subagents(const Channel& channel,
                                 std::vector<std::pair<std::string, std::string>> tasks);

private:
    Provider provider_;
    std::string base_prompt_;
};

// shared with the Discord bot's !command shortcut (agent.cpp)
nlohmann::json exec_sub_tool(const Channel& channel, const ToolCall& tc);
}  // namespace sysc
