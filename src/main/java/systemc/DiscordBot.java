package systemc;

import net.dv8tion.jda.api.*;
import net.dv8tion.jda.api.entities.Message;
import net.dv8tion.jda.api.entities.MessageEmbed;
import net.dv8tion.jda.api.entities.User;
import net.dv8tion.jda.api.entities.channel.concrete.TextChannel;
import net.dv8tion.jda.api.entities.channel.middleman.MessageChannel;
import net.dv8tion.jda.api.entities.emoji.Emoji;
import net.dv8tion.jda.api.events.message.MessageReceivedEvent;
import net.dv8tion.jda.api.events.message.react.MessageReactionAddEvent;
import net.dv8tion.jda.api.events.session.ReadyEvent;
import net.dv8tion.jda.api.hooks.ListenerAdapter;
import net.dv8tion.jda.api.requests.GatewayIntent;
import net.dv8tion.jda.api.utils.FileUpload;

import java.awt.Color;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.OffsetDateTime;
import java.time.format.DateTimeFormatter;
import java.util.*;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

/**
 * Port of bot.py + the Discord side of chat_platform.py.
 * Modes WAKEWORD / MENTION / EVERYTHING, "!command" shell shortcut, "/reset",
 * attachment downloads, reaction approvals (✅/❌).
 */
public final class DiscordBot extends ListenerAdapter {
    static final int HISTORY_TRIM = 60;
    static final Pattern NOT_FILENAME = Pattern.compile("[^\\w.\\-]");

    static final Set<String> ALLOWED_CHANNEL_IDS = new HashSet<>();
    static String MODE, WAKE_WORD, TOKEN;

    final Agent agent;
    private JDA jda;

    /** WAITING -> future for reaction-based approvals, by message id. */
    final Map<Long, CompletableFuture<Boolean>> pendingApprovals = new ConcurrentHashMap<>();

    public static DiscordBot start(Agent agent) {
        TOKEN = Env.get("DISCORD_BOT_TOKEN");
        for (String x : Env.get("ALLOWED_CHANNEL_IDS").split(",")) {
            if (!x.strip().isEmpty()) ALLOWED_CHANNEL_IDS.add(x.strip());
        }
        MODE = Env.get("BOT_MODE", "WAKEWORD").toUpperCase();
        WAKE_WORD = Env.get("WAKE_WORD", "hey claw").toLowerCase();
        if (TOKEN.isBlank()) return null;
        DiscordBot bot = new DiscordBot(agent, null);
        JDA jda = JDABuilder.createDefault(TOKEN)
                .enableIntents(GatewayIntent.MESSAGE_CONTENT)
                .addEventListeners(bot)
                .build();
        bot.jda = jda;
        return bot;
    }

    private DiscordBot(Agent agent, JDA jda) {
        this.agent = agent;
        this.jda = jda;
    }

    @Override
    public void onReady(ReadyEvent event) {
        System.out.println("✓ logged in as " + event.getJDA().getSelfUser().getName());
        System.out.println("  mode:      " + MODE);
        if (MODE.equals("WAKEWORD")) System.out.println("  wake word: \"" + WAKE_WORD + "\"");
        if (!ALLOWED_CHANNEL_IDS.isEmpty())
            System.out.println("  channels:  " + String.join(", ", ALLOWED_CHANNEL_IDS));
        else
            System.out.println("  WARNING: no ALLOWED_CHANNEL_IDS set — responding everywhere");
    }

    @Override
    public void onMessageReactionAdd(MessageReactionAddEvent event) {
        var fut = pendingApprovals.get(event.getMessageIdLong());
        if (fut == null) return;
        String emoji = event.getEmoji().getName();
        User user = event.getUser();
        if (user == null || user.isBot()) return;
        if (emoji.equals("✅") || emoji.equals("❌")) {
            CompletableFuture<Boolean> done = pendingApprovals.remove(event.getMessageIdLong());
            fut.complete(emoji.equals("✅"));
        }
    }

    boolean shouldRespond(Message msg) {
        if (msg.getAuthor().equals(jda.getSelfUser())) return false;
        if (!ALLOWED_CHANNEL_IDS.isEmpty() && !ALLOWED_CHANNEL_IDS.contains(String.valueOf(msg.getChannel().getId()))) return false;
        String content = msg.getContentRaw();
        if (content.isBlank()) return false;
        if (content.startsWith("!")) return false;
        boolean mentioned = msg.getMentions().getUsers().contains(jda.getSelfUser());
        String lower = content.toLowerCase();
        return switch (MODE) {
            case "EVERYTHING" -> true;
            case "MENTION" -> mentioned;
            case "WAKEWORD" -> mentioned || lower.startsWith(WAKE_WORD);
            default -> false;
        };
    }

