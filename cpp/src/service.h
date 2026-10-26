// service.h — systemd integration (port of service.rs)
#pragma once
#include <nlohmann/json.hpp>
#include <string>

namespace sysc {
struct ServiceOpts {
    std::string profile;  // "" = default
    bool linger = false;
    bool system = false;
};

ServiceOpts parse_service_args(const std::vector<std::string>& args, const std::string& action);
nlohmann::json service_install(const ServiceOpts& opts);
nlohmann::json service_uninstall(const std::string& profile, bool system);
std::string service_status();
}  // namespace sysc
