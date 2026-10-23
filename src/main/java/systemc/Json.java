package systemc;

import com.fasterxml.jackson.databind.*;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.annotation.JsonInclude;

import java.util.*;

/**
 * JSON helpers + the common agent message format (providers.py's contract):
 *
 *   user:      {role: "user", content: "..."}
 *   assistant: {role: "assistant", content: "text?"  , tool_calls: [{id, name, input}]}
 *   tool:      {role: "tool", tool_call_id, name, content: "json string"}
 */
public final class Json {
    public static final ObjectMapper M = new ObjectMapper()
            .setSerializationInclusion(JsonInclude.Include.NON_NULL)
            .enable(SerializationFeature.INDENT_OUTPUT);
    public static final ObjectMapper COMPACT = new ObjectMapper()
            .setSerializationInclusion(JsonInclude.Include.NON_NULL);

    private static final TypeReference<Map<String, Object>> MB = new TypeReference<>() {};
    private static final TypeReference<List<Map<String, Object>>> LMB = new TypeReference<>() {};

    private Json() {}

    public static Map<String, Object> map() { return new LinkedHashMap<>(); }

    public static Map<String, Object> parse(String s) {
        try { return M.readValue(s, MB); } catch (Exception e) { return null; }
    }

    public static List<Map<String, Object>> parseList(String s) {
        try { return M.readValue(s, LMB); } catch (Exception e) { return List.of(); }
    }

    /** Serialize a tool result to a JSON string for the model (agent._fmt). */
    public static String fmt(Object result) {
        try {
            if (result instanceof Map || result instanceof List) return COMPACT.writeValueAsString(result);
            return String.valueOf(result);
        } catch (Exception e) { return String.valueOf(result); }
    }

    public static String write(Object o, boolean compact) {
        try { return (compact ? COMPACT : M).writeValueAsString(o); }
        catch (Exception e) { return "{}"; }
    }

    // ── message constructors ──────────────────────────────────────────────
    public static Map<String, Object> user(String text) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("role", "user");
        m.put("content", text);
        return m;
    }

    public static Map<String, Object> assistant(String text, List<Map<String, Object>> toolCalls) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("role", "assistant");
        if (text != null && !text.isBlank()) m.put("content", text);
        List<Map<String, Object>> calls = toolCalls == null ? List.of() : toolCalls;
        if (!calls.isEmpty()) m.put("tool_calls", calls);
        return m;
    }

    public static Map<String, Object> toolCall(String id, String name, Map<String, Object> input) {
        Map<String, Object> tc = new LinkedHashMap<>();
        tc.put("id", id);
        tc.put("name", name);
        tc.put("input", input == null ? map() : input);
        return tc;
    }

    public static Map<String, Object> toolResult(String callId, String name, String content) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("role", "tool");
        m.put("tool_call_id", callId);
        m.put("name", name);
        m.put("content", content);
        return m;
    }

    // convenience getters over loosely-typed maps
    public static String str(Map<String, Object> m, String k) {
        Object v = m.get(k);
        return v == null ? null : String.valueOf(v);
    }

    @SuppressWarnings("unchecked")
    public static List<Map<String, Object>> toolCalls(Map<String, Object> msg) {
        Object v = msg.get("tool_calls");
        if (v instanceof List<?> l) {
            List<Map<String, Object>> out = new ArrayList<>();
            for (Object o : l) if (o instanceof Map) out.add((Map<String, Object>) o);
            return out;
        }
        return List.of();
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> obj(Map<String, Object> m, String k) {
        Object v = m.get(k);
        return v instanceof Map ? (Map<String, Object>) v : new LinkedHashMap<>();
    }
}
