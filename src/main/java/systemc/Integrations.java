package systemc;

import java.io.IOException;
import java.net.URL;
import java.net.URLClassLoader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.io.InputStream;
import java.util.*;
import java.util.concurrent.TimeUnit;

/**
 * <h2>The Java integrations engine</h2>
 *
 * <p>Simultaneous sources of extra agent tools (discovery order):</p>
 * <ol>
 *   <li><b>Java (SPI)</b> — implementations of {@link systemc.integrations.Integration} found
 *       via ServiceLoader: built-ins in the main jar + third-party JARs dropped into
 *       {@code <baseDir>/integrations/}.</li>
 *   <li><b>Python-compat mode</b> (deliberately simple backwards compatibility):
 *       {@code integrations/*.py} files following the python port's module contract,
 *       bridged through a helper {@code python3} process per call. Off/On/Auto via
 *       {@code PY_COMPAT} (default auto: only used when .py files are present).</li>
 * </ol>
 *
 * <p><b>Enabling</b> mirrors the python port: {@code INTEGRATIONS=} in .env, comma or
 * semicolon separated names, or {@code all}. Unknown names are kept and warned.</p>
 *
 * <p><b>Settings menus</b> come from the optional {@code panel()} descriptor (same
 * shape as the python PANEL dict) and are rendered in the Swing panel's
 * <i>Integrations</i> tab, the TUI's Integrations menu, and the web API's
 * {@code /api/v1/integrations*}. Values are stored in .env using each field's key
 * (which must start with the integration's uppercased name).</p>
 */
public final class Integrations {

    // ── an entry = one integration ───────────────────────────────────────────

    public static final class Entry implements systemc.integrations.Integration {
        public final String name;
        public final String description;
        public final String source;          // "java" | "python"
        public final String origin;          // jar name or .py path
        public final String error;           // non-null = unusable
        private final systemc.integrations.Integration impl;
        private final Map<String, Object> manifest;    // python manifest
        public final List<Map<String, Object>> tools = new ArrayList<>();

        Entry(systemc.integrations.Integration impl, String origin, String error) {
            this.impl = impl;
            this.error = error;
            this.origin = origin;
            this.source = "java";
            String n = impl == null ? null : impl.name();
            this.name = impl == null ? String.valueOf(origin) : (n == null || n.isBlank() ? "?" : n);
            this.description = impl == null ? "" : String.valueOf(impl.description());
            this.manifest = null;
        }

        Entry(String origin, String name, String description, Map<String, Object> manifest) {
            this.impl = null;
            this.error = null;
            this.origin = origin;
            this.source = "python";
            this.name = (name == null || name.isBlank()) ? sourceName(origin) : name;
            this.description = description == null ? "" : description;
            this.manifest = manifest;
        }

        @Override public String name() { return name; }
        @Override public String description() { return description; }
        @Override public List<Map<String, Object>> tools() { return tools; }

        @Override public String prompt() {
            if (impl != null) return impl.prompt() == null ? "" : impl.prompt();
            Object p = manifest == null ? null : manifest.get("PROMPT");
            return p == null ? "" : String.valueOf(p);
        }

        @Override public boolean available(systemc.integrations.Integration.Context ctx) {
            if (impl != null) {
                try { return impl.available(ctx); } catch (Exception e) { return false; }
            }
            return true;    // python tools are always offered once the module loads
        }

        @Override public Object handle(systemc.integrations.Integration.Context ctx, String tname, Map<String, Object> args) throws Exception {
            if (impl != null) return impl.handle(ctx, tname, args);
            return PyBridge.call(origin, tname, args, ctx);
        }

        @SuppressWarnings("unchecked")
        @Override public Map<String, Object> panel() {
            if (impl != null) {
                try { return impl.panel(); } catch (Exception e) { return null; }
            }
            Object p = manifest == null ? null : manifest.get("PANEL");
            return p instanceof Map<?, ?> m ? (Map<String, Object>) m : null;
        }

        public boolean ok() { return error == null && !tools.isEmpty(); }

        public String summary() {
            return (error == null ? "" : "FAILED: " + error + " · ")
                    + source + " · " + (tools.isEmpty() ? "no tools" : tools.size() + " tool" + (tools.size() == 1 ? "" : "s"))
                    + (origin == null || origin.isBlank() ? "" : " · " + source(origin));
        }

