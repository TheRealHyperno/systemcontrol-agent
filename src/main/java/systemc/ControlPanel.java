package systemc;

import com.formdev.flatlaf.FlatLightLaf;
import com.formdev.flatlaf.themes.FlatMacLightLaf;

import javax.swing.*;
import javax.swing.border.EmptyBorder;
import java.awt.*;
import java.io.*;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.*;
import java.util.List;

/**
 * Port of control_panel.py — but this time a Swing desktop app themed to feel
 * like macOS (FlatLaf "macOS Light", big rounded cards, traffic-light accents).
 *
 * Tabs:
 *   Overview      status, quick actions, live log of this panel
 *   Setup         first-run configuration: tokens, channels, bot modes, wake words,
 *                 Ollama local/cloud + key pool, model picker, workspace/account
 *   Profiles      multi-instance cards: new / import / export / migrate / deps
 *   Personalize   name / gender / pronouns / system prompt (AGENT_* in .env)
 *   Dependencies  environment health + per-profile pip packages (Pull Dependencies)
 *   Run & Logs    start/stop instances, auto-restart-on-crash, log tailing
 *   Approvals     resolve destructive-command requests from .approvals/
 *   Integrations  java SPI + python-compat integrations and their settings menus
 *   Sessions      per-channel histories, reset button
 *   Updates       check GitHub releases, Update now, Roll back, pre-releases
 */
public final class ControlPanel extends JFrame {
    final JTextArea logArea = new JTextArea();
    final javax.swing.Timer uiTimer;
    final JPanel approvalsList = new JPanel();
    final DefaultListModel<String> sessionsModel = new DefaultListModel<>();
    final Map<String, Map<String, Object>> sessionsByKey = new LinkedHashMap<>();

    public ControlPanel() {
        super("SystemC — Control Panel");
        setDefaultCloseOperation(EXIT_ON_CLOSE);
        setSize(980, 680);
        setMinimumSize(new Dimension(860, 560));
        setLocationByPlatform(true);
        setWindowIcons();

        // macOS-style chrome
        getRootPane().putClientProperty("JRootPane.titleBarStyle", "none");
        getRootPane().putClientProperty("FlatLaf.styleClass", "title");

        var tabs = new JTabbedPane();
        tabs.addTab("Overview", overviewTab());
        tabs.addTab("Setup", setupTab());
        tabs.addTab("Profiles", profilesTab());
        tabs.addTab("Personalize", personalizeTab());
        tabs.addTab("Dependencies", dependenciesTab());
        tabs.addTab("Run & Logs", runTab());
        tabs.addTab("Approvals", approvalsTab());
        tabs.addTab("Integrations", integrationsTab());
        tabs.addTab("Sessions", sessionsTab());
        tabs.addTab("Updates", updatesTab());
        add(tabs);

        // pipe our own stdout into the log area, and refresh lists periodically
        uiTimer = new javax.swing.Timer(1500, e -> {
            refreshSessions();
            refreshApprovals();
            refreshIntegrations();
            runTick();
        });
        uiTimer.start();
    }

    // ── helpers ──────────────────────────────────────────────────────────
    /** Branding: favicon PNG as window/taskbar icon (multi-size). */
    void setWindowIcons() {
        try (var in = ControlPanel.class.getResourceAsStream("/favicon.png")) {
            if (in == null) return;
            var logo = new ImageIcon(in.readAllBytes()).getImage();
            var images = new java.util.ArrayList<java.awt.Image>();
            for (int s : java.util.List.of(16, 24, 48, 128, 256)) {
                images.add(logo.getScaledInstance(s, s, java.awt.Image.SCALE_SMOOTH));
            }
            setIconImages(images);
        } catch (Exception ignored) {}
    }

    static JPanel card(String title, JComponent content) {
        var p = new JPanel(new BorderLayout(8, 8));
        p.setBackground(new Color(0xFFFFFF));
        p.setBorder(BorderFactory.createCompoundBorder(
                new EmptyBorder(12, 14, 12, 14),
                BorderFactory.createLineBorder(new Color(0xE3E3E6), 1, true)));
        var lbl = new JLabel(title);
        lbl.setFont(new Font(Font.SANS_SERIF, Font.BOLD, 14));
        p.add(lbl, BorderLayout.NORTH);
        p.add(content, BorderLayout.CENTER);
        return p;
    }

    void log(String s) {
        SwingUtilities.invokeLater(() -> {
            logArea.append(s + "\n");
            logArea.setCaretPosition(logArea.getDocument().getLength());
        });
    }

    // ── Overview ─────────────────────────────────────────────────────────
    JComponent overviewTab() {
        var actions = new JPanel(new GridLayout(0, 1, 8, 8));
        var watchDir = new JButton("Open workspace");
        watchDir.addActionListener(e -> {
            try {
                if (Desktop.isDesktopSupported()) Desktop.getDesktop().open(Tools.WORKDIR.toFile());
            } catch (Exception ex) { log("⚠ " + ex); }
        });
        var launchConsole = new JButton("Launch web UI in browser");
        launchConsole.addActionListener(e -> {
            try {
                Desktop.getDesktop()
                        .browse(new java.net.URI("http://127.0.0.1:" + Env.getInt("WEBUI_PORT", WebUi.PORT)));
            } catch (Exception ex) { log("⚠ " + ex); }
        });
        var editEnv = new JButton("Edit .env");
        editEnv.addActionListener(e -> {
            try {
                Desktop.getDesktop().open(Env.baseDir().resolve(".env").toFile());
            } catch (Exception ex) { log("⚠ " + ex); }
        });
        actions.add(watchDir);
        actions.add(launchConsole);
        actions.add(editEnv);

        logArea.setEditable(false);
        logArea.setFont(new Font(Font.MONOSPACED, Font.PLAIN, 12));
        var scroll = new JScrollPane(logArea);
        scroll.setPreferredSize(new Dimension(500, 260));

        var root = new JPanel(new BorderLayout(10, 10));
        root.setBorder(new EmptyBorder(14, 14, 14, 14));
        root.add(brandHeader(), BorderLayout.NORTH);
        root.add(card("Quick actions", actions), BorderLayout.CENTER);
        root.add(card("Live log (this console)", scroll), BorderLayout.SOUTH);
        return root;
    }

    /** The full "SYSTEM ⊙C" logo, scaled to a slim banner for the Overview tab. */
    JComponent brandHeader() {
        try (var in = ControlPanel.class.getResourceAsStream("/logo.png")) {
            if (in == null) return new JLabel();
            var icon = new ImageIcon(in.readAllBytes()).getImage();
            var scaled = icon.getScaledInstance(120, 120, java.awt.Image.SCALE_SMOOTH);
            var img = new JLabel(new ImageIcon(scaled));
            var txt = new JLabel("<html><b style='font-size:15px'>SystemC Drovex</b><br>"
                    + "<span style='color:#6e6e73'>Java port — bots, approvals and profiles,<br>"
                    + "all in one control room.</span></html>");
            var p = new JPanel(new BorderLayout(12, 0));
            p.setOpaque(false);
            p.add(img, BorderLayout.WEST);
            p.add(txt, BorderLayout.CENTER);
            return p;
        } catch (Exception e) {
            return new JLabel();
        }
    }

    // ── Sessions ─────────────────────────────────────────────────────────
    JComponent sessionsTab() {
        var list = new JList<>(sessionsModel);
        list.setSelectionMode(ListSelectionModel.SINGLE_SELECTION);
        var resetBtn = new JButton("Reset selected session");
        resetBtn.addActionListener(e -> {
            String sel = list.getSelectedValue();
            if (sel == null) return;
            String key = sel.split(" ")[0];
            try {
                var result = Sessions.reset(key);
                list.clearSelection();
                log("Reset " + key + " → cleared " + result.get("cleared_turns") + " turn(s)");
            } catch (Exception ex) {
                log("⚠ " + ex.getMessage());
            }
        });
        var root = new JPanel(new BorderLayout(10, 10));
        root.setBorder(new EmptyBorder(14, 14, 14, 14));
        root.add(new JScrollPane(list), BorderLayout.CENTER);
        root.add(resetBtn, BorderLayout.SOUTH);
        return root;
    }

    void refreshSessions() {
        var rows = Sessions.listSessions();
        sessionsByKey.clear();
        sessionsModel.clear();
        java.time.format.DateTimeFormatter f =
                java.time.format.DateTimeFormatter.ofPattern("MM-dd HH:mm");
        for (var s : rows) {
            sessionsByKey.put((String) s.get("key"), s);
            String when = java.time.Instant.ofEpochMilli(((Number) s.get("last_active")).longValue())
                    .atZone(java.time.ZoneId.systemDefault()).format(f);
            sessionsModel.addElement(s.get("key") + "  —  " + s.get("label") + "  ·  "
                    + s.get("turns") + " turns · " + when + " · " + s.get("preview"));
        }
    }

    // ── Approvals ────────────────────────────────────────────────────────
    JComponent approvalsTab() {
        approvalsList.setLayout(new BoxLayout(approvalsList, BoxLayout.Y_AXIS));
        approvalsList.setBackground(new Color(0xFFFFFF));
        var root = new JPanel(new BorderLayout());
        root.setBorder(new EmptyBorder(14, 14, 14, 14));
        root.add(new JScrollPane(approvalsList), BorderLayout.CENTER);
        return root;
    }

    void refreshApprovals() {
        var pending = Approvals.listPending();
        approvalsList.removeAll();
        if (pending.isEmpty()) {
            var l = new JLabel("No pending approvals.");
            l.setBorder(new EmptyBorder(10, 10, 10, 10));
            approvalsList.add(l);
        }
        for (var p : pending) {
            String id = String.valueOf(p.get("id"));
            String cmd = String.valueOf(p.get("command"));
            var body = new JTextArea(cmd);
            body.setLineWrap(true);
            body.setWrapStyleWord(true);
            body.setEditable(false);
            body.setFont(new Font(Font.MONOSPACED, Font.PLAIN, 12));
            var p2 = new JPanel(new BorderLayout(8, 8));
            p2.setBackground(Color.WHITE);
            p2.setBorder(BorderFactory.createCompoundBorder(
                    new EmptyBorder(8, 8, 8, 8),
                    BorderFactory.createLineBorder(new Color(0xE3E3E6), 1, true)));
            var header = new JLabel("Approval request " + id);
            header.setFont(header.getFont().deriveFont(Font.BOLD));
            var btns = new JPanel(new FlowLayout(FlowLayout.RIGHT));
            var ok = new JButton("✅ Approve");
            var no = new JButton("❌ Deny");
            ok.addActionListener(e -> { Approvals.resolve(id, true); refreshApprovals(); });
            no.addActionListener(e -> { Approvals.resolve(id, false); refreshApprovals(); });
            btns.add(ok);
            btns.add(no);
            p2.add(header, BorderLayout.NORTH);
            p2.add(new JScrollPane(body), BorderLayout.CENTER);
            p2.add(btns, BorderLayout.SOUTH);
            approvalsList.add(p2);
        }
        approvalsList.revalidate();
        approvalsList.repaint();
    }

