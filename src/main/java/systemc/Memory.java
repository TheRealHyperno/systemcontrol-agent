package systemc;

import java.time.LocalDate;
import java.time.LocalTime;
import java.time.format.DateTimeFormatter;
import java.util.List;
import java.util.Map;

/**
 * Port of memory.py — file-based memory in .memory/: MEMORY.md (curated facts via
 * the `remember` tool) + .memory/daily/YYYY-MM-DD.md append-only logs. Today's and
 * yesterday's logs + MEMORY.md are injected into the system prompt every turn.
 */
public final class Memory {
    private static final int MAX_DAILY_CHARS = 4000;
    private static final int MAX_LONG_TERM_CHARS = 4000;
    private static final int MAX_ENTRY_CHARS = 500;

    private Memory() {}

    private static java.nio.file.Path memoryDir() {
        String profile = Env.get("SYSTEMC_PROFILE_ID");
        if (!profile.isBlank() && !profile.equals("default")) {
            var pdir = Env.baseDir().resolve(".memory").resolve("profiles").resolve(profile);
            try { java.nio.file.Files.createDirectories(pdir.resolve("daily")); } catch (Exception ignored) {}
            return pdir;
        }
        var dir = Env.baseDir().resolve(".memory");
        try { java.nio.file.Files.createDirectories(dir.resolve("daily")); } catch (Exception ignored) {}
        return dir;
    }

    private static java.nio.file.Path dailyPath(LocalDate d) {
        return memoryDir().resolve("daily").resolve(d.format(DateTimeFormatter.ISO_LOCAL_DATE) + ".md");
    }

    private static java.nio.file.Path longTermPath() {
        return memoryDir().resolve("MEMORY.md");
    }

    private static String readCapped(java.nio.file.Path p, int cap) {
        if (!java.nio.file.Files.exists(p)) return "";
        try {
            String text = java.nio.file.Files.readString(p).strip();
            if (text.length() > cap) text = "...[truncated, older lines cut]...\n" + text.substring(text.length() - cap);
            return text;
        } catch (Exception e) { return ""; }
    }

    public static String loadContext() {
        LocalDate today = LocalDate.now();
        LocalDate yesterday = today.minusDays(1);
        String longTerm = readCapped(longTermPath(), MAX_LONG_TERM_CHARS);
        String ys = readCapped(dailyPath(yesterday), MAX_DAILY_CHARS);
        String td = readCapped(dailyPath(today), MAX_DAILY_CHARS);
        if (longTerm.isEmpty() && ys.isEmpty() && td.isEmpty()) return "";
        StringBuilder sb = new StringBuilder("## Memory\n")
                .append("Notes carried over from previous sessions/days. Use them for context; ")
                .append("don't recite them back verbatim unless asked.");
        if (!longTerm.isBlank()) sb.append("\n\n### Long-term notes\n").append(longTerm);
        if (!ys.isBlank()) sb.append("\n\n### Yesterday's log (").append(yesterday).append(")\n").append(ys);
        if (!td.isBlank()) sb.append("\n\n### Today's log (").append(today).append(")\n").append(td);
        return sb.toString();
    }

    private static String trim(String text, int limit) {
        if (text == null) return "";
        text = text.replace("\n", " ").strip();
        return text.length() <= limit ? text : text.substring(0, limit) + "…";
    }

    public static void logTurn(String channelLabel, String userText, String assistantText) {
        try {
            String ts = LocalTime.now().format(DateTimeFormatter.ofPattern("HH:mm"));
            StringBuilder sb = new StringBuilder("\n## ").append(ts).append(" — #")
                    .append(channelLabel).append("\n- user: ").append(trim(userText, MAX_ENTRY_CHARS)).append("\n");
            if (assistantText != null) sb.append("- agent: ").append(trim(assistantText, MAX_ENTRY_CHARS)).append("\n");
            java.nio.file.Files.writeString(dailyPath(LocalDate.now()), sb.toString(),
                    java.nio.file.StandardOpenOption.CREATE, java.nio.file.StandardOpenOption.APPEND);
        } catch (Exception ignored) {}
    }

    public static Map<String, Object> remember(String fact) {
        if (fact == null || fact.isBlank()) return Map.of("error", "empty fact — nothing to remember");
        fact = fact.strip();
        String date = LocalDate.now().format(DateTimeFormatter.ISO_LOCAL_DATE);
        try {
            java.nio.file.Files.writeString(longTermPath(), "- [" + date + "] " + fact + "\n",
                    java.nio.file.StandardOpenOption.CREATE, java.nio.file.StandardOpenOption.APPEND);
            return Map.of("remembered", fact);
        } catch (Exception e) {
            return Map.of("error", String.valueOf(e));
        }
    }

    public static final List<Map<String, Object>> TOOL_SCHEMAS = List.of(
        Map.of(
            "name", "remember",
            "description", "Save a short, durable fact to long-term memory (MEMORY.md). Use this "
                + "when the user asks you to remember something, or shares a stable "
                + "preference/fact/decision that should persist across sessions and days "
                + "— not for one-off details that don't matter later.",
            "input_schema", Map.of(
                "type", "object",
                "properties", Map.of("fact", Map.of("type", "string",
                    "description", "The fact to remember, written concisely in plain text.")),
                "required", List.of("fact"))
        )
    );
}
