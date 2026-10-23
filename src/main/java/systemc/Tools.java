package systemc;

import java.io.*;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import java.util.zip.*;

/**
 * Port of tools.py. Shell execution uses a real PTY on Linux via GNU `script`
 * (gives the same pseudo-terminal behaviour as Python's pty.openpty without the
 * need for native JNI), so colour output and TTY-aware programs behave properly.
 * Destructive commands are detected with the same regex gate; the agent asks for
 * approval before running them.
 */
public final class Tools {
    public static final Path WORKDIR = resolveWorkdir();
    public static final Path ATTACHMENTS_DIR = WORKDIR.resolve("attachments");
    public static final Path APP_DIR = Env.baseDir();
    static final int SHELL_TIMEOUT = 60;
    static final int OUTPUT_CAP = 6000;
    public static final int MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

    private Tools() {}

    static {
        try { Files.createDirectories(WORKDIR); } catch (IOException ignored) {}
        try { Files.createDirectories(ATTACHMENTS_DIR); } catch (IOException ignored) {}
    }

    private static Path resolveWorkdir() {
        String w = Env.get("AGENT_WORKDIR");
        Path base = w.isBlank() ? Env.baseDir().resolve("workspace") : Path.of(w);
        try {
            Files.createDirectories(base);
            return base;
        } catch (IOException e) {
            Path fb = Env.baseDir().resolve("workspace");
            try { Files.createDirectories(fb); } catch (IOException ignored) {}
            return fb;
        }
    }

    // ── Destructive command detection ──────────────────────────────────
    public static final List<String> DEFAULT_APPROVAL_COMMANDS = List.of(
        "rm", "rmdir", "shred", "mkfs", "fdisk", "parted", "wipefs",
        "dd", "shutdown", "reboot", "halt", "poweroff", "sudo", "su",
        "pip", "pip3", "apt", "apt-get", "dpkg", "snap", "flatpak",
        "npm", "yarn", "pnpm", "systemctl", "service", "chown", "chmod",
        "iptables", "ip6tables", "ufw", "nft", "kill", "killall", "pkill",
        "del", "erase", "format", "diskpart", "cipher",
        "remove-item", "clear-disk", "stop-computer", "restart-computer",
        "reg", "taskkill", "net"
    );

    public static List<String> approvalCommands() {
        String raw = Env.get("APPROVAL_COMMANDS");
        if (raw.isBlank()) return DEFAULT_APPROVAL_COMMANDS;
        var out = new ArrayList<String>();
        var seen = new HashSet<String>();
        for (String x : raw.replace(";", ",").split(",")) {
            if (!x.isBlank()) { String s = x.strip().toLowerCase(); if (seen.add(s)) out.add(s); }
        }
        return out.isEmpty() ? DEFAULT_APPROVAL_COMMANDS : out;
    }

    public static String approvalMode() {
        String v = Env.get("APPROVAL_MODE", "ask").strip().toLowerCase().replace(" ", "_").replace("-", "_");
        return switch (v) {
            case "approve_all", "approveall", "approve" -> "approve_all";
            case "deny_all", "denyall", "deny" -> "deny_all";
            default -> "ask";
        };
    }

    public static boolean isDestructive(String command) {
        if (command == null) return false;
        String alt = approvalCommands().stream()
                .map(Pattern::quote).collect(Collectors.joining("|"));
        // token must start a pipeline segment: line start or after |;&`(
        Pattern cmdRe = Pattern.compile("(?:^|[|;&`(]\\s*)(?:" + alt + ")\\b",
                Pattern.CASE_INSENSITIVE | Pattern.MULTILINE);
        Pattern pipeShell = Pattern.compile("\\|\\s*(ba)?sh\\b|\\|\\s*(iex|invoke-expression)\\b",
                Pattern.CASE_INSENSITIVE);
        return cmdRe.matcher(command).find() || pipeShell.matcher(command).find();
    }

    public static boolean showCommandsUsed() {
        return List.of("1", "true", "yes", "on").contains(Env.get("SHOW_COMMANDS_USED", "").toLowerCase());
    }

    public static boolean showToolsUsed() {
        return List.of("1", "true", "yes", "on").contains(Env.get("SHOW_TOOLS_USED", "").toLowerCase());
    }

    // ── PTY-based shell exec ────────────────────────────────────────────
    static String shellBinary() {
        String sh = Env.get("SHELL", "/bin/bash");
        String base = Path.of(sh).getFileName().toString();
        if (List.of("fish", "zsh", "csh", "tcsh").contains(base)) {
            for (String candidate : List.of("/bin/bash", "/bin/sh")) {
                if (new File(candidate).exists()) return candidate;
            }
        }
        return sh;
    }