    // ── Profiles (multi-instance manager) ────────────────────────────────
    // Each profile = one entry in .profiles/profiles.json + its own .env overlay at
    // .profiles/<id>.env (+ optional data dirs). python parity: profile_transfer.py.

    final JPanel profilesCards = new JPanel();

    JComponent profilesTab() {
        profilesCards.setLayout(new BoxLayout(profilesCards, BoxLayout.Y_AXIS));
        profilesCards.setBackground(new Color(0xF5F5F7));
        var buttons = new JPanel(new FlowLayout(FlowLayout.RIGHT, 8, 0));
        var bNew = new JButton("New profile…");
        bNew.addActionListener(e -> profileNewDialog());
        var bImport = new JButton("Import archive…");
        bImport.addActionListener(e -> profileImportDialog());
        var bMigrate = new JButton("Migrate .env…");
        bMigrate.addActionListener(e -> profileMigrateDialog());
        var bOpen = new JButton("Open .profiles folder");
        bOpen.addActionListener(e -> openFolder(Env.baseDir().resolve(".profiles")));
        buttons.add(bNew);
        buttons.add(bImport);
        buttons.add(bMigrate);
        buttons.add(bOpen);

        var root = new JPanel(new BorderLayout(10, 10));
        root.setBorder(new EmptyBorder(14, 14, 14, 14));
        var north = new JPanel(new BorderLayout());
        north.add(new JLabel("<html><body style='width:820px'><b>Profiles</b> — run as many bots as you "
                + "like. Each profile has its own .env (tokens, keys, model), its own integrations, "
                + "memory and chat history. The default profile keeps the original folders.</body></html>"),
                BorderLayout.NORTH);
        north.add(buttons, BorderLayout.SOUTH);
        root.add(north, BorderLayout.NORTH);
        root.add(new JScrollPane(profilesCards), BorderLayout.CENTER);
        rebuildProfilesCards();
        return root;
    }

    void rebuildProfilesCards() {
        profilesCards.removeAll();
        // card for the default instance first, then every profile
        addProfileCard(null, "default", "the main install of this panel (its own .env at the install root)");
        for (var p : Profiles.list()) {
            String id = String.valueOf(p.get("id"));
            String note = String.valueOf(p.getOrDefault("note", ""));
            addProfileCard(id, id, note);
        }
        var filler = new JPanel();
        filler.setOpaque(false);
        profilesCards.add(filler);
        profilesCards.revalidate();
        profilesCards.repaint();
    }

    void addProfileCard(String id, String title, String note) {
        boolean isDefault = id == null || id.equals("default");
        String model, platformInfo, depsInfo;
        Map<String, String> env = Profiles.readEnvFile(Profiles.envPath(isDefault ? null : id));
        if (env.isEmpty() && !isDefault) {
            model = String.valueOf(Env.get("AGENT_MODEL", "(main .env)"));
            platformInfo = "inherits the main .env until it gets its own";
        } else {
            model = env.getOrDefault("AGENT_MODEL", Env.get("AGENT_MODEL", ""));
            boolean d = !env.getOrDefault("DISCORD_BOT_TOKEN", "").strip().isEmpty();
            boolean s = !env.getOrDefault("STOAT_BOT_TOKEN", "").strip().isEmpty();
            platformInfo = (d ? "Discord " : "Discord– ") + "· " + (s ? "Stoat " : "Stoat– ");
        }
        String deps = env.getOrDefault("DEPENDENCIES", "");
        depsInfo = deps.isBlank() ? "no pip deps" : deps.split("[,;]").length + " pip deps";
        var mp = instanceManager.get(isDefault ? null : id);

        var head = new JLabel("<html><b>" + title + "</b>  <span style='color:#6e6e73'>model="
                + (model.isBlank() ? "(agent default)" : model) + " · " + platformInfo + " · " + depsInfo
                + (mp != null ? " · <b>RUNNING</b>" : "") + "</span>"
                + (note.isBlank() ? "" : "<br>" + note) + "</html>");
        var btns = new JPanel(new FlowLayout(FlowLayout.RIGHT, 6, 2));
        if (!isDefault) {
            var start = new JButton("Start");
            start.addActionListener(e -> startProfile(id));
            var stop = new JButton("Stop");
            stop.addActionListener(e -> stopProfile(id));
            var depsPull = new JButton("Pull Dependencies");
            depsPull.addActionListener(e -> background(() -> {
                try {
                    String out = Profiles.pullDependencies(id);
                    log("[deps/" + id + "] " + out);
                } catch (Exception ex) {
                    log("⚠ deps/" + id + " failed: " + ex.getMessage());
                }
            }));
            var export = new JButton("Export…");
            export.addActionListener(e -> profileExportDialog(id));
            var editEnv = new JButton("Edit .env");
            editEnv.addActionListener(e -> openFolder(Profiles.envPath(id)));
            var delete = new JButton("Delete");
            delete.addActionListener(e -> profileDeleteDialog(id));
            btns.add(start);
            btns.add(stop);
            btns.add(depsPull);
            btns.add(export);
            btns.add(editEnv);
            btns.add(delete);
        } else {
            var start = new JButton("Start");
            start.addActionListener(e -> startProfile(null));
            var stop = new JButton("Stop");
            stop.addActionListener(e -> stopProfile(null));
            var webui = new JButton("Open web UI");
            webui.addActionListener(e -> openWebUi());
            btns.add(start);
            btns.add(stop);
            btns.add(webui);
        }
        var card = new JPanel(new BorderLayout(8, 4));
        card.setBackground(Color.WHITE);
        card.setBorder(BorderFactory.createCompoundBorder(
                new EmptyBorder(8, 10, 8, 10),
                BorderFactory.createLineBorder(new Color(0xE3E3E6), 1, true)));
        card.add(head, BorderLayout.CENTER);
        card.add(btns, BorderLayout.SOUTH);
        card.setAlignmentX(0f);
        profilesCards.add(card);
        profilesCards.add(Box.createVerticalStrut(8));
    }

    String selectedProfileId() {
        // profile cards own their listeners; selection lives only in dialogs
        return null;
    }

    // ── profile dialogs (new / import / export / migrate / delete) ───────────

    void profileNewDialog() {
        var idField = new JTextField();
        var modelField = new JTextField(Env.get("AGENT_MODEL", ""));
        var seed = new JCheckBox("Copy basic keys from the main .env (no tokens)", true);
        var panel = new JPanel(new GridLayout(0, 2, 8, 8));
        panel.add(new JLabel("Profile id ([A-Za-z0-9_-])")); panel.add(idField);
        panel.add(new JLabel("AGENT_MODEL"));               panel.add(modelField);
        panel.add(seed); panel.add(new JLabel(""));
        while (JOptionPane.showConfirmDialog(this, panel, "New profile",
                JOptionPane.OK_CANCEL_OPTION, JOptionPane.PLAIN_MESSAGE) == JOptionPane.OK_OPTION) {
            String id = idField.getText().strip();
            if (!id.matches("[A-Za-z0-9_-]+")) {
                JOptionPane.showMessageDialog(this, "id must be [A-Za-z0-9_-]"); continue;
            }
            if (Profiles.get(id) != null) {
                JOptionPane.showMessageDialog(this, "a profile named '" + id + "' already exists");
                continue;
            }
            var patch = new HashMap<String, Object>();
            if (!modelField.getText().isBlank()) patch.put("AGENT_MODEL", modelField.getText().strip());
            if (patch.containsKey("AGENT_MODEL")) patch.put("note", "created via the panel");
            Profiles.upsert(id, patch);
            if (seed.isSelected()) {
                var env = new LinkedHashMap<String, String>();
                env.put("AGENT_MODEL", modelField.getText().strip());
                Profiles.writeEnvFile(Profiles.envPath(id), env);
            }
            log("profiles: created " + id + " (env at .profiles/" + id + ".env)");
            rebuildProfilesCards();
            break;
        }
    }

    void profileMigrateDialog() {
        var profiles = Profiles.list();
        if (profiles.isEmpty()) {
            infoBox("Create a profile first — there is nothing to migrate into.");
            return;
        }
        var ids = new String[profiles.size()];
        for (int i = 0; i < profiles.size(); i++) ids[i] = String.valueOf(profiles.get(i).get("id"));
        String pick = (String) JOptionPane.showInputDialog(this,
                "Copy the current main .env into which profile?\n(Overwrites that profile's env.)",
                "Migrate .env", JOptionPane.PLAIN_MESSAGE, null, ids, ids[0]);
        if (pick == null) return;
        try {
            Files.createDirectories(Env.baseDir().resolve(".profiles"));
            Files.copy(Env.baseDir().resolve(".env"), Profiles.envPath(pick),
                    java.nio.file.StandardCopyOption.REPLACE_EXISTING);
            // per-profile .env must not inherit the default profile's identity marker
            Profiles.writeEnvFile(Profiles.envPath(pick), Map.of("SYSTEMC_PROFILE_ID", pick));
            log("profiles: migrated main .env → profile " + pick);
            rebuildProfilesCards();
        } catch (IOException ex) {
            infoBox("migrate failed: " + ex.getMessage());
        }
    }

