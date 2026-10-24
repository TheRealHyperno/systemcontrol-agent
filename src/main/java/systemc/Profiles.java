package systemc;

import java.io.IOException;
import java.nio.file.*;
import java.util.*;

/**
 * Port of profiles.py (light) — the multi-instance manager. Stores profiles in
 * .profiles/profiles.json and spawns one `systemc-java` process per profile by
 * re-executing this jar with SYSTEMC_PROFILE_ID set. Passwordless sudo/runuser
 * for a dedicated account is honoured via AGENT_RUN_AS_USER (runas parity).
 */
public final class Profiles {
    static java.nio.file.Path storePath() {
        String profile = Env.get("SYSTEMC_PROFILE_ID");
        return Env.baseDir().resolve(".profiles").resolve("profiles.json");
    }

    public static List<Map<String, Object>> list() {
        try {
            Map<String, Object> data = Json.parse(Files.readString(storePath()));
            Object profiles = data == null ? null : data.get("profiles");
            if (profiles instanceof List<?> l) {
                var out = new ArrayList<Map<String, Object>>();
                for (Object o : l) if (o instanceof Map<?, ?> m) out.add((Map<String, Object>) m);
                return out;
            }
        } catch (Exception ignored) {}
        return List.of();
    }

    public static void save(List<Map<String, Object>> profiles) {
        try {
            Files.createDirectories(storePath().getParent());
            Map<String, Object> wrap = Json.map();
            wrap.put("profiles", profiles);
            Files.writeString(storePath(), Json.write(wrap, true));
        } catch (Exception ignored) {}
    }

    public static Map<String, Object> get(String id) {
        for (var p : list()) if (id.equals(p.get("id"))) return p;
        return null;
    }

    public static Map<String, Object> upsert(String id, Map<String, Object> patch) {
        var profiles = new ArrayList<>(list());
        Map<String, Object> existing = null;
        for (var p : profiles) if (id.equals(p.get("id"))) { existing = p; break; }
        if (existing == null) {
            existing = Json.map();
            existing.put("id", id);
            profiles.add(existing);
        }
        existing.putAll(patch == null ? Map.of() : patch);
        save(profiles);
        return existing;
    }

    public static boolean delete(String id) {
        var profiles = new ArrayList<>(list());
        boolean removed = profiles.removeIf(p -> id.equals(p.get("id")));
        if (removed) save(profiles);
        return removed;
    }

    /** Start a bot instance for a profile (spawns a separate JVM, like profiles.py). */
    public static Process start(String id) throws Exception {
        Map<String, Object> profile = get(id);
        if (profile == null) throw new IllegalArgumentException("no such profile: " + id);
        var pb = new ProcessBuilder(argvFor(id));
        pb.directory(Env.baseDir().toFile());
        return pb.start();      // output goes to the caller (Run&Logs tees it into the log pane)
    }

    /**
     * Full spawn command for one instance. id == null → the default instance (own .env).
     * Honours AGENT_RUN_AS_USER via runuser (root) / sudo -n (panel user), runas parity.
     */
    public static List<String> argvFor(String id) {
        String jar = jarPath();
        String javaBin = Path.of(System.getProperty("java.home"), "bin", "java").toString();
        List<String> bare = id == null
                ? List.of(javaBin, "-cp", jar, "systemc.Main")
                : List.of(javaBin, "-cp", jar, "systemc.Main", "--profile", id);
        String runAs = Env.get("AGENT_RUN_AS_USER");
        if (runAs.isBlank() || id == null) return bare;       // run-as applies to profile instances
        if (Tools.isRoot()) {
            var out = new ArrayList<String>(List.of("runuser", "-u", runAs, "--"));
            out.addAll(bare);
            return out;
        }
        var out = new ArrayList<String>(List.of("sudo", "-n", "-u", runAs, "-H", "--"));
        out.addAll(bare);
        return out;
    }

    /** Path of the first systemc-java jar on the classpath (for re-spawning instances). */
    public static String jarPath() {
        for (String part : System.getProperty("java.class.path", "").split(":")) {
            Path p = Paths.get(part);
            String name = p.getFileName().toString();
            if (Files.isRegularFile(p) && name.startsWith("systemc-java") && name.endsWith(".jar"))
                return p.toAbsolutePath().toString();
        }
        return "systemc-java.jar";
    }

