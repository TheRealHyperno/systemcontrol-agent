"""
Experimental JavaScript integration host.

A JS integration is a Node.js script that follows a tiny JSON protocol:

  node integration.js manifest   -> prints a JSON manifest on stdout
  echo '{...}' | node integration.js run -> reads a call envelope on stdin,
                                            prints a JSON result on stdout

Manifest shape:
  {
    "name": "hello_js",
    "description": "optional",
    "prompt": "optional system-prompt text",
    "tools": [ {"name", "description", "input_schema"} ... ]
  }

Call envelope:
  { "tool": "tool_name", "args": {...}, "context": {author_id, author_name, platform} }

Stdout result:
  any JSON value (usually an object). Non-zero exit / bad JSON -> error.

Enabled only when JS_INTEGRATIONS_ENABLED=... is set in .env (see control
panel -> Experiments). The .js files live next to Python integrations and
must NOT start with "_".
"""

import asyncio
import json
import os
import subprocess
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTEGRATIONS_DIR = os.path.join(APP_DIR, "integrations")
import shutil as _shutil
NODE = _shutil.which("node") or "node"

JS_INTEGRATIONS_ENV = os.environ.get("JS_INTEGRATIONS", "").strip()


def enabled() -> bool:
    return os.environ.get("JS_INTEGRATIONS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _discover_files():
    roots = [INTEGRATIONS_DIR, os.path.join(INTEGRATIONS_DIR, "js")]
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for fn in sorted(os.listdir(root)):
            if not fn.endswith(".js") or fn.startswith("_"):
                continue
            allowed = None
            if JS_INTEGRATIONS_ENV:
                names = [x.strip() for x in JS_INTEGRATIONS_ENV.replace(";", ",").split(",") if x.strip()]
                if "all" not in (x.lower() for x in names):
                    allowed = set(names)
            if allowed is not None and fn not in allowed and os.path.splitext(fn)[0] not in allowed:
                continue
            found.append(os.path.join(root, fn))
    return found


def _run_sync(path, args, stdin_data=None, timeout=15):
    try:
        proc = subprocess.run(
            [NODE, path] + list(args),
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=APP_DIR,
        )
    except Exception as e:
        return None, str(e)
    if proc.returncode != 0:
        return None, proc.stderr.strip() or f"exit {proc.returncode}"
    return proc.stdout, ""


def _manifest(path):
    out, err = _run_sync(path, ["manifest"])
    if out is None:
        print(f"[js] manifest failed for {path}: {err}", flush=True)
        return None
    # Some tools print logs; take the last non-empty line.
    for line in reversed([ln for ln in out.splitlines() if ln.strip()]):
        try:
            m = json.loads(line)
            if isinstance(m, dict) and isinstance(m.get("name"), str):
                return m
        except Exception:
            continue
    print(f"[js] no valid manifest JSON from {path}", flush=True)
    return None


class JsIntegration:
    def __init__(self, path, manifest):
        self._path = path
        self._manifest = manifest
        self.NAME = manifest.get("name") or os.path.splitext(os.path.basename(path))[0]
        self.DESCRIPTION = manifest.get("description", "")
        self.PROMPT = manifest.get("prompt", "") or ""
        self.TOOLS = manifest.get("tools") or []

    def available(self, ctx):
        return True

    async def handle(self, ctx, name, args):
        try:
            from integrations import _js_runtime as selfmod  # noqa
        except Exception:
            selfmod = sys.modules.get("integrations._js_runtime")
        envelope = {
            "tool": name,
            "args": args or {},
            "context": {
                "author_id": getattr(ctx, "author_id", None),
                "author_name": getattr(ctx, "author_name", None),
                "platform": getattr(ctx, "platform", None),
            },
        }
        proc = await asyncio.create_subprocess_exec(
            NODE, self._path, "run",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=APP_DIR,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(json.dumps(envelope).encode()), timeout=30
            )
        except asyncio.TimeoutError:
            proc.kill()
            return {"error": "JS integration timed out"}
        if proc.returncode != 0:
            return {"error": (stderr.decode("utf-8", "replace").strip()
                              or f"exit {proc.returncode}")}
        text = stdout.decode("utf-8", "replace").strip()
        if not text:
            return {"error": "no output from JS integration"}
        for line in reversed([ln for ln in text.splitlines() if ln.strip()]):
            try:
                return json.loads(line)
            except Exception:
                continue
        return {"error": f"invalid JSON from JS integration: {text[:200]}"}


def discover_js():
    """Return {name: JsIntegration instance} for discovered JS integrations."""
    if not enabled():
        return {}
    out = {}
    for path in _discover_files():
        m = _manifest(path)
        if not m:
            continue
        js = JsIntegration(path, m)
        out[js.NAME] = js
    return out
