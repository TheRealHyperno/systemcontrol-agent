package systemc;

import java.util.List;
import java.util.Map;

/**
 * Port of chat_platform.py — the abstraction layer the agent speaks to.
 * Both the Discord frontend and the Stoat frontend implement this.
 */
public interface ChatChannel {
    String platform();      // "discord" | "stoat"
    String id();            // channel id
    String name();          // human-readable label for the sessions list ("" ok)

    /** Send a message (may need splitting for the 2000-char limits). */
    void send(String content) throws Exception;

    /** Upload a workspace file as attachment. */
    void sendFile(String path, String caption) throws Exception;

    /** Recent channel history, oldest first: [{id, author, content, timestamp, reply_to?}]. */
    List<Map<String, Object>> history(int limit) throws Exception;

    /** True for a moment while this channel shows "typing..." (best-effort). */
    void setTyping(boolean on) throws Exception;

    /**
     * Ask the channel's users to approve a command in-chat (yes/no reply), racing
     * the approver against the .approvals queue (which the agent also polls).
     * Return true on an explicit yes; the agent decision logic handles timeouts.
     */
    Boolean askApproval(String command, int timeoutSeconds) throws Exception;

    /**
     * chat_platform.request_approval: race the platform's in-chat approval
     * against the .approvals queue, honouring APPROVAL_MODE.
     */
    default boolean requestApproval(String command, int timeoutSeconds) {
        String mode = Tools.approvalMode();
        if ("approve_all".equals(mode)) return true;
        if ("deny_all".equals(mode)) return false;
        String reqId = Approvals.create(command);
        try (var pool = java.util.concurrent.Executors.newVirtualThreadPerTaskExecutor()) {
            var inChat = pool.submit(() -> {
                Boolean r = askApproval(command, timeoutSeconds);
                return Boolean.TRUE.equals(r);
            });
            var local = pool.submit(() -> {
                while (true) {
                    String s = Approvals.getStatus(reqId);
                    if ("approved".equals(s)) return true;
                    if ("denied".equals(s)) return false;
                    Thread.sleep(1000);
                }
            });
            long deadline = System.currentTimeMillis() + timeoutSeconds * 1000L;
            while (System.currentTimeMillis() < deadline) {
                if (local.isDone() && local.get()) return true;
                if (inChat.isDone() && inChat.get()) return true;
                if (local.isDone() || inChat.isDone()) return false; // one finished with "no"
                Thread.sleep(500);
            }
            inChat.cancel(true);
            local.cancel(true);
            return false;
        } catch (Exception e) {
            return false;
        } finally {
            Approvals.cleanup(reqId);
        }
    }

    /** chat_platform.sender_tag */
    static String senderTag(String displayName, String username, String userId) {
        String name = (displayName == null || displayName.isBlank() ? username : displayName);
        if (name == null || name.isBlank()) name = "unknown";
        name = name.replace("\n", " ").strip();
        var extra = new java.util.ArrayList<String>();
        if (username != null && !username.isBlank() && !username.equals(name)) extra.add("@" + username);
        if (userId != null && !userId.isBlank()) extra.add("id " + userId);
        return "[from: " + name + (extra.isEmpty() ? "" : " (" + String.join(", ", extra) + ")") + "]";
    }
}
