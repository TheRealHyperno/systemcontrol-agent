package systemc;

import java.io.*;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URL;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.TimeUnit;

/**
 * Community integration (plugin) directory client.
 *
 * Registry index: https://raw.githubusercontent.com/TheRealHyperno/systemcontrol-plugins/main/index.json
 * (override with $PLUGINS_INDEX). Each entry is validated by CI on the repo and
 * carries author (GitHub login), backend tags (java/python/both), status
 * (working/warning/broken/untested) and assets (+ sha256).
 *
 * Usage:
 *   systemc plugins list                      # browse every plugin
 *   systemc plugins list python                # filter by backend tag
 *   systemc plugins install <id>               # installs the jar/py of the
 *   systemc plugins install <id> python       # current backend into integrations/
 */
public final class Plugins {
    public static final String DEFAULT_INDEX =
            "https://raw.githubusercontent.com/TheRealHyperno/systemcontrol-plugins/main/index.json";

    public static void main(String[] args) throws Exception {
        String cmd = (args.length == 0 ? "list" : args[0]).toLowerCase(Locale.ROOT);
        switch (cmd) {
            case "list", "ls" -> list(Arrays.copyOfRange(args, 1, args.length));
            case "install", "i", "get" -> install(args.length < 2 ? null : args[1],
                    args.length < 3 ? null : args[2].toLowerCase(Locale.ROOT));
            default -> {
                System.out.println("usage: systemc plugins [list [filter]] [install <id> [java|python]]");
                System.exit(1);
            }
        }
    }

    private static Map<String, Object> getIndex() throws Exception {
        String idx = System.getenv("PLUGINS_INDEX");
        if (idx == null || idx.isBlank()) idx = DEFAULT_INDEX;
        Map<String, Object> parsed = fetchIndex(idx);
        if (parsed == null || plugins(parsed).isEmpty()) {
            try {
                Map<String, Object> api = fetchIndex("https://api.github.com/repos/TheRealHyperno/systemcontrol-plugins/contents/index.json?ref=main");
                if (api == null) {
                    if (parsed != null) return parsed;
                }
                if (api != null) parsed = (plugins(api).isEmpty() && parsed != null) ? parsed : api;
            } catch (Exception ignored) {
                if (parsed == null) throw ignored;
            }
        }
        return parsed == null ? Map.of() : parsed;
    }