    void profileExportDialog(String id) {
        // parts checkboxes, python profile_transfer parity
        var envBox = new JCheckBox("Tokens & config (.env)", true);
        var integrations = new JCheckBox("Integrations (jars + .py + their settings)", true);
        var memory = new JCheckBox("Memory (.memory/)", true);
        var history = new JCheckBox("Chat history (.history/)", true);
        var workspace = new JCheckBox("Workspace files", false);
        var panel = new JPanel(new GridLayout(0, 1));
        panel.add(new JLabel("What goes into the archive?"));
        for (var c : new JCheckBox[]{envBox, integrations, memory, history, workspace}) panel.add(c);
        while (JOptionPane.showConfirmDialog(this, panel, "Export profile " + id,
                JOptionPane.OK_CANCEL_OPTION, JOptionPane.PLAIN_MESSAGE) == JOptionPane.OK_OPTION) {
            if (!envBox.isSelected() && !integrations.isSelected() && !memory.isSelected()
                    && !history.isSelected() && !workspace.isSelected()) {
                JOptionPane.showMessageDialog(this, "pick at least one part");
                continue;
            }
            if (envBox.isSelected()) {
                int choice = JOptionPane.showConfirmDialog(this,
                        "Heads-up: the .env part contains bot tokens and API keys.\n"
                                + "Store the archive somewhere safe — anyone with it can control your bots.",
                        "Secrets included", JOptionPane.OK_CANCEL_OPTION, JOptionPane.WARNING_MESSAGE);
                if (choice != JOptionPane.OK_OPTION) return;
            }
            var fc = new JFileChooser();
            fc.setSelectedFile(new java.io.File("systemc-profile-" + id + ".zip"));
            if (fc.showSaveDialog(this) != JFileChooser.APPROVE_OPTION) return;
            try {
                profileExportZip(id, fc.getSelectedFile().toPath(), envBox.isSelected(),
                        integrations.isSelected(), memory.isSelected(), history.isSelected(),
                        workspace.isSelected());
                log("profiles: exported " + id + " → " + fc.getSelectedFile().getName());
            } catch (Exception ex) {
                infoBox("export failed: " + ex.getMessage());
            }
            break;
        }
    }

    /** zips: systemc-profile-export/<id>/{profile.env + selected folders} + manifest.json */
    void profileExportZip(String id, Path dest, boolean envPart, boolean integrationsPart,
                          boolean memoryPart, boolean historyPart, boolean workspacePart) throws Exception {
        Files.createDirectories(dest.getParent() == null ? Path.of(".") : dest.getParent());
        try (var zf = new java.util.zip.ZipOutputStream(Files.newOutputStream(dest))) {
            String prefix = "systemc-profile-export/" + id + "/";
            zf.putNextEntry(new java.util.zip.ZipEntry(prefix + "manifest.json"));
            zf.write(Json.write(Map.of("app", "systemc-java", "id", id,
                    "version", Env.get("SYSTEMC_PROFILE_ID", id), "created",
                    java.time.Instant.now().toString(), "python_compat", true), true).getBytes());
            zf.closeEntry();
            if (envPart) zfPut(zf, prefix + "profile.env", Files.readAllBytes(Profiles.envPath(id)));
            if (integrationsPart) zfPutTree(zf, prefix + "integrations/", Integrations.dir());
            if (memoryPart) zfPutTree(zf, prefix + ".memory/", Env.baseDir().resolve(".memory"));
            if (historyPart) zfPutTree(zf, prefix + ".history/", Env.baseDir().resolve(".history"));
            if (workspacePart) zfPutTree(zf, prefix + "workspace/", Env.baseDir().resolve("workspace"));
        }
    }

    private static void zfPut(java.util.zip.ZipOutputStream zf, String name, byte[] bytes) throws IOException {
        zf.putNextEntry(new java.util.zip.ZipEntry(name));
        zf.write(bytes);
        zf.closeEntry();
    }

    private static void zfPutTree(java.util.zip.ZipOutputStream zf, String prefix, Path src) throws IOException {
        if (!Files.exists(src)) return;
        try (var st = Files.walk(src)) {
            for (Path p : (Iterable<Path>) st.filter(Files::isRegularFile).sorted().toList())
                zfPut(zf, prefix + src.relativize(p), Files.readAllBytes(p));
        }
    }

    void profileImportDialog() {
        var fc = new JFileChooser();
        fc.setFileFilter(new javax.swing.filechooser.FileNameExtensionFilter("SystemC profile archive", "zip"));
        if (fc.showOpenDialog(this) != JFileChooser.APPROVE_OPTION) return;
        try {
            Map<String, String> manifest = null;
            Map<String, String> env = null;
            var entries = new LinkedHashMap<String, byte[]>();
            try (var zf = new java.util.zip.ZipInputStream(Files.newInputStream(fc.getSelectedFile().toPath()))) {
                java.util.zip.ZipEntry e;
                while ((e = zf.getNextEntry()) != null) {
                    String n = e.getName();
                    byte[] data = zf.readAllBytes();
                    if (n.endsWith("manifest.json")) {
                        var m = Json.parse(new String(data));
                        if (m != null) manifest = Json.str(m, "app") == null ? null
                                : Map.of("app", Json.str(m, "app"), "id", String.valueOf(m.getOrDefault("id", "")));
                    }
                    if (n.endsWith("profile.env")) env = Profiles.readEnvBytes(data);
                    else if (!n.endsWith("/")) entries.put(n, data);
                    zf.closeEntry();
                }
            }
            if (env == null) { infoBox("not a SystemC profile archive (no profile.env)"); return; }
            String srcId = manifest == null ? "" : manifest.getOrDefault("id", "profile");
            String suggested = uniqueProfileId(srcId.isBlank() ? "imported" : srcId);
            var idField = new JTextField(suggested);
            var config = new JPanel(new GridLayout(0, 1));
            config.add(new JLabel("Import as profile id:"));
            config.add(idField);
            if (manifest != null && !"systemc-java".equals(manifest.get("app")))
                config.add(new JLabel("(python export — imported into the java port as-is)"));
            if (JOptionPane.showConfirmDialog(this, config, "Import profile",
                    JOptionPane.OK_CANCEL_OPTION, JOptionPane.PLAIN_MESSAGE) != JOptionPane.OK_OPTION) return;
            String id = idField.getText().strip();
            if (!id.matches("[A-Za-z0-9_-]+")) { infoBox("id must be [A-Za-z0-9_-]"); return; }

            // unzip the parts into the install tree
            for (var en : entries.entrySet()) {
                String name = en.getKey();
                String rest = name.replaceFirst("^systemc-profile-export/" + java.util.regex.Pattern
                        .quote(manifest == null ? "" : String.valueOf(manifest.getOrDefault("id", ""))) + "/", "");
                if (rest.equals(name)) rest = name;              // unknown layout: keep
                if (rest.startsWith("integrations/")) copyBytes(Integrations.dir().resolve(rest.substring("integrations/".length())), en.getValue());
                else if (rest.startsWith(".memory/")) copyBytes(Env.baseDir().resolve(rest), en.getValue());
                else if (rest.startsWith(".history/")) copyBytes(Env.baseDir().resolve(rest), en.getValue());
                else if (rest.startsWith("workspace/")) copyBytes(Env.baseDir().resolve(rest), en.getValue());
            }
            // env: strip the old identity marker, write ours
            var cleaned = new LinkedHashMap<String, String>();
            for (var e : env.entrySet())
                if (!e.getKey().equals("SYSTEMC_PROFILE_ID")) cleaned.put(e.getKey(), e.getValue());
            cleaned.put("SYSTEMC_PROFILE_ID", id);
            Profiles.writeEnvFile(Profiles.envPath(id), cleaned);
            Profiles.upsert(id, Map.of("note", "imported" + (manifest == null ? "" : " from " + manifest.get("id"))));
            log("profiles: imported " + id + " (" + cleaned.size() + " env keys, "
                    + entries.size() + " files) — check tokens, model and integrations");
            rebuildProfilesCards();
        } catch (Exception ex) {
            infoBox("import failed: " + ex.getMessage());
        }
    }

    /** flat file copy used by profile import (creates parent dirs). */
    void copyBytes(Path dst, byte[] bytes) {
        try {
            if (dst.getParent() != null) Files.createDirectories(dst.getParent());
            Files.write(dst, bytes);
        } catch (IOException ex) {
            log("⚠ couldn't write " + dst.getFileName() + ": " + ex.getMessage());
        }
    }

    void profileDeleteDialog(String id) {
        if (JOptionPane.showConfirmDialog(this, "Delete profile '" + id
                + "' and its env file? (Its started instance will stop.)",
                "Delete profile", JOptionPane.OK_CANCEL_OPTION, JOptionPane.WARNING_MESSAGE)
                != JOptionPane.OK_OPTION) return;
        stopProfile(id);
        Profiles.delete(id);
        try { Files.deleteIfExists(Profiles.envPath(id)); } catch (IOException ignored) {}
        log("profiles: deleted " + id);
        rebuildProfilesCards();
    }

    static String uniqueProfileId(String base) {
        String clean = base.replaceAll("[^A-Za-z0-9_-]", "-");
        String id = clean;
        for (int i = 1; Profiles.get(id) != null; i++) id = clean + "-" + i;
        return id;
    }


    // ── Integrations (java SPI + python-compat) ──────────────────────────

    final DefaultListModel<String> integrationsModel = new DefaultListModel<>();
    final Map<String, Integrations.Entry> integrationEntries = new LinkedHashMap<>();
    final JList<String> integrationsList = new JList<>(integrationsModel);

