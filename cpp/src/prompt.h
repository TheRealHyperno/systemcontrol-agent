// prompt.h — system prompt template + {VARIABLES} (port of prompt.rs)
#pragma once
#include <string>

namespace sysc {
std::string build_prompt(const std::string& model, const std::string& source);
extern const char* SUBAGENT_PROMPT;
}  // namespace sysc
