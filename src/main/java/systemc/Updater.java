package systemc;

import java.io.*;
import java.net.URI;
import java.net.http.*;
import java.nio.file.*;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.*;

/**
 * Port of updater.py — self-update against GitHub releases of
 * {@code TheRealHyperno/systemcontrol-agent} (override with {@code UPDATE_REPO}).
 *
 * Flow: check → (newest stable, or newest pre-release when opted in) → download the
 * {@code systemc-java-linux-x64.tar.gz} asset → verify it against the published
 * {@code .sha256} → back up the install (bin/ + lib/ + share/ + dist) → extract the
 * tarball over the install dir → record the new tag in {@code .updates/state.json}.
 * Rollback restores the newest backup.
 *
 * Used from the control panel's Updates tab and as {@code systemc update
 * [--check|--rollback] [--repo owner/repo] [--pre-release]}.
 */
public final class Updater {

    public static final String VERSION = "1.0.8";
    public static final String ASSET = "systemc-java-linux-x64.tar.gz";
    /** used when UPDATE_REPO is not set (same default as python) */
    public static final String DEFAULT_REPO = "TheRealHyperno/systemcontrol-agent";

    // ── version helpers ────────────────────────────────────────────────────

    /** The version of the jar this code runs from (systemc-java-<v>.jar), falling back to VERSION. */
    public static String installedVersion() {
        try {
            for (String part : System.getProperty("java.class.path", "").split(":")) {
                String f = Paths.get(part).getFileName().toString();
                if (f.startsWith("systemc-java-") && f.endsWith(".jar") && !f.equals("systemc-java.jar"))
                    return f.replaceFirst("^systemc-java-", "").replaceFirst("\\.jar$", "");
            }
        } catch (Exception ignored) {}
        return VERSION;
    }

    // ── check ──────────────────────────────────────────────────────────────

    /**
     * One GitHub release, shaped for the UI. "LATEST" = GitHub's absolute latest
     * release (newest publication — a stable v1.2 published after a "2.2" alpha is
     * LATEST, the alpha never wins by tag number). With pre-releases enabled the
     * newest published release of any kind wins, still purely by date, not tag.
     */
    public static Map<String, Object> check(String repo, boolean preRelease) throws Exception {
        if (repo == null || repo.isBlank()) repo = Env.get("UPDATE_REPO", DEFAULT_REPO);
        repo = repo.strip().replaceAll("^https?://github\\.com/", "").replaceAll("/+$", "");
        if (!repo.matches("[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"))
            throw new IllegalArgumentException("repo must look like owner/name, got: " + repo);

        String url = "https://api.github.com/repos/" + repo
                + (preRelease ? "/releases" : "/releases/latest");
        HttpRequest.Builder rb = HttpRequest.newBuilder(URI.create(url))
                .timeout(Duration.ofSeconds(20))
                .header("Accept", "application/vnd.github+json")
                .header("User-Agent", "systemc-updater/" + VERSION);
        String token = System.getenv("GITHUB_TOKEN");
        if (token != null && !token.isBlank()) rb.header("Authorization", "Bearer " + token);
        HttpResponse<String> resp = HTTP.send(rb.GET().build(), HttpResponse.BodyHandlers.ofString());

        Map<String, Object> chosen;
        if (preRelease) {
            if (resp.statusCode() != 200)
                throw new IOException("GitHub returned HTTP " + resp.statusCode() + " for " + repo);
            var releases = Json.parseList(resp.body());
            if (releases == null) throw new IOException("no releases published on " + repo);
            chosen = null;
            for (var r : releases) {                        // API returns newest published first
                if (Boolean.TRUE.equals(r.get("draft"))) continue;
                chosen = r;
                break;
            }
            if (chosen == null) throw new IOException("no published releases on " + repo);
        } else {
            if (resp.statusCode() == 404)
                throw new IOException("no published releases on " + repo + " yet");
            if (resp.statusCode() != 200)
                throw new IOException("GitHub returned HTTP " + resp.statusCode() + " for " + repo);
            chosen = Json.parse(resp.body());
            if (chosen == null) throw new IOException("could not parse the latest release");
        }

        Map<String, Object> out = Json.map();
        out.put("tag", String.valueOf(chosen.getOrDefault("tag_name", "")));
        out.put("name", String.valueOf(chosen.getOrDefault("name", chosen.get("tag_name"))));
        out.put("prerelease", Boolean.TRUE.equals(chosen.get("prerelease")));
        out.put("published", String.valueOf(chosen.getOrDefault("published_at", "")));
        out.put("notes", String.valueOf(chosen.getOrDefault("body", "")));
        out.put("repo", repo);

        String assetUrl = null, shaUrl = null;
        if (chosen.get("assets") instanceof List<?> assets)
            for (Object o : assets)
                if (o instanceof Map<?, ?> a) {
                    String n = String.valueOf(a.get("name"));
                    String dl = String.valueOf(a.get("browser_download_url"));
                    if (n.equals(ASSET)) assetUrl = dl;
                    if (n.equals(ASSET + ".sha256")) shaUrl = dl;
                }
        if (assetUrl == null)
            throw new IOException("the latest release (" + out.get("tag")
                    + ") doesn't ship a java build — no " + ASSET
                    + " asset (it's a python-backend release; either release a java build for it or pick a repo that carries one)");
        out.put("asset_url", assetUrl);
        if (shaUrl != null) out.put("sha_url", shaUrl);

        String installed = installedVersion();
        out.put("installed", installed);
        // java and python share one release line now, so "newer" is decided by
        // github's latest selection, not tag arithmetic: we need an update iff
        // the latest release isn't the one we're already running.
        String tag = String.valueOf(out.get("tag"));
        out.put("update_available", !normalize(tag).equals(normalize(installed)));
        out.put("app_dir", appDir().toString());
        return out;
    }