    public static void main(String[] a) {}

    private static String runAsPrefix() {
        String user = Env.get("AGENT_RUN_AS_USER");
        return user.isBlank() ? "" : (isRoot() ? "runuser -u " + user + " -- " : "sudo -n -u " + user + " -H -- ");
    }

    static boolean isRoot() {
        return "root".equals(System.getProperty("user.name"));
    }

    public static Map<String, Object> runShell(String command) {
        return new File("/usr/bin/script").canExecute() || which("script")
                ? runShellPty(command)
                : runShellPiped(shellBinary() + " -c " + command, List.of(shellBinary(), "-c", command));
    }

    /** tiny PATH lookup so we degrade gracefully if util-linux isn't present */
    static boolean which(String name) {
        if (name == null || name.isBlank()) return false;
        return java.util.Arrays.stream(System.getenv("PATH").split(":"))
                .map(d -> new File(d, name)).anyMatch(File::canExecute);
    }

    /** PTY path: `script -qfec cmd /dev/null` allocates a pty (Python pty.openpty parity). */
    private static Map<String, Object> runShellPty(String command) {
        String inner = shellBinary() + " -c " + shQuote(command);
        List<String> args = new ArrayList<>();
        args.add(new File("/usr/bin/script").canExecute() ? "/usr/bin/script" : "script");
        args.add("-qfec");
        args.add(runAsPrefix().isEmpty() ? inner : runAsPrefix() + inner);
        args.add("/dev/null");
        return runShellPiped(inner, args);
    }

    /** PTY-less path (plain pipes, stderr mixed into stdout), as on Windows in the original. */
    private static Map<String, Object> runShellPiped(String label, List<String> argv) {
        try {
            var pb = new ProcessBuilder(argv).directory(WORKDIR.toFile())
                    .redirectErrorStream(true);
            var env = pb.environment();
            if (Files.exists(WORKDIR)) { env.put("HOME", WORKDIR.toString()); env.put("PWD", WORKDIR.toString()); }
            Process proc = pb.start();
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            Thread pump = Thread.ofVirtual().start(() -> {
                try (var in = proc.getInputStream()) { in.transferTo(buf); } catch (IOException ignored) {}
            });
            boolean done = proc.waitFor(SHELL_TIMEOUT, TimeUnit.SECONDS);
            if (!done) {
                proc.toHandle().descendants().forEach(ProcessHandle::destroyForcibly);
                proc.destroyForcibly();
                Map<String, Object> out = Json.map();
                out.put("stdout", clean(buf.toByteArray()));
                out.put("exit_code", -1);
                out.put("error", "timed out after " + SHELL_TIMEOUT + "s");
                out.put("cwd", WORKDIR.toString());
                return out;
            }
            pump.join();
            Map<String, Object> out = Json.map();
            out.put("stdout", clean(buf.toByteArray()));
            out.put("exit_code", proc.exitValue());
            out.put("cwd", WORKDIR.toString());
            return out;
        } catch (Exception e) {
            Map<String, Object> out = Json.map();
            out.put("stdout", "");
            out.put("exit_code", -1);
            out.put("error", String.valueOf(e));
            out.put("cwd", WORKDIR.toString());
            return out;
        }
    }

    static String shQuote(String s) {
        return "'" + s.replace("'", "'\\''") + "'";
    }

    // ── Elevated shell exec ─────────────────────────────────────────────
    public static boolean elevatedEnabled() {
        String v = Env.get("AGENT_ALLOW_ELEVATED").toLowerCase();
        return switch (v) {
            case "1", "true", "yes", "on" -> true;
            case "0", "false", "no", "off" -> false;
            default -> Env.get("AGENT_RUN_AS_USER").isEmpty();
        };
    }

    public static Map<String, Object> runShellElevated(String command) {
        if (!elevatedEnabled()) {
            return Map.of("stdout", "", "exit_code", -1,
                    "error", "Elevated commands are turned off on this machine (Control Panel > Setup > 'Allow running elevated?').",
                    "cwd", WORKDIR.toString());
        }
        if (!Env.get("AGENT_RUN_AS_USER").isEmpty() && !isRoot()) {
            boolean ok = runShell("sudo -n true").get("exit_code") instanceof Integer i && i == 0;
            if (!ok) {
                return Map.of("stdout", "", "exit_code", -1,
                        "error", "This account has no passwordless sudo rule.",
                        "cwd", WORKDIR.toString());
            }
        }
        // run_shell has the approval gate in the agent; here prefix with sudo
        return runShell(command.startsWith("sudo ") ? command : "sudo " + command);
    }

