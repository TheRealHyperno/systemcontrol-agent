"""
Integrations — drop-in tool packs for the agent.

An integration is one Python file in this folder. Drop it in, add its name to
INTEGRATIONS in .env, restart, and the agent gets its tools. See README.md here
and _example.py for a minimal working one.

    INTEGRATIONS=discord_admin,my_other_one      # comma-separated file names
    INTEGRATIONS=all                              # everything in this folder

Nothing is enabled by default: integrations can be powerful (discord_admin can
ban people), so you opt in explicitly.

Module contract (only TOOLS and handle are required):

    NAME        = "my_integration"        # defaults to the file name
    DESCRIPTION = "One line for humans"
    PROMPT      = "Extra system-prompt text explaining how to use the tools"
    TOOLS       = [ {name, description, input_schema}, ... ]   # same shape as tools.py
    def available(ctx) -> bool            # optional: hide the tools in this chat
    async def handle(ctx, name, args) -> dict    # (a plain def works too)
    PANEL = {...}                         # optional: shows the integration in the control
                                          # panel's Integrations tab (see panel_entries below)

`ctx` is an integrations.Context: which channel/platform the message came from,
WHO sent it (author_id comes from the platform itself, never from message text,
so it can't be spoofed), plus ctx.confirm(text) to ask for a yes/no approval
and ctx.send(text) to post in the chat.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import os
import pkgutil
import re
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional


@dataclass
class Context:
    channel: Any                                    # exactly what the bot handed run_turn
    platform: str                                   # "discord" | "stoat" | other
    author_id: Optional[str]                        # platform-verified sender ID
    author_name: Optional[str]
    confirm: Callable[[str], Awaitable[bool]]       # ask for approval (chat reaction / control panel)
    send: Callable[[str], Awaitable[Any]]           # post a message in this chat

    @property
    def raw_channel(self):
        """The platform's own channel object (unwraps chat_platform wrappers)."""
        return getattr(self.channel, "_ch", self.channel)


_cache: dict[str, Any] = {}
_errors: dict[str, str] = {}


def discover() -> dict[str, Any]:
    """Import every integration module in this folder (cached)."""
    if _cache or _errors:
        return _cache
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
            _cache[info.name] = mod
        except Exception as e:                      # a broken integration must not break the bot
            _errors[info.name] = f"{type(e).__name__}: {e}"
            print(f"[integrations] failed to load {info.name}: {e}", flush=True)
            traceback.print_exc()
    return _cache


def enabled_names() -> list[str]:
    raw = os.environ.get("INTEGRATIONS", "")
    names = [n.strip() for n in raw.replace(";", ",").split(",") if n.strip()]
    if "all" in (n.lower() for n in names):
        return sorted(discover())
    return names


def status() -> list[dict]:
    """For the control panel / startup log: what exists, what's on."""
    found, on = discover(), set(enabled_names())
    rows = [
        {"name": n, "enabled": n in on, "description": getattr(m, "DESCRIPTION", ""),
         "tools": len(getattr(m, "TOOLS", []))}
        for n, m in sorted(found.items())
    ]
    rows += [{"name": n, "enabled": n in on, "error": e} for n, e in sorted(_errors.items())]
    return rows


# ── Control panel support ────────────────────────────────────────────────────
# The control panel's Integrations tab lists every integration that declares a
# module-level PANEL field. The files are READ, never imported (via ast), so the
# panel can show an integration even when its libraries aren't installed, and
# nothing in the file runs.
#
#     PANEL = {
#         "title": "Discord Admin",              # optional; defaults to NAME / file name
#         "icon": "🛡️",                          # optional
#         "submenu": [                           # optional; shown under the integration
#             {"id": "admins", "title": "Admins", "description": "Who may use the tools"},
#         ],
#     }
#
# Every integration file gets a row in the tab, PANEL or not (PANEL = False opts
# out). A PANEL only adds a title, icon and settings.
#
# SETTINGS PAGES COME FOR FREE. List the settings under "fields" (on PANEL for a
# single page, or on each submenu entry for one page per entry) and both the
# control panel and the HTTP API build, validate and save them (to .env) with no
# other code. A field is plain data:
#
#     {"key": "MY_THING_GREETING",   # .env name; must start with MY_THING_ (the file name)
#      "label": "Greeting",          # optional, defaults to the key
#      "type": "text",               # text | secret | number | bool | choice | list
#      "default": "hello",           # optional
#      "description": "Shown under the field",
#      "choices": ["a", "b"],        # choice only
#      "min": 0, "max": 10,          # number only
#      "pattern": "^\\d+$",          # text / list items: regex that must match
#      "required": False}           # True only adds a * in the UI; it never blocks saving
#
# Read the value in your integration with  integrations.get_setting("MY_THING_GREETING").
# Fields with an invalid key or type are skipped and reported in the row's "problems".
# Submenu entries with no "fields" and no hand-written page still show a stub.

