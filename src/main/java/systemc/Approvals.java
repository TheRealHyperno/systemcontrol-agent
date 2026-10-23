package systemc;

import java.nio.file.*;
import java.time.Instant;
import java.util.*;
import java.util.stream.Collectors;

/**
 * Port of approvals.py — tiny file-based approval queue in .approvals/<id>.json:
 * {id, command, status: pending|approved|denied, created_at}. Written by the bot,
 * read/resolved by the Swing control panel's Approvals tab.
 */
public final class Approvals {
    static final Path DIR = Env.baseDir().resolve(".approvals");

    private Approvals() {}

    static {
        try { Files.createDirectories(DIR); } catch (Exception ignored) {}
    }

    private static Path path(String id) { return DIR.resolve(id + ".json"); }

    public static String create(String command) {
        String id = UUID.randomUUID().toString().replace("-", "").substring(0, 8);
        Map<String, Object> data = Json.map();
        data.put("id", id);
        data.put("command", command);
        data.put("status", "pending");
        data.put("created_at", Instant.now().toEpochMilli() / 1000.0);
        try { Files.writeString(path(id), Json.write(data, true)); } catch (Exception ignored) {}
        return id;
    }

    public static String getStatus(String id) {
        if (!Files.exists(path(id))) return "gone";
        try {
            Map<String, Object> d = Json.parse(Files.readString(path(id)));
            return d == null ? "pending" : d.getOrDefault("status", "pending").toString();
        } catch (Exception e) { return "pending"; }
    }

    public static void resolve(String id, boolean approved) {
        Path p = path(id);
        if (!Files.exists(p)) return;
        try {
            Map<String, Object> d = Json.parse(Files.readString(p));
            if (d == null) return;
            d.put("status", approved ? "approved" : "denied");
            Files.writeString(p, Json.write(d, true));
        } catch (Exception ignored) {}
    }

    public static List<Map<String, Object>> listPending() {
        try (var stream = Files.list(DIR)) {
            return stream.filter(f -> f.getFileName().toString().endsWith(".json"))
                .map(f -> {
                    try { return Json.parse(Files.readString(f)); }
                    catch (Exception e) { return null; }
                })
                .filter(d -> d != null && "pending".equals(d.get("status")))
                .sorted(Comparator.comparingDouble(d -> ((Number) d.getOrDefault("created_at", 0.0)).doubleValue()))
                .collect(Collectors.toList());
        } catch (Exception e) {
            return List.of();
        }
    }

    public static void cleanup(String id) {
        try { Files.deleteIfExists(path(id)); } catch (Exception ignored) {}
    }
}
