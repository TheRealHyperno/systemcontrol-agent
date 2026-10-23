package systemc;

import java.io.IOException;
import java.nio.file.*;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * .env loader + env lookup, mirroring the Python side's `_load_dotenv()`.
 * Values in .env win over the process environment only if the process env
 * doesn't already define them (os.environ.setdefault semantics).
 */
public final class Env {
    private static final Map<String, String> FILE = new HashMap<>();
    private static Path baseDir = Path.of(".").toAbsolutePath();

    private Env() {}

    public static Path baseDir() { return baseDir; }

    public static void setBaseDir(Path dir) {
        baseDir = dir.toAbsolutePath().normalize();
    }

    /** Loads .env from the base dir. Returns true if found. */
    public static boolean load() {
        return load(baseDir.resolve(".env"));
    }

    public static boolean load(Path p) {
        if (!Files.exists(p)) return false;
        try {
            for (String line : Files.readAllLines(p)) {
                line = line.strip();
                if (line.isEmpty() || line.startsWith("#") || !line.contains("=")) continue;
                int i = line.indexOf('=');
                String k = line.substring(0, i).strip();
                String v = line.substring(i + 1).strip();
                // strip matching quotes (sh-style .env)
                if (v.length() >= 2 && (v.charAt(0) == '"' && v.charAt(v.length() - 1) == '"'
                        || v.charAt(0) == '\'' && v.charAt(v.length() - 1) == '\'')) {
                    v = v.substring(1, v.length() - 1);
                }
                if (k.matches("[A-Za-z_][A-Za-z0-9_.]*")) {
                    FILE.putIfAbsent(k, v); // setdefault semantics
                }
            }
            return true;
        } catch (IOException e) {
            return false;
        }
    }

    public static String get(String key) { return get(key, ""); }

    public static String get(String key, String def) {
        String v = FILE.get(key);
        if (v == null) v = System.getenv(key);
        if (v != null && !v.isBlank()) return v.strip();
        return def;
    }

    public static int getInt(String key, int def) {
        try { return Math.max(1, Integer.parseInt(get(key, String.valueOf(def)))); }
        catch (NumberFormatException e) { return def; }
    }

    public static boolean getBool(String key, boolean def) {
        String v = get(key, "").toLowerCase();
        if (List.of("1", "true", "yes", "on").contains(v)) return true;
        if (List.of("0", "false", "no", "off").contains(v)) return false;
        return def;
    }

    /** Writes a KEY=VALUE to .env (replacing an existing line or appending). */
    public static synchronized void set(String key, String value) {
        FILE.put(key, value);
        Path p = baseDir.resolve(".env");
        var lines = new java.util.ArrayList<String>();
        try {
            if (Files.exists(p)) lines.addAll(Files.readAllLines(p));
        } catch (IOException ignored) {}
        boolean replaced = false;
        for (int i = 0; i < lines.size(); i++) {
            if (lines.get(i).strip().startsWith(key + "=")) {
                lines.set(i, key + "=" + value);
                replaced = true;
                break;
            }
        }
        if (!replaced) lines.add(key + "=" + value);
        try {
            Files.createDirectories(p.getParent());
            Files.write(p, lines, StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING);
        } catch (IOException ignored) {}
    }
}
