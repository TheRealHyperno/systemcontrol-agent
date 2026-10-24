package systemc;

import java.util.*;
import java.util.concurrent.*;
import java.util.stream.Collectors;

/**
 * Port of agent.py — the tool-calling loop. Drives the model through tool calls
 * until it stops, with the approval gate for destructive shell commands and
 * parallel subagents (up to 5, limited toolset, no messaging).
 */
public final class Agent {
    static final int SUB_MAX_TASKS = 5;

    final Providers.Provider provider;
    volatile Providers.Provider subProvider;   // created on first use (honours SUBAGENT_MODEL)

    private static final String SUBAGENT_PROMPT = """
            You are a subagent working for a parent agent on one focused task, directly on
            the owner's computer with full shell and filesystem access.

            You can't talk to the user or ask questions. Make reasonable assumptions, do
            the work with your tools, then finish with a concise final report: findings,
            results, and the paths of any files you created. That report is everything
            the parent will see, so include the specifics it asked for.

            Tools: run_shell, run_python, read_file, write_file, list_dir, web_search.
            - Check exit_code != 0 for errors; chain commands with && not ;
            - Destructive commands need user approval — don't retry if denied
            - WORKDIR is your working directory; use relative paths inside it
            - Anything outside WORKDIR is the owner's real data: don't delete or overwrite it
            - Never claim you can't run something — try it and report the actual result.
            """;

    private static final List<String> SUB_TOOL_NAMES =
            List.of("run_shell", "run_python", "read_file", "write_file", "list_dir");

    public static final List<Map<String, Object>> SUBAGENT_TOOL_SCHEMAS = List.of(
        Map.of(
            "name", "spawn_subagent",
            "description", "Delegate ONE self-contained task to a subagent (a fresh copy of you with its "
                + "own context, shell, python, files and web search). It cannot see this conversation, "
                + "so include all context and say exactly what to report back. Returns its final report.",
            "input_schema", Map.of(
                "type", "object",
                "properties", Map.of(
                    "task", Map.of("type", "string", "description", "Complete, self-contained instructions"),
                    "name", Map.of("type", "string", "description", "Short label, e.g. 'disk-audit'")),
                "required", List.of("task"))
        ),
        Map.of(
            "name", "spawn_subagents",
            "description", "Run up to 5 independent tasks in PARALLEL, one subagent each. Same rules as "
                + "spawn_subagent: every task must be self-contained. Returns all reports.",
            "input_schema", Map.of(
                "type", "object",
                "properties", Map.of(
                    "tasks", Map.of(
                        "type", "array",
                        "items", Map.of(
                            "type", "object",
                            "properties", Map.of(
                                "task", Map.of("type", "string"),
                                "name", Map.of("type", "string")),
                            "required", List.of("task")))),
                "required", List.of("tasks"))
        )
    );

    public Agent() {
        this.provider = Providers.buildProvider(Prompt.buildSystemPrompt(""), null);
    }

    static boolean subagentsEnabled() {
        return !Env.get("SUBAGENTS", "on").toLowerCase().equals("off");
    }