    private static Map<String, Object> fetchIndex(String u) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URI(u).toURL().openConnection();
        c.setConnectTimeout((int) TimeUnit.SECONDS.toMillis(20));
        c.setReadTimeout((int) TimeUnit.SECONDS.toMillis(30));
        c.setRequestProperty("User-Agent", "systemc/" + Updater.VERSION);
        if (c.getResponseCode() != 200) return null;
        String body;
        try (InputStream in = c.getInputStream()) { body = new String(in.readAllBytes()); }
        Map<String, Object> parsed = Json.parse(body);
        if (parsed == null) return null;
        // GitHub contents API wraps the file in base64("content")
        if (parsed.containsKey("content") && parsed.containsKey("encoding")) {
            String enc = String.valueOf(parsed.get("encoding"));
            if (enc.equalsIgnoreCase("base64")) {
                String b64 = String.valueOf(parsed.get("content")).replaceAll("\\s", "");
                String dec = new String(java.util.Base64.getDecoder().decode(b64));
                parsed = Json.parse(dec);
            }
        }
        return parsed == null ? Map.of() : parsed;
    }

    private static List<Map<String, Object>> plugins(Map<String, Object> idx) {
        if (idx == null) return List.of();
        Object v = idx.get("plugins");
        if (!(v instanceof List<?> l)) return List.of();
        var out = new ArrayList<Map<String, Object>>();
        for (Object o : l) if (o instanceof Map<?, ?> m)
            out.add((Map<String, Object>) m);
        return out;
    }

    @SuppressWarnings("unchecked")
    private static void list(String[] args) throws Exception {
        String filter = args.length == 0 ? "" : String.join(" ", args);
        var idx = getIndex();
        var ps = plugins(idx);
        int shown = 0;
        System.out.println("SystemC plugin directory  (api/v" + (idx.getOrDefault("api", "")) + ", " + ps.size() + " listed)");
        System.out.println();
        for (Map<String, Object> p : ps) {
            String id = String.valueOf(p.getOrDefault("id", "?"));
            String name = String.valueOf(p.getOrDefault("name", "?"));
            String status = String.valueOf(p.getOrDefault("status", "untested"));
            String author = String.valueOf(p.getOrDefault("author", "?"));
            List<String> backs = (List<String>) p.getOrDefault("backends", List.of());
            String backStr = backs == null || backs.isEmpty() ? "?" : String.join(",", backs);
            if (!filter.isBlank()) {
                String hay = (id + " " + name + " " + backStr + " " + status + " " + author).toLowerCase(Locale.ROOT);
                if (!hay.contains(filter.toLowerCase(Locale.ROOT))) continue;
            }
            shown++;
            System.out.println("[" + status + "] " + id + "  " + name);
            System.out.println("    author: " + author + " · backends: " + backStr
                    + " · version: " + p.getOrDefault("version", "?"));
            System.out.println("    " + p.getOrDefault("description", ""));
            Object repo = p.get("repo");
            if (repo != null) System.out.println("    " + repo);
            System.out.println();
        }
        System.out.println(shown + " of " + ps.size() + " plugins."
                + (shown < ps.size() ? "  (filtered)" : ""));
    }

    @SuppressWarnings("unchecked")
    private static void install(String id, String backendArg) throws Exception {
        if (id == null || id.isBlank()) {
            System.out.println("usage: systemc plugins install <id> [java|python]");
            return;
        }
        var idx = getIndex();
        Map<String, Object> chosen = null;
        for (var p : plugins(idx))
            if (id.equals(String.valueOf(p.get("id")))) { chosen = p; break; }
        if (chosen == null) {
            System.out.println("no plugin named '" + id + "' — run 'systemc plugins list'");
            return;
        }
        String backend;
        if (backendArg != null && !backendArg.isBlank()) {
            backend = backendArg;
        } else {
            backend = Env.get("BACKEND", "java").trim().toLowerCase(Locale.ROOT);
        }
        if (!backend.equals("java") && !backend.equals("python")) {
            System.out.println("backend must be java or python (got: " + backendArg + ")");
            return;
        }
        Map<String, Object> assets = chosen.get("assets") instanceof Map<?, ?> m ? (Map<String, Object>) m : Map.of();
        String url = assets.get(backend) == null ? null : String.valueOf(assets.get(backend));
        if (url == null || url.isBlank()) {
            System.out.println("plugin '" + id + "' doesn't ship a " + backend + " build"
                    + (assets.get(other(backend)) == null ? "" : " (try 'systemc plugins install " + id + " " + other(backend) + "')"));
            return;
        }
        Map<String, Object> shas = chosen.get("sha256") instanceof Map<?, ?> m ? (Map<String, Object>) m : Map.of();
        String sha = shas.get(backend) == null ? null : String.valueOf(shas.get(backend));

        Path destDir = Integrations.dir();
        Files.createDirectories(destDir);
        Path dest = destDir.resolve(id + suffix(url));
        System.out.println("↓ " + url);
        download(url, dest);
        if (sha != null && sha.matches("[0-9a-fA-F]{64}")) {
            String actual = sha256(dest);
            if (!actual.equalsIgnoreCase(sha)) {
                Files.deleteIfExists(dest);
                throw new IOException("sha256 mismatch!  published=" + sha + "  actual=" + actual);
            }
            System.out.println("sha256 verified ✓");
        } else {
            System.out.println("(no published sha256 for this asset — saved without verification; check the repo before trusting it)");
        }
        System.out.println("installed " + id + " → " + dest);
        System.out.println("next: enable it — set INTEGRATIONS=" + id + " (or 'all') in .env, then restart the bots");
    }

    private static String other(String b) { return b.equals("java") ? "python" : "java"; }
    private static String suffix(String u) { return u.toLowerCase(Locale.ROOT).endsWith(".py") ? ".py" : ".jar"; }
    private static void download(String url, Path dest) throws IOException {
        HttpURLConnection c = (HttpURLConnection) URI.create(url).toURL().openConnection();
        c.setConnectTimeout(20000);
        c.setReadTimeout(300000);
        c.setRequestProperty("User-Agent", "systemc/plugins");
        if (c.getResponseCode() != 200) throw new IOException("HTTP " + c.getResponseCode() + " for " + url);
        try (InputStream in = c.getInputStream(); OutputStream out = new BufferedOutputStream(Files.newOutputStream(dest))) {
            in.transferTo(out);
        }
    }
    private static String sha256(Path p) throws Exception {
        var md = MessageDigest.getInstance("SHA-256");
        try (var in = new BufferedInputStream(Files.newInputStream(p))) {
            byte[] b = new byte[65536]; int n;
            while ((n = in.read(b)) > 0) md.update(b, 0, n);
        }
        var hex = new StringBuilder();
        for (byte b : md.digest())
            hex.append(Character.forDigit((b >> 4) & 0xF, 16)).append(Character.forDigit(b & 0xF, 16));
        return hex.toString();
    }
}