        private static String sourceName(String origin) {
            String s = String.valueOf(origin);
            int i = s.lastIndexOf('/');
            return i < 0 ? s : s.substring(i + 1);
        }
    }

    // ── discovery ────────────────────────────────────────────────────────────

    private static volatile Map<String, Entry> cache;

    /** Where integrations live: {@code <baseDir>/integrations/}. */
    public static Path dir() { return Env.baseDir().resolve("integrations"); }

    /** Discovers java (SPI, jar + built-ins) and python-compat integrations. */
    @SuppressWarnings("unchecked")
    public static synchronized Map<String, Entry> discover(boolean reload) {
        if (cache != null && !reload) return cache;
        Map<String, Entry> out = new LinkedHashMap<>();

        try { Files.createDirectories(dir()); } catch (IOException ignored) {}

        // 1) java SPI: built-ins + jars in integrations/
        List<URL> urls = new ArrayList<>();
        try {
            try (var st = Files.list(dir())) {
                for (Path p : st.filter(f -> f.toString().endsWith(".jar")).sorted().toList())
                    urls.add(p.toUri().toURL());
            }
        } catch (IOException ignored) {}
        ClassLoader cl = urls.isEmpty() ? systemc.integrations.Integration.class.getClassLoader()
                : new URLClassLoader(urls.toArray(URL[]::new), systemc.integrations.Integration.class.getClassLoader());
        for (systemc.integrations.Integration i : ServiceLoader.load(systemc.integrations.Integration.class, cl)) {
            String fqcn = i == null ? "?" : i.getClass().getName();
            try {
                i.init();
            } catch (Exception ex) {
                out.put(UUID.randomUUID().toString(), new Entry(null, fqcn,
                        "init() failed: " + ex.getClass().getSimpleName() + ": " + ex.getMessage()));
                continue;
            }
            try {
                var e = new Entry(i, fqcn, null);
                e.tools.addAll(i.tools() == null ? List.of() : i.tools());
                if (e.name.isBlank() || e.name.equals("?") || out.containsKey(e.name))
                    out.put(UUID.randomUUID().toString(), new Entry(null, fqcn,
                            "skipped: blank or duplicate name '" + e.name + "'"));
                else out.put(e.name, e);
            } catch (Exception ex) {
                out.put(UUID.randomUUID().toString(), new Entry(null, fqcn,
                        "tools() failed: " + ex.getClass().getSimpleName() + ": " + ex.getMessage()));
            }
        }

        // 2) python-compat (switch: PY_COMPAT on|auto|off; auto = only when .py exist)
        String compat = Env.get("PY_COMPAT", "auto").toLowerCase(Locale.ROOT);
        boolean wants = switch (compat) {
            case "off", "0", "no", "false" -> false;
            case "on", "1", "true", "yes" -> true;
            default -> true;
        };
        if (wants) {
            try (var st = Files.list(dir())) {
                for (Path py : st.filter(f -> f.getFileName().toString().endsWith(".py")
                        && !f.getFileName().toString().startsWith("_")).sorted().toList()) {
                    try {
                        Map<String, Object> m = PyBridge.manifest(py.toAbsolutePath().toString());
                        var e = new Entry(py.toAbsolutePath().toString(),
                                m.get("NAME") == null ? null : String.valueOf(m.get("NAME")),
                                m.get("DESCRIPTION") == null ? "" : String.valueOf(m.get("DESCRIPTION")), m);
                        if (m.get("TOOLS") instanceof List<?> tl) {
                            for (Object o : tl) {
                                if (!(o instanceof Map<?, ?> tm)) continue;
                                Object tn = tm.get("name");
                                if (tn == null || String.valueOf(tn).isBlank()) continue;
                                LinkedHashMap<String, Object> sch = new LinkedHashMap<>();
                                sch.put("name", String.valueOf(tn));
                                sch.put("description", tm.get("description") == null ? "" : String.valueOf(tm.get("description")));
                                if (tm.get("input_schema") instanceof Map) sch.put("input_schema", tm.get("input_schema"));
                                e.tools.add(sch);
                            }
                        }
                        if (out.containsKey(e.name))                       // java wins collisions
                            out.put(UUID.randomUUID().toString(), new Entry(null, origin(e),
                                    "skipped: name collides with an existing integration (" + e.name + ")"));
                        else out.put(e.name, e);
                    } catch (Exception x) {
                        out.put(UUID.randomUUID().toString(),
                                new Entry(null, py.getFileName().toString(),
                                        "failed to load: " + firstLine(x.getMessage())));
                        continue;
                    }
                }
            } catch (IOException ignored) {}
        }
        cache = out;
        return out;
    }

