package systemc.integrations;

import java.util.List;
import java.util.Map;
import java.util.function.BiConsumer;
import java.util.function.BiPredicate;
import java.util.function.Consumer;

/**
 * The integration SPI for the Java port of SystemC Drovex.
 *
 * <p>An integration is a class from a JAR dropped into {@code <install>/integrations/}
 * (or compiled into the main jar) implementing this interface and registered in
 * {@code META-INF/services/systemc.int.Integration}. The agent then sees the
 * integration's tools next to its built-ins and decides when to call them, exactly
 * like built-ins do.</p>
 *
 * <p>To ship an integration:
 * <pre>
 *   javac -cp systemc-java.jar -d classes MyIntegration.java
 *   jar --create --file my-integration.jar -C classes . \
 *       --main ... (no)  + META-INF/services/systemc.int.Integration listing FQCN
 * </pre></p>
 */
public interface Integration {

    /** Stable name — also used in {@code INTEGRATIONS=} in .env and as settings key prefix. */
    String name();

    /** One line shown in the integrations menus (control panel / TUI / web API). */
    String description();

    /**
     * Optional text added under "## Integration: <name>" to the agent's system prompt.
     * Write it like documentation for the model. Default: empty (no section added).
     */
    default String prompt() { return ""; }

    /** Per-turn gate. Keep fast and side-effect free (e.g. restrict to admins/discord). */
    default boolean available(Context ctx) { return true; }

    /**
     * Tool schemas the agent can call, plain JSON Schema:
     * {@code {name, description, input_schema: {type":"object", properties..., required...}}}.
     * Names must be globally unique — later integrations lose against built-ins.
     */
    List<Map<String, Object>> tools();

    /** Executes one tool call. Return a Map / String / anything JSON-serializable. */
    Object handle(Context ctx, String tool, Map<String, Object> args) throws Exception;

    /**
     * Settings-menu descriptor (same shape as the Python port's PANEL dict):
     * <pre>
     * Map.of("title", "Dice", "icon", "🎲",
     *        "fields", List.of(Map.of("key","DICE_MAX","label","Max per roll",
     *                                 "type","number","default",20)),
     *        "submenu", List.of(Map.of("id","advanced","title","Advanced","fields", ...)))
     * </pre>
     * Field types: text | secret | number | bool | choice (+ "choices": List.of(...)) | list.
     * Setting keys MUST start with the integration's uppercased name ({@code DICE_}).
     * Return null for no settings menu.
     */
    default Map<String, Object> panel() { return null; }

    /** Called once when discovered; throw to refuse loading (message is logged). */
    default void init() {}

    /** The chat context handed to {@link #available} and {@link #handle}. */
    final class Context {
        public String platform = "";
        public String channelId = "";
        public String authorId = "";
        public String authorName = "";
        /** Stream a message to the chat the tool was called from. */
        public Consumer<String> send = s -> {};
        /** Send a file attachment (path, caption). */
        public BiConsumer<String, String> sendFile = (path, caption) -> {};
        /** Ask the owner for approval — true on yes. */
        public BiPredicate<String, Integer> requestApproval = (text, timeout) -> false;

        public static Context silent() { return new Context(); }
    }
}