    /** v1.2 → 1.2, alpha-v2.2.3 stays alpha-v2.2.3 (case-insensitive, leading v dropped). */
    static String normalize(String v) {
        return v == null ? "" : v.strip().replaceFirst("^v", "").toLowerCase(Locale.ROOT);
    }

    // ── install / apply ────────────────────────────────────────────────────

    /** Downloads + verifies + backs up + extracts. Returns a result summary map. */
    public static Map<String, Object> install(Map<String, Object> info, java.util.function.Consumer<String> log)
            throws Exception {
        String tag = String.valueOf(info.get("tag"));
        Path app = appDir();
        Path tmp = Files.createTempDirectory("systemc-update-");

        // 1. download
        String url = String.valueOf(info.get("asset_url"));
        Path tarball = tmp.resolve(ASSET);
        log.accept("↓ " + url);
        download(url, tarball);
        log.accept("downloaded " + mib(Files.size(tarball)) + " MiB → " + tarball);

        // 2. verify against the published sha256 (python parity: refuse without it)
        Object shaUrlO = info.get("sha_url");
        if (!(shaUrlO instanceof String s) || s.isBlank())
            throw new IOException("release has no " + ASSET + ".sha256 asset — aborting (never install unverified)");
        String published = HTTP.send(HttpRequest.newBuilder(URI.create(s))
                        .timeout(Duration.ofSeconds(20))
                        .header("User-Agent", "systemc-updater/" + VERSION).GET().build(),
                HttpResponse.BodyHandlers.ofString()).body().trim().split("\\s+")[0];
        String actual = sha256(tarball);
        if (!actual.equalsIgnoreCase(published))
            throw new IOException("sha256 mismatch! published=" + published + " actual=" + actual);
        log.accept("sha256 verified ✓");

        // 3. backup current install
        Path backup = backup(app, String.valueOf(info.get("installed")));
        log.accept("backup: " + backup);

        // 4. extract tarball over the install (asset layout: systemc-java/bin|lib|share)
        var pb = new ProcessBuilder("tar", "xzf", tarball.toString(), "--strip-components=1")
                .directory(app.toFile());
        pb.redirectErrorStream(true);
        var p = pb.start();
        byte[] out = p.getInputStream().readAllBytes();
        if (!p.waitFor(300, java.util.concurrent.TimeUnit.SECONDS) || p.exitValue() != 0)
            throw new IOException("tar failed: " + new String(out));
        log.accept("extracted " + tag + " over " + app);

        // drop versioned jars of other releases so the launcher runs the new one
        int pruned = pruneOldJars(app, tag);
        if (pruned > 0) log.accept("removed " + pruned + " old systemc-java-*.jar from lib/");

        // clean the tmp download
        Files.walk(tmp).sorted(Comparator.reverseOrder()).forEach(f -> f.toFile().delete());

        // 5. record state
        Map<String, Object> state = loadState();
        state.put("installed_tag", tag);
        state.put("updated_at", java.time.Instant.now().toString());
        saveState(state);
        Map<String, Object> res = Json.map();
        res.put("ok", true);
        res.put("tag", tag);
        res.put("backup", backup.toString());
        return res;
    }