    String channelLabel(Message msg) {
        try {
            var ch = msg.getChannel();
            if (ch instanceof TextChannel t) {
                String guild = t.getGuild() == null ? null : t.getGuild().getName();
                return guild != null && !guild.isBlank() ? "#" + t.getName() + " (" + guild + ")" : "#" + t.getName();
            }
            return "DM with " + msg.getAuthor().getEffectiveName();
        } catch (Exception e) {
            return "";
        }
    }

    String cleanInput(Message msg) {
        String content = msg.getContentRaw();
        content = content.replace("<@" + jda.getSelfUser().getId() + ">", "")
                .replace("<@!" + jda.getSelfUser().getId() + ">", "").strip();
        if (content.toLowerCase().startsWith(WAKE_WORD)) content = content.substring(WAKE_WORD.length()).strip();
        return content.isBlank() ? msg.getContentRaw() : content;
    }

    void handleMessage(Message msg) {
        var ch = msg.getChannel();
        if (!(ch instanceof MessageChannel mc)) return;
        String cid = mc.getId();
        boolean inAllowed = ALLOWED_CHANNEL_IDS.isEmpty() || ALLOWED_CHANNEL_IDS.contains(cid);
        String content = msg.getContentRaw();

        // !command shortcut (bot.py lines 138-159)
        if (content.startsWith("!") && content.length() > 1 && inAllowed) {
            String command = content.substring(1).strip();
            try { mc.sendTyping().queue(); } catch (Exception ignored) {}
            var channel = new DiscordChannel(mc, jda);
            if (Tools.isDestructive(command) && !channel.requestApproval(command, 60)) {
                pendingApprovals.keySet().removeIf(Objects::nonNull);
                return;
            }
            var result = Tools.runShell(command);
            String stdout = Json.str(result, "stdout") == null ? "" : Json.str(result, "stdout").strip();
            String exit = String.valueOf(result.getOrDefault("exit_code", "?"));
            String out = stdout.isBlank() ? "(no output)" : (stdout.length() > 1900 ? stdout.substring(stdout.length() - 1900) : stdout);
            Agent.safeSend(channel, "```\n" + out + "\n```\nexit: `" + exit + "`");
            return;
        }

        // /reset (bot.py 161-178)
        String stripped = content.replace("<@" + jda.getSelfUser().getId() + ">", "")
                .replace("<@!" + jda.getSelfUser().getId() + ">", "").strip().toLowerCase();
        if (stripped.equals("/reset") && inAllowed) {
            Sessions.reset(Sessions.makeKey("discord", cid));
            mc.sendMessage("🧹 Context cleared for this channel. Long-term memory is untouched.").queue();
            return;
        }

        if (!shouldRespond(msg)) return;

        try { mc.sendTyping().queue(); } catch (Exception ignored) {}
        var channel = new DiscordChannel(mc, jda);
        String prompt = ChatChannel.senderTag(msg.getAuthor().getEffectiveName(),
                msg.getAuthor().getName(), String.valueOf(msg.getAuthor().getId())) + " " + cleanInput(msg);

        List<String> saved = new ArrayList<>();
        if (!msg.getAttachments().isEmpty()) {
            try {
                Files.createDirectories(Tools.ATTACHMENTS_DIR);
                for (Message.Attachment att : msg.getAttachments()) {
                    String safe = NOT_FILENAME.matcher(att.getFileName()).replaceAll("_");
                    Path dest = Tools.ATTACHMENTS_DIR.resolve(att.getId() + "_" + safe);
                    try (InputStream in = att.getProxy().download().join()) {
                        Files.copy(in, dest, StandardCopyOption.REPLACE_EXISTING);
                        saved.add(Tools.WORKDIR.relativize(dest).toString());
                    } catch (Exception ignored) {}
                }
            } catch (Exception ignored) {}
        }
        if (!saved.isEmpty()) prompt += "\n\n[Attached files saved to workspace: " + String.join(", ", saved) + "]";

        var session = Sessions.begin(Sessions.makeKey("discord", cid));
        List<Map<String, Object>> history = agent.runTurn(channel, prompt, session.history,
                Map.of("id", String.valueOf(msg.getAuthor().getId()),
                       "name", msg.getAuthor().getEffectiveName()));
        history = trimHistory(history);
        session.commit(history, channelLabel(msg));
    }

    static List<Map<String, Object>> trimHistory(List<Map<String, Object>> history) {
        if (history.size() <= HISTORY_TRIM) return history;
        var h = new ArrayList<>(history.subList(history.size() - HISTORY_TRIM, history.size()));
        while (!h.isEmpty() && !"user".equals(h.get(0).get("role"))) h.remove(0);
        return h;
    }

    @Override
    public void onMessageReceived(MessageReceivedEvent event) {
        handleMessage(event.getMessage());
    }