    JComponent integrationsTab() {
        integrationsList.setSelectionMode(ListSelectionModel.SINGLE_SELECTION);
        var hint = new JLabel("<html><body style='width:640px'><b>Integrations</b> give the agent extra tools. "
                + "Drop an integration <code>.jar</code> (SPI: implements <code>systemc.integrations.Integration</code>) "
                + "or a python-compat <code>.py</code> into the <code>integrations/</code> folder, then enable it here — "
                + "the agent sees its tools on the next bot start.</body></html>");
        var buttons = new JPanel(new FlowLayout(FlowLayout.RIGHT, 8, 0));
        var toggle = new JButton("Enable / disable");
        toggle.addActionListener(e -> {
            var ent = selectedIntegration();
            if (ent == null) return;
            Integrations.toggleEnabled(ent.name);
            log("integrations: toggled " + ent.name + " (restart the bots to apply)");
        });
        var settings = new JButton("Settings…");
        settings.addActionListener(e -> {
            var ent = selectedIntegration();
            if (ent == null) return;
            if (Integrations.fieldsOf(ent).isEmpty()) {
                log("integrations: " + ent.name + " has no settings page");
                return;
            }
            showIntegrationSettings(ent);
        });
        var install = new JButton("Install from .jar…");
        install.addActionListener(e -> {
            var fc = new JFileChooser();
            fc.setFileFilter(new javax.swing.filechooser.FileNameExtensionFilter("Integration .jar", "jar"));
            if (fc.showOpenDialog(this) != JFileChooser.APPROVE_OPTION) return;
            try {
                var jar = fc.getSelectedFile().toPath();
                Files.copy(jar, Integrations.dir().resolve(jar.getFileName()),
                        java.nio.file.StandardCopyOption.REPLACE_EXISTING);
                Integrations.discover(true);
                refreshIntegrations();
                log("integrations: installed " + jar.getFileName() + " (restart the bots to apply)");
            } catch (Exception ex) { logString(ex.getMessage()); }
        });
        var openFolder = new JButton("Open folder");
        openFolder.addActionListener(e -> {
            try {
                if (Desktop.isDesktopSupported()) Desktop.getDesktop().open(Integrations.dir().toFile());
            } catch (Exception ex) { logString(ex.getMessage()); }
        });
        var refresh = new JButton("Reload");
        refresh.addActionListener(e -> { Integrations.discover(true); refreshIntegrations(); });
        buttons.add(toggle);
        buttons.add(settings);
        buttons.add(install);
        buttons.add(openFolder);
        buttons.add(refresh);

        var all = new JPanel(new BorderLayout(10, 4));
        all.add(hint, BorderLayout.NORTH);
        all.add(buttons, BorderLayout.SOUTH);

        var root = new JPanel(new BorderLayout(10, 10));
        root.setBorder(new EmptyBorder(14, 14, 14, 14));
        root.add(all, BorderLayout.NORTH);
        root.add(new JScrollPane(integrationsList), BorderLayout.CENTER);
        return root;
    }

    void logString(String s) { log("⚠ " + s); }

    Integrations.Entry selectedIntegration() {
        int i = integrationsList.getSelectedIndex();
        if (i < 0 || i >= integrationEntries.size()) return null;
        var row = integrationsModel.get(i);
        for (var e : integrationEntries.values()) {
            if (row.startsWith(e.name + " ") || row.startsWith(e.name + "\t")) return e;
        }
        return null;
    }

    void refreshIntegrations() {
        var entries = Integrations.discover(false);
        var enabled = new HashSet<>(Integrations.enabledNames(entries));
        integrationEntries.clear();
        integrationsModel.clear();
        for (var e : entries.values()) {
            if (e.tools == null) continue;
            integrationEntries.put(e.name, e);
            integrationsModel.addElement(String.format("%-12s  %s    %s    %s", e.name,
                    enabled.contains(e.name) ? "✔ enabled" : "  disabled",
                    e.tools.size() + " tool(s)", e.summary()));
        }
    }

    static boolean truthy(String v) {
        v = v == null ? "" : v.toLowerCase().strip();
        return List.of("1", "true", "yes", "on").contains(v);
    }

    /** One settings dialog generated from the integration's panel() descriptor. */
    void showIntegrationSettings(Integrations.Entry e) {
        var values = Integrations.currentSettings(e);
        var ui = new LinkedHashMap<String, JComponent>();
        var dia = new JDialog(this, "Integrations — " + Integrations.menuTitle(e), true);
        var panel = new JPanel(new GridBagLayout());
        panel.setBorder(new EmptyBorder(12, 14, 12, 14));
        var c = new GridBagConstraints();
        c.gridx = 0; c.gridy = 0; c.insets = new Insets(4, 4, 4, 4); c.anchor = GridBagConstraints.WEST;
        int row = 0;
        for (var f : Integrations.fieldsOf(e)) {
            c.gridy = row++;
            c.gridx = 0;
            panel.add(new JLabel(f.label()), c);
            c.gridx = 1;
            JComponent field;
            switch (f.type()) {
                case "bool" -> {
                    var box = new JCheckBox();
                    box.setSelected(values.containsKey(f.key()) && !values.get(f.key()).isBlank()
                            ? truthy(values.get(f.key())) : truthy(f.def()));
                    field = box;
                }
                case "choice" -> {
                    var combo = new JComboBox<String>();
                    if (f.choices() != null) for (String s : f.choices()) combo.addItem(s);
                    combo.setSelectedItem(values.getOrDefault(f.key(), f.def()));
                    field = combo;
                }
                case "secret" -> field = new JPasswordField(values.getOrDefault(f.key(), f.def()));
                default -> field = new JTextField(values.getOrDefault(f.key(), f.def()));
            }
            ui.put(f.key(), field);
            panel.add(field, c);
            if (!f.description().isEmpty()) {
                c.gridy = row++;
                c.gridx = 1;
                var d = new JLabel("<html><body style='width:380px;color:#6e6e73'>" + f.description() + "</body></html>");
                d.setFont(d.getFont().deriveFont(11f));
                panel.add(d, c);
            }
        }
        var save = new JButton("Save to .env");
        save.addActionListener(ev -> {
            for (var f : Integrations.fieldsOf(e)) {
                JComponent comp = ui.get(f.key());
                String v;
                if (comp instanceof JCheckBox box) v = box.isSelected() ? "true" : "false";
                else if (comp instanceof JComboBox<?> box) v = String.valueOf(box.getSelectedItem());
                else if (comp instanceof JPasswordField pf) v = new String(pf.getPassword());
                else if (comp instanceof JTextField tf) v = tf.getText().strip();
                else if (comp != null) v = String.valueOf(comp);
                else v = values.getOrDefault(f.key(), f.def());
                values.put(f.key(), v == null ? "" : v);
            }
            Integrations.saveSettings(e, values);
            log("integrations: saved " + e.name + " settings to .env — restart the bots to apply");
            dia.dispose();
        });
        var cancel = new JButton("Cancel");
        cancel.addActionListener(ev -> dia.dispose());
        var bottom = new JPanel(new FlowLayout(FlowLayout.RIGHT));
        bottom.add(cancel);
        bottom.add(save);
        var root = new JPanel(new BorderLayout());
        root.add(new JScrollPane(panel), BorderLayout.CENTER);
        root.add(bottom, BorderLayout.SOUTH);
        dia.setContentPane(root);
        dia.setSize(700, 440);
        dia.setLocationRelativeTo(this);
        dia.setVisible(true);
    }

    JComponent personalizeTab() {
        var name = new JTextField(Env.get("AGENT_NAME", cred()));
        var gender = new JTextField(Env.get("AGENT_GENDER"));
        var pronouns = new JTextField(Env.get("AGENT_PRONOUNS"));
        var prompt = new JTextArea(Env.get("AGENT_SYSTEM_PROMPT", Prompt.DEFAULT_PROMPT_TEMPLATE), 16, 60);
        prompt.setLineWrap(true);
        var casual = new JCheckBox("Casual tone (AGENT_CASUAL_TONE)", Env.getBool("AGENT_CASUAL_TONE", false));

        var saveBtn = new JButton("Save to .env");
        saveBtn.addActionListener(e -> {
            Env.set("AGENT_NAME", name.getText().strip());
            Env.set("AGENT_GENDER", gender.getText().strip());
            Env.set("AGENT_PRONOUNS", pronouns.getText().strip());
            Env.set("AGENT_SYSTEM_PROMPT", prompt.getText());
            Env.set("AGENT_CASUAL_TONE", casual.isSelected() ? "1" : "0");
            Env.set("AGENT_ALLOW_ELEVATED", Env.getBool("AGENT_ALLOW_ELEVATED", Tools.elevatedEnabled()) ? "1" : "0");
            log("saved AGENT_* to .env — restart the bots to apply");
        });

        var grid = new JPanel(new GridBagLayout());
        var c = new GridBagConstraints();
        c.fill = GridBagConstraints.HORIZONTAL;
        c.insets = new Insets(4, 4, 4, 4);
        int row = 0;
        c.gridx = 0; c.gridy = row; grid.add(new JLabel("Agent name ({NAME})"), c);
        c.gridx = 1; grid.add(name, c); row++;
        c.gridx = 0; c.gridy = row; grid.add(new JLabel("Gender ({GENDER})"), c);
        c.gridx = 1; grid.add(gender, c); row++;
        c.gridx = 0; c.gridy = row; grid.add(new JLabel("Pronouns ({PRONOUNS})"), c);
        c.gridx = 1; grid.add(pronouns, c); row++;
        c.gridx = 0; c.gridy = row; c.gridwidth = 2; grid.add(casual, c); row++;
        c.gridx = 0; c.gridy = row; c.gridwidth = 2; grid.add(new JScrollPane(prompt), c); row++;
        c.gridx = 0; c.gridy = row; c.gridwidth = 2; grid.add(saveBtn, c);

        var root = new JPanel(new BorderLayout());
        root.setBorder(new EmptyBorder(14, 14, 14, 14));
        root.add(grid, BorderLayout.CENTER);
        return root;
    }

    // ── Setup (first-run configuration, python "1. Setup" parity) ────────