    /** Deletes every lib/systemc-java-<v>.jar whose <v> isn't {@code keepVer} (it self-updates too). */
    static int pruneOldJars(Path app, String keepVer) {
        String want = normalize(keepVer);
        int n = 0;
        Path lib = app.resolve("lib");
        try (var st = Files.list(lib)) {
            for (Path j : (Iterable<Path>) st.toList()) {
                String name = j.getFileName().toString();
                if (!name.matches("systemc-java-.+\\.jar")) continue;
                String ver = name.substring("systemc-java-".length(), name.length() - ".jar".length());
                if (!normalize(ver).equals(want)) {
                    Files.deleteIfExists(j);
                    n++;
                }
            }
        } catch (IOException ignored) {}
        return n;
    }

    /** Copies the current install's bin/lib/share (+dist) into .updates/backups/<tag>-<ts>/ . */
    public static Path backup(Path app, String tagBefore) throws Exception {
        Path dir = Env.baseDir().resolve(".updates").resolve("backups")
                .resolve((tagBefore == null || tagBefore.isBlank() ? "unknown" : tagBefore)
                        + "-" + java.time.LocalDateTime.now()
                        .format(java.time.format.DateTimeFormatter.ofPattern("yyyyMMdd-HHmmss")));
        Files.createDirectories(dir);
        for (String sub : List.of("bin", "lib", "share", "dist"))
            copyTree(app.resolve(sub), dir.resolve(sub));
        return dir;
    }

    public static List<Map<String, Object>> listBackups() {
        var out = new ArrayList<Map<String, Object>>();
        try (var st = Files.list(Env.baseDir().resolve(".updates").resolve("backups"))) {
            for (Path b : st.filter(Files::isDirectory).sorted(Comparator.reverseOrder()).toList()) {
                Map<String, Object> m = Json.map();
                m.put("name", b.getFileName().toString());
                m.put("path", b.toString());
                m.put("when", java.nio.file.Files.getLastModifiedTime(b).toInstant().toString());
                out.add(m);
            }
        } catch (IOException ignored) {}
        return out;
    }

    /** Restores the newest backup (or the named one). */
    public static Map<String, Object> rollback(String backupName, java.util.function.Consumer<String> log)
            throws Exception {
        var backups = listBackups();
        Map<String, Object> chosen = null;
        for (var b : backups) if (backupName == null || backupName.isBlank() || backupName.equals(b.get("name"))) {
            chosen = b;
            break;
        }
        if (chosen == null) throw new IOException("no backups found under .updates/backups/");
        Path src = Path.of(String.valueOf(chosen.get("path")));
        Path app = appDir();
        for (String sub : List.of("bin", "lib", "share", "dist"))
            copyTree(src.resolve(sub), app.resolve(sub));
        // restore really restores: drop jars of the release we rolled back FROM
        String backTo = String.valueOf(chosen.get("name")).replaceFirst("-\\d{8}-\\d{6}$", "");
        int pruned = pruneOldJars(app, backTo);
        if (pruned > 0 && log != null) log.accept("removed " + pruned + " newer systemc-java-*.jar from lib/");
        Map<String, Object> state = loadState();
        state.put("installed_tag", backTo);
        state.put("rolled_back_at", java.time.Instant.now().toString());
        saveState(state);
        log.accept("restored " + src);
        Map<String, Object> res = Json.map();
        res.put("ok", true);
        res.put("backup", chosen.get("name"));
        return res;
    }

    private static void copyTree(Path src, Path dst) throws IOException {
        if (!Files.exists(src)) return;
        Files.walk(src).forEach(s -> {
            try {
                Path d = dst.resolve(src.relativize(s).toString());
                if (Files.isDirectory(s)) Files.createDirectories(d);
                else {
                    Files.createDirectories(d.getParent());
                    Files.copy(s, d, StandardCopyOption.REPLACE_EXISTING);
                }
                if (d.toString().contains("/bin/")) d.toFile().setExecutable(true, true);
            } catch (IOException ignored) {}
        });
    }