def _module_files() -> list[Path]:
    return sorted(p for p in Path(__file__).parent.glob("*.py") if not p.name.startswith("_"))


def available_names() -> list[str]:
    """File names (without .py) of every loadable integration, no importing."""
    return [p.stem for p in _module_files()]


def parse_enabled(raw: str, available: list[str]) -> list[str]:
    """The INTEGRATIONS value -> list of names. \"all\" expands to `available`."""
    names = [n.strip() for n in (raw or "").replace(";", ",").split(",") if n.strip()]
    if "all" in (n.lower() for n in names):
        return sorted(available)
    return names


def set_enabled(raw: str, name: str, on: bool, available: list[str]) -> str:
    """Return a new INTEGRATIONS value with `name` switched on or off.
    Names that aren't in the folder are left alone so nobody's config gets lost."""
    names = [n for n in parse_enabled(raw, available) if n != name]
    if on:
        names.append(name)
    return ",".join(dict.fromkeys(names))


def _read_fields(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            names = [node.target.id]
        else:
            continue
        for n in names:
            if n in ("NAME", "DESCRIPTION", "PANEL"):
                try:
                    out[n] = ast.literal_eval(node.value)
                except Exception:
                    pass
    return out


def declares(name: str, attr: str) -> bool:
    """Does integrations/<name>.py define a top-level `attr` (a def, class or assignment)?
    Checked by reading the file, so the integration is not imported."""
    try:
        tree = ast.parse((Path(__file__).parent / f"{name}.py").read_text(encoding="utf-8"))
    except Exception:
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == attr:
            return True
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == attr for t in node.targets):
            return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == attr:
            return True
    return False


FIELD_TYPES = ("text", "secret", "number", "bool", "choice", "list")
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def key_prefix(name: str) -> str:
    """Every setting key an integration declares must start with this (its file name,
    upper-cased). That keeps integrations from colliding with each other or with
    the app's own .env keys (INTEGRATIONS, SYSTEMC_API_TOKEN, DISCORD_TOKEN, ...)."""
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") + "_"


def normalize_fields(name: str, raw: Any, problems: Optional[list] = None) -> list[dict]:
    """Validate an integration's declared `fields`. Bad entries are skipped and
    described in `problems` (so the panel can tell the author what to fix)."""
    problems = problems if problems is not None else []
    out, seen = [], set()
    prefix = key_prefix(name)
    for i, f in enumerate(raw if isinstance(raw, (list, tuple)) else []):
        if not isinstance(f, dict):
            problems.append(f"field #{i + 1}: must be a dict")
            continue
        key = str(f.get("key") or "")
        ftype = str(f.get("type") or "text").lower()
        if not _KEY_RE.match(key):
            problems.append(f"field #{i + 1}: key {key!r} must be UPPER_SNAKE_CASE")
            continue
        if not key.startswith(prefix):
            problems.append(f"{key}: key must start with {prefix}")
            continue
        if key in seen:
            problems.append(f"{key}: declared twice")
            continue
        if ftype not in FIELD_TYPES:
            problems.append(f"{key}: type must be one of {', '.join(FIELD_TYPES)}")
            continue
        choices = [str(c) for c in f.get("choices") or []] if ftype == "choice" else []
        if ftype == "choice" and not choices:
            problems.append(f"{key}: a choice field needs a non-empty 'choices' list")
            continue
        pattern = str(f.get("pattern") or "")
        if pattern:
            try:
                re.compile(pattern)
            except re.error:
                problems.append(f"{key}: 'pattern' is not a valid regex")
                continue
        seen.add(key)
        default = f.get("default")
        out.append({
            "key": key,
            "label": str(f.get("label") or key),
            "type": ftype,
            "description": str(f.get("description") or ""),
            "default": default if default is not None else ([] if ftype == "list" else None),
            "choices": choices,
            "min": f.get("min") if isinstance(f.get("min"), (int, float)) else None,
            "max": f.get("max") if isinstance(f.get("max"), (int, float)) else None,
            "pattern": pattern,
            "required": bool(f.get("required")),
        })
    return out


