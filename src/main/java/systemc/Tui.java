package systemc;

import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;

/**
 * Terminal (TUI) control panel for SystemC — works over SSH or on a bare TTY
 * with no desktop environment. Zero dependencies: plain ANSI + `stty` raw mode.
 *
 *   ↑/↓ or j/k : move   Enter : select/edit   ←→ : cycle choices   q/Esc : back/quit
 */
public final class Tui {
    private static final String CLEAR = "\033[H\033[2J", DIM = "\033[2m", BOLD = "\033[1m",
            RESET = "\033[0m", SEL = "\033[7m", BLUE = "\033[38;5;69m", YELLOW = "\033[38;5;179m";

    private static InputStreamReader in;

    public static void main(String[] args) {
        Env.load();
        try {
            runLoop();
        } finally {
            restore();
        }
    }

    // ── data model ────────────────────────────────────────────────────────────

    record Row(String key, String label, String def, List<String> cycle, boolean secret) {
        static Row field(String key, String label, String def) { return new Row(key, label, def, List.of(), false); }
        static Row choice(String key, String label)    { return new Row(key, label, "", List.of("(default)", "yes"), false); }
        static Row secret(String key, String label)    { return new Row(key, label, "", List.of(), true); }
    }

    private static final List<Row> FRONTEND = List.of(
            Row.secret("DISCORD_BOT_TOKEN", "Discord bot token"),
            Row.field("ALLOWED_CHANNEL_IDS", "Discord: allowed channel IDs (comma-sep)", ""),
            Row.secret("STOAT_BOT_TOKEN", "Stoat bot token"),
            Row.field("STOAT_ALLOWED_CHANNEL_IDS", "Stoat: allowed channel IDs (comma-sep)", ""),
            Row.field("BOT_MODE", "Bot mode (wake / mention / always)", "wake"),
            Row.field("WAKE_WORD", "Wake word", "systemc"));
    private static final List<Row> AGENTCFG = List.of(
            Row.field("AGENT_MODEL", "Local Ollama model", "qwen2.5:0.5b"),
            Row.field("OLLAMA_HOST", "Ollama host", "http://localhost:11434"),
            Row.field("OLLAMA_API_KEY", "Ollama API key (leave empty for local)", ""),
            Row.field("OLLAMA_API_KEYS", "Extra Ollama keys, comma-sep pool", ""),
            Row.field("AGENT_PROVIDER", "Cloud provider (empty = local only)", "anthropic"),
            Row.secret("ANTHROPIC_API_KEY", "Anthropic API key"),
            Row.field("APPROVAL_MODE", "Approval mode (auto / manual)", "auto"));
    private static final List<Row> CONSOLE = List.of(
            Row.field("WEBUI_PORT", "Web UI port", "7842"),
            Row.field("SYSTEMC_API_TOKEN", "Web-UI auth token (empty = allow all on this IP)", ""),
            Row.field("SYSTEMC_WEBSITE", "Website the bots announce", ""));
    private static final List<Row> SUBAGENTCNF = List.of(
            Row.field("SUBAGENTS", "Subagent spec (empty = none)", ""));

    // ── main loop ─────────────────────────────────────────────────────────────

    private static void runLoop() {
        while (true) {
            String[] items = {
                    "Bots      — Discord / Stoat tokens, wake word",
                    "Agent     — model backend, approvals",
                    "Console   — web UI port & token",
                    "Subagents — extra helper agents",
                    "Integrations — enable/disable + settings (java & python)",
                    "Startup   — systemd service (install / stop / remove)",
                    "Edit .env — open in $EDITOR",
                    "Exit"};
            int pick = menu(header() + "\n\n" + statusLine(), items);

            if (pick == 7 || pick == -1) return;
            switch (pick) {
                case 0 -> pillar("Bots — Discord & Stoat front-ends", FRONTEND);
                case 1 -> pillar("Agent — model backend and approval style", AGENTCFG);
                case 2 -> pillar("Console — web UI", CONSOLE);
                case 3 -> pillar("Subagents", SUBAGENTCNF);
                case 4 -> integrationsMenu();
                case 5 -> serviceMenu();
                case 6 -> editEnvFile();
            }
        }
    }

    private static String header() {
        return CLEAR + BOLD + BLUE + "  System ⊙ C" + RESET
                + "  " + DIM + "SystemC Drovex — terminal control panel" + RESET;
    }

