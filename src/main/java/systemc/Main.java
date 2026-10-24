package systemc;

import java.util.concurrent.CountDownLatch;

/**
 * Port of main.py — loads .env, builds one shared Agent, and runs the Discord
 * and Stoat bots (separate daemon threads), plus the local web UI/API server.
 * First-run without a config prints setup instructions and exits (the Python
 * version launches a tkinter wizard; this port uses setup.env / the control panel).
 */
public final class Main {
    public static void main(String[] args) throws Exception {
        System.out.println(Prompt.banner());

        // parse --profile <id>
        for (int i = 0; i < args.length - 1; i++) {
            if ("--profile".equals(args[i])) {
                System.setProperty("systemc.profile", args[i + 1]);
                System.setProperty("systemc.env.systemc_profile_id", args[i + 1]);
            }
        }

        Env.setBaseDir(java.nio.file.Path.of(".").toAbsolutePath());
        boolean found = Env.load();
        String profile = args.length >= 2 && "--profile".equals(args[0])
                ? args[1] : System.getProperty("systemc.profile", "");
        if (!profile.isBlank()) {
            // per-profile .env lives in .profiles/<id>.env (same layout profiles.py used)
            Env.load(Env.baseDir().resolve(".profiles").resolve(profile + ".env"));
            Env.set("SYSTEMC_PROFILE_ID", profile);
        }

        boolean discord = !Env.get("DISCORD_BOT_TOKEN").isBlank();
        boolean stoat = !Env.get("STOAT_BOT_TOKEN").isBlank();
        if (!found || (!discord && !stoat)) {
            System.out.println("""
                No config found.

                Create a .env file in this folder with at least one of:
                    DISCORD_BOT_TOKEN=...      # discord.com/developers/applications
                    STOAT_BOT_TOKEN=...        # stoat.chat/settings -> My Bots
                Optional (pick a model backend):
                    OLLAMA_HOST=http://localhost:11434     # local ollama
                    OLLAMA_API_KEY=...                     # ollama.com cloud
                    AGENT_PROVIDER=anthropic
                    ANTHROPIC_API_KEY=...
                Or run the Swing control panel:  java -cp systemc-java.jar systemc.ControlPanel
                """);
            return;
        }

        Agent agent = new Agent();
        var latch = new CountDownLatch(1);

        // integrations banner (python-parity [integrations] lines)
        for (String l : Integrations.summary()) System.out.println(l);

        if (discord) {
            Thread.ofPlatform().daemon().name("discord").start(() -> {
                try {
                    var bot = DiscordBot.start(agent);
                    if (bot == null) System.out.println("[bot] DISCORD_BOT_TOKEN missing — Discord disabled");
                } catch (Throwable t) {
                    System.out.println("[bot] Discord failed: " + t);
                }
            });
        } else {
            System.out.println("[bot] DISCORD_BOT_TOKEN not set — Discord bot disabled");
        }

        if (stoat) {
            Thread.ofPlatform().daemon().name("stoat").start(() -> {
                try {
                    new StoatBot(agent, Env.get("STOAT_BOT_TOKEN"));
                } catch (Throwable t) {
                    System.out.println("[stoat] connection failed: " + t);
                    System.out.println("[stoat] check STOAT_BOT_TOKEN (stoat.chat -> Settings -> My Bots)");
                }
            });
        } else {
            System.out.println("[stoat] STOAT_BOT_TOKEN not set — Stoat bot disabled");
        }

        // Local web UI + API (webui_server.py). On by default; SYSTEMC_WEBSITE=0 to disable.
        if (Env.getBool("SYSTEMC_WEBSITE", true)) {
            try {
                new WebUi(agent).start(Env.getInt("WEBUI_PORT", WebUi.PORT));
            } catch (Throwable t) {
                System.out.println("[webui] failed to start: " + t);
            }
        }

        // profiles.py: spawn other configured non-default profiles? (multi-instance)
        String single = Env.get("SINGLE_PROFILE");
        if (single.isBlank() && profile.isBlank()) {
            // stay in shared-agent mode like the Python main loop
        }

        System.out.println("Running. Ctrl+C to stop.");
        latch.await();
    }
}
