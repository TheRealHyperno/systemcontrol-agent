cmake_minimum_required(VERSION 3.16)
project(systemc-cpp CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_BUILD_TYPE Release)

find_package(OpenSSL REQUIRED)

add_executable(systemc
    src/env.cpp
    src/http.cpp
    src/http_ws.cpp
    src/tools.cpp
    src/memory_sessions.cpp
    src/providers.cpp
    src/prompt.cpp
    src/chat_platform.cpp
    src/agent.cpp
    src/discord.cpp
    src/panel.cpp
    src/service.cpp
    src/main.cpp
)
target_include_directories(systemc PRIVATE src vendor)
target_compile_options(systemc PRIVATE -O2 -Wall)
target_link_libraries(systemc PRIVATE OpenSSL::SSL OpenSSL::Crypto pthread)