def _submenu(raw: Any, name: str, problems: list) -> list[dict]:
    items = []
    for i, it in enumerate(raw if isinstance(raw, (list, tuple)) else []):
        if isinstance(it, str):
            it = {"title": it}
        if not isinstance(it, dict) or not it.get("title"):
            continue
        title = str(it["title"])
        items.append({
            "id": str(it.get("id") or title.lower().replace(" ", "_") or i),
            "title": title,
            "description": str(it.get("description") or ""),
            "fields": normalize_fields(name, it.get("fields"), problems),
        })
    return items


def panel_entries(raw_enabled: Optional[str] = None) -> list[dict]:
    """Rows for the control panel: every integration file, minus those that set PANEL = False.

    raw_enabled is the INTEGRATIONS value to judge "enabled" by (the panel
    passes what is in .env); defaults to the process environment.
    """
    files = _module_files()
    if raw_enabled is None:
        raw_enabled = os.environ.get("INTEGRATIONS", "")
    on = set(parse_enabled(raw_enabled, [p.stem for p in files]))
    rows = []
    for p in files:
        try:
            f = _read_fields(p)
        except Exception:                           # unreadable file: the startup log reports it
            continue
        panel = f.get("PANEL")
        if panel is False:
            continue
        if panel is None or panel is True or not isinstance(panel, dict):
            panel = {}
        problems: list = []
        submenu = _submenu(panel.get("submenu"), p.stem, problems)
        general = normalize_fields(p.stem, panel.get("fields"), problems)
        if general:                                 # PANEL-level fields -> a page of their own
            submenu.insert(0, {"id": "settings", "title": "Settings" if not submenu else "General",
                               "description": "", "fields": general})
        rows.append({
            "name": p.stem,
            "title": str(panel.get("title") or f.get("NAME") or p.stem),
            "icon": str(panel.get("icon") or ""),
            "description": str(f.get("DESCRIPTION") or ""),
            "enabled": p.stem in on,
            "submenu": submenu,
            "problems": problems,
        })
    return rows


_index_cache: tuple = (None, {})


def _field_index() -> dict[str, dict]:
    """{KEY: field} for every declared setting. Cached until an integration file changes,
    because get_setting() is called from available()/handle() on every turn."""
    global _index_cache
    sig = tuple((p.name, p.stat().st_mtime_ns) for p in _module_files())
    if _index_cache[0] != sig:
        idx: dict[str, dict] = {}
        for row in panel_entries(""):
            for item in row["submenu"]:
                for fld in item["fields"]:
                    idx.setdefault(fld["key"], fld)
        _index_cache = (sig, idx)
    return _index_cache[1]


def coerce(field: dict, raw: Optional[str]) -> Any:
    """A .env string -> the Python value for `field` (falls back to the default
    when unset or unparseable). Used by get_setting; the settings layer validates
    separately on the way in."""
    t, default = field["type"], field["default"]
    if raw is None or raw == "":
        return default
    if t == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if t == "number":
        try:
            n = float(raw)
        except ValueError:
            return default
        return int(n) if n.is_integer() and "." not in raw else n
    if t == "list":
        return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
    return raw