    private static String statusLine() {
        boolean d = !Env.get("DISCORD_BOT_TOKEN").isBlank(), s = !Env.get("STOAT_BOT_TOKEN").isBlank();
        String bots = (d || s) ? BLUE + (d ? "discord " : "") + (s ? "stoat" : "") + RESET
                : DIM + "no bot token yet — configure Bots" + RESET;
        return "  bots: " + bots + "   model: " + YELLOW
                + (Env.get("AGENT_MODEL").isBlank() ? "(default)" : Env.get("AGENT_MODEL")) + RESET
                + "   provider: " + (Env.get("AGENT_PROVIDER").isBlank() ? "local" : Env.get("AGENT_PROVIDER"))
                + "   approvals: " + Env.get("APPROVAL_MODE", "auto")
                + "   webui: http://127.0.0.1:" + Env.getInt("WEBUI_PORT", 7842);
    }

    // ── generic menu (returns chosen index, or -1 on q/Esc) ───────────────────

    private static int menu(String head, String[] items) {
        int sel = 0;
        while (true) {
            var sb = new StringBuilder(head).append("\n");
            for (int i = 0; i < items.length; i++) {
                sb.append(i == sel ? SEL + " ▸ " : "   ")
                  .append(i == sel ? items[i] + RESET : items[i])
                  .append("\n");
            }
            sb.append("\n").append(DIM).append("  ↑/↓ move · Enter select · q/Esc back").append(RESET).append("\n");
            System.out.print(sb); System.out.flush();
            int c = readKey();
            if (c == 'q' || c == 'Q' || c == 27 || c == -1) return -1;
            if (c == 'j' || c == 'B') sel = (sel + 1) % items.length;
            if (c == 'k' || c == 'A') sel = (sel + items.length - 1) % items.length;
            if (c == '\n' || c == '\r') return sel;
        }
    }

    // ── settings pillars ──────────────────────────────────────────────────────

    private static void pillar(String title, List<Row> rows) {
        int sel = 0;
        while (true) {
            var sb = new StringBuilder(header()).append("\n\n").append(BLUE).append(title).append(RESET).append("\n\n");
            for (int i = 0; i < rows.size(); i++) {
                var r = rows.get(i);
                String v = Env.get(r.key, r.def);
                String shown = r.secret
                        ? (v.isBlank() ? DIM + "(not set)" + RESET
                                       : YELLOW + "●".repeat(Math.min(20, Math.max(8, v.length() / 8 + 6))) + RESET)
                        : (v.isBlank() ? DIM + "(default)" + RESET : YELLOW + v + RESET);
                sb.append(i == sel ? SEL + " ▸ " : "   ")
                  .append(String.format("%-42s", r.label))
                  .append(i == sel ? shown + RESET : shown)
                  .append("\n");
            }
            sb.append("\n").append(DIM).append("  Enter edit · q/Esc back").append(RESET).append("\n");
            System.out.print(sb); System.out.flush();
            int c = readKey();
            if (c == 'q' || c == 'Q' || c == 27 || c == -1) return;
            if (c == 'j' || c == 'B') sel = (sel + 1) % rows.size();
            if (c == 'k' || c == 'A') sel = (sel + rows.size() - 1) % rows.size();
            if (c == '\n' || c == '\r') editField(rows.get(sel));
        }
    }

    private static void editField(Row r) {
        String current = Env.get(r.key, r.def);
        restore();
        System.out.println();
        System.out.print(BOLD + r.label + RESET + (current.isBlank() ? "" : DIM + "  (enter to keep: " + current + ")" + RESET) + "\n> ");
        System.out.flush();
        String v = new java.util.Scanner(System.in).nextLine().strip();
        Env.set(r.key, v);
        raw();
    }