    JComponent setupTab() {
        var pad = new EmptyBorder(14, 14, 14, 14);

        // Discord / Stoat discriminator: token, channels, mode, wake word
        var discordToken = new JPasswordField(Env.get("DISCORD_BOT_TOKEN"));
        var discordChannels = new JTextField(Env.get("ALLOWED_CHANNEL_IDS"));
        var discordAll = new JCheckBox("All channels", Env.get("ALLOWED_CHANNEL_IDS").isBlank());
        var discordMode = radioRow(new String[]{"WAKEWORD", "MENTION", "EVERYTHING"},
                Env.get("BOT_MODE", "WAKEWORD"));
        var discordWake = new JTextField(Env.get("WAKE_WORD", "hey claw"));
        discordChannels.setEnabled(!discordAll.isSelected());

        var stoatToken = new JPasswordField(Env.get("STOAT_BOT_TOKEN"));
        var stoatChannels = new JTextField(Env.get("STOAT_ALLOWED_CHANNEL_IDS"));
        var stoatAll = new JCheckBox("All channels", Env.get("STOAT_ALLOWED_CHANNEL_IDS").isBlank());
        var stoatMode = radioRow(new String[]{"WAKEWORD", "MENTION", "EVERYTHING"},
                Env.get("STOAT_BOT_MODE", "WAKEWORD"));
        var stoatWake = new JTextField(Env.get("STOAT_WAKE_WORD", "hey claw"));
        stoatChannels.setEnabled(!stoatAll.isSelected());
        discordAll.addActionListener(ev -> discordChannels.setEnabled(!discordAll.isSelected()));
        stoatAll.addActionListener(ev -> stoatChannels.setEnabled(!stoatAll.isSelected()));

        // model backend: local vs cloud key pool + model picker
        boolean cloudCurrently = "cloud".equalsIgnoreCase(Env.get("OLLAMA_MODE", ""))
                || !Env.get("OLLAMA_API_KEY", "").isBlank();
        var cloudRadio = new JRadioButton("Ollama Cloud (ollama.com + API key)", cloudCurrently);
        var localRadio = new JRadioButton("Local Ollama", !cloudCurrently);
        var backendGroup = new ButtonGroup();
        backendGroup.add(localRadio);
        backendGroup.add(cloudRadio);
        var ollamaHost = new JTextField(Env.get("OLLAMA_HOST", "http://localhost:11434"));

        // key pool: index 0 = primary (OLLAMA_API_KEY), rest = OLLAMA_API_KEYS fallback order
        var keyPoolModel = new DefaultListModel<String>();
        var keyPool = new JList<>(keyPoolModel);
        keyPool.setVisibleRowCount(4);
        if (!Env.get("OLLAMA_API_KEY").isBlank()) keyPoolModel.addElement(Env.get("OLLAMA_API_KEY"));
        for (String k : Env.get("OLLAMA_API_KEYS").split("[,;]"))
            if (!k.isBlank()) keyPoolModel.addElement(k);
        var keyInput = new JTextField();
        var modelCombo = new JComboBox<String>();
        modelCombo.setEditable(true);
        var currentModel = Env.get("AGENT_MODEL");
        if (!currentModel.isBlank()) modelCombo.addItem(currentModel);

        var modelStatus = new JLabel(" ");
        Runnable fetchModels = () -> background(() -> {
            try {
                List<String> models;
                if (localRadio.isSelected()) {
                    models = ollamaListOrHttp(ollamaHost.getText().strip());
                } else {
                    String primary = keyPoolModel.isEmpty() ? "" : keyPoolModel.get(0);
                    models = Providers.listOllamaModels(Providers.OLLAMA_CLOUD_HOST, primary);
                }
                SwingUtilities.invokeLater(() -> {
                    var selected = modelCombo.getEditor().getItem();
                    var seen = new HashSet<String>();
                    modelCombo.removeAllItems();
                    for (String m : models) if (seen.add(m)) modelCombo.addItem(m);
                    if (selected != null && !String.valueOf(selected).isBlank()) {
                        modelCombo.setSelectedItem(selected);
                        if (!seen.contains(String.valueOf(selected))) modelCombo.addItem(String.valueOf(selected));
                    }
                    modelStatus.setText("✓ " + models.size() + " model(s) available");
                });
            } catch (Exception ex) {
                SwingUtilities.invokeLater(() -> modelStatus.setText("⚠ " + ex.getMessage()));
            }
        });
        var fetchBtn = new JButton("Fetch available models");
        fetchBtn.addActionListener(e -> fetchModels.run());

        // workspace & account
        String runAsCurrent = Env.get("AGENT_RUN_AS_USER");
        String workdirCurrent = Env.get("AGENT_WORKDIR", Env.baseDir().resolve("workspace").toString());
        var modeSelf = new JRadioButton("This account (" + System.getProperty("user.name") + ")", runAsCurrent.isBlank());
        var modeOther = new JRadioButton("Another user account", !runAsCurrent.isBlank());
        var accountGroup = new ButtonGroup();
        accountGroup.add(modeSelf);
        accountGroup.add(modeOther);
        var runAsUser = new JTextField(runAsCurrent);
        var workdir = new JTextField(workdirCurrent);
        var elevated = new JCheckBox("Allow running elevated? (sudo — the agent still asks in chat first)",
                Env.getBool("AGENT_ALLOW_ELEVATED", false));
        runAsUser.setEnabled(!runAsCurrent.isBlank());
        modeSelf.addActionListener(ev -> runAsUser.setEnabled(false));
        modeOther.addActionListener(ev -> runAsUser.setEnabled(true));
        var setupStatus = new JLabel(" ");

        var setupDirBtn = new JButton("Set up / Verify directory");
        setupDirBtn.addActionListener(e -> {
            String wd = workdir.getText().strip();
            background(() -> {
                String msg = setupDirectory(modeOther.isSelected() ? runAsUser.getText().strip() : "", wd);
                SwingUtilities.invokeLater(() -> setupStatus.setText(msg));
            });
        });
        var grantBtn = new JButton("Allow panel to run as user");
        grantBtn.addActionListener(e -> {
            String user = runAsUser.getText().strip();
            if (!user.matches("[a-zA-Z0-9_-]+")) { setupStatus.setText("⚠ username may only contain a-z 0-9 _ -"); return; }
            if (user.equals(System.getProperty("user.name"))) { setupStatus.setText("that's this panel's own user — nothing to do"); return; }
            background(() -> {
                String out = runAsRootScript(grantScript(user, System.getProperty("user.name"),
                        Env.baseDir().resolve("workspace").toString()));
                SwingUtilities.invokeLater(() -> setupStatus.setText(out.strip().isEmpty()
                        ? "done — check the log" : out.strip().lines().reduce((a, b) -> b).orElse("")));
            });
        });

        // save python-parity: pop keys then write our values
        var saveBtn = new JButton("Save config to .env");
        var savedLabel = new JLabel(" ");
        saveBtn.addActionListener(e -> {
            boolean cloud = cloudRadio.isSelected();
            List<String> keys = new ArrayList<>();
            for (int i = 0; i < keyPoolModel.size(); i++) keys.add(keyPoolModel.get(i));
            Env.set("DISCORD_BOT_TOKEN", new String(discordToken.getPassword()).strip());
            Env.set("ALLOWED_CHANNEL_IDS", discordAll.isSelected() ? "" : discordChannels.getText().strip());
            Env.set("BOT_MODE", chosenRadio(discordMode, "WAKEWORD"));
            Env.set("WAKE_WORD", discordWake.getText().strip().isBlank() ? "hey claw" : discordWake.getText().strip());
            Env.set("STOAT_BOT_TOKEN", new String(stoatToken.getPassword()).strip());
            Env.set("STOAT_ALLOWED_CHANNEL_IDS", stoatAll.isSelected() ? "" : stoatChannels.getText().strip());
            Env.set("STOAT_BOT_MODE", chosenRadio(stoatMode, "WAKEWORD"));
            Env.set("STOAT_WAKE_WORD", stoatWake.getText().strip().isBlank() ? "hey claw" : stoatWake.getText().strip());
            Env.set("AGENT_PROVIDER", "ollama");
            String model = String.valueOf(modelCombo.getEditor().getItem()).strip();
            if (model.isBlank())
                model = cloud ? "gpt-oss:120b-cloud" : Env.get("AGENT_MODEL", "qwen2.5:0.5b");
            Env.set("AGENT_MODEL", model);
            Env.set("OLLAMA_MODE", cloud ? "cloud" : "local");
            Env.set("OLLAMA_HOST", cloud ? Providers.OLLAMA_CLOUD_HOST
                    : (ollamaHost.getText().strip().isBlank() ? "http://localhost:11434" : ollamaHost.getText().strip()));
            // key pool: primary + fallbacks (python parity: keys survive only in cloud mode on main .env)
            if (cloud && !keys.isEmpty()) {
                Env.set("OLLAMA_API_KEY", keys.get(0));
                Env.set("OLLAMA_API_KEYS", keys.size() > 1 ? String.join(",", keys.subList(1, keys.size())) : "");
            } else {
                Env.set("OLLAMA_API_KEY", "");
                Env.set("OLLAMA_API_KEYS", "");
            }
            String runAs = modeOther.isSelected() ? runAsUser.getText().strip() : "";
            Env.set("AGENT_RUN_AS_USER", runAs);
            Env.set("AGENT_ACCOUNT_MODE", runAs.isBlank() ? "own" : "other");
            Env.set("AGENT_WORKDIR", workdir.getText().strip());
            Env.set("AGENT_ALLOW_ELEVATED",
                    runAs.isBlank() ? "" : elevated.isSelected() ? "1" : "0");
            savedLabel.setText("✓ saved — restart the bots to apply (Run & Logs tab)");
            log("setup: config saved to .env");
        });

        // ── assemble ──
        var box = new JPanel();
        box.setLayout(new BoxLayout(box, BoxLayout.Y_AXIS));
        box.setBorder(pad);

        var discordCard = card("Discord", setupForm(
                new String[]{"Bot token", "Allowed channel ids", "Bot mode", "Wake word"},
                new JComponent[]{labelledPassword(discordToken), discordChannels,
                        discordMode, discordWake},
                new JComponent[]{null, discordAll, null, null}));
        var stoatCard = card("Stoat", setupForm(
                new String[]{"Bot token", "Allowed channel ids", "Bot mode", "Wake word"},
                new JComponent[]{labelledPassword(stoatToken), stoatChannels,
                        stoatMode, stoatWake},
                new JComponent[]{null, stoatAll, null, null}));

        var backend = setupForm(
                new String[]{"Source", "Ollama host (local)", "API keys (first = primary)", "Model"},
                new JComponent[]{boxRadio(localRadio, cloudRadio), ollamaHost,
                        keyPoolPanel(keyPoolModel, keyPool, keyInput), modelCombo},
                new JComponent[]{null, null, null, null});
        var backendSouth = new JPanel(new BorderLayout());
        backendSouth.add(fetchBtn, BorderLayout.WEST);
        backendSouth.add(modelStatus, BorderLayout.EAST);
        var backendWrap = new JPanel(new BorderLayout());
        backendWrap.add(backend, BorderLayout.CENTER);
        backendWrap.add(backendSouth, BorderLayout.SOUTH);
        var backendCard = card("Model backend (Ollama)", backendWrap);
        // (note: fetchBtn only lives here — the grid row's extra slot stays empty)

        var account = setupForm(
                new String[]{"Run the agent as", "Username (other = runuser)", "Workspace directory",
                        "Dedicated account wiring"},
                new JComponent[]{boxRadio(modeSelf, modeOther), runAsUser, workdir, null},
                new JComponent[]{null, null, setupDirBtn, grantBtn});
        var accountSouth = new JPanel(new FlowLayout(FlowLayout.LEFT));
        accountSouth.add(elevated);
        accountSouth.add(setupStatus);
        var accountWrap = new JPanel(new BorderLayout());
        accountWrap.add(account, BorderLayout.CENTER);
        accountWrap.add(accountSouth, BorderLayout.SOUTH);

        box.add(discordCard);
        box.add(Box.createVerticalStrut(10));
        box.add(stoatCard);
        box.add(Box.createVerticalStrut(10));
        box.add(backendCard);
        box.add(Box.createVerticalStrut(10));
        box.add(card("Workspace & account", accountWrap));
        box.add(Box.createVerticalStrut(10));

        var bottom = new JPanel(new FlowLayout(FlowLayout.LEFT));
        bottom.add(saveBtn);
        bottom.add(savedLabel);
        box.add(bottom);

        box.add(Box.createVerticalGlue());
        var scroll = new JScrollPane(box);
        scroll.getVerticalScrollBar().setUnitIncrement(18);
        var root = new JPanel(new BorderLayout());
        root.add(scroll, BorderLayout.CENTER);
        SwingUtilities.invokeLater(fetchModels::run);      // fill the model list for picking
        return root;
    }

