"""
HTTP API for integrations — everything the control panel's Integrations tab
does, for scripts and other programs. Mounted by webui_server.py at /api/v1
(so http://127.0.0.1:7842/api/v1/...). Full reference: API.md.

    GET  /api/v1/ping
    GET  /api/v1/integrations                        (add ?profile=<id> for one profile's settings)
    GET  /api/v1/integrations/{name}
    PUT  /api/v1/integrations/{name}/enabled        {"enabled": true}
    GET  /api/v1/integrations/{name}/settings        declared settings + current values
    PUT  /api/v1/integrations/{name}/settings        {"values": {"MY_KEY": ...}}
    ...plus whatever each integration adds under /api/v1/integrations/{name}/...

Security: every route needs  Authorization: Bearer <SYSTEMC_API_TOKEN>.  With no
token set the API refuses everything (503) rather than being open. Create the
token in the control panel (Integrations tab), or:
    python -c "import integration_settings as s; print(s.new_api_token())"

Integrations can add their own routes: define  def api_router() -> APIRouter  in
the integration file and it is mounted under /api/v1/integrations/{name}, behind
the same token. See API.md → "Adding endpoints to an integration".
"""

from __future__ import annotations

import hmac
import importlib
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

import integration_settings as settings
import integrations

PREFIX = "/api/v1"


# ── helpers for integration routers (import these inside your api_router()) ──

async def json_body(req: Request) -> dict:
    """The request's JSON object, or HTTP 400."""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    return body


def changed(**data: Any) -> dict:
    """Standard response for anything that changed .env. Settings are read at startup,
    so they apply the next time the bot starts."""
    return {**data, "restart_required": True}


def bad_request(e: Exception) -> HTTPException:
    """settings.SettingsError -> HTTP 400 (use `raise bad_request(e)`)."""
    return HTTPException(400, str(e))


# ── auth ─────────────────────────────────────────────────────────────────────

def require_token(request: Request) -> None:
    token = settings.get_api_token()                 # read per request: a new token works at once
    if not token:
        raise HTTPException(503, "API disabled: no SYSTEMC_API_TOKEN is set. "
                                 "Generate one in the control panel (Integrations tab).")
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip().encode(), token.encode()):
        raise HTTPException(401, "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"})


# ── routes ───────────────────────────────────────────────────────────────────

def _scope(profile: Optional[str]) -> Optional[str]:
    """?profile=<id> -> that profile's settings file (None = the global .env). 404 if there is no such profile."""
    if not profile:
        return None
    try:
        path = settings.profile_env_path(profile)
    except settings.SettingsError as e:
        raise bad_request(e)
    if not os.path.exists(os.path.join(settings.PROFILES_DIR, profile + ".json")):
        raise HTTPException(404, f"no profile named {profile!r}")
    return path


def _row(row: dict) -> dict:
    return {**row, "has_api": integrations.declares(row["name"], "api_router"),
            "has_settings": any(i["fields"] for i in row["submenu"])}


def build_router() -> APIRouter:
    router = APIRouter(prefix=PREFIX, dependencies=[Depends(require_token)])

    @router.get("/ping")
    async def ping():
        return {"ok": True}

    @router.get("/integrations")
    async def list_integrations(profile: Optional[str] = None):
        return {"integrations": [_row(r) for r in settings.list_integrations(_scope(profile))]}

    @router.get("/integrations/{name}")
    async def get_integration(name: str, profile: Optional[str] = None):
        try:
            return _row(settings.get_integration(name, _scope(profile)))
        except KeyError:
            raise HTTPException(404, f"no integration named {name!r} (or it opted out with PANEL = False)")

    @router.put("/integrations/{name}/enabled")
    async def set_enabled(name: str, request: Request, profile: Optional[str] = None):
        body = await json_body(request)
        if not isinstance(body.get("enabled"), bool):
            raise HTTPException(400, 'body must be {"enabled": true} or {"enabled": false}')
        try:
            return changed(**_row(settings.set_enabled(name, body["enabled"], _scope(profile))))
        except KeyError:
            raise HTTPException(404, f"no integration named {name!r} (or it opted out with PANEL = False)")

    # Each recognised integration that defines api_router() gets its own sub-tree.
    for row in settings.list_integrations():
        name = row["name"]
        if not integrations.declares(name, "api_router"):
            continue
        try:
            sub = importlib.import_module(f"integrations.{name}").api_router()
            router.include_router(sub, prefix=f"/integrations/{name}", dependencies=[Depends(require_token)])
        except Exception as e:                       # one broken integration must not take the API down
            print(f"[api] integration '{name}': API not mounted ({type(e).__name__}: {e})", flush=True)

    # Generic settings for any integration that declares PANEL "fields". Registered after the
    # loop above, so an integration that defines its own /settings route wins.
    @router.get("/integrations/{name}/settings")
    async def get_settings(name: str, profile: Optional[str] = None):
        try:
            return settings.get_settings(name, _scope(profile))
        except KeyError:
            raise HTTPException(404, f"no integration named {name!r}")

    @router.put("/integrations/{name}/settings")
    async def put_settings(name: str, request: Request, profile: Optional[str] = None):
        values = (await json_body(request)).get("values")
        if not isinstance(values, dict) or not values:
            raise HTTPException(400, 'body must be {"values": {"MY_KEY": "value", ...}}')
        try:
            return changed(**settings.set_settings(name, values, _scope(profile)))
        except KeyError:
            raise HTTPException(404, f"no integration named {name!r}")
        except settings.SettingsError as e:
            raise bad_request(e)

    return router
