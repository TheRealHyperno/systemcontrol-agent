# Integrations (Java port)

How the Java integrations engine gives the language model extra tools, how its menus
work, and how to write one. See also `README.md`, and the original port's docs under
the `main` (python) branch.

---

## 1. The idea

An integration is a **Java class (or a python-compat `.py` file)** that adds extra tools
to the agent. The LLM sees them next to the built-in tools (`run_shell`, `read_file`,
`web_search`, …), decides when to call them, and the engine routes the call into your
code — the result becomes part of the conversation so the model can use it.

```
 your integration (.jar SPI or .py compat)
        │  declares TOOLS (JSON-schema) + handle(tool,args)
        ▼
 integrations engine  (systemc.Integrations)
        │  per chat turn: builds the toolset (availability gate, collision checks)
        ▼
 agent loop  (systemc.Agent)  →  the MODEL decides which tool to call and when
        ▼
 handle() runs;  Map result → JSON → back into the conversation → model continues
```

Three things you can expose from an integration, all optional except the tools:

| Piece | Used for | Seen where |
|---|---|---|
| `tools()` + `handle(ctx, tool, args)` | extra agent tools | the agent (chat turns) |
| `prompt()` | "## Integration: <name>" section in the system prompt | the agent |
| `panel()` | the settings menu (fields + descriptions) | Swing panel, TUI, web API |

## 2. Enabling an integration

`INTEGRATIONS=` in `.env` — comma or semicolon separated names, or `all`:

```
INTEGRATIONS=dice,notes
```

You never edit that line by hand if you don't want to: the **Integrations tab**
(Swing), the **Integrations menu** (TUI) and the **web API** all toggle it for you.
Nothing is enabled by default. Unknown names are kept and nagged at startup
(python-parity), and `all` expands to everything discovered.

Settings are stored in `.env` under each field's own key. By convention keys start
with the integration's uppercased name (`DICE_`, `NOTES_`); changes apply at the next
bot start, exactly like the python port.

## 3. Discovery — where integrations come from

The engine scans `<install dir>/integrations/` (the folder next to `.env`) for:

1. **Java SPI — `integrations/*.jar`.** Each jar must register implementations in
   `META-INF/services/systemc.integrations.Integration` (fully-qualified class names,
   one per line). Compiled against the main jar:
   ```bash
   javac -cp systemc-java.jar -d classes MyIntegration.java
   jar --create --file my-integration.jar -C classes .
   # + META-INF/services/systemc.integrations.Integration inside, listing the FQCN
   ```
2. **Built-ins** registered in the main jar itself (currently: `dice`, `notes` — they
   double as living templates).
3. **Python-compat — `integrations/*.py`** (the backwards compatibility mode). Enabled
   with `PY_COMPAT=on|auto|off` in .env (default `auto`: python files are only bridged
   when they exist). See section 6.

Two built-in tools are always visible in menus even when disabled, so you can find
and flip them: `dice` (dice/coins/pick a number) and `notes` (shared scratchpad).

## 4. The menus — Integrations have their own settings UI

Every place that can configure SystemC also presents integrations, driven by the same
engine (no duplicated logic):

### 4.1 Swing control panel → "Integrations" tab
 `systemc panel` → tab **Integrations**

* list of every discovered integration with state (`✔ enabled` / `disabled`), tool
  count, source (java / python-compat) and load errors,
* **Enable / disable** — flips the integration in the `INTEGRATIONS=` list,
* **Settings…** — autogenerates a dialog from `panel()`: `text`/`number` → text box,
  `secret` → password box, `bool` → checkbox, `choice` → combo box with values from
  `choices`, plus a muted description line under each field. Saving writes the values
  into `.env` and closes.
* **Install from .jar…** — file picker that copies your jar into `integrations/`,
* **Open folder**, **Reload**.

### 4.2 TUI → "Integrations" menu
 `systemc tui` → **Integrations**

* scrollable list, same summary as the swing tab per row (`name ✔/disabled · source ·
  N tools`),
* **Enter** on an entry toggles it in `INTEGRATIONS=` immediately, then:
  * throws you into that integration's **settings page** (rows per field: labels,
    current values — secrets masked; Enter on a `choice` field shows numbered options,
    Enter on any other field prompts a value; Enter on empty keeps the current value),
  * Esc brings you back to the list; the header explains where values were saved.

### 4.3 Web API (`/api/v1/integrations*`, bearer-protected like the rest)

* `GET  /api/v1/integrations` → every integration: `name`, `description`, `source`,
  `enabled`, `tools` (names), `menu_title`, `fields` (the panel descriptor flattened)
  and `settings` (current values merged with defaults),
* `POST /api/v1/integrations/enable` `{"name":"dice","enabled":true}` → flips it,
* `PUT  /api/v1/integrations/settings` `{"name":"dice","settings":{"DICE_MAX_PER_ROLL":"10"}}`
  → writes the settings into .env and returns the merged values.

## 5. The Java module contract (`systemc.integrations.Integration`)