    /** GridBag label/component/form row builder (the Setup tab's inner layout). */
    static JPanel setupForm(String[] labels, JComponent[] comps, JComponent[] extras) {
        var grid = new JPanel(new GridBagLayout());
        grid.setOpaque(false);
        var c = new GridBagConstraints();
        c.fill = GridBagConstraints.HORIZONTAL;
        c.insets = new Insets(3, 4, 3, 8);
        c.weightx = 1;
        for (int i = 0; i < labels.length; i++) {
            c.gridx = 0; c.gridy = i;
            grid.add(new JLabel(labels[i]), c);
            c.gridx = 1;
            if (comps[i] != null) grid.add(comps[i], c);
            if (extras != null && extras[i] != null) {
                c.gridx = 2; c.weightx = 0;
                grid.add(extras[i], c);
                c.weightx = 1;
            }
        }
        return grid;
    }

    static JTextField labelledPassword(JPasswordField pf) {
        pf.setEchoChar('•');
        var out = new JTextField();
        out.setText(new String(pf.getPassword()));
        return out;                       // java decision: passwords here are machine-local .env values
    }

    static JPanel radioRow(String[] options, String current) {
        var row = new JPanel(new FlowLayout(FlowLayout.LEFT, 10, 0));
        row.setOpaque(false);
        ButtonGroup group = new ButtonGroup();
        for (String o : options) {
            var r = new JRadioButton(o, o.equalsIgnoreCase(current));
            group.add(r);
            row.add(r);
            row.putClientProperty("radio." + o, r);
        }
        row.putClientProperty("group", group);
        return row;
    }

    static String chosenRadio(JPanel row, String def) {
        for (String o : new String[]{"WAKEWORD", "MENTION", "EVERYTHING"}) {
            var r = (JRadioButton) row.getClientProperty("radio." + o);
            if (r != null && r.isSelected()) return o;
        }
        return def;
    }

    static JPanel boxRadio(JRadioButton... radios) {
        var row = new JPanel(new FlowLayout(FlowLayout.LEFT, 12, 0));
        row.setOpaque(false);
        for (var r : radios) row.add(r);
        return row;
    }

    static JPanel keyPoolPanel(DefaultListModel<String> model, JList<String> list, JTextField input) {
        var btns = new JPanel(new GridLayout(5, 1, 4, 4));
        var add = new JButton("Add");
        var up = new JButton("▲");
        var down = new JButton("▼");
        var remove = new JButton("Remove");
        btns.add(add);
        btns.add(up);
        btns.add(down);
        btns.add(remove);
        add.addActionListener(e -> {
            String v = input.getText().strip();
            if (!v.isBlank()) model.addElement(v);
            input.setText("");
        });
        remove.addActionListener(e -> {
            int i = list.getSelectedIndex();
            if (i >= 0) model.remove(i);
        });
        up.addActionListener(e -> {
            int i = list.getSelectedIndex();
            if (i > 0) {
                String v = model.get(i);
                model.set(i, model.get(i - 1));
                model.set(i - 1, v);
                list.setSelectedIndex(i - 1);
            }
        });
        down.addActionListener(e -> {
            int i = list.getSelectedIndex();
            if (i >= 0 && i < model.size() - 1) {
                String v = model.get(i);
                model.set(i, model.get(i + 1));
                model.set(i + 1, v);
                list.setSelectedIndex(i + 1);
            }
        });
        var split = new JPanel(new BorderLayout(6, 0));
        split.add(new JScrollPane(list), BorderLayout.CENTER);
        split.add(btns, BorderLayout.EAST);
        var south = new JPanel(new BorderLayout(6, 0));
        south.add(input, BorderLayout.CENTER);
        var outer = new JPanel(new BorderLayout(0, 4));
        outer.add(split, BorderLayout.CENTER);
        outer.add(south, BorderLayout.SOUTH);
        return outer;
    }

    /** Tab-local helper: drops checkboxes/status into an already-assembled form's south. */

    /** lists models: prefer `ollama list` CLI, fall back to HTTP GET <host>/api/tags. */
    static List<String> ollamaListOrHttp(String host) throws Exception {
        try {
            var pb = new ProcessBuilder("ollama", "list").redirectErrorStream(true);
            var p = pb.start();
            String out = new String(p.getInputStream().readAllBytes());
            p.waitFor(8, java.util.concurrent.TimeUnit.SECONDS);
            if (p.exitValue() == 0) {
                var models = new ArrayList<String>();
                for (String line : out.strip().lines().skip(1).toList())
                    if (!line.isBlank()) models.add(line.split("\\s+")[0]);
                if (!models.isEmpty()) return models;
            }
        } catch (Exception ignored) {}
        return Providers.listOllamaModels(host, "");
    }

    /** mkdirs + starter .env + permission check (runuser probe when other-user mode). */
    String setupDirectory(String runAsUser, String workdir) {
        try {
            Path wd = Path.of(workdir.isBlank() ? Env.baseDir().resolve("workspace").toString() : workdir);
            Files.createDirectories(wd);
            if (!Files.exists(Env.baseDir().resolve(".env")))
                Files.write(Env.baseDir().resolve(".env"),
                        Files.readAllBytes(Env.baseDir().resolve("example.env")));
            String msg = "✓ workspace dir ready: " + wd + " · .env "
                    + (Files.exists(Env.baseDir().resolve(".env")) ? "present" : "created");
            if (!runAsUser.isBlank()) {
                // can `user` actually use the install? (python runas.can_access parity)
                boolean ok = Tools.isRoot()
                        ? new ProcessBuilder("runuser", "-u", runAsUser, "--", "sh", "-c",
                                "test -r .env && test -w .env", wd.toString())
                                .directory(Env.baseDir().toFile()).start().waitFor(10, java.util.concurrent.TimeUnit.SECONDS)
                        : new ProcessBuilder("sudo", "-n", "-u", runAsUser, "-H", "sh", "-c",
                                "test -r .env && test -w .env", wd.toString())
                                .directory(Env.baseDir().toFile()).start().waitFor(10, java.util.concurrent.TimeUnit.SECONDS);
                msg += ok ? " · '" + runAsUser + "' can access it"
                        : " · '" + runAsUser + "' CANNOT access it yet — set up with 'Allow panel to run as user'";
            }
            return msg;
        } catch (Exception e) {
            return "⚠ " + e.getMessage();
        }
    }

    /** Runs a root shell snippet via pkexec (GUI prompt) → sudo fallback. Returns output. */
    static String runAsRootScript(String script) {
        for (var cmd : List.of(new String[]{"pkexec", "bash", "-c", script},
                new String[]{"sudo", "-n", "bash", "-c", script})) {
            try {
                var p = new ProcessBuilder(cmd).start();
                byte[] out = p.getInputStream().readAllBytes();
                byte[] err = p.getErrorStream().readAllBytes();
                boolean done = p.waitFor(120, java.util.concurrent.TimeUnit.SECONDS);
                String text = new String(out) + (done && p.exitValue() == 0 ? "" : " " + new String(err));
                if (done && p.exitValue() == 0) return text.strip();
            } catch (Exception ignored) {}
        }
        return "";
    }

    /** Grant script (python runas._grant_script parity): sudoers rule + ACLs. */
    static String grantScript(String agentUser, String panelUser, String dir) {
        return """
                set -e
                AGENT=%s
                PANEL=%s
                DIR=%s
                id "$AGENT" >/dev/null 2>&1 || { echo "user '$AGENT' does not exist" >&2; exit 2; }
                TMP=$(mktemp)
                echo "$PANEL ALL=($AGENT) NOPASSWD: ALL" > "$TMP"
                visudo -cf "$TMP" >/dev/null || { rm -f "$TMP"; echo "generated sudoers rule failed validation" >&2; exit 4; }
                install -m 0440 -o root -g root "$TMP" "/etc/sudoers.d/systemc-runas-$AGENT"
                rm -f "$TMP"
                command -v setfacl >/dev/null 2>&1 || { echo "setfacl not found — install acl (dnf install acl)" >&2; exit 3; }
                p=$(dirname "$DIR")
                while [ "$p" != "/" ] && [ -n "$p" ]; do
                    [ "$(stat -c %%U "$p")" = "$AGENT" ] && setfacl -m "u:$PANEL:--x" "$p"
                    p=$(dirname "$p")
                done
                setfacl -R -m "u:$PANEL:rwX" "$DIR"
                find "$DIR" -type d -exec setfacl -d -m "u:$PANEL:rwX" {} +
                setfacl -R -m "u:$AGENT:rwX" "$DIR"
                echo "granted: '$PANEL' can run the bot as '$AGENT', and has access to $DIR"
                """.formatted(agentUser, panelUser, dir == null ? "" : dir);
    }

    // ── Dependencies (environment health + per-profile pip deps) ─────────

    JTextArea depsOutput;