def get_setting(key: str, default: Any = None) -> Any:
    """The current value of a declared setting, typed (bool / int-or-float / list / str).
    Reads the process environment, which is filled from .env at startup — so, like
    every setting here, a change applies after the bot restarts. An undeclared key
    returns the raw string from the environment, or `default`."""
    field = _field_index().get(key)
    raw = os.environ.get(key)
    if field is None:
        return default if raw is None else raw
    value = coerce(field, raw)
    return default if value is None else value


def print_startup_summary() -> None:
    names = enabled_names()
    if not names:
        return
    found = discover()
    for n in names:
        if n in found:
            print(f"[integrations] enabled: {n} ({len(getattr(found[n], 'TOOLS', []))} tools)", flush=True)
        elif n not in _errors:
            print(f"[integrations] WARNING: '{n}' is in INTEGRATIONS but no integrations/{n}.py exists", flush=True)


def _valid_schema(s: Any) -> bool:
    return (
        isinstance(s, dict) and isinstance(s.get("name"), str) and s.get("description")
        and isinstance(s.get("input_schema"), dict)
    )


class Toolset:
    """What the agent gets for ONE turn: extra tool schemas, prompt text, a dispatcher."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.schemas: list[dict] = []
        self.prompt: str = ""
        self._owner: dict[str, Any] = {}

    def owns(self, tool_name: str) -> bool:
        return tool_name in self._owner

    async def call(self, tool_name: str, args: dict) -> Any:
        mod = self._owner[tool_name]
        res = mod.handle(self.ctx, tool_name, args or {})
        if inspect.isawaitable(res):
            res = await res
        return res


def build(ctx: Context, reserved: set[str] | None = None) -> Toolset:
    """Assemble the tools of every enabled, available integration for this turn."""
    ts = Toolset(ctx)
    taken = set(reserved or ())
    prompts = []
    found = discover()
    for name in enabled_names():
        mod = found.get(name)
        if mod is None:
            continue
        try:
            avail = getattr(mod, "available", None)
            if avail is not None and not avail(ctx):
                continue
            added = 0
            for schema in getattr(mod, "TOOLS", []):
                if not _valid_schema(schema):
                    print(f"[integrations] {name}: skipped malformed tool schema", flush=True)
                    continue
                if schema["name"] in taken:
                    print(f"[integrations] {name}: tool name '{schema['name']}' already taken — skipped", flush=True)
                    continue
                taken.add(schema["name"])
                ts.schemas.append(schema)
                ts._owner[schema["name"]] = mod
                added += 1
            if added and getattr(mod, "PROMPT", ""):
                prompts.append(f"## Integration: {getattr(mod, 'NAME', name)}\n{mod.PROMPT.strip()}")
        except Exception as e:
            print(f"[integrations] {name}: error while building tools: {e}", flush=True)
    # Experimental JavaScript integrations (opt-in).
    try:
        from integrations import _js_runtime as _jsrt
        if _jsrt.enabled():
            for _name, _js in _jsrt.discover_js().items():
                try:
                    _avail = getattr(_js, "available", None)
                    if _avail is not None and not _avail(ctx):
                        continue
                    _added = 0
                    for _schema in getattr(_js, "TOOLS", []):
                        if not _valid_schema(_schema):
                            print(f"[integrations] {_name}: skipped malformed tool schema", flush=True)
                            continue
                        if _schema["name"] in taken:
                            print(f"[integrations] {_name}: tool name '{_schema['name']}' already taken — skipped", flush=True)
                            continue
                        taken.add(_schema["name"])
                        ts.schemas.append(_schema)
                        ts._owner[_schema["name"]] = _js
                        _added += 1
                    if _added and getattr(_js, "PROMPT", ""):
                        prompts.append(f"## Integration: {getattr(_js, 'NAME', _name)}\n{_js.PROMPT.strip()}")
                except Exception as _e:
                    print(f"[integrations] {_name}: error while building JS tools: {_e}", flush=True)
    except Exception as _e:
        print(f"[integrations] JS runtime unavailable: {_e}", flush=True)

    ts.prompt = "\n\n".join(prompts)
    return ts
