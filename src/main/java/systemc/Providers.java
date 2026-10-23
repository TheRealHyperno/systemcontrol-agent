package systemc;

import java.net.URI;
import java.net.http.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.locks.ReentrantLock;

/**
 * Port of providers.py — two backends, one interface. step() takes the common
 * message history and tool schemas and returns one assistant message.
 */
public final class Providers {

    public interface Provider {
        String model();
        String systemPrompt();
        void setSystemPrompt(String prompt);
        /** Returns {role:"assistant", content?, tool_calls:[{id,name,input}]}. */
        Map<String, Object> step(List<Map<String, Object>> history,
                                 List<Map<String, Object>> toolSchemas) throws Exception;
    }

    // ── HTTP client ───────────────────────────────────────────────────────
    static final HttpClient HTTP = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(20))
            .build();

    // ── Ollama settings (providers.ollama_settings) ───────────────────────
    public static final String OLLAMA_LOCAL_HOST = "http://localhost:11434";
    public static final String OLLAMA_CLOUD_HOST = "https://ollama.com";

    public static List<String> collectApiKeys() {
        var keys = new ArrayList<String>();
        for (String src : List.of(Env.get("OLLAMA_API_KEY"), Env.get("OLLAMA_API_KEYS"))) {
            for (String k : src.split("[,\\s;]+")) {
                if (!k.isBlank() && !keys.contains(k.strip())) keys.add(k.strip());
            }
        }
        return keys;
    }

    public record OllamaSettings(String mode, String host, String apiKey, List<String> apiKeys) {}

    public static OllamaSettings ollamaSettings() {
        var keys = collectApiKeys();
        String host = Env.get("OLLAMA_HOST").strip().replaceAll("/+$", "");
        String mode = Env.get("OLLAMA_MODE").strip().toLowerCase();
        if (!mode.equals("local") && !mode.equals("cloud")) {
            mode = !keys.isEmpty() && (host.isBlank() || host.contains("ollama.com")) ? "cloud" : "local";
        }
        if (mode.equals("cloud")) {
            if (host.isBlank() || host.contains("localhost") || host.contains("127.0.0.1")) host = OLLAMA_CLOUD_HOST;
        } else {
            host = host.isBlank() ? OLLAMA_LOCAL_HOST : host;
            keys = List.of(); // never send a cloud key to a local server
        }
        return new OllamaSettings(mode, host, keys.isEmpty() ? "" : keys.get(0), keys);
    }

    // ── API key pool (providers.KeyPool) ─────────────────────────────────
    static final Map<Integer, Long> KEY_COOLDOWNS = Map.of(   // ms
            429, 15L * 60_000, 402, 60L * 60_000, 401, 24L * 3600_000, 403, 24L * 3600_000);
    static final Map<Integer, String> KEY_REASONS = Map.of(
            429, "hit its usage limit (429)", 402, "has no usage left for this model (402)",
            401, "was rejected (401)", 403, "was rejected (403)");

    public static final class KeyPool {
        private final List<String> keys;
        private final Map<Integer, long[]> until = new HashMap<>(); // idx -> {retryAt, code}
        private final ReentrantLock lock = new ReentrantLock();
        private final List<String> notices = new ArrayList<>();

        KeyPool(List<String> keys) { this.keys = List.copyOf(keys); }

        static String mask(String key) {
            return key.length() > 4 ? "…" + key.substring(key.length() - 4) : "…";
        }

        public int acquire() {
            long now = System.currentTimeMillis();
            lock.lock();
            try {
                for (int i = 0; i < keys.size(); i++) {
                    long[] u = until.get(i);
                    if (u == null || u[0] <= now) return i;
                }
            } finally { lock.unlock(); }
            return -1;
        }

        public void markOk(int idx) {
            lock.lock();
            try { until.remove(idx); } finally { lock.unlock(); }
        }

        public void markBad(int idx, int code) {
            long now = System.currentTimeMillis();
            lock.lock();
            try {
                long[] cur = until.get(idx);
                if (cur != null && cur[0] > now) return;
                until.put(idx, new long[]{now + KEY_COOLDOWNS.getOrDefault(code, 900_000L), code});
                int nxt = -1;
                for (int j = 0; j < keys.size(); j++) {
                    long[] u = until.get(j);
                    if (j != idx && (u == null || u[0] <= now)) { nxt = j; break; }
                }
                String why = KEY_REASONS.getOrDefault(code, "failed (HTTP " + code + ")");
                String msg = nxt >= 0
                        ? "🔑 API key #" + (idx + 1) + " (" + mask(keys.get(idx)) + ") " + why + " — switching to key #" + (nxt + 1) + " (" + mask(keys.get(nxt)) + ")."
                        : "🔑 API key #" + (idx + 1) + " (" + mask(keys.get(idx)) + ") " + why + ". No other keys available.";
                notices.add(msg);
                System.out.println("[ollama] " + msg);
            } finally { lock.unlock(); }
        }

        public List<String> drainNotices() {
            lock.lock();
            try {
                var out = List.copyOf(notices);
                notices.clear();
                return out;
            } finally { lock.unlock(); }
        }

        public String exhaustedMessage(String host, String model) {
            long now = System.currentTimeMillis();
            lock.lock();
            var state = new HashMap<>(until);
            lock.unlock();
            if (keys.size() == 1) {
                long[] u = state.get(0);
                return explainKeyFailure(u == null ? 0 : (int) u[1], host, model);
            }
            var parts = new ArrayList<String>();
            for (int i = 0; i < keys.size(); i++) {
                long[] u = state.get(i);
                if (u != null) {
                    long mins = Math.max(1, (u[0] - now) / 60_000);
                    parts.add("#" + (i + 1) + " (" + mask(keys.get(i)) + ") HTTP " + u[1] + ", retry in ~" + mins + " min");
                }
            }
            return "All " + keys.size() + " Ollama API keys are unavailable right now: " + String.join("; ", parts)
                    + ". Add credits or another key at https://ollama.com/settings/keys, or pick a model included "
                    + "in your plan ('" + model + "' may not be).";
        }
    }

    static String explainKeyFailure(int code, String host, String model) {
        return switch (code) {
            case 401, 403 -> host + " rejected the API key (HTTP " + code + "). Check your key — keys live at https://ollama.com/settings/keys";
            case 402 -> "'" + model + "' isn't included in your free Ollama Cloud usage (HTTP 402). Pick another model, add another API key, or add credits at https://ollama.com/settings";
            case 429 -> "Ollama Cloud usage limit reached (HTTP 429). Wait a bit, use another model or another API key, or upgrade at https://ollama.com/upgrade";
            default -> "Ollama request failed (HTTP " + code + ")";
        };
    }

    private static volatile KeyPool pool;
    private static final ReentrantLock poolLock = new ReentrantLock();
    private static List<String> poolKeys = List.of();

    public static KeyPool getPool(List<String> keys) {
        poolLock.lock();
        try {
            if (pool == null || !poolKeys.equals(keys)) {
                pool = new KeyPool(keys);
                poolKeys = List.copyOf(keys);
            }
            return pool;
        } finally { poolLock.unlock(); }
    }

    public static List<String> drainKeyNotices() {
        KeyPool p = pool;
        return p == null ? List.of() : p.drainNotices();
    }

    // ── model listing (GET /api/tags) ────────────────────────────────────
    public static List<String> listOllamaModels(String host, String apiKey) throws Exception {
        String h = (host == null || host.isBlank() ? OLLAMA_CLOUD_HOST : host).replaceAll("/+$", "");
        var req = HttpRequest.newBuilder(URI.create(h + "/api/tags"))
                .timeout(Duration.ofSeconds(15))
                .header("Authorization", "Bearer " + apiKey)
                .GET().build();
        HttpResponse<String> resp = HTTP.send(req, HttpResponse.BodyHandlers.ofString());
        if (resp.statusCode() == 401 || resp.statusCode() == 403)
            throw new RuntimeException(h + " rejected the API key (HTTP " + resp.statusCode() + "). Create one at https://ollama.com/settings/keys");
        if (resp.statusCode() != 200) throw new RuntimeException(h + " returned HTTP " + resp.statusCode());
        Map<String, Object> data = Json.parse(resp.body());
        var names = new ArrayList<String>();
        Object models = data.get("models");
        if (models instanceof List<?> l) {
            for (Object o : l) if (o instanceof Map<?, ?> m) {
                String n = String.valueOf(m.get("model") != null ? m.get("model") : m.get("name"));
                if (!n.isBlank() && !"null".equals(n)) names.add(n);
            }
        }
        Collections.sort(names);
        return names;
    }

    static String cloudModelId(String model) {
        for (String suffix : List.of("-cloud", ":cloud")) {
            if (model.endsWith(suffix)) return model.substring(0, model.length() - suffix.length());
        }
        return model;
    }

    static Map<String, Object> toOpenaiToolSchema(Map<String, Object> schema) {
        Map<String, Object> fn = Json.map();
        fn.put("name", schema.get("name"));
        fn.put("description", schema.getOrDefault("description", ""));
        Object params = schema.get("input_schema");
        if (!(params instanceof Map)) params = Map.of("type", "object", "properties", Map.of());
        fn.put("parameters", params);
        Map<String, Object> out = Json.map();
        out.put("type", "function");
        out.put("function", fn);
        return out;
    }

    // ── OllamaProvider ───────────────────────────────────────────────────
    public static final class OllamaProvider implements Provider {
        private final String host;
        private final List<String> keys;
        private final boolean isCloud;
        private final KeyPool keyPool;
        private final String model;
        private volatile String systemPrompt;

        public OllamaProvider(String model, String host, String systemPrompt, List<String> apiKeys) {
            this.host = host;
            this.keys = apiKeys == null ? List.of() : apiKeys;
            this.isCloud = !keys.isEmpty();
            this.keyPool = keys.isEmpty() ? null : getPool(keys);
            this.model = keys.isEmpty() ? model : cloudModelId(model);
            this.systemPrompt = systemPrompt;
        }

        @Override public String model() { return model; }
        @Override public String systemPrompt() { return systemPrompt; }
        @Override public void setSystemPrompt(String s) { this.systemPrompt = s; }

        private List<Map<String, Object>> toNative(List<Map<String, Object>> history) {
            var nativeMsgs = new ArrayList<Map<String, Object>>();
            var sys = Json.map();
            sys.put("role", "system");
            sys.put("content", systemPrompt);
            nativeMsgs.add(sys);
            for (var m : history) {
                String role = String.valueOf(m.get("role"));
                if (role.equals("user")) {
                    var n = Json.map();
                    n.put("role", "user");
                    n.put("content", m.get("content"));
                    nativeMsgs.add(n);
                } else if (role.equals("assistant")) {
                    var n = Json.map();
                    n.put("role", "assistant");
                    n.put("content", m.get("content") == null ? "" : m.get("content"));
                    var calls = Json.toolCalls(m);
                    if (!calls.isEmpty()) {
                        var oas = new ArrayList<Map<String, Object>>();
                        for (var tc : calls) {
                            var f = Json.map();
                            f.put("name", tc.get("name"));
                            f.put("arguments", tc.get("input"));
                            var t = Json.map();
                            t.put("function", f);
                            oas.add(t);
                        }
                        n.put("tool_calls", oas);
                    }
                    nativeMsgs.add(n);
                } else if (role.equals("tool")) {
                    var n = Json.map();
                    n.put("role", "tool");
                    n.put("content", m.get("content"));
                    n.put("name", m.get("name"));
                    nativeMsgs.add(n);
                }
            }
            return nativeMsgs;
        }

        @Override
        public Map<String, Object> step(List<Map<String, Object>> history,
                                        List<Map<String, Object>> toolSchemas) throws Exception {
            var oaTools = new ArrayList<Map<String, Object>>();
            for (var s : toolSchemas) oaTools.add(toOpenaiToolSchema(s));

            Map<String, Object> body = Json.map();
            body.put("model", model);
            body.put("messages", toNative(history));
            body.put("tools", oaTools);
            body.put("stream", false);

            int idx = -1;
            String key = "";
            HttpResponse<String> resp;
            long attempts = 0;
            while (true) {
                if (keyPool != null) {
                    idx = keyPool.acquire();
                    if (idx < 0) throw new RuntimeException(keyPool.exhaustedMessage(host, model));
                    key = keys.get(idx);
                }
                var rb = HttpRequest.newBuilder(URI.create(host.replaceAll("/+$", "") + "/api/chat"))
                        .timeout(Duration.ofSeconds(240))
                        .header("Content-Type", "application/json");
                if (!key.isBlank()) rb.header("Authorization", "Bearer " + key);
                resp = HTTP.send(rb.POST(HttpRequest.BodyPublishers.ofString(Json.COMPACT.writeValueAsString(body)))
                        .build(), HttpResponse.BodyHandlers.ofString());
                if (resp.statusCode() == 200) break;
                if (keyPool != null && KEY_COOLDOWNS.containsKey(resp.statusCode())) {
                    keyPool.markBad(idx, resp.statusCode());
                    continue;
                }
                if (resp.statusCode() == 404)
                    throw new RuntimeException("Model '" + model + "' not found on " + host + ". List valid IDs with: java -cp systemc-java.jar systemc.Providers models");
                throw new RuntimeException("Ollama request failed (HTTP " + resp.statusCode() + "): "
                        + resp.body().substring(0, Math.min(300, resp.body().length())));
            }
            if (keyPool != null) keyPool.markOk(idx);

            Map<String, Object> data = Json.parse(resp.body());
            Map<String, Object> msg = data == null ? Json.map() : Json.obj(data, "message");
            String text = Json.str(msg, "content");
            text = text == null || text.strip().isEmpty() ? "" : text.strip();
            var toolCalls = new ArrayList<Map<String, Object>>();
            Object tcs = msg.get("tool_calls");
            if (tcs instanceof List<?> l) {
                for (Object o : l) {
                    if (!(o instanceof Map<?, ?> tc)) continue;
                    Map<String, Object> fn = Json.obj((Map<String, Object>) tc, "function");
                    String id = "call_" + UUID.randomUUID().toString().replace("-", "").substring(0, 8);
                    toolCalls.add(Json.toolCall(id, String.valueOf(fn.get("name")),
                            fn.get("arguments") instanceof Map<?, ?> a ? (Map<String, Object>) a : Json.map()));
                }
            }
            return Json.assistant(text.isEmpty() ? null : text, toolCalls);
        }
    }

    // ── AnthropicProvider ────────────────────────────────────────────────
    public static final class AnthropicProvider implements Provider {
        private static final String API = "https://api.anthropic.com";
        private final String apiKey;
        private final String model;
        private volatile String systemPrompt;

        public AnthropicProvider(String model, String apiKey, String systemPrompt) {
            this.model = model;
            this.apiKey = apiKey;
            this.systemPrompt = systemPrompt;
        }

        @Override public String model() { return model; }
        @Override public String systemPrompt() { return systemPrompt; }
        @Override public void setSystemPrompt(String s) { this.systemPrompt = s; }

        private List<Map<String, Object>> toNative(List<Map<String, Object>> history) {
            var nativeMsgs = new ArrayList<Map<String, Object>>();
            for (var m : history) {
                String role = String.valueOf(m.get("role"));
                if (role.equals("user")) {
                    var n = Json.map();
                    n.put("role", "user");
                    n.put("content", m.get("content"));
                    nativeMsgs.add(n);
                } else if (role.equals("assistant")) {
                    var blocks = new ArrayList<Map<String, Object>>();
                    String content = m.get("content") == null ? "" : Json.str(m, "content");
                    if (content != null && !content.isBlank()) {
                        var b = Json.map();
                        b.put("type", "text");
                        b.put("text", content);
                        blocks.add(b);
                    }
                    for (var tc : Json.toolCalls(m)) {
                        var b = Json.map();
                        b.put("type", "tool_use");
                        b.put("id", tc.get("id"));
                        b.put("name", tc.get("name"));
                        b.put("input", tc.get("input"));
                        blocks.add(b);
                    }
                    var n = Json.map();
                    n.put("role", "assistant");
                    n.put("content", blocks);
                    nativeMsgs.add(n);
                } else if (role.equals("tool")) {
                    var tr = Json.map();
                    tr.put("type", "tool_result");
                    tr.put("tool_use_id", m.get("tool_call_id"));
                    tr.put("content", m.get("content"));
                    var n = Json.map();
                    n.put("role", "user");
                    n.put("content", List.of(tr));
                    nativeMsgs.add(n);
                }
            }
            return nativeMsgs;
        }

        @Override
        public Map<String, Object> step(List<Map<String, Object>> history,
                                        List<Map<String, Object>> toolSchemas) throws Exception {
            Map<String, Object> body = Json.map();
            body.put("model", model);
            body.put("max_tokens", 1500);
            body.put("system", systemPrompt);
            body.put("tools", toolSchemas);
            body.put("messages", toNative(history));

            HttpResponse<String> resp = HTTP.send(HttpRequest.newBuilder(URI.create(API + "/v1/messages"))
                    .timeout(Duration.ofSeconds(240))
                    .header("x-api-key", apiKey)
                    .header("anthropic-version", "2023-06-01")
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(Json.COMPACT.writeValueAsString(body)))
                    .build(), HttpResponse.BodyHandlers.ofString());
            if (resp.statusCode() != 200)
                throw new RuntimeException("Anthropic request failed (HTTP " + resp.statusCode() + "): "
                        + resp.body().substring(0, Math.min(300, resp.body().length())));
            Map<String, Object> data = Json.parse(resp.body());
            StringBuilder text = new StringBuilder();
            var toolCalls = new ArrayList<Map<String, Object>>();
            Object content = data.get("content");
            if (content instanceof List<?> l) {
                for (Object o : l) {
                    if (!(o instanceof Map<?, ?> b)) continue;
                    Map<String, Object> bm = (Map<String, Object>) b;
                    String type = String.valueOf(bm.get("type"));
                    if ("text".equals(type) && bm.get("text") != null) text.append(String.valueOf(bm.get("text")));
                    else if ("tool_use".equals(type)) {
                        toolCalls.add(Json.toolCall(String.valueOf(bm.get("id")), String.valueOf(bm.get("name")),
                                bm.get("input") instanceof Map<?, ?> a ? (Map<String, Object>) a : Json.map()));
                    }
                }
            }
            String t = text.toString().strip();
            return Json.assistant(t.isEmpty() ? null : t, toolCalls);
        }
    }

    // ── buildProvider ────────────────────────────────────────────────────
    public static Provider buildProvider(String systemPrompt, String modelOverride) {
        String kind = Env.get("AGENT_PROVIDER", "ollama").toLowerCase();
        if (kind.equals("anthropic")) {
            return new AnthropicProvider(
                    modelOverride == null || modelOverride.isBlank() ? Env.get("AGENT_MODEL", "claude-sonnet-4-6") : modelOverride,
                    Env.get("ANTHROPIC_API_KEY"),
                    systemPrompt);
        }
        var cfg = ollamaSettings();
        if (cfg.mode().equals("cloud") && cfg.apiKeys().isEmpty())
            throw new RuntimeException("Ollama Cloud mode needs OLLAMA_API_KEY (or OLLAMA_API_KEYS) "
                    + "(create one at https://ollama.com/settings/keys)");
        return new OllamaProvider(
                modelOverride == null || modelOverride.isBlank() ? Env.get("AGENT_MODEL", "llama3.2") : modelOverride,
                cfg.host(), systemPrompt, cfg.apiKeys());
    }

    /** java -cp systemc-java.jar systemc.Providers models */
    public static void main(String[] args) throws Exception {
        Env.load(Env.baseDir().resolve(".env"));
        var cfg = ollamaSettings();
        System.out.println("[" + cfg.mode() + "] " + cfg.host());
        RuntimeException last = null;
        for (String key : cfg.apiKeys().isEmpty() ? List.of("") : cfg.apiKeys()) {
            try {
                for (String name : listOllamaModels(cfg.host(), key)) System.out.println("  " + name);
                last = null;
                break;
            } catch (RuntimeException e) { last = e; }
        }
        if (last != null) { System.out.println("Error: " + last.getMessage()); System.exit(1); }
    }
}