```java
package systemc.integrations;
import java.util.*;

public final class MyExample implements Integration {

    @Override public String name() { return "myexample"; }            // unique, no spaces
    @Override public String description() { return "Says hello."; }

    @Override public String prompt() {                                 // optional
        return "Use myexample_hello when the user asks you to greet people.";
    }

    @Override public boolean available(Context ctx) {                  // optional per-turn gate
        return !"stoat".equals(ctx.platform);                          // e.g. discord-only
    }

    @Override public List<Map<String, Object>> tools() {
        return List.of(
            Map.of("name", "myexample_hello",
                   "description", "Greet a person by name.",
                   "input_schema", Map.of(
                       "type", "object",
                       "properties", Map.of(
                           "who", Map.of("type", "string", "description", "Who to greet")),
                       "required", List.of("who"))));
    }

    @Override public Object handle(Context ctx, String tool, Map<String, Object> args) {
        return switch (tool) {
            case "myexample_hello" -> Map.of("greeting", "Hello, " + args.get("who") + "!");
            default -> Map.of("error", "unknown tool: " + tool);
        };
    }

    @Override public Map<String, Object> panel() {                     // optional settings menu
        return Map.of("title", "My Example", "icon", "👋",
            "fields", List.of(
                Map.of("key", "MYEXAMPLE_GREETING", "label", "Greeting word",
                       "type", "text", "default", "Hello",
                       "description", "The word myexample_hello starts with.")));
    }
}
```

Contract rules:

* `name()` must be unique, short, lowercase; it also keys the settings prefix.
* `tools()` values are plain **JSON Schema** (same as python): each tool is a map with
  `name` (unique across ALL tools), `description` (this is your docstring to the model),
  `input_schema`, like `properties`, `required`.
* `handle()` returns anything JSON-serializable (a `Map`, a `List`, a `String`, …).
  Exceptions become `{"error": …}` results, never crashes.
* `available(ctx)` must be fast and side-effect free; it runs once per chat turn.
* `init()` runs once at startup; throw there to refuse loading (message is logged).
* The engine drops: malformed schemas, name collisions with built-ins or earlier
  integrations and integrations whose `available()` is false — logging exactly why.

### 4.1 ctx (what handle() gets)

`Integration.Context` approximates the chat the tool was called from:

| Field | Meaning |
|---|---|
| `platform` | "discord", "stoat" (or "" in offline/testing contexts) |
| `channelId` / `authorId` / `authorName` | who + where |
| `send(text)` | stream a message into that chat |
| `sendFile(path, caption)` | attach a file |
| `requestApproval(text, timeout)` | ask the owner for approval — returns true if yes |

If the bot is not yet connected or you're in the settings page, the ctx is a silent
stand-in (send is a no-op, approvals default to deny/outer policies). The agent's own
approval gate (destructive shell etc.) is built-in and unchanged.

### 5.1 Python-compat ("backwards compatibility mode") — module contract

`integrations/*.py`, same shape the python port already uses:

```python
NAME = "myexample"
DESCRIPTION = "Says hello."

TOOLS = [
    {"name": "myexample_hello",
     "description": "Greet a person by name.",
     "input_schema": {"type": "object",
                      "properties": {"who": {"type": "string"}},
                      "required": ["who"]}},
]

PROMPT = "Use myexample_hello when the user asks you to greet people."

PANEL = {  # optional — same descriptor the Swing/TUI/API menus parse
    "title": "My Example", "icon": "👋",
    "fields": [
        {"key": "MYEXAMPLE_GREETING", "label": "Greeting word",
         "type": "text", "default": "Hello"},
    ],
}

def handle(ctx, tool, args):          # ctx approximates the java Context
    if tool == "myexample_hello":
        ctx.send("Bridge running…")       # optional streaming
        ok = ctx.request_approval("Say hi to " + args["who"] + "?")
        return {"greeting": "Hello, " + args["who"] + "!" if ok else "denied"}
    return {"error": "unknown tool"}
```

How the bridge works (so you know the limits): the engine runs a tiny helper python
process per manifest and per tool call. `ctx.send/request_approval/send_file` are
delivered to the chat as events; results come back as one JSON event. So:

* tool calls are one-shot python processes — no state on ordinary variables between
  calls (use the file system),
* module-level `import`s run at discovery — a broken python integration shows up as
  FAILED in the menus instead of breaking others (same as python),
* `ctx.confirm` and richer flows from the python version are approximated; a
  request_approval defaults to yes at the python side, with the Java side enforcing
  the actual approval flow,
* keep each tool call short — `INTEGRATION_TIMEOUT=…` seconds (default 180) kills it.

Not-premium: this compat layer exists so `.py` integrations keep working when a
machine only runs the Java bot. Prefer the Java SPI for anything new.

## 6. How the agent uses these tools (the pipeline)

On every chat message:

```
runTurn
  1. built-in schemas    (Tools.*, sessions, memory, subagents, ...)
  2. INTEGRATIONS= list  → engine skips tools that "shouldn't run now":
         available(ctx)==false, malformed schema, name collision
  3. prompts:  SYSTEM_PROMPT  +  "## Integration: <name>\n<prompt>" per integration  +  memory
  4. model loop: model proposes tool calls; java built-ins handled internally,
             integration-owned calls run handle(ctx, tool, args) in caller thread,
             serialized as JSON into history, model continues (max AGENT_MAX_TURNS=20)
```

Consequences worth knowing:

* **The toolset is rebuilt every turn** — `available(ctx)` can depend on who's typing
  or where. Keep it cheap.
* **The model decides** when to call your tools: `description`/`prompt` are the docs
  it reads. Write them well.
* **Collisions**: a built-in always wins; after that the first INTEGRATIONS= entry in
  env-order wins.
* **Subagents don't get integration tools** (same as python).
* **Settings changes need a restart** to reach the agent (the .env of the agent
  process was already loaded).

## 7. Distribution checklist

1. Build your jar against `systemc-java.jar` (the released one is fine).
2. `META-INF/services/systemc.integrations.Integration` with your FQCN inside.
3. Copy the jar (or the `.py`) into `<install>/integrations/` (or use the
   "Install from .jar…" button).
4. Enable it (panel tab, TUI menu, web API or `INTEGRATIONS=` in .env).
5. Restart `systemc run` — startup logs `[integrations] enabled: <name> — java · N tools`.
