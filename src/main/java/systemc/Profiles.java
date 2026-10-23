package systemc;

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
        String jar = System.getProperty("java.class.path", "systemc-java.jar");
        var pb = new ProcessBuilder("java", "-jar")
                .directory(Env.baseDir().toFile())
                .redirectOutput(ProcessBuilder.Redirect.appendTo(
                        Env.baseDir().resolve(".profiles").resolve(id + ".log").toFile()))
                .redirectErrorStream(true);
        pb.environment().put("SYSTEMC_PROFILE_ID", id);
        pb.environment().put("JAVA_MAIN_CLASS", "systemc.Main");
        String runAs = Env.get("AGENT_RUN_AS_USER");
        List<String> argv;
        if (runAs.isBlank()) {
            argv = List.of("java", "-cp", jar, "systemc.Main", "--profile", id);
        } else {
            argv = Tools.isRoot()
                    ? List.of("runuser", "-u", runAs, "--", "java", "-cp", jar, "systemc.Main", "--profile", id)
                    : List.of("sudo", "-n", "-u", runAs, "-H", "java", "-cp", jar, "systemc.Main", "--profile", id);
        }
        pb.command(argv);
        return pb.start();
    }
}