    private static String origin(Entry e) { return e == null ? "" : e.origin; }
    private static String source(String o) { return String.valueOf(o); }
    private static String firstLine(String s) {
        if (s == null) return "error";
        int i = s.indexOf('\n');
        return (i > 0 ? s.substring(0, i) : s).strip();
    }

    // ── enabling (INTEGRATIONS= in .env) ────────────────────────────────────

    /** Enabled names: INTEGRATIONS split on ',' and ';', "all" expands to everything. */
    public static List<String> enabledNames(Map<String, Entry> discovered) {
        String raw = Env.get("INTEGRATIONS", "");
        if (raw.isBlank()) return List.of();
        var names = new ArrayList<String>();
        for (String n : raw.split("[,;]")) {
            String t = n.strip();
            if (t.isEmpty()) continue;
            if (t.equalsIgnoreCase("all")) names.addAll(discovered.keySet());
            else names.add(t);
        }
        return names.stream().distinct().toList();
    }

    public static boolean isEnabled(String name) {
        return enabledNames(discover(false)).contains(name);
    }

    /** Replaces the .env INTEGRATIONS list with an explicit list. */
    public static void setEnabledNames(List<String> names) {
        Env.set("INTEGRATIONS", String.join(",", new LinkedHashSet<>(names)));
    }

    public static void toggleEnabled(String name) {
        var cur = new ArrayList<>(enabledNames(discover(false)));
        if (cur.contains(name)) cur.remove(name); else cur.add(name);
        setEnabledNames(cur);
    }

    // ── settings menus ──────────────────────────────────────────────────────

    public record Field(String key, String label, String type, String def,
                        List<String> choices, String description) {}

    /** Flattened field list across the panel's main page and submenus. */
    @SuppressWarnings("unchecked")
    public static List<Field> fieldsOf(Entry e) {
        var out = new ArrayList<Field>();
        Map<String, Object> panel = e.panel();
        if (panel == null) return out;
        addFields(out, panel);
        if (panel.get("submenu") instanceof List<?> subs)
            for (Object o : subs) if (o instanceof Map<?, ?> m) addFields(out, (Map<String, Object>) m);
        return out;
    }

    @SuppressWarnings("unchecked")
    private static void addFields(List<Field> out, Map<String, Object> page) {
        if (page.get("fields") instanceof List<?> fs)
            for (Object f : fs) {
                if (!(f instanceof Map<?, ?> m) || m.get("key") == null) continue;
                Map<String, Object> mm = (Map<String, Object>) m;
                out.add(new Field(String.valueOf(mm.get("key")),
                        String.valueOf(mm.getOrDefault("label", mm.get("key"))),
                        String.valueOf(mm.getOrDefault("type", "text")),
                        mm.get("default") == null ? "" : String.valueOf(mm.get("default")),
                        mm.get("choices") instanceof List<?> c
                                ? c.stream().map(String::valueOf).toList() : null,
                        m.get("description") == null ? "" : String.valueOf(m.get("description"))));
            }
    }

    /** Icon + title for menus (PANEL.icon / PANEL.title, falling back to the name). */
    public static String menuTitle(Entry e) {
        Map<String, Object> panel = e.panel();
        String title = null, icon = "";
        if (panel != null) {
            if (panel.get("title") != null) title = String.valueOf(panel.get("title"));
            if (panel.get("icon") != null) icon = String.valueOf(panel.get("icon")) + " ";
        }
        return icon + (title == null || title.isBlank() ? e.name : title);
    }

    /** Current values (merged with .env / defaults) for a settings page. */
    public static Map<String, String> currentSettings(Entry e) {
        var out = new LinkedHashMap<String, String>();
        for (var f : fieldsOf(e)) out.put(f.key(), Env.get(f.key(), f.def()));
        return out;
    }

    /** Saves a settings page into .env (blank number → 0 to keep env sane). */
    public static void saveSettings(Entry e, Map<String, String> values) {
        for (var f : fieldsOf(e)) {
            String v = values.get(f.key());
            if (v == null) continue;
            Env.set(f.key(), "number".equals(f.type()) && v.isBlank() ? "0" : v);
        }
    }