    JComponent dependenciesTab() {
        var root = new JPanel(new BorderLayout(10, 10));
        root.setBorder(new EmptyBorder(14, 14, 14, 14));

        var health = new JPanel(new GridLayout(0, 1, 4, 4));
        health.setOpaque(false);
        health.add(healthRow("Java", System.getProperty("java.version") + " (" + System.getProperty("java.vm.name") + ")"));
        Runnable ollamaProbe = () -> background(() ->
                SwingUtilities.invokeLater(() -> {
                    String status;
                    try {
                        List<String> models = Providers.listOllamaModels(
                                Env.get("OLLAMA_HOST", "http://localhost:11434"), "");
                        status = "reachable · " + models.size() + " model(s)";
                    } catch (Exception e) {
                        status = "not reachable (" + firstLine(e.getMessage()) + ")";
                    }
                    refreshRow(health, "Ollama", status);
                }));
        health.add(healthRow("Ollama", "checking…"));
        ollamaProbe.run();
        health.add(healthRow("python3", probe("python3", "--version").replace("Python ", "present — v")));
        health.add(healthRow("pip3", probe("pip3", "--version").isBlank() ? "missing (python-compat integrations need it)" : "present"));
        health.add(healthRow("tar (update apply)", probe("tar", "--version").isBlank() ? "missing (Updates need GNU tar)" : "present"));
        health.add(healthRow("install root", Env.baseDir().toString()));

        // per-profile pip dependencies (DEPENDENCIES= in each env)
        var profileBox = new JComboBox<String>();
        profileBox.addItem("default");
        for (var p : Profiles.list()) profileBox.addItem(String.valueOf(p.get("id")));
        var depsField = new JTextField();
        var depsLabel = new JLabel("pip packages (comma separated) stored as DEPENDENCIES= in the profile's env");
        var pull = new JButton("Pull Dependencies");
        var reload = new JButton("← Reload from env");
        reload.addActionListener(e -> {
            String id = String.valueOf(profileBox.getSelectedItem());
            Map<String, String> env = Profiles.readEnvFile(Profiles.envPath("default".equals(id) ? null : id));
            depsField.setText(env.getOrDefault("DEPENDENCIES", ""));
        });
        profileBox.addActionListener(e -> {
            String id = String.valueOf(profileBox.getSelectedItem());
            Map<String, String> env = Profiles.readEnvFile(Profiles.envPath("default".equals(id) ? null : id));
            depsField.setText(env.getOrDefault("DEPENDENCIES", ""));
        });
        depsField.setText(Profiles.readEnvFile(Profiles.envPath(null)).getOrDefault("DEPENDENCIES", ""));
        pull.addActionListener(e -> {
            String id = String.valueOf(profileBox.getSelectedItem());
            String pkgs = depsField.getText().strip();
            background(() -> {
                try {
                    Map<String, String> patch = Map.of("DEPENDENCIES", pkgs);
                    Profiles.writeEnvFile(Profiles.envPath("default".equals(id) ? null : id), patch);
                    String out = Profiles.pullDependencies("default".equals(id) ? null : id);
                    SwingUtilities.invokeLater(() -> appendDeps("$ " + out));
                } catch (Exception ex) {
                    SwingUtilities.invokeLater(() -> appendDeps("⚠ " + ex.getMessage()));
                }
            });
        });
        var depsRow = new JPanel(new BorderLayout(8, 8));
        var left = new JPanel(new GridLayout(0, 2, 8, 8));
        left.add(new JLabel("Profile")); left.add(profileBox);
        left.add(new JLabel("DEPENDENCIES")); left.add(depsField);
        left.add(reload); left.add(pull);
        depsRow.add(left, BorderLayout.NORTH);
        depsRow.add(depsLabel, BorderLayout.SOUTH);

        depsOutput = new JTextArea(8, 60);
        depsOutput.setFont(new Font(Font.MONOSPACED, Font.PLAIN, 12));
        depsOutput.setEditable(false);

        var center = new JPanel(new BorderLayout(10, 10));
        center.add(health, BorderLayout.NORTH);
        center.add(depsRow, BorderLayout.CENTER);
        root.add(center, BorderLayout.NORTH);
        root.add(card("Pull output", new JScrollPane(depsOutput)), BorderLayout.CENTER);
        return root;
    }

    static String probe(String... cmd) {
        try {
            var p = new ProcessBuilder(cmd).redirectErrorStream(true).start();
            String out = new String(p.getInputStream().readAllBytes());
            p.waitFor(6, java.util.concurrent.TimeUnit.SECONDS);
            return out.strip();
        } catch (Exception e) {
            return "";
        }
    }

    static String firstLine(String s) {
        if (s == null) return "unknown";
        int i = s.indexOf('\n');
        return (i > 0 ? s.substring(0, i) : s).strip();
    }

    JComponent healthRow(String k, String v) {
        var row = new JPanel(new GridLayout(1, 2));
        row.setOpaque(false);
        var kl = new JLabel(k);
        kl.setFont(kl.getFont().deriveFont(Font.BOLD));
        row.add(kl);
        row.add(new JLabel(v));
        return row;
    }

    void refreshRow(JPanel health, String key, String value) {
        for (var c : health.getComponents())
            if (c instanceof JPanel row && row.getComponentCount() == 2
                    && row.getComponent(0) instanceof JLabel l && key.equals(l.getText())) {
                ((JLabel) row.getComponent(1)).setText(value);
                return;
            }
        health.add(healthRow(key, value));
        health.revalidate();
    }

    void appendDeps(String line) {
        if (depsOutput != null) SwingUtilities.invokeLater(() -> {
            depsOutput.append(line + "\n");
            depsOutput.setCaretPosition(depsOutput.getDocument().getLength());
        });
    }

    // ── Run & Logs (managed instances) ───────────────────────────────────

    final Map<String, Inst> instanceManager = new LinkedHashMap<>();
    JPanel runRows;
    JComboBox<String> runLogPicker;
    JTextArea runLogArea;

    JComponent runTab() {
        runRows = new JPanel();
        runRows.setLayout(new BoxLayout(runRows, BoxLayout.Y_AXIS));
        runRows.setOpaque(false);
        var buttons = new JPanel(new FlowLayout(FlowLayout.LEFT, 8, 0));
        var openUi = new JButton("Open web UI");
        openUi.addActionListener(e -> openWebUi());
        var openLogDir = new JButton("Open log folder");
        openLogDir.addActionListener(e -> openFolder(Env.baseDir().resolve(".profiles")));
        buttons.add(openUi);
        buttons.add(openLogDir);
        runLogPicker = new JComboBox<>();
        runLogPicker.addActionListener(e -> tailRunLog(true));
        runLogArea = new JTextArea();
        runLogArea.setFont(new Font(Font.MONOSPACED, Font.PLAIN, 12));
        runLogArea.setEditable(false);

        var root = new JPanel(new BorderLayout(10, 10));
        root.setBorder(new EmptyBorder(14, 14, 14, 14));
        var north = new JPanel(new BorderLayout());
        north.add(runRows, BorderLayout.CENTER);
        north.add(buttons, BorderLayout.SOUTH);
        var centerSplit = new JPanel(new BorderLayout(10, 10));
        centerSplit.add(new JScrollPane(runLogPicker), BorderLayout.NORTH);
        centerSplit.add(card("Instance log", new JScrollPane(runLogArea)), BorderLayout.CENTER);
        root.add(north, BorderLayout.NORTH);
        root.add(centerSplit, BorderLayout.CENTER);
        rebuildRunRows();
        return root;
    }

    void rebuildRunRows() {
        runRows.removeAll();
        addRunRow(null, "default  (bots + web UI, main .env)", Profiles.argvFor(null).toString());
        for (var p : Profiles.list()) {
            String id = String.valueOf(p.get("id"));
            addRunRow(id, "profile: " + id, Profiles.argvFor(id).toString());
        }
        var pickerModel = new DefaultComboBoxModel<String>();
        pickerModel.addElement("default  ·  .run.log");
        for (var p : Profiles.list())
            pickerModel.addElement(String.valueOf(p.get("id")) + "  ·  .profiles/" + p.get("id") + ".log");
        var sel = runLogPicker.getSelectedItem();
        runLogPicker.setModel(pickerModel);
        if (sel != null) runLogPicker.setSelectedItem(sel);
        runRows.revalidate();
        runRows.repaint();
    }

    void addRunRow(String id, String label, String argvHint) {
        Inst inst = instanceManager.computeIfAbsent(id, k -> new Inst(k));
        var row = new JPanel(new FlowLayout(FlowLayout.LEFT, 8, 4));
        row.setOpaque(false);
        var status = new JLabel();
        String running = inst.running() ? "● RUNNING" : "○ stopped";
        var l = new JLabel("<html><b>" + label + "</b> <span style='color:#6e6e73'>· " + argvHint
                + "</span>  " + "" + "</html>");
        var name = new JLabel(running);
        var start = new JButton("Start");
        start.addActionListener(e -> startProfile(id));
        var stop = new JButton("Stop");
        stop.addActionListener(e -> stopProfile(id));
        var auto = new JCheckBox("auto-restart", inst.autoRestart);
        auto.addActionListener(e -> inst.autoRestart = auto.isSelected());
        row.add(l);
        row.add(name);
        row.add(start);
        row.add(stop);
        row.add(auto);
        row.putClientProperty("status", name);
        row.putClientProperty("id", id);
        runRows.add(row);
        runRows.add(Box.createVerticalStrut(4));
    }

    /** every timer tick:-statuses, watchdog restart, log tail. */
    void runTick() {
        if (runRows == null || runLogArea == null) return;
        // statuses + crash watchdog
        for (var c : runRows.getComponents()) {
            if (!(c instanceof JPanel row)) continue;
            String id = (String) row.getClientProperty("id");   // null = default instance
            Inst inst = instanceManager.get(id);
            if (inst == null) continue;
            var name = (JLabel) row.getClientProperty("status");
            boolean crashed = inst.pollCrash();
            if (crashed) {
                log("run: " + (id == null ? "default" : id) + " exited"
                        + (inst.exitCode() == null ? "" : " (code " + inst.exitCode() + ")")
                        + (inst.autoRestart ? " — restarting (auto-restart on)" : ""));
                if (inst.autoRestart) try { inst.start(); } catch (IOException ignored) {}
            }
            name.setText(inst.running() ? "● RUNNING" : "○ stopped");
        }
        tailRunLog(false);
    }

    void tailRunLog(boolean force) {
        if (runLogPicker == null) return;
        String sel = String.valueOf(runLogPicker.getSelectedItem());
        String filePart = sel.contains("·") ? sel.substring(sel.lastIndexOf('·') + 1).strip() : "";
        if (filePart.isBlank()) return;
        Path f = Env.baseDir().resolve(filePart);
        if (!Files.exists(f)) return;
        try {
            List<String> lines = Files.readAllLines(f);
            int from = Math.max(0, lines.size() - 400);
            runLogArea.setText(lines.subList(from, lines.size()).stream()
                    .collect(java.util.stream.Collectors.joining("\n")));
            runLogArea.setCaretPosition(runLogArea.getDocument().getLength());
        } catch (IOException ignored) {}
    }

