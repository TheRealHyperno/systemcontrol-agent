"""
Tiny FastAPI server that gives the web UI access to local tools.
Runs on localhost:7842, called by webui.html via fetch().

The browser can't run shell commands — this server can. It exposes the
same tools as the Discord bot (run_shell, read_file, write_file, list_dir)
plus an endpoint to proxy Ollama streaming so the UI gets real-time tokens.

Start with: python webui_server.py
Then open:  http://localhost:7842
"""

import os
import sys
import json
import asyncio
import httpx
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# Import our existing tool implementations
sys.path.insert(0, str(Path(__file__).parent))
import tools

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# Integrations API (/api/v1/...): token-protected, see API.md. Never fatal.
try:
    import integrations_api
    app.include_router(integrations_api.build_router())
except Exception as e:
    print(f"[api] integrations API not loaded: {type(e).__name__}: {e}", flush=True)

# Profiles & Multi-Instance API (/api/v1/profiles/...)
try:
    import profiles_api
    app.include_router(profiles_api.build_router())
except Exception as e:
    print(f"[api] profiles API not loaded: {type(e).__name__}: {e}", flush=True)


# ── Serve the UI ─────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    html_path = Path(__file__).parent / "webui.html"
    return HTMLResponse(html_path.read_text())


# ── Tool endpoints ────────────────────────────────────────────────────────────
@app.post("/tools/run_shell")
async def api_run_shell(req: Request):
    body = await req.json()
    command = body.get("command", "")
    result = tools.run_shell(command)
    return JSONResponse(result)


@app.post("/tools/run_shell_elevated")
async def api_run_shell_elevated(req: Request):
    body = await req.json()
    command = body.get("command", "")
    if not tools.elevated_enabled():
        return JSONResponse({"error": "elevated commands are turned off on this machine",
                             "exit_code": -1}, status_code=403)
    result = tools.run_shell_elevated(command)
    return JSONResponse(result)


@app.post("/tools/read_file")
async def api_read_file(req: Request):
    body = await req.json()
    return JSONResponse(tools.read_file(body.get("path", "")))


@app.post("/tools/write_file")
async def api_write_file(req: Request):
    body = await req.json()
    return JSONResponse(tools.write_file(body.get("path", ""), body.get("content", "")))


@app.post("/tools/list_dir")
async def api_list_dir(req: Request):
    body = await req.json()
    return JSONResponse(tools.list_dir(body.get("path", ".")))


@app.post("/tools/run_python")
async def api_run_python(req: Request):
    body = await req.json()
    return JSONResponse(tools.run_python(body.get("code", "")))


@app.post("/tools/web_search")
async def api_web_search(req: Request):
    body = await req.json()
    result = await tools.web_search(body.get("query", ""), body.get("max_results", 5))
    return JSONResponse(result)


# ── Ollama streaming proxy ────────────────────────────────────────────────────
# The browser can't hit Ollama directly due to CORS on some setups,
# and we need to intercept tool calls server-side anyway.
@app.post("/ollama/chat")
async def proxy_chat(req: Request):
    body = await req.json()

    async def stream():
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                f"{OLLAMA_HOST}/api/chat",
                json=body,
            ) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk

    return StreamingResponse(stream(), media_type="application/x-ndjson")


# ── Config endpoint (so the UI knows which model to use) ─────────────────────
@app.get("/config")
async def get_config():
    def _load_dotenv():
        env = {}
        p = Path(__file__).parent / ".env"
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
        return env

    env = _load_dotenv()
    return JSONResponse({
        "model":   os.environ.get("AGENT_MODEL", env.get("AGENT_MODEL", "qwen2.5")),
        "workdir": tools.WORKDIR,
    })


if __name__ == "__main__":
    # Load .env
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

    print("╔══════════════════════════════════════╗")
    print("║   systemc web UI                    ║")
    print("╚══════════════════════════════════════╝")
    print(f"  open → http://localhost:7842")
    print(f"  model → {os.environ.get('AGENT_MODEL', 'qwen2.5')}")
    print(f"  workspace → {tools.WORKDIR}")
    try:
        import integration_settings
        print("  api → http://localhost:7842/api/v1  "
              + ("(token set)" if integration_settings.get_api_token()
                 else "(disabled: create a token in the control panel)"))
    except Exception:
        pass
    print()
    uvicorn.run(app, host="127.0.0.1", port=7842, log_level="warning")