    // ── plumbing ───────────────────────────────────────────────────────────

    static final HttpClient HTTP = HttpClient.newBuilder().followRedirects(HttpClient.Redirect.NORMAL).build();

    /** The install root: parent of the lib/ dir the running jar comes from. */
    public static Path appDir() {
        try {
            for (String part : System.getProperty("java.class.path", "").split(":")) {
                Path p = Paths.get(part).toAbsolutePath();
                if (p.getFileName().toString().startsWith("systemc-java-")) {
                    Path lib = p.getParent();
                    if (lib != null && lib.getFileName().toString().equals("lib")) return lib.getParent();
                    if (lib != null) return lib;                  // running from target/ in a build tree
                }
            }
        } catch (Exception ignored) {}
        return Env.baseDir().toAbsolutePath();
    }

    static String mib(long bytes) { return String.format(Locale.ROOT, "%.1f", bytes / 1048576.0); }

    public static void download(String url, Path dest) throws Exception {
        HttpRequest req = HttpRequest.newBuilder(URI.create(url))
                .timeout(Duration.ofMinutes(10))
                .header("User-Agent", "systemc-updater/" + VERSION)
                .GET().build();
        HttpResponse<InputStream> resp = HTTP.send(req, HttpResponse.BodyHandlers.ofInputStream());
        if (resp.statusCode() != 200) throw new IOException("download HTTP " + resp.statusCode() + " for " + url);
        try (var in = resp.body(); var out = new BufferedOutputStream(Files.newOutputStream(dest))) {
            in.transferTo(out);
        }
    }

    public static String sha256(Path p) throws Exception {
        var md = MessageDigest.getInstance("SHA-256");
        try (var in = new BufferedInputStream(Files.newInputStream(p))) {
            byte[] buf = new byte[65536];
            int n;
            while ((n = in.read(buf)) > 0) md.update(buf, 0, n);
        }
        var hex = new StringBuilder();
        for (byte b : md.digest()) hex.append(Character.forDigit((b >> 4) & 0xF, 16))
                .append(Character.forDigit(b & 0xF, 16));
        return hex.toString();
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> loadState() {
        try {
            Map<String, Object> m = Json.parse(Files.readString(Env.baseDir().resolve(".updates/state.json")));
            return m == null ? Json.map() : m;
        } catch (Exception e) {
            return Json.map();
        }
    }

    public static void saveState(Map<String, Object> state) {
        try {
            Files.createDirectories(Env.baseDir().resolve(".updates"));
            Files.writeString(Env.baseDir().resolve(".updates/state.json"), Json.write(state, true));
        } catch (IOException ignored) {}
    }

    // ── CLI: systemc update [--check] [--rollback] [--repo o/r] [--pre-release] ──

    public static void main(String[] args) {
        Env.setBaseDir(Path.of(".").toAbsolutePath());
        Env.load();
        boolean checkOnly = false, rollback = false, pre = Env.getBool("UPDATE_PRERELEASE", false);
        String repo = Env.get("UPDATE_REPO", DEFAULT_REPO);
        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--check", "-c" -> checkOnly = true;
                case "--rollback", "-r" -> rollback = true;
                case "--pre-release", "--pre" -> pre = true;
                case "--repo" -> repo = args[++i];
                default -> { }
            }
        }
        try {
            if (rollback) {
                rollback(null, s -> System.out.println(s));
                System.out.println("rolled back. Restart the bots to load the restored version.");
                return;
            }
            Map<String, Object> info = check(repo, pre);
            System.out.println("installed: " + info.get("installed"));
            System.out.println("latest:    " + info.get("tag") + (Boolean.TRUE.equals(info.get("prerelease")) ? " (pre-release)" : ""));
            if (!(Boolean) info.getOrDefault("update_available", false)) {
                System.out.println("up to date.");
                return;
            }
            if (checkOnly) {
                System.out.println("update available — run: systemc update");
                return;
            }
            install(info, System.out::println);
            System.out.println("updated to " + info.get("tag") + ". Restart the bots: systemc service restart (or re-run systemc run).");
        } catch (Exception e) {
            System.err.println("update failed: " + e.getMessage());
            System.exit(1);
        }
    }
}
