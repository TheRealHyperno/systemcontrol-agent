package systemc;

import com.formdev.flatlaf.FlatLightLaf;
import com.formdev.flatlaf.themes.FlatMacLightLaf;

import javax.swing.*;
import javax.swing.border.EmptyBorder;
import java.awt.*;
import java.io.IOException;
import java.nio.file.Files;
import java.util.*;
import java.util.List;

/**
 * Port of control_panel.py — but this time a Swing desktop app themed to feel
 * like macOS (FlatLaf "macOS Light", big rounded cards, traffic-light accents).
 *
 * Tabs:
 *   Overview      status, start/stop the server + bots, live log
 *   Sessions      per-channel histories, reset button
 *   Approvals     resolve destructive-command requests from .approvals/
 *   Profiles      multi-instance management (.profiles/profiles.json)
 *   Personalize   name / gender / pronouns / system prompt (AGENT_* in .env)
 */
public final class ControlPanel extends JFrame {
    final JTextArea logArea = new JTextArea();
    final javax.swing.Timer uiTimer;
    final JPanel approvalsList = new JPanel();
    final DefaultListModel<String> sessionsModel = new DefaultListModel<>();
    final Map<String, Map<String, Object>> sessionsByKey = new LinkedHashMap<>();
    final DefaultListModel<String> profilesModel = new DefaultListModel<>();

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
        tabs.addTab("Sessions", sessionsTab());
        tabs.addTab("Approvals", approvalsTab());
        tabs.addTab("Profiles", profilesTab());
        tabs.addTab("Personalize", personalizeTab());
        tabs.addTab("Integrations", integrationsTab());
        add(tabs);

        // pipe our own stdout into the log area, and refresh lists periodically
        uiTimer = new javax.swing.Timer(1500, e -> {
            refreshSessions();
            refreshApprovals();
            refreshProfiles();
            refreshIntegrations();
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
        root.add(card("Setup", actions), BorderLayout.CENTER);
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

    // ── Profiles ─────────────────────────────────────────────────────────
    JComponent profilesTab() {
        var list = new JList<>(profilesModel);
        var form = new JPanel(new GridLayout(0, 2, 8, 8));
        var idField = new JTextField();
        var modelField = new JTextField();
        form.add(new JLabel("Profile id"));
        form.add(idField);
        form.add(new JLabel("AGENT_MODEL"));
        form.add(modelField);
        var save = new JButton("Save / create");
        save.addActionListener(e -> {
            String id = idField.getText().strip();
            if (id.isBlank() || !id.matches("[A-Za-z0-9_-]+")) { log("⚠ profile id must be [A-Za-z0-9_-]"); return; }
            var patch = new HashMap<String, Object>();
            if (!modelField.getText().isBlank()) patch.put("AGENT_MODEL", modelField.getText().strip());
            Profiles.upsert(id, patch);
            refreshProfiles();
        });
        var start = new JButton("Start instance");
        start.addActionListener(e -> {
            String id = idField.getText().strip();
            try {
                Profiles.start(id);
                log("started instance " + id + " (log: .profiles/" + id + ".log)");
            } catch (Exception ex) {
                log("⚠ " + ex.getMessage());
            }
        });
        var btns = new JPanel(new FlowLayout(FlowLayout.RIGHT));
        btns.add(save);
        btns.add(start);
        var right = new JPanel(new BorderLayout(8, 8));
        right.add(form, BorderLayout.NORTH);
        right.add(btns, BorderLayout.SOUTH);
        var root = new JPanel(new BorderLayout(10, 10));
        root.setBorder(new EmptyBorder(14, 14, 14, 14));
        root.add(new JScrollPane(list), BorderLayout.CENTER);
        root.add(right, BorderLayout.SOUTH);
        return root;
    }

    void refreshProfiles() {
        profilesModel.clear();
        for (var p : Profiles.list()) {
            profilesModel.addElement(p.get("id") + "  model=" + p.getOrDefault("AGENT_MODEL", "(agent default)"));
        }
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