    // ── output cleaning ─────────────────────────────────────────────────
    static String clean(byte[] raw) {
        String text = new String(raw, StandardCharsets.UTF_8);
        text = text.replaceAll("\u001B\\[[0-9;]*[A-Za-z]", "");
        text = text.replaceAll("\u001B\\][^\u0007]*\u0007", "");
        text = text.replaceAll("\r\n", "\n").replaceAll("\r", "\n");
        if (text.length() > OUTPUT_CAP) {
            int half = OUTPUT_CAP / 2;
            text = text.substring(0, half) + "\n... [truncated] ...\n" + text.substring(text.length() - half);
        }
        return text.strip();
    }

    // ── File tools ──────────────────────────────────────────────────────
    static Path resolve(String path) {
        if (path == null || path.isBlank()) path = ".";
        return Path.of(path).isAbsolute() ? Path.of(path) : WORKDIR.resolve(path);
    }

    public static Map<String, Object> readFile(String path) {
        Path full = resolve(path);
        try {
            String content = Files.readString(full, StandardCharsets.UTF_8);
            boolean truncated = content.length() >= 8000;
            if (truncated) content = content.substring(0, 8000);
            Map<String, Object> out = Json.map();
            out.put("content", content);
            out.put("path", full.toString());
            out.put("truncated", truncated);
            return out;
        } catch (Exception e) {
            return Map.of("error", String.valueOf(e));
        }
    }

    public static Map<String, Object> writeFile(String path, String content) {
        Path full = resolve(path);
        try {
            if (full.getParent() != null) Files.createDirectories(full.getParent());
            Files.writeString(full, content == null ? "" : content);
            return Map.of("written", (content == null ? 0 : content.length()), "path", full.toString());
        } catch (Exception e) {
            return Map.of("error", String.valueOf(e));
        }
    }

    static Long fileSize(Path p) {
        try { return Files.size(p); } catch (IOException e) { return null; }
    }

    public static Map<String, Object> listDir(String path) {
        Path full = resolve(path == null || path.isBlank() ? "." : path);
        try (var stream = Files.list(full)) {
            var entries = new ArrayList<Map<String, Object>>();
            stream.sorted().forEach(p -> {
                Map<String, Object> e = Json.map();
                String name = p.getFileName().toString();
                if (name.startsWith(".")) return;   // hidden entries stay out of the model's way
                e.put("name", name);
                e.put("type", Files.isDirectory(p) ? "dir" : "file");
                e.put("size", Files.isRegularFile(p) ? fileSize(p) : null);
                entries.add(e);
            });
            Map<String, Object> out = Json.map();
            out.put("path", full.toString());
            out.put("entries", entries);
            return out;
        } catch (Exception e) {
            return Map.of("error", String.valueOf(e));
        }
    }

    // ── Python exec tool ────────────────────────────────────────────────
    public static Map<String, Object> runPython(String code) {
        Path tmp = null;
        try {
            tmp = Files.createTempFile(WORKDIR, "sysc_", ".py");
            Files.writeString(tmp, code == null ? "" : code);
            var pb = new ProcessBuilder("python3", tmp.toString()).directory(WORKDIR.toFile())
                    .redirectErrorStream(true);
            pb.environment().put("HOME", WORKDIR.toString());
            pb.environment().put("PWD", WORKDIR.toString());
            Process proc = pb.start();
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            Thread pump = Thread.ofVirtual().start(() -> {
                try (var in = proc.getInputStream()) { in.transferTo(buf); } catch (IOException ignored) {}
            });
            boolean done = proc.waitFor(SHELL_TIMEOUT, TimeUnit.SECONDS);
            if (!done) {
                proc.toHandle().descendants().forEach(ProcessHandle::destroyForcibly);
                proc.destroyForcibly();
                Map<String, Object> out = Json.map();
                out.put("stdout", clean(buf.toByteArray()));
                out.put("exit_code", -1);
                out.put("error", "timed out after " + SHELL_TIMEOUT + "s");
                out.put("cwd", WORKDIR.toString());
                return out;
            }
            pump.join();
            Map<String, Object> out = Json.map();
            out.put("stdout", clean(buf.toByteArray()));
            out.put("exit_code", proc.exitValue());
            out.put("cwd", WORKDIR.toString());
            return out;
        } catch (Exception e) {
            Map<String, Object> out = Json.map();
            out.put("error", String.valueOf(e));
            out.put("exit_code", -1);
            out.put("cwd", WORKDIR.toString());
            return out;
        } finally {
            try { if (tmp != null) Files.deleteIfExists(tmp); } catch (IOException ignored) {}
        }
    }

