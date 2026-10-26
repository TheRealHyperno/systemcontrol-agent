// http_ws.cpp — WebSocket client on the same OpenSSL plumbing (Discord gateway)
#include "http.h"
#include <openssl/err.h>
#include <openssl/ssl.h>
#include <random>
#include <sstream>
#include <cstring>

#if defined(_WIN32)
#include <winsock2.h>
#else
#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/select.h>
#include <unistd.h>
#endif

namespace sysc {

std::string base64_encode(const unsigned char* data, size_t len) {
    static const char* tbl = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    out.reserve((len + 2) / 3 * 4);
    size_t i = 0;
    for (; i + 2 < len; i += 3) {
        unsigned v = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2];
        out += tbl[(v >> 18) & 63]; out += tbl[(v >> 12) & 63];
        out += tbl[(v >> 6) & 63];  out += tbl[v & 63];
    }
    if (i + 1 == len) {
        unsigned v = data[i] << 16;
        out += tbl[(v >> 18) & 63]; out += tbl[(v >> 12) & 63]; out += '='; out += '=';
    } else if (i + 2 == len) {
        unsigned v = (data[i] << 16) | (data[i + 1] << 8);
        out += tbl[(v >> 18) & 63]; out += tbl[(v >> 12) & 63]; out += tbl[(v >> 6) & 63]; out += '=';
    }
    return out;
}

namespace {
SSL* S = nullptr;

int rd(int fd, char* buf, size_t n) {
    return S ? SSL_read(S, buf, (int)n) : (int)::recv(fd, buf, n, 0);
}
bool wr(int fd, const char* buf, size_t n) {
    while (n) {
        int w = S ? SSL_write(S, buf, (int)n) : (int)::send(fd, buf, n, 0);
        if (w <= 0) return false;
        buf += w; n -= (size_t)w;
    }
    return true;
}
}  // namespace

WebSocket::~WebSocket() { teardown(); }

void WebSocket::teardown() {
    if (fd_ >= 0) ::close(fd_);
    fd_ = -1;
    if (ssl_) { SSL_free((SSL*)ssl_); ssl_ = nullptr; }
    if (ctx_) { SSL_CTX_free((SSL_CTX*)ctx_); ctx_ = nullptr; }
    inbuf_.clear();
}

bool WebSocket::connect(const std::string& url) {
    teardown();
    bool tls = url.rfind("wss://", 0) == 0;
    std::string rest = url.substr(tls ? 6 : 5);
    auto slash = rest.find('/');
    std::string authority = slash == std::string::npos ? rest : rest.substr(0, slash);
    std::string path = slash == std::string::npos ? "/" : rest.substr(slash);
    std::string host = authority;
    int port = tls ? 443 : 80;
    auto colon = authority.rfind(':');
    if (colon != std::string::npos) {
        host = authority.substr(0, colon);
        port = std::atoi(authority.substr(colon + 1).c_str());
        if (port == 0) port = tls ? 443 : 80;
    }

    // reuse the TCP connect logic via a tiny local impl
    addrinfo hints{};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    addrinfo* res = nullptr;
    if (::getaddrinfo(host.c_str(), std::to_string(port).c_str(), &hints, &res) != 0) return false;
    for (auto* ai = res; ai; ai = ai->ai_next) {
        fd_ = ::socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
        if (fd_ < 0) continue;
        if (::connect(fd_, ai->ai_addr, ai->ai_addrlen) == 0) break;
        ::close(fd_); fd_ = -1;
    }
    ::freeaddrinfo(res);
    if (fd_ < 0) return false;

    S = nullptr;
    SSL_CTX* ctx = nullptr;
    if (tls) {
        ctx = SSL_CTX_new(TLS_client_method());
        SSL_CTX_set_default_verify_paths(ctx);
        S = SSL_new(ctx);
        SSL_set_tlsext_host_name(S, host.c_str());
        SSL_set_fd(S, fd_);
        if (SSL_connect(S) != 1) { SSL_free(S); SSL_CTX_free(ctx); teardown(); return false; }
        ssl_ = S;   // S is used by rd/wr below
        ctx_ = ctx;
    }

    unsigned char nonce[16];
    std::mt19937_64 rng((unsigned long long)std::random_device{}() << 32
                        ^ (unsigned long long)::time(nullptr) ^ (unsigned long long)::getpid());
    for (auto& b : nonce) b = (unsigned char)(rng() >> 32);
    std::ostringstream req;
    req << "GET " << path << " HTTP/1.1\r\n"
        << "Host: " << host << ":" << port << "\r\n"
        << "Upgrade: websocket\r\n"
        << "Connection: Upgrade\r\n"
        << "Sec-WebSocket-Key: " << base64_encode(nonce, sizeof nonce) << "\r\n"
        << "Sec-WebSocket-Version: 13\r\n"
        << "User-Agent: systemc-cpp/1.0\r\n\r\n";
    if (!wr(fd_, req.str().data(), req.str().size())) { teardown(); return false; }

    // read the handshake reply
    char buf[4096];
    std::string resp;
    while (resp.find("\r\n\r\n") == std::string::npos) {
        int n = rd(fd_, buf, sizeof buf);
        if (n <= 0) { teardown(); return false; }
        resp.append(buf, buf + n);
    }
    inbuf_ = resp.substr(resp.find("\r\n\r\n") + 4);
    // note: after handshake S stays set for subsequent rd()/wr() calls
    return resp.find(" 101") != std::string::npos;
}

