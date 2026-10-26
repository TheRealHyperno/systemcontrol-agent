// chat_platform.h — Discord REST half + the channel abstraction (port of chat_platform.rs)
#pragma once
#include <nlohmann/json.hpp>
#include <string>
#include <vector>

namespace sysc {

constexpr size_t MAX_MESSAGE_LEN = 2000;

struct Author {
    std::string id;
    std::string name;
};

std::string sender_tag(const Author& a);          // "[from: Name (@Name, id N)]:"

// Discord REST client for one channel
struct DiscordRest {
    std::string token;
    std::string channel_id;
    std::string app_id;   // the bot's user id (to exclude its own reactions)
    std::string label;

    void send_message(const std::string& content) const;
    nlohmann::json send_file(const std::string& path, const std::string& caption) const;
    void typing() const;
    nlohmann::json fetch_history(int limit) const;   // oldest first
    bool request_approval(const std::string& command, bool elevated, int timeout_secs) const;
private:
    nlohmann::json api(const std::string& method, const std::string& path,
                       const nlohmann::json& body) const;
    bool has_reaction(const std::string& mid, const std::string& emoji) const;
};

// ChannelAdapter equivalent
struct Channel {
    enum Kind { Cli, Discord } kind = Cli;
    DiscordRest rest{};

    std::string platform() const { return kind == Cli ? "cli" : "discord"; }
    std::string id() const { return kind == Cli ? "cli" : rest.channel_id; }
    std::string label() const { return kind == Cli ? "terminal" : rest.label; }

    void send(const std::string& content) const;
    nlohmann::json send_file(const std::string& path, const std::string& caption) const;
    void typing() const;
    bool request_approval(const std::string& command, bool elevated, int timeout_secs) const;
    nlohmann::json history(int limit) const;
};

std::vector<std::string> chunk_message(const std::string& content);
std::string download_attachment(const std::string& url, const std::string& filename,
                                bool* ok);
}  // namespace sysc
