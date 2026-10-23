package systemc;

import java.io.*;
import java.net.URI;
import java.net.URLDecoder;
import java.net.http.*;
import java.net.http.WebSocket;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;
import java.util.regex.Pattern;

/**
 * Port of stoat_bot.py. There is no mature Stoat/Revolt client for Java, so this
 * is a small custom client on the JDK WebSocket + HttpClient:
 *
 *   REST  POST {API}/channels/{id}/messages           send message
 *         GET  {API}/channels/{id}/messages?limit=N   history
 *         GET  {API}/users/@me                        self info (bot id)
 *   GW    wss://...  {"type":"Authenticate","token":...}
 *   FILES POST {AUTUMN}/attachments  (multipart) -> {"id"} then message.attachments
 *
 * Base URLs are configurable:  STOAT_API_BASE / STOAT_WS_BASE / STOAT_AUTUMN_BASE
 * (defaults target api.stoat.chat / ws.stoat.chat / autumn.stoat.chat).
 *
 * Fixes vs. the Python version (which only had wakeword/mention/everything here):
 * the !command shell shortcut, /reset, attachment downloads and in-chat
 * yes/no approvals all work on Stoat too.
 */
public final class StoatBot {
    static final Pattern NOT_FILENAME = Pattern.compile("[^\\w.\\-]");
    static final int HISTORY_TRIM = 60;

    final Agent agent;
    final String apiBase, wsBase, autumnBase;
    volatile String botId;
    final String token;

    // pending approvals: channelId -> future
    final Map<String, CompletableFuture<Boolean>> pending = new ConcurrentHashMap<>();
    final WebSocket ws;

    public StoatBot(Agent agent, String token) {
        this.agent = agent;
        this.token = token;
        this.apiBase = orDefault(Env.get("STOAT_API_BASE"), "https://api.stoat.chat");
        this.wsBase = orDefault(Env.get("STOAT_WS_BASE"), "wss://ws.stoat.chat");
        this.autumnBase = orDefault(Env.get("STOAT_AUTUMN_BASE"), "https://autumn.stoat.chat");
        this.ws = connect();
        // fetch id of ourselves
        Thread.ofVirtual().start(this::fetchSelfId);
    }

    static String orDefault(String v, String def) { return v.isBlank() ? def : v; }

    WebSocket connect() {
        System.out.println("[stoat] connecting...");
        CompletableFuture<WebSocket> ready = new CompletableFuture<>();
        WebSocket ws = HttpClient.newHttpClient().newWebSocketBuilder()
                .connectTimeout(Duration.ofSeconds(20))
                .buildAsync(URI.create(wsBase), new Listener(ready)).join();
        ws.sendText("{\"type\":\"Authenticate\",\"token\":\"" + token + "\"}", false);
        return ws;
    }

    private final class Listener implements WebSocket.Listener {
        private final CompletableFuture<WebSocket> ready;
        private final StringBuilder buf = new StringBuilder();

        Listener(CompletableFuture<WebSocket> ready) { this.ready = ready; }

        @Override
        public CompletionStage<?> onText(WebSocket ws, CharSequence data, boolean last) {
            buf.append(data);
            if (last) { handleGateway(buf.toString()); buf.setLength(0); }
            return WebSocket.Listener.super.onText(ws, data, last);
        }

        @Override
        public CompletionStage<?> onClose(WebSocket ws, int statusCode, String reason) {
            System.out.println("[stoat] gateway closed: " + statusCode + " " + reason);
            return WebSocket.Listener.super.onClose(ws, statusCode, reason);
        }
    }

    private void handleGateway(String json) {
        Map<String, Object> ev = Json.parse(json);
        if (ev == null) return;
        String type = String.valueOf(ev.get("type"));
        try {
            switch (type) {
                case "Authenticated" -> System.out.println("[stoat] authenticated");
                case "Ready" -> {
                    Map<String, Object> user = Json.obj(ev, "user");
                    botId = String.valueOf(user.get("id"));
                    logReady();
                }
                case "Message" -> onMessage(ev);
                default -> {}
            }
        } catch (Exception e) {
            System.out.println("[stoat] event error: " + e + " for " + type);
        }
    }

