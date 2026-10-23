package systemc;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import com.sun.net.httpserver.HttpsServer;

import java.io.*;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.URLDecoder;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.*;
import java.util.concurrent.Executors;
import java.util.zip.GZIPOutputStream;

/**
 * Port of webui_server.py — local HTTP server on 127.0.0.1:7842 serving:
 *   /                 webui.html (see webui.html; the piped JSON API is unchanged)
 *   /tools/run_shell|read_file|write_file|list_dir|run_python|web_search
 *   /ollama/chat      NDJSON-streaming proxy to the local Ollama server
 *   /config           {workdir, mode, ...}
 *   /api/v1/...       bearer-token-protected: approvals, sessions, profiles
 */
public final class WebUi {
    public static final int PORT = 7842;

    private final Agent agent;
    private HttpServer server;

    // webui.html is loaded from the classpath; the Python version read it from disk.
    static String webuiHtml() throws IOException {
        try (var in = WebUi.class.getResourceAsStream("/webui.html")) {
            return in == null ? "<h1>webui.html missing</h1>" : new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    public WebUi(Agent agent) { this.agent = agent; }

    public static final ObjectMapper OM = new ObjectMapper().enable(SerializationFeature.INDENT_OUTPUT);

    public void start(int port) throws IOException {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
        server.setExecutor(Executors.newVirtualThreadPerTaskExecutor());
        server.createContext("/", ex -> {
            try {
                String p = ex.getRequestURI().getPath();
                String token = Env.get("SYSTEMC_API_TOKEN");
                if (p.startsWith("/api/v1/")) {
                    if (token.isBlank()) { sendJson(ex, 503, "{\"error\":\"SYSTEMC_API_TOKEN not set\"}"); return; }
                    String auth = ex.getRequestHeaders().getFirst("Authorization");
                    if (auth == null || !java.security.MessageDigest.isEqual(auth.getBytes(StandardCharsets.UTF_8),
                            ("Bearer " + token).getBytes(StandardCharsets.UTF_8))) {
                        sendJson(ex, 401, "{\"error\":\"bad token\"}"); return;
                    }
                    handleApi(ex, p);
                    return;
                }
                switch (p) {
                    case "/", "/index.html" -> sendHtml(ex, 200, webuiHtml());
                    case "/favicon.png" -> sendPng(ex, "favicon.png");
                    case "/logo.png" -> sendPng(ex, "logo.png");
                    case "/tools/run_shell" -> handleShell(ex, false);
                    case "/tools/run_shell_elevated" -> handleShell(ex, true);
                    case "/tools/read_file" -> handleSimple(ex, "/tools/read_file");
                    case "/tools/write_file" -> handleSimple(ex, "/tools/write_file");
                    case "/tools/list_dir" -> handleSimple(ex, "/tools/list_dir");
                    case "/tools/run_python" -> handleSimple(ex, "/tools/run_python");
                    case "/tools/web_search" -> handleSimple(ex, "/tools/web_search");
                    case "/ollama/chat" -> handleOllamaChat(ex);
                    case "/config" -> {
                        Map<String, Object> cfg = Json.map();
                        cfg.put("workdir", Tools.WORKDIR.toString());
                        cfg.put("port", port);
                        sendJson(ex, 200, OM.writeValueAsString(cfg));
                    }
                    default -> {
                        if (p.startsWith("/api/v1")) return; // done above
                        sendHtml(ex, 404, "<h1>404</h1>");
                    }
                }
            } catch (Throwable t) {
                try { sendJson(ex, 500, "{\"error\":" + OM.writeValueAsString(String.valueOf(t)) + "}"); }
                catch (Exception ignored) {}
            }
        });
        server.start();
        System.out.println("✓ web UI + API on http://127.0.0.1:" + port);
    }

    void handleApi(HttpExchange ex, String path) throws Exception {
        if (path.equals("/api/v1/ping")) { sendJson(ex, 200, "{\"ok\":true}"); return; }
        if (path.equals("/api/v1/approvals")) {
            if ("POST".equals(ex.getRequestMethod())) {
                Map<String, Object> body = readJson(ex);
                Approvals.resolve(String.valueOf(body.get("id")),
                        body.get("approved") instanceof Boolean b && b);
            }
            sendJson(ex, 200, OM.writeValueAsString(Approvals.listPending()));
            return;
        }
        if (path.equals("/api/v1/sessions")) {
            var req = new TypeReference<List<Map<String, Object>>>() {};
            sendJson(ex, 200, OM.writeValueAsString(Sessions.listSessions()));
            return;
        }
        if (path.equals("/api/v1/sessions/reset") && "POST".equals(ex.getRequestMethod())) {
            Map<String, Object> body = readJson(ex);
            String key = String.valueOf(body.getOrDefault("key", ""));
            Object r = Sessions.reset(key);
            sendJson(ex, 200, OM.writeValueAsString(r));
            return;
        }
        if (path.equals("/api/v1/memory")) {
            String mem = Memory.loadContext();
            Map<String, Object> out = Json.map();
            out.put("context", mem);
            sendJson(ex, 200, OM.writeValueAsString(out));
            return;
        }
        sendJson(ex, 404, "{\"error\":\"no such endpoint\"}");
    }

    void handleShell(HttpExchange ex, boolean elevated) throws Exception {
        Map<String, Object> body = readJson(ex);
        String cmd = String.valueOf(body.getOrDefault("command", body.getOrDefault("cmd", "")));
        Object result = elevated ? Tools.runShellElevated(cmd) : Tools.runShell(cmd);
        sendJson(ex, 200, OM.writeValueAsString(result));
    }

    void handleSimple(HttpExchange ex, String tool) throws Exception {
        Map<String, Object> body = readJson(ex);
        Object result = switch (tool) {
            case "/tools/read_file" -> Tools.readFile(String.valueOf(body.get("path")));
            case "/tools/write_file" -> Tools.writeFile(String.valueOf(body.get("path")),
                    String.valueOf(body.getOrDefault("content", "")));
            case "/tools/list_dir" -> Tools.listDir(String.valueOf(body.getOrDefault("path", ".")));
            case "/tools/run_python" -> Tools.runPython(String.valueOf(body.getOrDefault("code", "")));
            case "/tools/web_search" -> Tools.webSearch(String.valueOf(body.get("query")),
                    body.get("max_results") instanceof Integer i ? i : null);
            default -> Map.of("error", "unknown endpoint");
        };
        sendJson(ex, 200, OM.writeValueAsString(result));
    }

    /** Streaming NDJSON proxy like webui_server.py's /ollama/chat (local Ollama only). */
    void handleOllamaChat(HttpExchange ex) throws Exception {
        if (!"POST".equals(ex.getRequestMethod())) { sendJson(ex, 405, "{\"error\":\"POST only\"}"); return; }
        byte[] body = ex.getRequestBody().readAllBytes();
        String host = Env.get("OLLAMA_HOST").isBlank() ? Providers.OLLAMA_LOCAL_HOST + "" : Env.get("OLLAMA_HOST");
        HttpResponse<InputStream> upstream;
        try {
            upstream = Providers.HTTP.send(HttpRequest.newBuilder(URI.create(host.replaceAll("/+$", "") + "/api/chat"))
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofByteArray(body)).build(),
                    HttpResponse.BodyHandlers.ofInputStream());
        } catch (Exception e) {
            sendJson(ex, 502, "{\"error\":\"can not reach ollama: " + e.getMessage() + "\"}");
            return;
        }
        ex.getResponseHeaders().set("Content-Type", "application/x-ndjson");
        ex.getResponseHeaders().set("Cache-Control", "no-cache");
        ex.sendResponseHeaders(upstream.statusCode() == 200 ? 200 : upstream.statusCode(), 0);
        try (var out = ex.getResponseBody(); var in = upstream.body()) {
            in.transferTo(out); // NDJSON lines flow straight through (Jackson just relays them)
        }
    }

    public static Map<String, Object> readJson(HttpExchange ex) throws IOException {
        byte[] b = ex.getRequestBody().readAllBytes();
        Map<String, Object> m = Json.parse(b.length == 0 ? "{}" : new String(b, StandardCharsets.UTF_8));
        return m == null ? Json.map() : m;
    }

    static void sendJson(HttpExchange ex, int status, String body) throws IOException {
        ex.getResponseHeaders().set("Content-Type", "application/json");
        ex.getResponseHeaders().set("Access-Control-Allow-Origin", "*");
        byte[] b = body.getBytes(StandardCharsets.UTF_8);
        ex.sendResponseHeaders(status, b.length == 0 ? -1 : b.length);
        try (var out = ex.getResponseBody()) {
            out.write(b);
        }
    }

    static void sendPng(HttpExchange ex, String resource) throws IOException {
        ex.getResponseHeaders().set("Content-Type", "image/png");
        ex.getResponseHeaders().set("Cache-Control", "max-age=86400");
        byte[] b;
        try (var in = WebUi.class.getResourceAsStream("/" + resource)) {
            if (in == null) { sendHtml(ex, 404, "missing"); return; }
            b = in.readAllBytes();
        }
        ex.sendResponseHeaders(200, b.length);
        try (var out = ex.getResponseBody()) { out.write(b); }
    }

    static void sendHtml(HttpExchange ex, int status, String body) throws IOException {
        ex.getResponseHeaders().set("Content-Type", "text/html; charset=utf-8");
        ex.sendResponseHeaders(status, 0);
        try (var out = ex.getResponseBody()) {
            out.write(body.getBytes(StandardCharsets.UTF_8));
        }
    }
}