    void startProfile(String id) {
        try {
            Inst inst = instanceManager.computeIfAbsent(id, k -> new Inst(k));
            if (inst.running()) { log("run: " + (id == null ? "default" : id) + " already running"); return; }
            inst.start();
            log("run: started " + (id == null ? "default" : id) + " — log: "
                    + (id == null ? ".run.log" : ".profiles/" + id + ".log"));
            runTick();
        } catch (Exception ex) {
            log("⚠ start failed: " + ex.getMessage());
        }
    }

    void stopProfile(String id) {
        Inst inst = instanceManager.get(id);
        if (inst == null || !inst.running()) {
            log("run: " + (id == null ? "default" : id) + " is not running (start it here first)");
            return;
        }
        inst.stop();
        log("run: stopped " + (id == null ? "default" : id));
        runTick();
    }

    /** One managed instance (python ManagedProcess parity): tee'd log, watchdog, stop. */
    final class Inst {
        final String id;                 // null = default instance
        final Path logFile;
        volatile Process proc;
        volatile boolean deliberateStop;
        volatile boolean wasRunning;
        volatile Integer lastExit;
        volatile boolean autoRestart;

        Inst(String id) {
            this.id = id;
            this.logFile = Profiles.logPath(id);
        }

        boolean running() {
            Process p = proc;
            return p != null && p.isAlive();
        }

        Integer exitCode() { return lastExit; }

        void start() throws IOException {
            deliberateStop = false;
            lastExit = null;
            var pb = new ProcessBuilder(Profiles.argvFor(id));
            pb.directory(Env.baseDir().toFile());
            pb.redirectErrorStream(true);
            proc = pb.start();
            wasRunning = true;
            Thread.ofPlatform().daemon().name("log-" + (id == null ? "default" : id)).start(() -> {
                try (var r = new BufferedReader(new InputStreamReader(proc.getInputStream()));
                     var w = new BufferedWriter(new OutputStreamWriter(
                             new FileOutputStream(logFile.toFile(), true)))) {
                    String line;
                    while ((line = r.readLine()) != null) {
                        w.write(line);
                        w.write("\n");
                        w.flush();
                    }
                } catch (IOException ignored) {}
                if (!deliberateStop && autoRestart) {
                    // handled by runTick watchdog next tick
                }
            });
        }

        void stop() {
            deliberateStop = true;
            Process p = proc;
            if (p != null) {
                p.destroy();
                try { if (!p.waitFor(5, java.util.concurrent.TimeUnit.SECONDS)) p.destroyForcibly(); }
                catch (InterruptedException ignored) {}
            }
        }

        /** true exactly once per (crash + autoRestart enabled). */
        boolean pollCrash() {
            Process p = proc;
            if (p == null) { wasRunning = false; return false; }
            boolean alive = p.isAlive();
            boolean crashed = wasRunning && !alive && !deliberateStop && autoRestart;
            if (!alive) {
                try { lastExit = p.exitValue(); } catch (IllegalStateException ignored) {}
                wasRunning = false;
            }
            return crashed;
        }
    }

    // ── Updates (release check / install / rollback) ─────────────────────

    volatile Map<String, Object> updateInfo;
    JTextField updatesRepo;
    JCheckBox updatesPre;
    JButton updatesInstall;
    JLabel updatesStatus;

    JComponent updatesTab() {
        updatesRepo = new JTextField(Env.get("UPDATE_REPO", Updater.DEFAULT_REPO));
        updatesPre = new JCheckBox("Include pre-releases (betas)", Env.getBool("UPDATE_PRERELEASE", false));
        updatesStatus = new JLabel("Nothing checked yet — press Check for updates.");
        var checkBtn = new JButton("Check for updates");
        updatesInstall = new JButton("Update now");
        updatesInstall.setEnabled(false);
        var rollbackBtn = new JButton("Roll back");
        var backupsBtn = new JButton("Show backups");
        checkBtn.addActionListener(e -> checkUpdates());
        updatesInstall.addActionListener(e -> installUpdates());
        rollbackBtn.addActionListener(e -> {
            if (JOptionPane.showConfirmDialog(this,
                    "Restore the newest update backup? (Restores bin/ and lib/.)",
                    "Roll back", JOptionPane.OK_CANCEL_OPTION, JOptionPane.WARNING_MESSAGE)
                    == JOptionPane.OK_OPTION)
                background(() -> {
                    try {
                        Updater.rollback(null, s -> SwingUtilities.invokeLater(() -> log("updater: " + s)));
                        SwingUtilities.invokeLater(() -> updatesStatus
                                .setText("✓ rolled back — restart the bots to load the restored version"));
                    } catch (Exception ex) {
                        SwingUtilities.invokeLater(() -> updatesStatus.setText("⚠ " + ex.getMessage()));
                    }
                });
        });
        backupsBtn.addActionListener(e -> listUpdateBackups());

        var grid = new JPanel(new GridLayout(0, 2, 8, 4));
        grid.add(new JLabel("Release source (UPDATE_REPO)"));
        grid.add(updatesRepo);
        var middle = new JPanel(new BorderLayout());
        var controls = new JPanel(new FlowLayout(FlowLayout.LEFT));
        controls.add(checkBtn);
        controls.add(updatesInstall);
        controls.add(rollbackBtn);
        controls.add(backupsBtn);
        middle.add(updatesPre, BorderLayout.NORTH);
        middle.add(controls, BorderLayout.SOUTH);
        grid.add(new JLabel(" "));
        grid.add(new JLabel(" "));

        var updatesLog = new JTextArea(10, 60);
        updatesLog.setFont(new Font(Font.MONOSPACED, Font.PLAIN, 12));
        updatesLog.setEditable(false);
        updatesLogArea = updatesLog;
        var south = new JPanel(new FlowLayout(FlowLayout.LEFT));
        south.add(updatesStatus);

        var root = new JPanel(new BorderLayout(10, 10));
        root.setBorder(new EmptyBorder(14, 14, 14, 14));
        var top = new JPanel(new BorderLayout(10, 10));
        top.add(grid, BorderLayout.NORTH);
        top.add(middle, BorderLayout.CENTER);
        top.add(south, BorderLayout.SOUTH);
        root.add(top, BorderLayout.NORTH);
        root.add(card("Update log", new JScrollPane(updatesLog)), BorderLayout.CENTER);
        return root;
    }

    JTextArea updatesLogArea;

    void appendUpdateLog(String line) {
        if (updatesLogArea == null) return;
        updatesLogArea.append(line + "\n");
        updatesLogArea.setCaretPosition(updatesLogArea.getDocument().getLength());
    }

    void checkUpdates() {
        String repo = updatesRepo.getText().strip();
        if (!repo.isBlank()) Env.set("UPDATE_REPO", repo);
        Env.set("UPDATE_PRERELEASE", updatesPre.isSelected() ? "1" : "0");
        SwingUtilities.invokeLater(() -> updatesStatus.setText("checking…"));
        background(() -> {
            try {
                var info = Updater.check(updatesRepo.getText().strip(), updatesPre.isSelected());
                this.updateInfo = info;
                boolean avail = Boolean.TRUE.equals(info.get("update_available"));
                SwingUtilities.invokeLater(() -> {
                    updatesStatus.setText("installed " + info.get("installed") + " · latest "
                            + info.get("tag") + (Boolean.TRUE.equals(info.get("prerelease")) ? " (pre-release)" : "")
                            + " · " + (avail ? "update available → Update now" : "you're up to date"));
                    updatesInstall.setEnabled(avail);
                    appendUpdateLog("updates: checked " + info.get("repo") + " — latest " + info.get("tag")
                            + (avail ? " (update available)" : " (up to date)"));
                });
            } catch (Exception ex) {
                SwingUtilities.invokeLater(() -> {
                    updatesStatus.setText("⚠ " + ex.getMessage());
                    updatesInstall.setEnabled(false);
                });
            }
        });
    }

    void installUpdates() {
        Map<String, Object> info = updateInfo;
        if (info == null) { checkUpdates(); return; }
        updatesInstall.setEnabled(false);
        background(() -> {
            try {
                var res = Updater.install(info, s -> SwingUtilities.invokeLater(() -> log("updater: " + s)));
                SwingUtilities.invokeLater(() -> {
                    checkUpdates();
                    updatesInstall.setEnabled(false);
                    log("updates: installed " + res.get("tag") + " — restart the bots (Run & Logs → Stop/Start, "
                            + "or: systemc service restart)");
                });
            } catch (Exception ex) {
                SwingUtilities.invokeLater(() -> {
                    log("⚠ update failed: " + ex.getMessage());
                    updatesInstall.setEnabled(true);
                });
            }
        });
    }

    void listUpdateBackups() {
        var backups = Updater.listBackups();
        String[] names = backups.stream().map(b -> String.valueOf(b.get("name"))).toArray(String[]::new);
        var msg = names.length == 0 ? "No backups yet — the first update creates one."
                : "Backups (newest first):\n  " + String.join("\n  ", names);
        JOptionPane.showMessageDialog(this, msg, "Update backups", JOptionPane.INFORMATION_MESSAGE);
    }

    // ── shared helpers ───────────────────────────────────────────────────

    void background(Runnable r) {
        Thread.ofPlatform().daemon().start(r);
    }

    void infoBox(String s) {
        JOptionPane.showMessageDialog(this, s);
    }

    void openFolder(Path p) {
        try {
            if (p != null && !Files.exists(p)) Files.createDirectories(p);
            if (Desktop.isDesktopSupported() && p != null) Desktop.getDesktop().open(p.toFile());
        } catch (Exception ex) { log("⚠ " + ex.getMessage()); }
    }

    void openWebUi() {
        try {
            Desktop.getDesktop().browse(new java.net.URI(
                    "http://127.0.0.1:" + Env.getInt("WEBUI_PORT", WebUi.PORT)));
        } catch (Exception ex) { log("⚠ " + ex.getMessage()); }
    }

    static String cred() { return Env.get("AGENT_NAME", Prompt.DEFAULT_AGENT_NAME); }


    public static void main(String[] args) {
        Env.setBaseDir(java.nio.file.Path.of(".").toAbsolutePath());
        Env.load();
        try {
            UIManager.setLookAndFeel(new FlatMacLightLaf());
            UIManager.put("TitlePane.unifiedBackground", true);
        } catch (Exception ignored) {}
        SwingUtilities.invokeLater(() -> new ControlPanel().setVisible(true));
    }
}