    private void logReady() {
        System.out.println("✓ [stoat] logged in as " + botId);
        System.out.println("  mode:      " + Env.get("STOAT_BOT_MODE", "WAKEWORD").toUpperCase());
        if (Env.get("STOAT_BOT_MODE", "WAKEWORD").toUpperCase().equals("WAKEWORD"))
            System.out.println("  wake word: \"" + Env.get("STOAT_WAKE_WORD", "hey clam").toLowerCase() + "\"");
        if (!Env.get("STOAT_ALLOWED_CHANNEL_IDS").isEmpty())
            System.out.println("  channels:  " + Env.get("STOAT_ALLOWED_CHANNEL_IDS"));
        else
            System.out.println("  WARNING: no STOAT_ALLOWED_CHANNEL_IDS set — responding everywhere");
    }

    /** Revolt message.event: content may be a string or a Markdown-node array. */
    @SuppressWarnings("unchecked")
    static String extractText(Object content) {
        if (content == null) return "";
        if (content instanceof String s) return s;
        StringBuilder sb = new StringBuilder();
        appendNodes(content, sb);
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    static void appendNodes(Object node, StringBuilder sb) {
        if (node instanceof String s) { sb.append(s); return; }
        if (node instanceof Map<?, ?> m) {
            Object type = m.get("type");
            if ("text".equals(type) || "markdown".equals(type)) {
                Object c = m.get("content");
                if (c != null) sb.append(extractText(c));
            } else {
                for (String k : List.of("content", "children")) {
                    if (m.get(k) != null) appendNodes(m.get(k), sb);
                }
            }
        } else if (node instanceof List<?> l) {
            for (Object o : l) appendNodes(o, sb);
        }
    }

    // ── REST helpers ─────────────────────────────────────────────────────
    HttpResponse<String> request(String method, String path, String jsonBody) throws Exception {
        var rb = HttpRequest.newBuilder(URI.create(apiBase + path)).timeout(Duration.ofSeconds(20))
                .header("X-Bot-Token", token)
                .header("Content-Type", "application/json");
        rb.method(method, jsonBody == null
                ? HttpRequest.BodyPublishers.noBody()
                : HttpRequest.BodyPublishers.ofString(jsonBody, StandardCharsets.UTF_8));
        return Providers.HTTP.send(rb.build(), HttpResponse.BodyHandlers.ofString());
    }

    Map<String, Object> requestJson(String method, String path, String body) {
        try {
            HttpResponse<String> resp = request(method, path, body);
            if (resp.statusCode() >= 300) {
                System.out.println("[stoat] " + method + " " + path + " -> HTTP " + resp.statusCode());
                return Json.map();
            }
            return Json.parse(resp.body());
        } catch (Exception e) {
            System.out.println("[stoat] request failed: " + e);
            return Json.map();
        }
    }

    void fetchSelfId() {
        try {
            Map<String, Object> me = requestJson("GET", "/users/@me", null);
            String id = Json.str(me, "id");
            if (id != null) {
                botId = id;
                logReady();
            }
        } catch (Exception e) {
            System.out.println("[stoat] couldn't fetch /users/@me — check STOAT_BOT_TOKEN");
        }
    }

    void sendChannelMessage(String chId, String content) throws Exception {
        Map<String, Object> body = Json.map();
        body.put("content", content);
        HttpResponse<String> resp = request("POST", "/channels/" + chId + "/messages",
                Json.COMPACT.writeValueAsString(body));
        if (resp.statusCode() >= 300)
            throw new RuntimeException("send failed: HTTP " + resp.statusCode() + " " + resp.body());
    }

    /** Send a message that may include an already-uploaded attachment id. */
    void sendWithAttachment(String chId, String content, String uploadId, String fileName) throws Exception {
        Map<String, Object> msg = Json.map();
        if (content != null && !content.isBlank()) msg.put("content", content);
        if (uploadId != null) {
            Map<String, Object> att = Json.map();
            att.put("id", uploadId);
            att.put("filename", fileName);
            msg.put("attachments", List.of(att));
        }
        HttpResponse<String> resp = request("POST", "/channels/" + chId + "/messages",
                Json.COMPACT.writeValueAsString(msg));
        if (resp.statusCode() >= 300)
            throw new RuntimeException("send failed: HTTP " + resp.statusCode() + " " + resp.body());
    }

    String uploadAttachment(Path file) throws Exception {
        String boundary = "----systemc" + Long.toHexString(System.currentTimeMillis());
        byte[] fileBytes = Files.readAllBytes(file);
        String pre = "--" + boundary + "\r\n"
                + "Content-Disposition: form-data; name=\"file\"; filename=\"" + file.getFileName() + "\"\r\n"
                + "Content-Type: application/octet-stream\r\n\r\n";
        String post = "\r\n--" + boundary + "--\r\n";
        byte[] body = new byte[pre.getBytes(StandardCharsets.UTF_8).length + fileBytes.length
                + post.getBytes(StandardCharsets.UTF_8).length];
        var idx = 0;
        for (byte b : pre.getBytes(StandardCharsets.UTF_8)) body[idx++] = b;
        for (byte b : fileBytes) body[idx++] = b;
        for (byte b : post.getBytes(StandardCharsets.UTF_8)) body[idx++] = b;

        String upBase = orDefault(Env.get("STOAT_AUTUMN_BASE"), "https://autumn.stoat.chat");
        HttpResponse<String> resp = Providers.HTTP.send(HttpRequest.newBuilder(
                        URI.create(upBase + "/attachments"))
                .timeout(Duration.ofSeconds(30))
                .header("X-Bot-Token", token)
                .header("Content-Type", "multipart/form-data; boundary=" + boundary)
                .POST(HttpRequest.BodyPublishers.ofByteArray(body)).build(),
                HttpResponse.BodyHandlers.ofString());
        if (resp.statusCode() >= 300) throw new RuntimeException("upload failed: HTTP " + resp.statusCode() + " " + resp.body());
        return String.valueOf(Json.parse(resp.body()).get("id"));
    }

    List<String> downloadAttachments(Map<String, Object> ev) {
        var saved = new ArrayList<String>();
        Object attachments = ev.get("attachments");
        if (!(attachments instanceof List<?> atts) || atts.isEmpty()) return saved;
        try {
            Files.createDirectories(Tools.ATTACHMENTS_DIR);
            for (Object o : atts) {
                if (!(o instanceof Map<?, ?> a)) continue;
                String url = String.valueOf(a.get("url"));
                String filename = String.valueOf(a.get("filename"));
                if (url.isBlank() || "null".equals(url) || filename.isBlank()) continue;
                String safe = NOT_FILENAME.matcher(filename).replaceAll("_");
                Path dest = Tools.ATTACHMENTS_DIR.resolve(ev.get("id") + "_" + safe);
                try {
                    HttpResponse<byte[]> r = Providers.HTTP.send(HttpRequest.newBuilder(URI.create(url)).build(),
                            HttpResponse.BodyHandlers.ofByteArray());
                    if (r.statusCode() == 200) {
                        Files.write(dest, r.body());
                        saved.add(Tools.WORKDIR.relativize(dest).toString());
                    }
                } catch (Exception ignored) {}
            }
        } catch (Exception ignored) {}
        return saved;
    }

    @Override
    public String toString() { return "StoatBot(" + botId + ")"; }

    // ── Message handler (the big on_message_create block) ────────────────
    void onMessage(Map<String, Object> ev) {
        String chId = String.valueOf(ev.getOrDefault("channel", ev.get("channel_id")));
        String authorId = String.valueOf(ev.getOrDefault("author", ev.get("author_id")));
        if (botId != null && (botId.equals(authorId))) return;
        String content = extractText(ev.get("content"));
        String cid = chId;

        Set<String> allowed = new HashSet<>();
        for (String x : Env.get("STOAT_ALLOWED_CHANNEL_IDS").split(",")) {
            if (!x.strip().isEmpty()) allowed.add(x.strip());
        }
        if (!allowed.isEmpty() && !allowed.contains(cid)) return;

        // ── pending approval? ────────────────────────
        var fut = pending.get(cid);
        if (fut != null) {
            String lower = content.strip().toLowerCase();
            if (List.of("y", "yes", "n", "no").contains(lower)) {
                pending.remove(cid);
                fut.complete(lower.startsWith("y"));
                return; // consumed
            }
            // fall through otherwise (could still be a normal chat message)
        }

        if (content.isBlank()) return;

        StoatChannel channel = new StoatChannel(cid);

        // !command shortcut — bypasses the LLM, runs shell directly
        if (content.startsWith("!") && content.length() > 1) {
            String command = content.substring(1).strip();
            try {
                if (Tools.isDestructive(command) && !channel.requestApproval(command, 60)) return;
                var result = Tools.runShell(command);
                String stdout = Json.str(result, "stdout");
                stdout = stdout == null || stdout.strip().isEmpty() ? "(no output)" : stdout.strip();
                if (stdout.length() > 1800) stdout = stdout.substring(stdout.length() - 1800);
                String exit = String.valueOf(result.getOrDefault("exit_code", "?"));
                sendChannelMessage(cid, "```\n" + stdout + "\n```\nexit: `" + exit + "`");
            } catch (Exception e) {
                try { sendChannelMessage(cid, "⚠️ " + e); } catch (Exception ignored) {}
            }
            return;
        }

        // /reset
        if (content.strip().toLowerCase().equals("/reset")) {
            Sessions.reset(Sessions.makeKey("stoat", cid));
            try { sendChannelMessage(cid, "🧹 Context cleared. Long-term memory untouched."); } catch (Exception ignored) {}
            return;
        }

        // Wake / mention / everything
        String mode = Env.get("STOAT_BOT_MODE", "WAKEWORD").toUpperCase();
        String wakeWord = Env.get("STOAT_WAKE_WORD", "hey claw").toLowerCase();
        boolean mentioned = botId != null && (content.contains("<@" + botId + ">"));
        String lower = content.toLowerCase().strip();
        boolean should = switch (mode) {
            case "EVERYTHING" -> true;
            case "MENTION" -> mentioned;
            case "WAKEWORD" -> mentioned || lower.startsWith(wakeWord);
            default -> false;
        };
        if (!should) return;

        String prompt = content;
        if (botId != null) prompt = prompt.replace("<@" + botId + ">", "").strip();
        if (prompt.toLowerCase().startsWith(wakeWord)) prompt = prompt.substring(wakeWord.length()).strip();
        if (prompt.isBlank()) prompt = content;

        // sender tag: fetch the author's name (fall back to the raw id)
        String name = authorId;
        try {
            Map<String, Object> user = requestJson("GET", "/users/" + authorId, null);
            String username = Json.str(user, "username");
            String display = Json.str(user, "display_name");
            if (display != null || username != null) name = orDefault(display, username);
        } catch (Exception ignored) {}
        addTyping(cid);
        prompt = ChatChannel.senderTag(name, usernameOrId(name, authorId), authorId) + " " + prompt;

        List<String> saved = downloadAttachments(ev);
        if (!saved.isEmpty()) prompt += "\n\n[Attached files saved to workspace: " + String.join(", ", saved) + "]";

        var session = Sessions.begin(Sessions.makeKey("stoat", cid));
        List<Map<String, Object>> history;
        try {
            history = agent.runTurn(channel, prompt, session.history,
                    Map.of("id", authorId, "name", name));
        } catch (Exception e) {
            System.out.println("[stoat] agent error: " + e.getClass().getSimpleName() + ": " + e.getMessage());
            String err = "⚠️ Error: `" + e.getClass().getSimpleName() + ": "
                    + (e.getMessage() == null ? "" : e.getMessage()) + "`";
            Agent.safeSend(channel, err.length() > 1900 ? err.substring(0, 1900) : err);
            return;
        }
        history = DiscordBot.trimHistory(history);
        session.commit(history, "");
    }

    static String usernameOrId(String name, String id) {
        return name.equals(id) ? null : name;
    }

    void addTyping(String chId) {
        try { request("POST", "/channels/" + chId + "/typing", "{}"); } catch (Exception ignored) {}
    }

    // ── Channel wrapper (chat_platform.StoatChannel) ─────────────────────
    final class StoatChannel implements ChatChannel {
        final String channelId;
        StoatChannel(String cid) { this.channelId = cid; }

        @Override public String platform() { return "stoat"; }
        @Override public String id() { return channelId; }
        @Override public String name() { return "stoat:" + channelId; }

        @Override
        public void send(String content) throws Exception {
            for (int i = 0; i < content.length(); i += 1900) {
                sendChannelMessage(channelId, content.substring(i, Math.min(content.length(), i + 1900)));
            }
        }

        @Override
        public void sendFile(String path, String caption) throws Exception {
            Path full = Tools.resolve(path);
            if (!Files.isRegularFile(full)) throw new IllegalArgumentException("no such file: " + full);
            if (Files.size(full) > Tools.MAX_UPLOAD_BYTES)
                throw new IllegalArgumentException("file is " + Files.size(full) + " bytes, over the "
                        + Tools.MAX_UPLOAD_BYTES + "-byte limit");
            String uploadId = uploadAttachment(full);
            sendWithAttachment(channelId, caption == null ? "" : caption, uploadId,
                    full.getFileName().toString());
        }

        @Override
        public List<Map<String, Object>> history(int limit) {
            try {
                Map<String, Object> me = requestJson("GET",
                        "/channels/" + channelId + "/messages?limit=" + Math.max(1, Math.min(limit, 100)), null);
                var out = new ArrayList<Map<String, Object>>();
                Object data = me.getOrDefault("messages", me); // old API returned an array
                if (data instanceof List<?> list) {
                    var msgs = new ArrayList<Map<String, Object>>();
                    for (Object o : list) if (o instanceof Map<?, ?> m) msgs.add((Map<String, Object>) m);
                    Collections.reverse(msgs);   // newest first -> oldest first
                    for (var m : msgs) {
                        Map<String, Object> e = Json.map();
                        e.put("id", m.get("id"));
                        e.put("author", String.valueOf(m.getOrDefault("author", "?")));
                        e.put("content", extractText(m.get("content")));
                        e.put("timestamp", "");
                        out.add(e);
                    }
                }
                return out;
            } catch (Exception e) {
                return List.of();
            }
        }

        @Override public void setTyping(boolean on) { if (on) addTyping(channelId); }

        @Override
        public Boolean askApproval(String command, int timeoutSec) {
            if ("approve_all".equals(Tools.approvalMode())) return true;
            if ("deny_all".equals(Tools.approvalMode())) return false;
            try {
                sendChannelMessage(channelId, "⚠️ **Approval required** — reply y/n to approve:\n```\n"
                        + (command.length() > 1000 ? command.substring(0, 1000) : command) + "\n```");
            } catch (Exception e) { return false; }
            CompletableFuture<Boolean> f = new CompletableFuture<>();
            pending.put(channelId, f);
            try {
                Boolean ok = f.get(timeoutSec, TimeUnit.SECONDS);
                sendChannelMessage(channelId, ok ? "✅ **Approved**" : "❌ **Denied**");
                return ok;
            } catch (Exception e) {
                pending.remove(channelId);
                try { sendChannelMessage(channelId, "⏱️ Timed out — command cancelled."); } catch (Exception ignored) {}
                return false;
            }
        }
    }
}
