package systemc.integrations.notes;

import systemc.integrations.Integration;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Built-in example integration "notes": a shared scratchpad file with
 * add / list / clear tools — demonstrates ctx.requestApproval gating and a
 * boolean settings field.
 */
public final class Notes implements Integration {

    @Override public String name() { return "notes"; }

    @Override public String description() {
        return "Shared persistent scratchpad the agent can append to and read.";
    }

    @Override public String prompt() {
        return "The notes integration keeps a small shared scratchpad (.scratchpad-notes.txt). "
                + "Use notes_list to read it, notes_add to append a short line, and notes_clear "
                + "only if the owner approves the prompt.";
    }

    @Override public List<Map<String, Object>> tools() {
        return List.of(
            Map.of("name", "notes_add",
                   "description", "Append one line to the shared scratchpad.",
                   "input_schema", Map.of(
                       "type", "object",
                       "properties", Map.of(
                           "text", Map.of("type", "string", "description", "One short line")),
                       "required", List.of("text"))),
            Map.of("name", "notes_list",
                   "description", "Read the last lines of the shared scratchpad (default 50).",
                   "input_schema", Map.of(
                       "type", "object",
                       "properties", Map.of(
                           "limit", Map.of("type", "integer", "description", "How many lines to show")),
                       "required", List.of())),
            Map.of("name", "notes_clear",
                   "description", "Erase the whole scratchpad (asks the owner for approval).",
                   "input_schema", Map.of("type", "object", "properties", Map.of(), "required", List.of())));
    }

    private Path file() {
        return systemc.Env.baseDir().resolve(".scratchpad-notes.txt");
    }

    @Override public Object handle(Context ctx, String tool, Map<String, Object> args) {
        Path f = file();
        try {
            switch (tool) {
                case "notes_add" -> {
                    String text = String.valueOf(args.getOrDefault("text", "")).strip();
                    if (text.isEmpty()) return Map.of("error", "empty note");
                    Files.writeString(f, text + System.lineSeparator(), StandardCharsets.UTF_8,
                            StandardOpenOption.CREATE, StandardOpenOption.APPEND);
                    var out = new LinkedHashMap<String, Object>();
                    out.put("added", text);
                    out.put("file", f.toString());
                    if (systemc.Env.getBool("NOTES_ANNOUNCE", false) && ctx.send != null) {
                        ctx.send.accept("📝 note kept: " + text);
                    }
                    return out;
                }
                case "notes_list" -> {
                    if (!Files.exists(f)) return Map.of("notes", List.of(), "total", 0);
                    List<String> all = Files.readAllLines(f, StandardCharsets.UTF_8);
                    int lim = args.get("limit") instanceof Number n ? n.intValue() : 50;
                    int limit = Math.max(1, Math.min(lim, 500));
                    int from = Math.max(0, all.size() - limit);
                    var out = new LinkedHashMap<String, Object>();
                    out.put("notes", all.subList(from, all.size()));
                    out.put("total", all.size());
                    return out;
                }
                case "notes_clear" -> {
                    boolean ok = ctx.requestApproval.test("Erase the shared notes scratchpad?", 60);
                    if (!ok) return Map.of("cleared", false, "reason", "denied by owner");
                    boolean existed = Files.exists(f);
                    Files.deleteIfExists(f);
                    return Map.of("cleared", true, "existed", existed);
                }
                default -> { return Map.of("error", "unknown tool: " + tool); }
            }
        } catch (Exception e) {
            return Map.of("error", String.valueOf(e));
        }
    }

    @Override public Map<String, Object> panel() {
        return Map.of(
            "title", "Notes",
            "icon", "📝",
            "fields", List.of(
                Map.of("key", "NOTES_ANNOUNCE",
                       "label", "Announce each note in chat",
                       "type", "bool",
                       "default", "false",
                       "description", "When on, every notes_add also sends a short chat line.")));
    }
}