    /** Settings page for one integration (from its panel() descriptor). */
    private static void integrationSettings(Integrations.Entry e) {
        var fields = Integrations.fieldsOf(e);
        var values = Integrations.currentSettings(e);
        int sel = 0;
        while (true) {
            var sb = new StringBuilder(header()).append("\n\n")
                    .append(BLUE).append(Integrations.menuTitle(e)).append(RESET)
                    .append(DIM).append("  — saved in .env (values apply at next bot start)").append(RESET).append("\n\n");
            for (int i = 0; i < fields.size(); i++) {
                var f = fields.get(i);
                String v = values.get(f.key());
                String shown = switch (f.type()) {
                    case "secret" -> v.isBlank() ? DIM + "(not set)" + RESET
                            : YELLOW + "●".repeat(Math.min(20, Math.max(8, v.length() / 8 + 6))) + RESET;
                    default -> v.isBlank() ? DIM + "(not set)" + RESET : YELLOW + v + RESET;
                };
                sb.append(i == sel ? SEL + " ▸ " : "   ")
                  .append(String.format("%-40s", f.label()))
                  .append(shown).append(RESET).append("\n");
            }
            sb.append("\n").append(DIM).append("  Enter edit · q/Esc back").append(RESET).append("\n");
            System.out.print(sb); System.out.flush();
            int c = readKey();
            if (c == 'q' || c == 'Q' || c == 27 || c == -1) return;
            if (c == 'j' || c == 'B') sel = (sel + 1) % Math.max(1, fields.size());
            if (c == 'k' || c == 'A') sel = (sel + Math.max(1, fields.size()) - 1) % Math.max(1, fields.size());
            if (c == '\n' || c == '\r' && !fields.isEmpty()) {
                var f = fields.get(sel);
                String current = values.get(f.key());
                restore();
                String hint = current.isBlank() ? "" : DIM + "  (Enter keeps: " + current + ")" + RESET;
                System.out.print(BOLD + f.label() + RESET + hint + "\n> ");
                if (f.choices() != null && !f.choices().isEmpty()) {
                    // show choices instead of free text
                    System.out.println();
                    for (int i = 0; i < f.choices().size(); i++)
                        System.out.println("   " + (i + 1) + ") " + f.choices().get(i));
                    System.out.print("pick 1-" + f.choices().size() + ": ");
                }
                System.out.flush();
                String in = new java.util.Scanner(System.in).nextLine().strip();
                String v = in;
                if (f.choices() != null && !f.choices().isEmpty()) {
                    try {
                        int idx = Integer.parseInt(in) - 1;
                        if (idx >= 0 && idx < f.choices().size()) v = f.choices().get(idx);
                    } catch (NumberFormatException ignored) {}
                }
                if (v.isBlank() && !current.isBlank()) v = current;   // Enter keeps
                values.put(f.key(), v);
                Integrations.saveSettings(e, values);
                raw();
            }
        }
    }

    // ── integrations menu (java SPI + python-compat) ─────────────────────────

    private static void integrationsMenu() {
        var discovered = Integrations.discover(false);
        var enabled = new HashSet<>(Integrations.enabledNames(discovered));
        var list = new ArrayList<>(discovered.entrySet());
        String[] labels = new String[list.size() + 2];
        String[] names = new String[list.size()];   // parallel array
        for (int i = 0; i < list.size(); i++) {
            var e = list.get(i).getValue();
            names[i] = e.name;
            labels[i] = String.format("%-12s %s · %s · %d tools%s", e.name,
                    enabled.contains(e.name) ? "✔" : "   ", e.source, e.tools.size(),
                    e.error == null ? "" : " ⚠ " + e.error);
        }
        labels[list.size()] = "Edit INTEGRATIONS= list in $EDITOR";
        labels[list.size() + 1] = "Back";
        int pick = menu(header() + "\n\n", labels);
        if (pick < 0 || pick >= list.size() + 1) return;
        if (pick == list.size()) { editEnvFile(); integrationsMenu(); return; }
        var e = list.get(pick).getValue();
        Integrations.toggleEnabled(e.name);
        discoveryStatus(e);
        if (!e.tools.isEmpty() && !Integrations.isEnabled(e.name)) discoveryStatus(e);
        if (Integrations.fieldsOf(e).isEmpty()) {
            System.out.println(DIM + "no settings page — " + e.name + " declares none" + RESET);
        } else {
            integrationSettings(e);
        }
        integrationsMenu();
    }

    private static void discoveryStatus(Integrations.Entry e) {
        System.out.println((Integrations.isEnabled(e.name)
                ? DIM + "enabled (restart the bots to load its tools)" + RESET
                : DIM + "disabled" + RESET) + " · " + e.name);
    }


