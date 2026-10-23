"""
Profiles & Multi-Instance API router for SystemC Drovex.
Mounted by webui_server.py at /api/v1/profiles.

Every route needs  Authorization: Bearer <SYSTEMC_API_TOKEN>  (the same token as the
integrations API; with no token set the API refuses everything with 503).

  GET    /api/v1/profiles                    list profiles + running state
  GET    /api/v1/profiles/{id}               one profile
  POST   /api/v1/profiles                    create, or update the profile with that "id"
  DELETE /api/v1/profiles/{id}               delete (not "default", not while running)
  POST   /api/v1/profiles/{id}/start | stop  start / stop that bot instance
  POST   /api/v1/profiles/{id}/install_deps  pip install the profile's dependencies
  POST   /api/v1/profiles/migrate            import the legacy .env into a profile
  GET    /api/v1/profiles/{id}/logs?limit=N  recent log lines

Credentials (bot tokens, API keys) are never returned: they come back as {"set": true|false}.
On update, a credential you leave out, send as null, or send back as an object is kept as is;
send a string to replace it, or "" to clear it. An update only changes the fields you send.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request

import profiles
from profiles import BotProfile, SECRET_FIELDS, manager, supervisor, valid_profile_id, slugify

try:
    from integrations_api import require_token
except Exception:                                    # integrations API unavailable: same rule, local copy
    import hmac
    import integration_settings as _settings

    def require_token(request: Request) -> None:
        token = _settings.get_api_token()
        if not token:
            raise HTTPException(503, "API disabled: create an API token in the control panel first.")
        scheme, _, supplied = (request.headers.get("authorization") or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip().encode(), token.encode()):
            raise HTTPException(401, "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"})

router = APIRouter(prefix="/api/v1/profiles", tags=["profiles"], dependencies=[Depends(require_token)])


def _check_id(profile_id: str) -> str:
    if not valid_profile_id(profile_id):
        raise HTTPException(status_code=400, detail="Invalid profile id (use a-z, 0-9, _ and -).")
    return profile_id


def _require(profile_id: str) -> BotProfile:
    prof = manager.get_profile(_check_id(profile_id))
    if not prof:
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found.")
    return prof


@router.get("")
async def list_profiles():
    """List all profiles along with current running state and process info."""
    overview = supervisor.get_status_overview()
    active_prof = manager.get_active_profile()
    return {
        "profiles": [p.to_public_dict() for p in manager.list_profiles()],
        "status": overview,
        "active_profile_id": active_prof.id if active_prof else "default",
        "running_count": sum(1 for item in overview if item["running"]),
    }


@router.post("/migrate")
async def migrate_env(req: Request):
    """Migrate the legacy .env into a profile. Only .env files inside the SystemC folder are accepted."""
    try:
        body = await req.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}

    target_id = str(body.get("target_id") or "default")
    env_file = os.path.realpath(str(body.get("env_path") or profiles.LEGACY_ENV_FILE))
    app_dir = os.path.realpath(profiles.APP_DIR)
    if os.path.dirname(env_file) != app_dir or not os.path.basename(env_file).startswith(".env"):
        raise HTTPException(status_code=400, detail="env_path must be an .env file inside the SystemC folder.")

    ok, msg = manager.migrate_from_env(env_path=env_file, target_id=_check_id(target_id))
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    prof = manager.get_profile(target_id)
    return {"ok": True, "message": msg, "profile": prof.to_public_dict() if prof else None}


@router.get("/{profile_id}")
async def get_profile(profile_id: str):
    """Retrieve the configuration of a single profile (credentials masked)."""
    prof = _require(profile_id)
    return {"profile": prof.to_public_dict(), "running": supervisor.is_running(profile_id)}


@router.post("")
async def create_or_update_profile(req: Request):
    """Create a new profile, or update the existing one with the same id (only the sent fields change)."""
    try:
        data = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object.")

    pid = str(data.get("id") or "").strip() or slugify(str(data.get("name") or ""), "custom_bot")
    _check_id(pid)

    existing = manager.get_profile(pid)
    merged: Dict[str, Any] = existing.to_dict() if existing else {}
    for key, value in data.items():
        if key in ("id", "created_at", "updated_at"):
            continue
        if key in SECRET_FIELDS and (value is None or isinstance(value, dict)):
            continue                                  # masked placeholder echoed back: keep the stored secret
        merged[key] = value
    merged["id"] = pid
    if existing:
        merged["created_at"] = existing.created_at

    try:
        profile = BotProfile.from_dict(merged)
        manager.save_profile(profile)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "ok": True,
        "created": existing is None,
        "profile": profile.to_public_dict(),
        "message": f"Profile '{profile.name}' ({profile.id}) saved successfully.",
    }


@router.delete("/{profile_id}")
async def delete_profile(profile_id: str):
    """Delete a profile (unless it is currently running or is the default)."""
    _check_id(profile_id)
    if profile_id == "default":
        raise HTTPException(status_code=400, detail="The default profile cannot be deleted.")
    if supervisor.is_running(profile_id):
        raise HTTPException(status_code=400, detail=f"Cannot delete profile '{profile_id}' while it is running. Stop it first.")
    _require(profile_id)
    if not manager.delete_profile(profile_id):
        raise HTTPException(status_code=400, detail=f"Could not delete profile '{profile_id}'.")
    return {"ok": True, "message": f"Profile '{profile_id}' deleted."}


@router.post("/{profile_id}/start")
async def start_instance(profile_id: str):
    """Start the bot instance for this profile."""
    _require(profile_id)
    ok, msg = supervisor.start_instance(profile_id)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg, "running": True}


@router.post("/{profile_id}/stop")
async def stop_instance(profile_id: str):
    """Stop the bot instance for this profile."""
    _require(profile_id)
    ok, msg = supervisor.stop_instance(profile_id)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg, "running": False}


@router.post("/{profile_id}/install_deps")
async def install_dependencies(profile_id: str):
    """pip-install this profile's custom dependencies (runs off the event loop)."""
    import asyncio
    prof = _require(profile_id)
    log_q = supervisor.get_log_queue(profile_id)
    ok, msg = await asyncio.get_running_loop().run_in_executor(
        None, lambda: manager.pull_dependencies(profile_id, log_queue=log_q))
    return {"ok": ok, "message": msg, "dependencies": prof.dependencies}


@router.get("/{profile_id}/logs")
async def get_logs(profile_id: str, limit: int = 100):
    """Recent log lines for a bot instance (non-destructive)."""
    _check_id(profile_id)
    limit = max(1, min(int(limit), 1000))
    lines = [item[1] for item in supervisor.get_log_queue(profile_id).history(limit)]
    return {"profile_id": profile_id, "lines": lines, "is_running": supervisor.is_running(profile_id)}


def build_router() -> APIRouter:
    return router
