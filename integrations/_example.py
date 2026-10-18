"""
Minimal example integration. Copy this file, rename it (no leading underscore),
and add its name to INTEGRATIONS in .env.

Files starting with "_" are never loaded, so this one is just documentation.
"""

NAME = "example"
DESCRIPTION = "Says hello — a template for writing your own integration."

# Optional: customises how this integration looks in the control panel's Integrations tab.
# Every integration file is listed there automatically; PANEL = False hides it.
#
# "fields" gives it a working Settings page with no extra code: the control panel draws the
# inputs, the HTTP API gets GET/PUT .../integrations/example/settings, and values are validated
# and saved to .env for you. Keys must start with the file name in capitals (EXAMPLE_ here) —
# rename them when you rename the file.
# Types: text | secret | number | bool | choice | list.
# Use several pages with "submenu": [{"id": ..., "title": ..., "fields": [...]}, ...].
PANEL = {
    "title": "Example",
    "icon": "👋",
    "fields": [
        {"key": "EXAMPLE_GREETING", "label": "Greeting", "type": "text", "default": "Hello",
         "description": "The word example_hello starts with."},
        {"key": "EXAMPLE_SHOUT", "label": "Shout it", "type": "bool", "default": False},
    ],
}

# Optional: text appended to the system prompt while this integration is active.
PROMPT = "Use example_hello when the user asks you to greet someone."

TOOLS = [
    {
        "name": "example_hello",
        "description": "Return a greeting for the given name.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Who to greet"}},
            "required": ["name"],
        },
    },
]


def available(ctx) -> bool:
    """Optional. Return False to hide these tools in this particular chat,
    e.g. `return ctx.platform == "discord"`."""
    return True


async def handle(ctx, name: str, args: dict) -> dict:
    """Called when the model uses one of TOOLS. Return a JSON-serialisable dict;
    return {"error": "..."} on failure so the model can explain what went wrong.

    ctx.author_id / ctx.author_name  — who asked (verified by the platform)
    await ctx.confirm("Do X?")       — ask for a yes/no approval first
    await ctx.send("text")           — post a message in the chat
    """
    if name == "example_hello":
        from integrations import get_setting          # typed value of a declared field
        greeting = f"{get_setting('EXAMPLE_GREETING', 'Hello')}, {args.get('name', 'world')}! (asked by {ctx.author_name})"
        return {"greeting": greeting.upper() if get_setting("EXAMPLE_SHOUT") else greeting}
    return {"error": f"unknown tool: {name}"}