    // ── Web search (DuckDuckGo HTML endpoint, no API key) ───────────────
    static final HttpClient HTTP = HttpClient.newBuilder().followRedirects(HttpClient.Redirect.NORMAL).build();

    public static Map<String, Object> webSearch(String query, Integer maxResultsIn) {
        int max = Math.max(1, Math.min(maxResultsIn == null ? 5 : maxResultsIn, 10));
        try {
            String body = URLEncoder.encode(query, StandardCharsets.UTF_8);
            HttpResponse<String> resp = HTTP.send(HttpRequest.newBuilder(URI.create("https://html.duckduckgo.com/html/"))
                    .timeout(java.time.Duration.ofSeconds(15))
                    .header("Content-Type", "application/x-www-form-urlencoded")
                    .header("User-Agent", "Mozilla/5.0 (compatible; systemc-java/1.0)")
                    .POST(HttpRequest.BodyPublishers.ofString("q=" + body)).build(),
                    HttpResponse.BodyHandlers.ofString());
            if (resp.statusCode() != 200) return Map.of("error", "search failed: HTTP " + resp.statusCode());

            var results = new ArrayList<Map<String, Object>>();
            Pattern titleRe = Pattern.compile("<a[^>]+class=\"result__a\"[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>",
                    Pattern.DOTALL);
            Pattern snippetRe = Pattern.compile("<a[^>]+class=\"result__snippet\"[^>]*>(.*?)</a>", Pattern.DOTALL);
            var ms = titleRe.matcher(resp.body());
            List<int[]> spans = new ArrayList<>();
            List<String[]> raw = new ArrayList<>();
            while (ms.find()) { spans.add(new int[]{ms.start(), ms.end()}); raw.add(new String[]{ms.group(1), ms.group(2)}); }
            for (int i = 0; i < raw.size() && results.size() < max; i++) {
                String window = resp.body().substring(spans.get(i)[1], i + 1 < spans.size() ? spans.get(i + 1)[0]
                        : Math.min(resp.body().length(), spans.get(i)[1] + 2000));
                var sm = snippetRe.matcher(window);
                String snippet = sm.find() ? sm.group(1) : "";
                String href = raw.get(i)[0];
                if (href.contains("uddg=")) {
                    int a = href.indexOf("uddg=") + 5;
                    int b = href.indexOf('&', a);
                    String uddg = java.net.URLDecoder.decode(b < 0 ? href.substring(a) : href.substring(a, b), StandardCharsets.UTF_8);
                    if (!uddg.isBlank()) href = uddg;
                }
                Map<String, Object> r = Json.map();
                r.put("title", stripTags(raw.get(i)[1]));
                r.put("url", href);
                r.put("snippet", stripTags(snippet));
                results.add(r);
            }
            if (results.isEmpty()) return Map.of("results", List.of(),
                    "note", "no results parsed — DuckDuckGo may have changed its markup");
            return Map.of("results", results);
        } catch (Exception e) {
            return Map.of("error", "search failed: " + e);
        }
    }

    static String stripTags(String s) {
        if (s == null) return "";
        return java.net.URLDecoder.decode(s.replaceAll("<[^>]+>", "").strip(), StandardCharsets.UTF_8);
    }