    // ── subagents ────────────────────────────────────────────────────────
    private Map<String, Object> runSubagents(ChatChannel channel, Object tasksIn) {
        List<Map<String, Object>> tasks;
        if (tasksIn instanceof String s) tasks = Json.parseList(s);
        else if (tasksIn instanceof List<?> l) {
            tasks = new ArrayList<>();
            for (Object o : l) if (o instanceof Map<?, ?> m) tasks = addTask(tasks, (Map<String, Object>) m);
        } else tasks = new ArrayList<>();
        tasks = tasks.stream()
                .filter(t -> t.get("task") instanceof String s && !s.strip().isEmpty())
                .limit(SUB_MAX_TASKS)
                .collect(Collectors.toList());
        if (tasks.isEmpty())
            return Map.of("error", "no tasks given — pass tasks=[{\"name\": ..., \"task\": ...}, ...]");

        if (subProvider == null) {
            subProvider = Providers.buildProvider(SUBAGENT_PROMPT, Env.get("SUBAGENT_MODEL", "").isBlank() ? null : Env.get("SUBAGENT_MODEL"));
        }
        var names = new ArrayList<String>();
        for (int i = 0; i < tasks.size(); i++) {
            String n = Json.str(tasks.get(i), "name");
            names.add(n == null || n.isBlank() ? "agent-" + (i + 1) : n);
        }
        try {
            safeSend(channel, "🧩 Spawning " + tasks.size() + " subagent(s): " + String.join(", ", names));
        } catch (Exception ignored) {}

        int subMaxTurns = Env.getInt("SUBAGENT_MAX_TURNS", 12);
        int subMaxParallel = Env.getInt("SUBAGENT_MAX_PARALLEL", 3);
        int subTimeout = Env.getInt("SUBAGENT_TIMEOUT", 600);

        var schemas = new ArrayList<Map<String, Object>>();
        for (var s : Tools.localToolSchemas()) {
            if (SUB_TOOL_NAMES.contains(s.get("name"))) schemas.add(s);
        }
        schemas.addAll(Tools.WEB_TOOL_SCHEMAS);

        Object lock = new Object();   // one approval prompt at a time
        Map<String, Object> results = Json.map();
        var list = new ArrayList<Map<String, Object>>();
        var pool = Executors.newFixedThreadPool(Math.max(1, subMaxParallel));

        try {
            var futures = new ArrayList<Future<Map<String, Object>>>();
            for (int i = 0; i < tasks.size(); i++) {
                final String name = names.get(i);
                final String task = Json.str(tasks.get(i), "task");
                final List<Map<String, Object>> fSchemas = schemas;
                futures.add(pool.submit(() -> runOneSubagent(channel, subProvider, name, task, fSchemas,
                        subMaxTurns, subTimeout, lock)));
            }
            for (var f : futures) {
                try {
                    Map<String, Object> r = f.get(subTimeout, TimeUnit.SECONDS);
                    list.add(r);
                } catch (java.util.concurrent.ExecutionException e) {
                    var r = Json.map();
                    r.put("name", "?");
                    r.put("status", "error");
                    r.put("report", String.valueOf(e.getCause()));
                    list.add(r);
                } catch (TimeoutException e) {
                    var r = Json.map();
                    r.put("name", "?");
                    r.put("status", "timeout");
                    r.put("report", "timed out after " + subTimeout + "s");
                    list.add(r);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }
        } finally {
            pool.shutdownNow();
        }
        results.put("results", list);
        return results;
    }

    @SuppressWarnings("unchecked")
    private static List<Map<String, Object>> addTask(List<Map<String, Object>> acc, Map<String, Object> t) {
        acc.add(t);
        return acc;
    }

    private Map<String, Object> runOneSubagent(ChatChannel channel, Providers.Provider prov, String name, String task,
                                               List<Map<String, Object>> schemas, int maxTurns, int timeoutSec,
                                               Object approvalLock) {
        var history = new ArrayList<Map<String, Object>>();
        history.add(Json.user(task));
        String lastText = "";
        try {
            for (int turn = 0; turn < maxTurns; turn++) {
                var msg = prov.step(history, schemas);
                for (String note : Providers.drainKeyNotices()) safeSend(channel, note);
                history.add(msg);
                String content = msg.get("content") == null ? null : Json.str(msg, "content");
                if (content != null && !content.isBlank()) lastText = content;
                var calls = Json.toolCalls(msg);
                if (calls.isEmpty()) {
                    var out = Json.map();
                    out.put("name", name);
                    out.put("status", "ok");
                    out.put("turns", turn + 1);
                    String report = content == null || content.isBlank() ? "(empty report)" : content;
                    out.put("report", report.length() > 6000 ? report.substring(0, 6000) : report);
                    return out;
                }
                for (var tc : calls) {
                    String tname = Json.str(tc, "name");
                    var args = Json.obj(tc, "input");
                    Map<String, Object> result;
                    try {
                        result = switch (tname) {
                            case "run_shell" -> {
                                String cmd = args.getOrDefault("command", "").toString();
                                if (Tools.isDestructive(cmd)) {
                                    boolean ok;
                                    synchronized (approvalLock) {
                                        ok = channel.requestApproval("[subagent: " + name + "]\n" + cmd, 60);
                                    }
                                    if (!ok) yield Map.of("error", "command was denied by user", "exit_code", -1);
                                }
                                yield Tools.runShell(cmd);
                            }
                            case "run_python" -> Tools.runPython(String.valueOf(args.getOrDefault("code", "")));
                            case "read_file" -> Tools.readFile(args.getOrDefault("path", "").toString());
                            case "write_file" -> Tools.writeFile(String.valueOf(args.get("path")),
                                    String.valueOf(args.getOrDefault("content", "")));
                            case "list_dir" -> Tools.listDir(String.valueOf(args.getOrDefault("path", ".")));
                            case "web_search" -> Tools.webSearch(String.valueOf(args.get("query")),
                                    (Integer) args.get("max_results"));
                            default -> Map.of("error", "unknown tool: " + tname);
                        };
                    } catch (Exception e) {
                        result = Map.of("error", String.valueOf(e), "exit_code", -1);
                    }
                    history.add(Json.toolResult(Json.str(tc, "id"), tname, Json.fmt(result)));
                }
            }
        } catch (Exception e) {
            var out = Json.map();
            out.put("name", name);
            out.put("status", "error");
            out.put("report", e.getClass().getSimpleName() + ": " + e.getMessage());
            return out;
        }
        var out = Json.map();
        out.put("name", name);
        out.put("status", "incomplete");
        out.put("turns", maxTurns);
        String report = lastText.isBlank() ? "hit the turn limit before finishing" : lastText;
        out.put("report", report.length() > 6000 ? report.substring(0, 6000) : report);
        return out;
    }

    // ── reset_session tool ───────────────────────────────────────────────
    private Map<String, Object> resetSessionTool(ChatChannel channel, String currentKey, String ref) {
        String[] res = Sessions.resolve(ref, currentKey);
        if (res[0] == null) {
            var known = Sessions.listSessions().stream().limit(10)
                    .map(s -> s.get("key") + " " + s.get("label")).collect(Collectors.toList());
            Map<String, Object> out = Json.map();
            out.put("error", res[1]);
            out.put("known_sessions", known);
            return out;
        }
        String key = res[0];
        if (!key.equals(currentKey)) {
            Map<String, Object> info = null;
            for (var s : Sessions.listSessions()) if (key.equals(s.get("key"))) info = s;
            if (info == null) return Map.of("error", "no saved session " + key + " — nothing to reset");
            String what = key + (info.get("label") == null || String.valueOf(info.get("label")).isEmpty()
                    ? "" : " (" + info.get("label") + ")");
            boolean approved = channel.requestApproval(
                    "[reset_session] Clear the conversation history of " + what
                    + " (" + info.get("turns") + " turn(s)). Long-term memory is not affected.", 60);
            if (!approved) return Map.of("error", "reset of another session was denied by the owner", "session", key);
        }
        Sessions.Session fresh = Sessions.begin(key);
        Map<String, Object> result = Sessions.reset(key);
        key = fresh.key; // shadow-note: Sessions.reset validates the key again
        if (result.get("ok") instanceof Boolean b && !b)
            return Map.of("error", "couldn't fully clear the session (check file permissions on .history/)", "session", key);
        int cleared = ((Number) result.get("cleared_turns")).intValue();
        Map<String, Object> out = Json.map();
        out.put("reset", true);
        out.put("session", key);
        out.put("cleared_turns", cleared);
        out.put("long_term_memory", "untouched");
        if (key.equals(currentKey)) {
            out.put("note", "This conversation's history is cleared once your current reply is sent; "
                    + "the next message starts from a blank slate.");
        }
        return out;
    }

    static void safeSend(ChatChannel channel, String content) {
        if (channel == null) return;
        try { channel.send(content); } catch (Exception ignored) {}
    }

    private void sendUsageSummary(ChatChannel channel, List<String> commandsUsed) {
        boolean showCmd = Tools.showCommandsUsed(), showTools = Tools.showToolsUsed();
        if (!showCmd && !showTools) return;
        var lines = new ArrayList<String>();
        if (showCmd) {
            var uniq = new ArrayList<String>();
            for (String c : commandsUsed) if (!uniq.contains(c)) uniq.add(c);
            if (!uniq.isEmpty()) {
                lines.add("-# __Commands executed__");
                for (int i = 0; i < Math.min(50, uniq.size()); i++) lines.add("-# - " + uniq.get(i));
            }
        }
        if (!lines.isEmpty()) safeSend(channel, String.join("\n", lines));
    }

    /**
     * agent.run_turn: append the user message, loop the LLM + tool calls, send the
     * final reply. Mutates and returns the (extended) history.
     */
    public List<Map<String, Object>> runTurn(ChatChannel channel, String userText,
                                             List<Map<String, Object>> history,
                                             Map<String, String> author) {
        history.add(Json.user(userText));

        String memContext = Memory.loadContext();
        var baseSchemas = new ArrayList<Map<String, Object>>();
        baseSchemas.addAll(Tools.localToolSchemas());
        baseSchemas.addAll(Tools.CHAT_TOOL_SCHEMAS);
        baseSchemas.addAll(Tools.WEB_TOOL_SCHEMAS);
        baseSchemas.addAll(Memory.TOOL_SCHEMAS);
        baseSchemas.addAll(Sessions.TOOL_SCHEMAS);
        if (subagentsEnabled()) baseSchemas.addAll(SUBAGENT_TOOL_SCHEMAS);
        var allSchemas = new ArrayList<>(baseSchemas);

        // integrations: extra tools + prompt sections (java SPI and python-compat)
        var builtinNames = new ArrayList<String>();
        for (var s : baseSchemas) if (s.get("name") instanceof String n) builtinNames.add(n);
        var toolset = Integrations.build(author, channel == null ? "" : channel.platform(), builtinNames);
        allSchemas.addAll(toolset.schemas);

        provider.setSystemPrompt(String.join("\n\n",
                java.util.stream.Stream.of(Prompt.buildSystemPrompt(provider.model()),
                                String.join("\n\n", toolset.promptSections), memContext)
                        .filter(s -> s != null && !s.isBlank())
                        .collect(Collectors.joining("\n\n"))).trim());

        var commandsUsed = new ArrayList<String>();
        String finalText = null;
        int maxTurns = Env.getInt("AGENT_MAX_TURNS", 20);

        try {
            String currentSession = Sessions.makeKey(channel.platform(), channel.id());
            for (int turn = 0; turn < maxTurns; turn++) {
                var assistantMsg = provider.step(history, allSchemas);
                for (String note : Providers.drainKeyNotices()) safeSend(channel, note);
                history.add(assistantMsg);

                var calls = Json.toolCalls(assistantMsg);
                if (calls.isEmpty()) {
                    String content = assistantMsg.get("content") == null ? null : Json.str(assistantMsg, "content");
                    if (content != null && !content.isBlank()) {
                        finalText = content;
                        sendUsageSummary(channel, commandsUsed);
                        safeSend(channel, content);
                    }
                    break;
                }

                for (var tc : calls) {
                    String name = Json.str(tc, "name");
                    var args = Json.obj(tc, "input");
                    Map<String, Object> result;
                    try {
                        result = switch (name) {
                            case "run_shell" -> {
                                String cmd = args.getOrDefault("command", "").toString();
                                commandsUsed.add(cmd);
                                if (Tools.isDestructive(cmd)) {
                                    boolean approved = channel.requestApproval(cmd, 60);
                                    if (!approved) yield Map.of("error", "command was denied by user", "exit_code", -1);
                                }
                                yield Tools.runShell(cmd);
                            }
                            case "run_shell_elevated" -> {
                                if (!Tools.elevatedEnabled())
                                    yield Map.of("error", "elevated commands are turned off on this machine", "exit_code", -1);
                                String cmd = args.getOrDefault("command", "").toString();
                                boolean approved = channel.requestApproval("[ELEVATED / sudo]\n" + cmd, 90);
                                if (!approved) yield Map.of("error", "elevated command was denied by user", "exit_code", -1);
                                yield Tools.runShellElevated(cmd);
                            }
                            case "run_python" -> Tools.runPython(String.valueOf(args.getOrDefault("code", "")));
                            case "read_file" -> Tools.readFile(String.valueOf(args.getOrDefault("path", "")));
                            case "write_file" -> Tools.writeFile(String.valueOf(args.get("path")),
                                    String.valueOf(args.getOrDefault("content", "")));
                            case "list_dir" -> Tools.listDir(String.valueOf(args.getOrDefault("path", ".")));
                            case "remember" -> Memory.remember(String.valueOf(args.getOrDefault("fact", "")));
                            case "send_message" -> {
                                safeSend(channel, String.valueOf(args.getOrDefault("content", "")));
                                yield Map.of("sent", true);
                            }
                            case "send_file" -> {
                                String p = String.valueOf(args.get("path"));
                                String cap = String.valueOf(args.getOrDefault("caption", ""));
                                channel.sendFile(p, cap == null || "null".equals(cap) ? "" : cap);
                                yield Map.of("sent", true);
                            }
                            case "fetch_channel_messages" -> {
                                int lim = args.get("limit") instanceof Number n ? n.intValue() : 20;
                                List<Map<String, Object>> msgs = channel.history(
                                        Math.max(1, Math.min(lim, 100)));
                                int count = msgs.size();
                                Map<String, Object> r = Json.map();
                                r.put("messages", msgs);
                                r.put("count", count);
                                yield r;
                            }
                            case "web_search" -> Tools.webSearch(String.valueOf(args.get("query")),
                                    args.get("max_results") instanceof Integer i ? i : null);
                            case "reset_session" -> resetSessionTool(channel, currentSession,
                                    args.get("session") == null ? "" : String.valueOf(args.get("session")));
                            case "spawn_subagent" -> {
                                String taskName = String.valueOf(args.getOrDefault("name", ""));
                                Map<String, Object> t = Json.map();
                                t.put("name", taskName);
                                t.put("task", String.valueOf(args.get("task")));
                                yield runSubagents(channel, List.of(t));
                            }
                            case "spawn_subagents" -> runSubagents(channel, args.get("tasks"));
                            default -> {
                                if (toolset.owns(name)) {
                                    var entry = toolset.owner(name);
                                    var ic = new systemc.integrations.Integration.Context();
                                    if (channel != null) {
                                        ic.platform = channel.platform(); ic.channelId = channel.id();
                                        ic.send = m -> safeSend(channel, m);
                                        ic.sendFile = (path, cap) -> {
                                            try { channel.sendFile(path, cap == null || "null".equals(cap) ? "" : cap); }
                                            catch (Exception ignored) {}
                                        };
                                        ic.requestApproval = (text, timeout) -> channel.requestApproval(text, timeout);
                                    }
                                    if (author != null) {
                                        ic.authorId = author.getOrDefault("id", "");
                                        ic.authorName = author.getOrDefault("name", "");
                                    }
                                    Object r = entry.handle(ic, name, args);
                                    Map<String, Object> rr = r instanceof Map<?, ?> m
                                            ? (Map<String, Object>) m
                                            : Map.of("result", r == null ? "" : r);
                                    yield rr;
                                }
                                yield Map.of("error", "unknown tool: " + name);
                            }
                        };
                    } catch (Exception e) {
                        result = Map.of("error", e.getClass().getSimpleName() + ": " + e.getMessage());
                    }
                    String content = Json.fmt(result == null ? Map.of() : result);
                    history.add(Json.toolResult(Json.str(tc, "id"), name,
                            content.length() > 12000 ? content.substring(0, 12000) : content));
                }
            }
        } catch (Exception e) {
            safeSend(channel, ("⚠️ " + e.getClass().getSimpleName() + ": " + e.getMessage()).substring(0,
                    Math.min(1900, ("⚠️ " + e.getClass().getSimpleName() + ": " + e.getMessage()).length())));
        }

        Memory.logTurn(channel.name(), userText, finalText);
        return history;
    }
}
