// http.h — minimal blocking HTTPS/HTTP client on OpenSSL (no libcurl headers here)
#pragma once
#include <map>
#include <optional>
#include <string>

namespace sysc {
struct HttpResponse {
    int status = 0;
    std::string body;
};

// POST/GET a JSON body; https when url begins with https://.
// Send `bearer` as Authorization when non-empty.
HttpResponse http_request(const std::string& method, const std::string& url,
                          const std::string& json_body /*may be ""*/,
                          const std::string& bearer /*may be ""*/,
                          const std::map<std::string, std::string>& extra_headers = {},
                          long timeout_seconds = 240);

// upload multipart/form-data (files[0]) — used by send_file in Discord REST
HttpResponse http_upload(const std::string& url, const std::string& bearer,
                         const std::string& field_name /*payload_json field via form*/,
                         const std::string& payload_json,
                         const std::string& filepath,
                         const std::string& remote_name);

std::string base64_encode(const unsigned char* data, size_t len);

// ── minimal WebSocket client (wss://host/path) — Discord gateway needs it ──
class WebSocket {
public:
    ~WebSocket();
    // only wss:// (and ws:// for tests); returns false on any failure
    bool connect(const std::string& url);
    bool ok() const { return fd_ >= 0; }
    // returns payload of one text message; nullopt on timeout; "" on close
    std::optional<std::string> read_message(long timeout_ms);
    bool send_text(const std::string& payload);

private:
    int fd_ = -1;
    void* ssl_ = nullptr;    // SSL*
    void* ctx_ = nullptr;    // SSL_CTX*
    std::string inbuf_;
    void teardown();
};
}  // namespace sysc