    // ── Channel wrapper (chat_platform.DiscordChannel) ───────────────────
    final class DiscordChannel implements ChatChannel {
        final MessageChannel mc;
        final JDA jda;

        DiscordChannel(MessageChannel mc, JDA jda) { this.mc = mc; this.jda = jda; }

        @Override public String platform() { return "discord"; }
        @Override public String id() { return mc.getId(); }
        @Override public String name() { return channelName(); }

        String channelName() {
            try {
                if (mc instanceof TextChannel t) {
                    String guild = t.getGuild() == null ? null : t.getGuild().getName();
                    return guild != null && !guild.isBlank() ? "#" + t.getName() + " (" + guild + ")" : "#" + t.getName();
                }
                return "DM";
            } catch (Exception e) { return ""; }
        }

        @Override
        public void send(String content) {
            for (int i = 0; i < content.length(); i += 1900) {
                mc.sendMessage(content.substring(i, Math.min(content.length(), i + 1900))).complete();
            }
        }

        @Override
        public void sendFile(String path, String caption) throws Exception {
            Path full = Tools.resolve(path);
            if (!Files.isRegularFile(full)) throw new IllegalArgumentException("no such file: " + full);
            long size = Files.size(full);
            if (size > Tools.MAX_UPLOAD_BYTES)
                throw new IllegalArgumentException("file is " + size + " bytes, over the " + Tools.MAX_UPLOAD_BYTES + "-byte limit");
            var fu = FileUpload.fromData(full.toFile());
            var action = mc.sendFiles(fu);
            if (caption != null && !caption.isBlank()) {
                for (int i = 0; i < caption.length(); i += 1900) {
                    mc.sendMessage(caption.substring(i, Math.min(caption.length(), i + 1900))).complete();
                }
            } else {
                action.complete();
            }
        }

        @Override
        public List<Map<String, Object>> history(int limit) {
            var retrieved = mc.getHistory().retrievePast(Math.max(1, Math.min(limit, 100))).complete();
            Collections.reverse(retrieved);
            var out = new ArrayList<Map<String, Object>>();
            for (Message m : retrieved) {
                Map<String, Object> e = Json.map();
                e.put("id", m.getId());
                e.put("author", m.getAuthor().getName());
                e.put("content", m.getContentRaw());
                e.put("timestamp", m.getTimeCreated().format(DateTimeFormatter.ISO_OFFSET_DATE_TIME));
                Message ref = m.getReferencedMessage();
                if (ref != null) {
                    Map<String, Object> reply = Json.map();
                    reply.put("author", ref.getAuthor().getName());
                    String c = ref.getContentRaw();
                    reply.put("content", c.length() > 200 ? c.substring(0, 200) : c);
                    e.put("reply_to", reply);
                }
                out.add(e);
            }
            return out;
        }

        @Override
        public void setTyping(boolean on) { if (on) mc.sendTyping().queue(); }

        @Override
        public Boolean askApproval(String command, int timeout) throws Exception {
            if ("approve_all".equals(Tools.approvalMode())) return true;
            if ("deny_all".equals(Tools.approvalMode())) return false;
            String code = command.length() > 1000 ? command.substring(0, 1000) : command;
            MessageEmbed embed = new EmbedBuilder()
                    .setTitle("⚠️ Approval required")
                    .setDescription("```\n" + code + "\n```")
                    .setColor(Color.ORANGE)
                    .setFooter("React ✅/❌ here, or approve from the control panel (" + timeout + "s timeout)")
                    .build();
            Message msg;
            try {
                msg = mc.sendMessageEmbeds(embed).complete();
            } catch (Exception e) { return false; }
            pendingApprovals.put(msg.getIdLong(), new CompletableFuture<>());
            msg.addReaction(Emoji.fromUnicode("✅")).queue();
            msg.addReaction(Emoji.fromUnicode("❌")).queue();
            try {
                Boolean ok = pendingApprovals.get(msg.getIdLong()).get(timeout, TimeUnit.SECONDS);
                String label = ok ? "✅ Approved" : "❌ Denied";
                try {
                    mc.editMessageEmbedsById(msg.getId(), new EmbedBuilder()
                            .setTitle(label)
                            .setDescription("```\n" + code + "\n```")
                            .setColor(ok ? Color.GREEN : Color.RED)
                            .build()).complete();
                } catch (Exception ignored) {}
                return ok;
            } catch (Exception e) {
                pendingApprovals.remove(msg.getIdLong());
                try {
                    mc.editMessageEmbedsById(msg.getId(), new EmbedBuilder()
                            .setTitle("⏱️ Timed out — command cancelled")
                            .setDescription("```\n" + code + "\n```")
                            .setColor(Color.GRAY)
                            .build()).complete();
                } catch (Exception ignored) {}
                return false;
            }
        }
    }
}