    // ── per-turn toolset (the "build" of python integrations.build) ─────────

    public static final class Toolset {
        public final List<Map<String, Object>> schemas = new ArrayList<>();
        public final List<String> promptSections = new ArrayList<>();
        private final Map<String, Entry> owner = new HashMap<>();
        public boolean owns(String toolName) { return owner.containsKey(toolName); }
        public Entry owner(String toolName) { return owner.get(toolName); }
    }

    /**
     * Builds the toolset for one chat turn: resolves INTEGRATIONS=, runs
     * available(ctx) per integration, adds non-colliding, well-shaped tools,
     * collects prompts, and logs python-parity warnings.
     */
    @SuppressWarnings("unchecked")
    public static Toolset build(Map<String, String> author, String platform, Collection<String> builtinNames) {
        var ctx = new systemc.integrations.Integration.Context();
        if (author != null) {
            ctx.platform = platform == null ? "" : platform;
            ctx.authorId = author.getOrDefault("id", "");
            ctx.authorName = author.getOrDefault("name", "");
        }
        var ts = new Toolset();
        var discovered = discover(false);
        var taken = new HashSet<String>(builtinNames);
        for (String n : enabledNames(discovered)) {
            Entry e = discovered.get(n);
            if (e == null) {
                System.out.println("[integrations] WARNING: '" + n + "' is in INTEGRATIONS but no such integration exists");
                continue;
            }
            if (e.error != null) {
                System.out.println("[integrations] failed to load " + n + ": " + e.error);
                continue;
            }
            if (!e.available(ctx)) continue;
            int added = 0;
            for (var s : e.tools) {
                Object tn = s.get("name"), d = s.get("description"), schema = s.get("input_schema");
                if (!(tn instanceof String t) || t.isBlank() || !taken.add(t)
                        || !(d instanceof String) || !(schema instanceof Map))
                    continue;
                ts.schemas.add(s);
                ts.owner.put(t, e);
                added++;
            }
            if (added > 0) {
                String p = e.prompt();
                if (p != null && !p.isBlank())
                    ts.promptSections.add("## Integration: " + e.name + "\n" + p);
            }
        }
        return ts;
    }

    /** Startup banner like python main.py's [integrations] lines. */
    public static List<String> summary() {
        var lines = new ArrayList<String>();
        var discovered = discover(false);
        var enabled = enabledNames(discovered);
        if (enabled.isEmpty() && discovered.isEmpty()) { lines.add("[integrations] none"); return lines; }
        for (String n : enabled) {
            Entry e = discovered.get(n);
            lines.add("[integrations] enabled: " + (e == null ? n + " (missing)" : n + " — " + e.summary()));
        }
        for (String n : discovered.keySet())
            if (!enabled.contains(n))
                lines.add("[integrations] available: " + n + " — " + discovered.get(n).summary());
        return lines;
    }

    public static void main(String[] args) {
        for (String l : summary()) System.out.println(l);
    }

    // ── python-compat bridge ────────────────────────────────────────────────

    /**
     * Deliberately simple bridge ("doesn't have to be good"): a small python
     * script (resource {@code /systemc/python_bridge.py}) imports the user's module
     * and talks JSON over stdout. Modules must follow the python module contract:
     * NAME, DESCRIPTION, TOOLS (list of dicts with name/description/input_schema),
     * optional PROMPT, PANEL — and {@code handle(ctx, tool, args)}.
     */
    static final class PyBridge {
        private static Path script;          // extracted once per BaseDir

        private static Path script() {
            if (script == null) {
                try (var in = Integrations.class.getResourceAsStream("/systemc/python_bridge.py")) {
                    if (in == null) throw new IllegalStateException("bridge script missing from jar");
                    Path p = Env.baseDir().resolve(".systemc_bridge.py");
                    Files.copy(in, p, StandardCopyOption.REPLACE_EXISTING);
                    script = p;
                } catch (IOException e) {
                    throw new IllegalStateException("couldn't write bridge script", e);
                }
            }
            return script;
        }

        private static ProcessBuilder pb(String arg) {
            var b = new ProcessBuilder("python3", "-X", "utf8", script().toString(), arg);
            b.directory(Env.baseDir().toFile());
            b.environment().putIfAbsent("PYTHONUNBUFFERED", "1");
            // python-parity: main.py copies .env into os.environ (setdefault semantics)
            b.environment().putAll(Env.fileEnv());
            return b;
        }