bool WebSocket::send_text(const std::string& payload) {
    if (fd_ < 0) return false;
    std::string frame;
    frame.push_back((char)(0x81));  // FIN + text
    unsigned char key[4];
    std::mt19937 rng((unsigned)::time(nullptr) ^ (unsigned)::getpid() ^ (unsigned)(size_t)this);
    for (auto& b : key) b = (unsigned char)rng();
    size_t n = payload.size();
    if (n < 126) frame.push_back((char)(0x80 | n));
    else if (n < 65536) {
        frame.push_back((char)(0x80 | 126));
        frame.push_back((char)((n >> 8) & 0xff)); frame.push_back((char)(n & 0xff));
    } else {
        frame.push_back((char)(0x80 | 127));
        for (int i = 7; i >= 0; --i) frame.push_back((char)((n >> (i * 8)) & 0xff));
    }
    frame.append((char*)key, (char*)key + 4);
    frame.resize(frame.size() + n);
    for (size_t i = 0; i < n; ++i) frame[frame.size() - n + i] = payload[i] ^ key[i % 4];
    return wr(fd_, frame.data(), frame.size());
}

std::optional<std::string> WebSocket::read_message(long timeout_ms) {
    // parse any complete frame already in the buffer
    for (;;) {
        if (inbuf_.size() >= 2) {
            size_t hdr = 2;
            uint64_t len = (unsigned char)inbuf_[1] & 0x7f;
            if (len == 126) {
                if (inbuf_.size() < 4) break;
                len = ((unsigned char)inbuf_[2] << 8) | (unsigned char)inbuf_[3];
                hdr = 4;
            } else if (len == 127) {
                if (inbuf_.size() < 10) break;
                len = 0;
                for (int i = 2; i < 10; ++i) len = (len << 8) | (unsigned char)inbuf_[i];
                hdr = 10;
            }
            if (inbuf_.size() < hdr + len) break;
            int opcode = inbuf_[0] & 0x0f;
            std::string payload = inbuf_.substr(hdr, (size_t)len);
            inbuf_.erase(0, hdr + len);
            if (opcode == 8) return std::string();               // close
            if (opcode == 1) return payload;                     // text
            // ping -> pong, pong/other ignored
            if (opcode == 9) send_text("");
            continue;
        }
        break;
    }
    // nothing parseable yet — wait for data
    if (ssl_ && SSL_pending((SSL*)ssl_) > 0) {
        char buf[65536];
        int n = rd(fd_, buf, sizeof buf);
        if (n > 0) inbuf_.append(buf, buf + n);
        return read_message(0);
    }
    fd_set rfds;
    FD_ZERO(&rfds);
    FD_SET(fd_, &rfds);
    timeval tv{timeout_ms / 1000, (timeout_ms % 1000) * 1000};
    if (::select(fd_ + 1, &rfds, nullptr, nullptr, &tv) <= 0) return std::nullopt;
    char buf[65536];
    int n = rd(fd_, buf, sizeof buf);
    if (n <= 0) return std::string();
    inbuf_.append(buf, buf + n);
    return read_message(0);
}
}  // namespace sysc
