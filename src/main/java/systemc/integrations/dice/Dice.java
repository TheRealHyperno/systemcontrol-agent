package systemc.integrations.dice;

import systemc.integrations.Integration;

import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.function.Function;

/**
 * Built-in example integration "dice": 3 tools + a settings page,
 * demonstrating the full SPI (tools + prompt + panel) in ~100 lines.
 * Copy this class as a template for your own integrations.
 */
public final class Dice implements Integration {
    private static final SecureRandom RND = new SecureRandom();

    @Override public String name() { return "dice"; }
    @Override public String description() { return "Roll dice and flip coins (built-in example)."; }
    @Override public String prompt() {
        return "Use these when the user asks to roll dice, flip a coin, or pick a number.";
    }

    @Override public void init() {}    // nothing to prepare

    private static int clamp(int v, int lo, int hi) { return Math.max(lo, Math.min(hi, v)); }

    private int maxPerRoll() {
        try { return clamp(Integer.parseInt(systemc.Env.get("DICE_MAX_PER_ROLL", "20")), 1, 10_000); }
        catch (NumberFormatException e) { return 20; }
    }

    @Override public List<Map<String, Object>> tools() {
        return List.of(
            Map.of("name", "dice_roll",
                   "description", "Roll N dice with S sides and report each roll plus the total.",
                   "input_schema", Map.of("type", "object",
                       "properties", Map.of(
                           "count", Map.of("type", "integer", "description", "How many dice"),
                           "sides", Map.of("type", "integer", "description", "Sides per die (2..1000)")),
                       "required", List.of())),
            Map.of("name", "coin_flip",
                   "description", "Flip one or more coins and report heads/tails.",
                   "input_schema", Map.of("type", "object",
                       "properties", Map.of("flips", Map.of("type", "integer")),
                       "required", List.of())),
            Map.of("name", "pick_number",
                   "description", "Pick a uniform random integer between min and max (inclusive).",
                   "input_schema", Map.of("type", "object",
                       "properties", Map.of(
                           "min", Map.of("type", "integer"),
                           "max", Map.of("type", "integer")),
                       "required", List.of("max"))));
    }

    @Override public Object handle(Context ctx, String tool, Map<String, Object> args) {
        // parseInt helper keeps this example small (JDK 21+; may be different than the
        // real stub — override args passed by the model).
        Function<String, Integer> num = k -> {
            try { return Integer.parseInt(String.valueOf(args.get(k))); }
            catch (Exception e) { return 0; }
        };
        try {
            return switch (tool) {
                case "dice_roll" -> {
                    int count = clamp(num.apply("count") == 0 ? 1 : num.apply("count"), 1, maxPerRoll());
                    int sides = clamp(num.apply("sides") == 0 ? 6 : num.apply("sides"), 2, 1000);
                    var rolls = new ArrayList<Integer>();
                    int total = 0;
                    for (int i = 0; i < count; i++) {
                        int r = RND.nextInt(sides) + 1;
                        rolls.add(r);
                        total += r;
                    }
                    yield Map.of("rolls", rolls, "total", total, "dice", count, "sides", sides);
                }
                case "coin_flip" -> {
                    int flips = clamp(num.apply("flips") == 0 ? 1 : num.apply("flips"), 1, 1000);
                    var out = new ArrayList<String>(flips);
                    for (int i = 0; i < flips; i++) out.add(RND.nextBoolean() ? "heads" : "tails");
                    yield Map.of("flips", out);
                }
                case "pick_number" -> {
                    int min = num.apply("min");
                    int max = num.apply("max") != 0 ? num.apply("max") : 100;
                    if (max < min) { int t = min; min = max; max = t; }
                    yield Map.of("picked", RND.nextInt(max - min + 1) + min, "min", min, "max", max);
                }
                default -> Map.of("error", "unknown tool: " + tool);
            };
        } catch (Exception e) {
            return Map.of("error", String.valueOf(e));
        }
    }

    @Override public Map<String, Object> panel() {
        return Map.of(
                "title", "Dice", "icon", "🎲",
                "fields", List.of(
                        Map.of("key", "DICE_MAX_PER_ROLL", "label", "Max dice per roll",
                               "type", "number", "default", 20,
                               "description", "Safety cap for dice_roll (1..10000).")));
    }
}
