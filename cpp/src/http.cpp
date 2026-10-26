// http.cpp — tiny blocking HTTP(S) client built directly on OpenSSL BIOs
#include "http.h"
#include <openssl/err.h>
#include <openssl/ssl.h>
#include <cctype>
#include <cstring>
#include <fstream>
#include <sstream>

#if defined(_WIN32)
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

namespace sysc {
namespace {

// Parse tcp://host:port from URL and whether TLS is needed.
struct ParsedUrl {
    bool tls = false;
    std::string host;
    int port = 443;
    std::string path;
    bool ok = false;
};

ParsedUrl parse_url(const std::string& url) {
    ParsedUrl p;
    std::string rest;
    if (url.rfind("https://", 0) == 0) { p.tls = true; rest = url.substr(8); }
    else if (url.rfind("http://", 0) == 0) { p.tls = false; rest = url.substr(7); }
    else return p;
    auto slash = rest.find('/');
    auto authority = slash == std::string::npos ? rest : rest.substr(0, slash);
    p.path = slash == std::string::npos ? "/" : rest.substr(slash);
    auto colon = authority.rfind(':');
    if (colon != std::string::npos && authority.find(']') == std::string::npos) {
        p.host = authority.substr(0, colon);
        p.port = std::atoi(authority.substr(colon + 1).c_str());
        if (p.port == 0) p.port = p.tls ? 443 : 80;
    } else {
        p.host = authority;
        p.port = p.tls ? 443 : 80;
    }
    p.ok = !p.host.empty();
    return p;
}

int tcp_connect(const std::string& host, int port) {
    addrinfo hints{};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    addrinfo* res = nullptr;
    std::string port_s = std::to_string(port);
    if (::getaddrinfo(host.c_str(), port_s.c_str(), &hints, &res) != 0) return -1;
    int fd = -1;
    for (auto* ai = res; ai; ai = ai->ai_next) {
        fd = ::socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
        if (fd < 0) continue;
        timeval tv{600, 0};
        ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
        ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
        if (::connect(fd, ai->ai_addr, ai->ai_addrlen) == 0) break;
        ::close(fd);
        fd = -1;
    }
    ::freeaddrinfo(res);
    return fd;
}

bool recv_until_eof(int fd, SSL* ssl, std::string& out) {
    char buf[16384];
    while (true) {
        int n = ssl ? SSL_read(ssl, buf, sizeof buf) : (int)::recv(fd, buf, sizeof buf, 0);
        if (n <= 0) break;
        out.append(buf, buf + n);
    }
    return true;
}

// Read a chunked transfer body with proper framing: parse each "size\r\n"
// chunk header, keep only payload, stop at the terminating 0-chunk.
bool read_chunked(int fd, SSL* ssl, std::string& body) {
    auto next_line = [&]() -> std::string {
        // read bytes until CRLF pulled from fd/ssl
        for (;;) {
            auto eol = body.find('\n');
            if (eol != std::string::npos) {
                std::string line = body.substr(0, eol);
                while (!line.empty() && line.back() == '\r') line.pop_back();
                body.erase(0, eol + 1);
                return line;
            }
            char buf[16384];
            int n = ssl ? SSL_read(ssl, buf, sizeof buf) : (int)::recv(fd, buf, sizeof buf, 0);
            if (n <= 0) return "";
            body.append(buf, buf + n);
        }
    };
    std::string decoded;
    while (true) {
        auto line = next_line();
        if (line.empty()) break;
        // chunk-size may be followed by ";ext"
        auto semi = line.find(';');
        unsigned long long size = std::strtoull(line.substr(0, semi).c_str(), nullptr, 16);
        if (size == 0) { next_line(); break; }  // trailing CRLF (and any trailers)
        while (body.size() < size + 2) {
            char buf[16384];
            int n = ssl ? SSL_read(ssl, buf, sizeof buf) : (int)::recv(fd, buf, sizeof buf, 0);
            if (n <= 0) return true;
            body.append(buf, buf + n);
        }
        decoded.append(body.substr(0, (size_t)size));
        body.erase(0, (size_t)size);
        // consume the trailing CRLF of the chunk
        next_line();
    }
    body = decoded;
    return true;
}
}  // namespace

HttpResponse http_request(const std::string& method, const std::string& url,
                          const std::string& json_body, const std::string& bearer,
                          const std::map<std::string, std::string>& extra_headers,
                          long timeout_seconds) {
    HttpResponse out;
    auto p = parse_url(url);
    if (!p.ok) { out.status = 0; out.body = "bad url: " + url; return out; }

    int fd = tcp_connect(p.host, p.port);
    if (fd < 0) { out.body = "connect failed to " + p.host; return out; }

    SSL* ssl = nullptr;
    SSL_CTX* ctx = nullptr;
    if (p.tls) {
        ctx = SSL_CTX_new(TLS_client_method());
        SSL_CTX_set_default_verify_paths(ctx);
        ssl = SSL_new(ctx);
        SSL_set_tlsext_host_name(ssl, p.host.c_str());
        SSL_set_fd(ssl, fd);
        if (SSL_connect(ssl) != 1) {
            ::close(fd);
            SSL_free(ssl);
            SSL_CTX_free(ctx);
            out.body = "TLS handshake failed";
            return out;
        }
    }

    std::ostringstream req;
    req << method << " " << p.path << " HTTP/1.1\r\n"
        << "Host: " << p.host << "\r\n"
        << "User-Agent: systemc-cpp/1.0\r\n";
    if (!bearer.empty()) req << "Authorization: Bearer " << bearer << "\r\n";
    if (!json_body.empty()) {
        bool has_ctype = false;
        for (auto& [k, v] : extra_headers)
            for (char& c : const_cast<std::string&>(k)) c = (char)tolower((unsigned char)c);
        for (auto& [k, v] : extra_headers)
            if (k == "content-type") has_ctype = true;
        if (!has_ctype) req << "Content-Type: application/json\r\n";
        req << "Content-Length: " << json_body.size() << "\r\n";
    }
    for (auto& [k, v] : extra_headers) req << k << ": " << v << "\r\n";
    req << "Connection: close\r\n\r\n";
    if (!json_body.empty()) req << json_body;

    std::string wire = req.str();
    int sent = 0;
    while (sent < (int)wire.size()) {
        int n = ssl ? SSL_write(ssl, wire.data() + sent, (int)(wire.size() - sent))
                    : (int)::send(fd, wire.data() + sent, wire.size() - sent, 0);
        if (n <= 0) break;
        sent += n;
    }

    // Read headers + body. Content-Length or chunked, or read-to-EOF.
    std::string raw;
    {
        char buf[16384];
        // header terminator scan
        std::size_t hdr_end = std::string::npos;
        while (hdr_end == std::string::npos) {
            int n = ssl ? SSL_read(ssl, buf, sizeof buf) : (int)::recv(fd, buf, sizeof buf, 0);
            if (n <= 0) break;
            raw.append(buf, buf + n);
            hdr_end = raw.find("\r\n\r\n");
        }
        if (hdr_end == std::string::npos) {
            ::close(fd);
            if (ssl) { SSL_free(ssl); SSL_CTX_free(ctx); }
            out.body = "no response headers";
            return out;
        }
        std::string head = raw.substr(0, hdr_end);
        // status line
        std::istringstream hline(head.substr(0, head.find("\r\n")));
        std::string proto, codestr;
        hline >> proto >> codestr;
        out.status = std::atoi(codestr.c_str());
        bool chunked = head.find("Transfer-Encoding: chunked") != std::string::npos
                    || head.find("transfer-encoding: chunked") != std::string::npos;
        std::size_t content_length = std::string::npos;
        {
            // find Content-Length case-insensitively
            std::string lower;
            lower.reserve(head.size());
            for (char c : head) lower.push_back((char)tolower(c));
            auto pos = lower.find("content-length:");
            if (pos != std::string::npos) content_length = std::strtoull(head.c_str() + pos + 15, nullptr, 10);
        }
        std::string body = raw.substr(hdr_end + 4);
        if (chunked) {
            read_chunked(fd, ssl, body);
            out.body = body;
        } else if (content_length != std::string::npos) {
            while (body.size() < content_length) {
                int n = ssl ? SSL_read(ssl, buf, sizeof buf) : (int)::recv(fd, buf, sizeof buf, 0);
                if (n <= 0) break;
                body.append(buf, buf + n);
            }
            out.body = body.substr(0, content_length);
        } else {
            recv_until_eof(fd, ssl, body);
            out.body = body;
        }
    }
    ::close(fd);
    if (ssl) { SSL_free(ssl); SSL_CTX_free(ctx); }
    return out;
}

HttpResponse http_upload(const std::string& url, const std::string& bearer,
                         const std::string& payload_field_name, const std::string& payload_json,
                         const std::string& filepath, const std::string& remote_name) {
    HttpResponse out;
    // build multipart body in memory
    std::string boundary = "----systemc" + std::to_string(::time(nullptr)) + std::to_string(::rand());
    std::ostringstream head;
    head << "--" << boundary << "\r\n"
         << "Content-Disposition: form-data; name=\"" << payload_field_name << "\"\r\n\r\n"
         << payload_json << "\r\n";
    std::ostringstream fpart;
    fpart << "--" << boundary << "\r\n"
          << "Content-Disposition: form-data; name=\"files[0]\"; filename=\"" << remote_name << "\"\r\n"
          << "Content-Type: application/octet-stream\r\n\r\n";
    std::string fdata;
    {
        std::ifstream f(filepath, std::ios::binary);
        std::ostringstream ss;
        ss << f.rdbuf();
        fdata = ss.str();
    }
    std::string body = head.str() + fpart.str() + fdata + "\r\n--" + boundary + "--\r\n";

    auto p = parse_url(url);
    if (!p.ok) { out.body = "bad url"; return out; }
    int fd = tcp_connect(p.host, p.port);
    if (fd < 0) { out.body = "connect failed"; return out; }
    SSL* ssl = nullptr; SSL_CTX* ctx = nullptr;
    if (p.tls) {
        ctx = SSL_CTX_new(TLS_client_method());
        SSL_CTX_set_default_verify_paths(ctx);
        ssl = SSL_new(ctx);
        SSL_set_tlsext_host_name(ssl, p.host.c_str());
        SSL_set_fd(ssl, fd);
        if (SSL_connect(ssl) != 1) { ::close(fd); SSL_free(ssl); SSL_CTX_free(ctx); out.body = "TLS2 failed"; return out; }
    }
    std::ostringstream req;
    req << "POST " << p.path << " HTTP/1.1\r\n"
        << "Host: " << p.host << "\r\n";
    if (!bearer.empty()) req << "Authorization: Bearer " << bearer << "\r\n";
    req << "Content-Type: multipart/form-data; boundary=" << boundary << "\r\n"
        << "Content-Length: " << body.size() << "\r\n"
        << "Connection: close\r\n\r\n" << body;
    std::string wire = req.str();
    size_t sent = 0;
    while (sent < wire.size()) {
        int n = ssl ? SSL_write(ssl, wire.data() + sent, (int)(wire.size() - sent))
                    : (int)::send(fd, wire.data() + sent, wire.size() - sent, 0);
        if (n <= 0) break;
        sent += (size_t)n;
    }
    char buf[16384];
    std::string resp;
    int n;
    while ((n = ssl ? SSL_read(ssl, buf, sizeof buf) : (int)::recv(fd, buf, sizeof buf, 0)) > 0) resp.append(buf, buf + n);
    std::size_t hdr_end = resp.find("\r\n\r\n");
    auto status = resp.find(' ');
    if (hdr_end != std::string::npos && status != std::string::npos) {
        out.status = std::atoi(resp.substr(status + 1, 3).c_str());
        out.body = resp.substr(hdr_end + 4);
    }
    ::close(fd);
    if (ssl) { SSL_free(ssl); SSL_CTX_free(ctx); }
    return out;
}
}  // namespace sysc
