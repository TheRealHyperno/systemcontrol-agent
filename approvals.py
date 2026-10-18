"""
Tiny file-based approval queue shared between bot.py (writer/waiter) and
control_panel.py (reader/resolver), so destructive commands can be approved
from the control panel's Approvals tab without Discord open.

Each pending request is one JSON file in .approvals/<id>.json:
  {"id", "command", "status": "pending"|"approved"|"denied", "created_at"}
"""

import json
import os
import time
import uuid

APPROVALS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".approvals")
os.makedirs(APPROVALS_DIR, exist_ok=True)


def _path(req_id: str) -> str:
    return os.path.join(APPROVALS_DIR, f"{req_id}.json")


def create(command: str) -> str:
    req_id = uuid.uuid4().hex[:8]
    with open(_path(req_id), "w") as f:
        json.dump(
            {"id": req_id, "command": command, "status": "pending", "created_at": time.time()},
            f,
        )
    return req_id


def get_status(req_id: str) -> str:
    path = _path(req_id)
    if not os.path.exists(path):
        return "gone"
    try:
        with open(path) as f:
            return json.load(f).get("status", "pending")
    except Exception:
        return "pending"


def resolve(req_id: str, approved: bool):
    path = _path(req_id)
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            data = json.load(f)
        data["status"] = "approved" if approved else "denied"
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def list_pending() -> list[dict]:
    out = []
    for name in os.listdir(APPROVALS_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(APPROVALS_DIR, name)) as f:
                data = json.load(f)
            if data.get("status") == "pending":
                out.append(data)
        except Exception:
            continue
    return sorted(out, key=lambda d: d.get("created_at", 0))


def cleanup(req_id: str):
    try:
        os.remove(_path(req_id))
    except OSError:
        pass