    /** Where an instance's log lives (.run.log for default, .profiles/<id>.log otherwise). */
    public static Path logPath(String id) {
        return id == null ? Env.baseDir().resolve(".run.log")
                : Env.baseDir().resolve(".profiles").resolve(id + ".log");
    }

    // ── per-profile .env files ───────────────────────────────────────────────

    public static Path envPath(String id) {
        return id == null || id.isBlank() ? Env.baseDir().resolve(".env")
                : Env.baseDir().resolve(".profiles").resolve(id + ".env");
    }

    /** Reads KEY=VALUE lines of a profile (.env overlay) into a map (no setter inheritance). */
    public static Map<String, String> readEnvFile(Path p) {
        var out = new LinkedHashMap<String, String>();
        try {
            for (String line : Files.readAllLines(p)) {
                String t = line.strip();
                if (t.isEmpty() || t.startsWith("#") || !t.contains("=")) continue;
                int i = t.indexOf('=');
                String k = t.substring(0, i).strip(), v = t.substring(i + 1).strip();
                if (k.matches("[A-Za-z_][A-Za-z0-9_.]*")) out.put(k, v);
            }
        } catch (IOException ignored) {}
        return out;
    }

    /** Reads KEY=VALUE lines from raw bytes (zip import). Key order not preserved. */
    public static Map<String, String> readEnvBytes(byte[] bytes) {
        var out = new LinkedHashMap<String, String>();
        for (String line : new String(bytes).split("\r?\n")) {
            String t = line.strip();
            if (t.isEmpty() || t.startsWith("#") || !t.contains("=")) continue;
            int i = t.indexOf('=');
            String k = t.substring(0, i).strip(), v = t.substring(i + 1).strip();
            if (k.matches("[A-Za-z_][A-Za-z0-9_.]*")) out.put(k, v);
        }
        return out;
    }

    /** Sets KEY=VALUE in a profile's env file (create if missing). */
    public static void writeEnvFile(Path p, Map<String, String> env) {
        try {
            Files.createDirectories(p.getParent());
            var lines = new ArrayList<String>();
            var keys = new LinkedHashSet<String>();
            for (String line : Files.exists(p) ? Files.readAllLines(p) : List.<String>of()) {
                String t = line.strip();
                if (t.isEmpty() || t.startsWith("#") || !t.contains("=")) { lines.add(line); continue; }
                String k = t.substring(0, t.indexOf('=')).strip();
                if (env.containsKey(k)) { lines.add(k + "=" + env.get(k)); }
                else lines.add(line);
                keys.add(k);
            }
            for (String k : env.keySet())
                if (!keys.contains(k)) lines.add(k + "=" + env.get(k));
            Files.write(p, lines);
        } catch (IOException ignored) {}
    }

    /**
     * "Pull Dependencies": installs the profile's pip packages (DEPENDENCIES= in its env,
     * comma separated) with pip install --user, under AGENT_RUN_AS_USER when set.
     * Returns the combined output.
     */
    public static String pullDependencies(String id) throws Exception {
        Map<String, String> env = readEnvFile(envPath(id));
        String deps = env.getOrDefault("DEPENDENCIES", "").strip();
        if (deps.isBlank()) return "no DEPENDENCIES= set in this profile's env — nothing to pull";
        List<String> pkgs = java.util.Arrays.stream(deps.split("[,;]")).map(String::strip)
                .filter(s -> !s.isEmpty() && !s.startsWith("#")).toList();
        if (pkgs.isEmpty()) return "no DEPENDENCIES= set in this profile's env — nothing to pull";
        var pip = new ArrayList<String>();
        String runAs = Env.get("AGENT_RUN_AS_USER");
        if (!runAs.isBlank() && !"default".equals(id)) {
            if (Tools.isRoot()) { pip.add("runuser"); pip.add("-u"); pip.add(runAs); pip.add("--"); }
            else { pip.add("sudo"); pip.add("-n"); pip.add("-u"); pip.add(runAs); pip.add("-H"); pip.add("--"); }
        }
        pip.add("pip3"); pip.add("install"); pip.add("--user"); pip.addAll(pkgs);
        var pb = new ProcessBuilder(pip).directory(Env.baseDir().toFile());
        pb.redirectErrorStream(true);
        var p = pb.start();
        String out = new String(p.getInputStream().readAllBytes());
        p.waitFor(300, java.util.concurrent.TimeUnit.SECONDS);
        return "$ pip3 install --user " + String.join(" ", pkgs) + "\n" + out.strip();
    }
}