        /** manifest <module.py> -> one JSON object on stdout. */
        static Map<String, Object> manifest(String pyPath) throws Exception {
            var p = pb("manifest");
            p.redirectOutput(ProcessBuilder.Redirect.PIPE);
            var args = p.command();
            args.add(pyPath);
            p.command(args);
            var proc = p.start();
            byte[] out;
            String err;
            try (var os = proc.getOutputStream()) {}    // no stdin needed
            try (InputStream is = proc.getInputStream()) { out = is.readAllBytes(); }
            err = new String(proc.getErrorStream().readAllBytes(), StandardCharsets.UTF_8);
            boolean done = proc.waitFor(45, TimeUnit.SECONDS);
            if (!done) { proc.destroyForcibly(); throw new IllegalStateException("timed out importing " + source(pyPath)); }
            if (proc.exitValue() != 0)
                throw new IllegalStateException("python error importing " + source(pyPath) + ": " + firstLine(err.strip()));
            Map<String, Object> m = Json.parse(new String(out, StandardCharsets.UTF_8).strip());
            if (m == null) throw new IllegalStateException("no manifest from " + source(pyPath));
            return m;
        }

        static String source(String py) {
            String s = String.valueOf(py);
            int i = s.lastIndexOf('/');
            return i < 0 ? s : s.substring(i + 1);
        }

        /**
         * call <module.py> — the JVM writes {"tool":…,"args":…,"ctx":…} to the
         * process stdin, then streams NDJSON events until {"event":"result"}.
         */
        static Object call(String pyPath, String tool, Map<String, Object> args,
                           systemc.integrations.Integration.Context ctx) throws Exception {
            var p = pb("call");
            p.redirectOutput(ProcessBuilder.Redirect.PIPE);
            var cmdArgs = p.command();
            cmdArgs.add(pyPath);
            p.command(cmdArgs);
            var proc = p.start();

            Thread errThread = new Thread(() -> {
                try (var es = proc.getErrorStream()) { es.readAllBytes(); }
                catch (IOException ignored) {}
            });
            errThread.setDaemon(true);
            errThread.start();

            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("tool", tool);
            payload.put("args", args == null ? Map.of() : args);
            payload.put("ctx", Map.of("platform", ctx.platform, "channel_id", ctx.channelId,
                    "author_id", ctx.authorId, "author_name", ctx.authorName));
            try (var os = proc.getOutputStream()) {
                os.write((Json.fmt(payload) + "\n").getBytes(StandardCharsets.UTF_8));
                os.flush();
            }

            Map<String, Object> result = null;
            long endAt = System.currentTimeMillis() + Env.getInt("INTEGRATION_TIMEOUT", 180) * 1000L;
            try (var in = new java.io.BufferedReader(new java.io.InputStreamReader(proc.getInputStream(), StandardCharsets.UTF_8))) {
                String line;
                while (!java.time.Instant.now().isAfter(java.time.Instant.ofEpochMilli(endAt))
                        && (line = in.readLine()) != null) {
                    Map<String, Object> ev = Json.parse(line.strip());
                    if (ev == null || !(ev.get("event") instanceof String kind)) continue; // ignore outsider noise
                    switch (kind) {
                        case "send" -> ctx.send.accept(String.valueOf(ev.getOrDefault("text", "")));
                        case "send_file" -> ctx.sendFile.accept(String.valueOf(ev.getOrDefault("path", "")),
                                String.valueOf(ev.getOrDefault("caption", "")));
                        case "request_approval" -> ctx.requestApproval.test(
                                String.valueOf(ev.getOrDefault("text", "integration requests approval")),
                                ev.get("timeout") instanceof Number n ? n.intValue() : 60);
                        case "result" -> result = ev;
                    }
                }
            }
            if (result == null) {
                long exitLater = 31;
                boolean exited = proc.waitFor(2, TimeUnit.SECONDS);
                return Map.of("error", exited
                        ? ("integration '" + source(pyPath) + "." + tool + "' ended without a result (python exit " + proc.exitValue() + ")")
                        : ("integration '" + source(pyPath) + "." + tool + "' timed out"));
            }
            Object data = result.get("data");
            return data == null ? Map.of() : data;
        }
    }
}