    private static void editEnvFile() {
        Path env = Env.baseDir().resolve(".env");
        try {
            if (!Files.exists(env)) {
                var ex = Env.baseDir().resolve("example.env");
                if (Files.exists(ex)) Files.copy(ex, env); else Files.createFile(env);
            }
        } catch (IOException e) {
            System.out.println("error preparing .env: " + e.getMessage());
            return;
        }
        restore();
        try {
            new ProcessBuilder("/bin/sh", "-c",
                    "${EDITOR:-${VISUAL:-nano}} \"$0\"", env.toString()).inheritIO().start().waitFor();
        } catch (Exception e) {
            System.out.println("couldn't launch $EDITOR: " + e.getMessage());
        }
        Env.load();
        raw();
    }

    // ── systemd service ──────────────────────────────────────────────────────

    private static void serviceMenu() {
        String[] items = {
                "Install — start the bots at login (systemd user unit)",
                "Manage a profile — install for --profile <id>",
                "Linger — start at true boot, before login",
                "Stop the bots (service stop)",
                "Uninstall the service",
                "Back"};
        String status = serviceStatusString();
        int pick = menu(header() + "\n" + status, items);
        String bin = launcherPath();
        try {
            restore();
            switch (pick) {
                case 0 -> sh(bin + " service install");
                case 1 -> {
                    restore();
                    System.out.print("profile id: "); System.out.flush();
                    String id = new java.util.Scanner(System.in).nextLine().strip();
                    sh(bin + " service install --profile '" + id.replace("'", "") + "'");
                }
                case 2 -> sh(bin + " service linger");
                case 3 -> sh(bin + " service stop");
                case 4 -> sh(bin + " service uninstall");
                default -> {}
            }
            if (pick >= 0 && pick <= 4) {
                System.out.println("\npress Enter…");
                System.in.read();
            }
        } catch (Exception e) {
            System.out.println("failed: " + e.getMessage());
        }
        Env.load();
        raw();
    }

    private static void sh(String cmd) throws Exception {
        new ProcessBuilder("/bin/sh", "-c", cmd).inheritIO().start().waitFor();
    }

    private static String serviceStatusString() {
        Path ud = Path.of(System.getProperty("user.home"), ".config/systemd/user");
        return String.format("  installed: %s   enabled: %s   active: %s%n",
                Files.exists(ud.resolve("systemc.service")) ? "yes" : "no",
                run("systemctl", "--user", "is-enabled", "systemc.service"),
                run("systemctl", "--user", "is-active", "systemc.service"));
    }

    private static String launcherPath() {
        try {
            var f = new java.io.File(Tui.class.getProtectionDomain().getCodeSource()
                    .getLocation().toURI()).getParentFile();                 // lib/
            var launcher = new java.io.File(f.getParentFile(), "bin/systemc"); // bin/systemc
            if (launcher.canExecute()) return launcher.getAbsolutePath();
        } catch (Exception ignored) {}
        return "systemc";
    }

    private static String run(String... cmd) {
        try {
            var p = new ProcessBuilder(cmd).redirectErrorStream(true).start();
            String out = "";
            try (var s = new java.util.Scanner(p.getInputStream()).useDelimiter("\\A")) {
                out = s.hasNext() ? s.next().trim() : "";
            }
            return out.replace("unit systemc.service", "").replace("unit ", "");
        } catch (IOException e) { return "n/a"; }
    }

    // ── terminal plumbing ────────────────────────────────────────────────────

    private static void raw() {
        try { new ProcessBuilder("/bin/sh", "-c", "stty -icanon -echo < /dev/tty").start().waitFor(); }
        catch (Exception ignored) {}
        System.out.print("\033[?25l"); System.out.flush();
    }

    private static void restore() {
        try { new ProcessBuilder("/bin/sh", "-c", "stty sane < /dev/tty").start().waitFor(); }
        catch (Exception ignored) {}
        System.out.print("\033[?25h"); System.out.flush();
    }

    /** Reads one logical key: arrow keys (CSI A/B/C/D), plain chars, Enter. */
    private static int readKey() {
        if (in == null) in = new InputStreamReader(System.in, StandardCharsets.UTF_8);
        try {
            int c = in.read();
            if (c != 27) return c;
            in.mark(1);
            int n1 = in.read();
            if (n1 != '[') return 27;              // bare Esc
            int n2 = in.read();
            return switch (n2) {                    // CSI arrow keys
                case 'A' -> 'A'; case 'B' -> 'B'; case 'C' -> 'C'; case 'D' -> 'D';
                default -> n2;
            };
        } catch (IOException e) { return -1; }
    }
}