    // ── tool schemas (built at call time so elevation toggle is honored) ─
    public static List<Map<String, Object>> localToolSchemas() {
        var schemas = new ArrayList<Map<String, Object>>();
        schemas.add(Map.of(
            "name", "run_shell",
            "description", "Run a shell command on the owner's computer (starts in the agent's workspace). "
                + "Returns stdout, exit_code, and cwd as structured fields. "
                + "Destructive commands (rm, sudo, apt install, etc.) will be sent for user approval before running. "
                + "If a command may take >30 seconds, run it in the background by redirecting its output and ending "
                + "with & (linux/macos), e.g. `long_cmd > /tmp/out.log 2>&1 &`, then read the log later. Never "
                + "background a command when you are checking for errors or syntax.",
            "input_schema", Map.of(
                "type", "object",
                "properties", Map.of("command", Map.of("type", "string", "description", "The shell command to run")),
                "required", List.of("command"))
        ));
        if (elevatedEnabled()) {
            schemas.add(Map.of(
                "name", "run_shell_elevated",
                "description", "Run a shell command with sudo (root/elevated privileges). ALWAYS requires user "
                    + "approval before running — use only when a normal run_shell fails due to permissions. "
                    + "Returns stdout, exit_code, and cwd.",
                "input_schema", Map.of(
                    "type", "object",
                    "properties", Map.of("command", Map.of("type", "string", "description", "The shell command to run as root")),
                    "required", List.of("command"))
            ));
        }
        schemas.addAll(List.of(
            Map.of(
                "name", "read_file",
                "description", "Read a text file from the workspace. Returns content and path.",
                "input_schema", Map.of(
                    "type", "object",
                    "properties", Map.of("path", Map.of("type", "string")),
                    "required", List.of("path"))
            ),
            Map.of(
                "name", "write_file",
                "description", "Write text content to a file in the workspace (overwrites if it exists).",
                "input_schema", Map.of(
                    "type", "object",
                    "properties", Map.of(
                        "path", Map.of("type", "string"),
                        "content", Map.of("type", "string")),
                    "required", List.of("path", "content"))
            ),
            Map.of(
                "name", "list_dir",
                "description", "List files and directories in a workspace path. Returns name, type, size.",
                "input_schema", Map.of(
                    "type", "object",
                    "properties", Map.of("path", Map.of("type", "string",
                        "description", "Path relative to workspace, defaults to workspace root")),
                    "required", List.of())
            ),
            Map.of(
                "name", "run_python",
                "description", "Run a Python snippet in the workspace using the system's python3 (the agent "
                    + "itself is written in Java, but python3 is available for scripts). Prefer this over "
                    + "run_shell for anything beyond a couple lines of Python. Returns stdout, exit_code, cwd.",
                "input_schema", Map.of(
                    "type", "object",
                    "properties", Map.of("code", Map.of("type", "string", "description", "The Python source to run")),
                    "required", List.of("code"))
            )
        ));
        return schemas;
    }

    public static final List<Map<String, Object>> WEB_TOOL_SCHEMAS = List.of(
        Map.of(
            "name", "web_search",
            "description", "Search the web and get back a list of {title, url, snippet} results. Use this for "
                + "anything current or outside your training data — news, docs, prices, 'what version is X on now'. "
                + "Doesn't fetch full page content; follow up with run_shell (curl) if you need a page body.",
            "input_schema", Map.of(
                "type", "object",
                "properties", Map.of(
                    "query", Map.of("type", "string", "description", "Search query"),
                    "max_results", Map.of("type", "integer", "description", "How many results to return (1-10, default 5)")),
                "required", List.of("query"))
        )
    );

    public static final List<Map<String, Object>> CHAT_TOOL_SCHEMAS = List.of(
        Map.of(
            "name", "fetch_channel_messages",
            "description", "Fetch recent message history from the current chat channel, including reply context.",
            "input_schema", Map.of(
                "type", "object",
                "properties", Map.of("limit", Map.of("type", "integer", "description", "Number of messages to fetch (1-100, default 20)")),
                "required", List.of())
        ),
        Map.of(
            "name", "send_message",
            "description", "Send a message to the current chat channel. Automatically splits messages over 1900 chars.",
            "input_schema", Map.of(
                "type", "object",
                "properties", Map.of("content", Map.of("type", "string")),
                "required", List.of("content"))
        ),
        Map.of(
            "name", "send_file",
            "description", "Upload a file from the workspace to the current chat channel as an attachment "
                + "(e.g. a chart you generated, a log file, an edited document). 10MB limit. Optionally a caption.",
            "input_schema", Map.of(
                "type", "object",
                "properties", Map.of(
                    "path", Map.of("type", "string", "description", "Path to the file, relative to the workspace"),
                    "caption", Map.of("type", "string", "description", "Optional message to send alongside the file")),
                "required", List.of("path"))
        )
    );

    // ── zip helper for updater/profile transfer ─────────────────────────
    public static void zip(Path srcDir, Path dest) throws IOException {
        try (var z = new ZipOutputStream(Files.newOutputStream(dest));
             var walk = Files.walk(srcDir)) {
            walk.filter(p -> !Files.isDirectory(p)).forEach(p -> {
                try {
                    z.putNextEntry(new ZipEntry(srcDir.relativize(p).toString()));
                    Files.copy(p, z);
                    z.closeEntry();
                } catch (IOException ignored) {}
            });
        }
    }
}
