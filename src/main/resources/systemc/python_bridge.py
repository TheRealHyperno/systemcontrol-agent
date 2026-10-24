# thin bridge the Java integrations engine uses for python-compat mode.
# manifest mode: imports a module file and prints JSON {NAME, DESCRIPTION, TOOLS, PROMPT, PANEL}
# call mode:     reads {"tool","args","ctx"} from stdin, calls handle(ctx, tool, args),
#                streams NDJSON events (send/request_approval/send_file/result) to stdout.

import importlib.util
import json
import os
import sys


def load(path):
    name = "sysc_int_" + os.path.splitext(os.path.basename(path))[0].replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def emit(ev):
    sys.stdout.write(json.dumps(ev) + "\n")
    sys.stdout.flush()


class Ctx:
    def __init__(self, platform="", channel_id="", author_id="", author_name=""):
        self.platform = platform
        self.channel_id = channel_id
        self.author_id = author_id or ""
        self.author = {"id": author_id or "", "name": author_name}
        self.author_name = author_name
        self.confirm = None

    def send(self, text):
        emit({"event": "send", "text": str(text)})

    def send_file(self, path, caption=""):
        emit({"event": "send_file", "path": str(path), "caption": str(caption or "")})

    def request_approval(self, text="approve?", timeout=60):
        ok = True   # compat: auto-approve; the Java side is authoritative anyway
        emit({"event": "request_approval", "text": str(text), "timeout": int(timeout)})
        return ok


def safe(v):
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return str(v)


def manifest(path):
    mod = load(path)
    out = {
        "NAME": getattr(mod, "NAME", os.path.splitext(os.path.basename(path))[0]),
        "DESCRIPTION": getattr(mod, "DESCRIPTION", ""),
        "TOOLS": [],
    }
    tools = getattr(mod, "TOOLS", None)
    if isinstance(tools, (list, tuple)):
        for t in tools:
            if isinstance(t, dict) and "name" in t:
                out["TOOLS"].append({
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "input_schema": t.get("input_schema") or {"type": "object", "properties": {}},
                })
    if getattr(mod, "PROMPT", None):
        out["PROMPT"] = mod.PROMPT
    if getattr(mod, "PANEL", None) and isinstance(mod.PANEL, dict):
        out["PANEL"] = mod.PANEL
    print(json.dumps(out))


def call(path):
    request = json.loads(sys.stdin.readline() or "{}")
    tool = request.get("tool") or ""
    args = request.get("args") or {}
    info = request.get("ctx") or {}
    ctx = Ctx(info.get("platform", ""), info.get("channel_id", ""),
              info.get("author_id", ""), info.get("author_name", ""))
    mod = load(path)
    handle = getattr(mod, "handle", None)
    if not callable(handle):
        emit({"event": "result", "data": {"error": f"{os.path.basename(path)} has no handle(ctx, tool, args)"}})
        return
    try:
        emit({"event": "result", "data": safe(handle(ctx, tool, args))})
    except Exception as e:
        emit({"event": "result", "data": {"error": f"{type(e).__name__}: {e}"}})


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "manifest":
        manifest(sys.argv[2])
    elif mode == "call":
        call(sys.argv[2])
    else:
        print("usage", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
