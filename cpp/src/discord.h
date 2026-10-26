// discord.h — Discord gateway client (discord.rs port)
#pragma once
#include <string>
#include "agent.h"

namespace sysc {
class DiscordBot {
public:
    explicit DiscordBot(std::string token) : token_(std::move(token)) {}
    // blocks, reconnecting forever after drops
    void run(Agent& agent);

private:
    void gateway_session(Agent& agent);
    void dispatch(const nlohmann::json& d, const std::string& me_id,
                  Agent& agent);
    std::string token_;
    std::string mode_, wake_;
    std::vector<std::string> allowed_;
};
}  // namespace sysc
