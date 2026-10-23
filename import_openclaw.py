"""Import memories and persona from an OpenClaw workspace into SystemC.

Usage:
    python import_openclaw.py [--workspace PATH] [--app-dir PATH] [--dry-run]

By default the OpenClaw workspace is ~/.openclaw/workspace and the SystemC
app dir is this file's folder. Long-term memory, daily logs and persona text
are imported into .memory/ (and, when available, the active profile's prompt).

Nothing sensitive is imported: .env / tokens are left alone; only markdown
memory and identity files are copied.
"""

import argparse
import os
import re
import shutil
from datetime import datetime

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OPENCLAW = os.path.join(os.path.expanduser("~"), ".openclaw", "workspace")


def _memory_dir(app_dir):
    md = os.path.join(app_dir, ".memory")
    os.makedirs(os.path.join(md, "daily"), exist_ok=True)
    return md


def _backup(path):
    if os.path.exists(path):
        bak = path + ".pre-openclaw.bak"
        shutil.copy2(path, bak)


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def import_memory(workspace, app_dir, dry=False):
    md = _memory_dir(app_dir)
    imported = []
    # Long-term MEMORY.md
    src = os.path.join(workspace, "MEMORY.md")
    dst = os.path.join(md, "MEMORY.md")
    text = _read(src)
    if text.strip():
        if not dry:
            _backup(dst)
            existing = _read(dst)
            combined = text.strip()
            if existing.strip():
                combined = existing.strip() + "\n\n---\n\n# Imported from OpenClaw\n\n" + text.strip()
            with open(dst, "w", encoding="utf-8") as f:
                f.write(combined + "\n")
        imported.append(("MEMORY.md", len(text)))

    # Daily logs
    mem_dir = os.path.join(workspace, "memory")
    if os.path.isdir(mem_dir):
        for root, _, files in os.walk(mem_dir):
            for fn in files:
                if not fn.endswith(".md"):
                    continue
                full = os.path.join(root, fn)
                content = _read(full)
                if not content.strip():
                    continue
                # Derive a YYYY-MM-DD date from the file name when possible.
                m = re.match(r"(\d{4}-\d{2}-\d{2})", fn)
                day = m.group(1) if m else datetime.now().date().isoformat()
                dst = os.path.join(md, "daily", day + ".md")
                if not dry:
                    _backup(dst) if os.path.exists(dst) else None
                    with open(dst, "a", encoding="utf-8") as f:
                        f.write("\n\n" + content.strip() + "\n")
                imported.append((os.path.relpath(full, workspace), len(content)))
    return imported


def import_persona(workspace, app_dir, dry=False):
    pieces = []
    for name in ("IDENTITY.md", "SOUL.md", "AGENTS.md", "USER.md"):
        p = os.path.join(workspace, name)
        txt = _read(p).strip()
        if txt:
            pieces.append(f"## {name}\n\n{txt}")
    if not pieces:
        return None
    persona = "\n\n".join(pieces)
    persona_path = os.path.join(_memory_dir(app_dir), "persona.md")
    if not dry:
        _backup(persona_path) if os.path.exists(persona_path) else None
        with open(persona_path, "w", encoding="utf-8") as f:
            f.write(persona + "\n")
    # Also try to fold it into the active profile's system prompt.
    try:
        import profiles
        pm = getattr(profiles, "manager", None)
        if pm is not None and not dry:
            prof = pm.get_active_profile()
            if prof is not None:
                from profiles import BotProfile
                data = prof.to_dict()
                existing = (data.get("system_prompt") or "").strip()
                marker = "## Imported OpenClaw persona"
                if marker not in existing:
                    block = f"\n\n{marker}\n\n{persona}\n"
                    data["system_prompt"] = (existing + block).strip()
                    pm.save_profile(BotProfile.from_dict(data))
    except Exception:
        pass
    return persona_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=DEFAULT_OPENCLAW)
    ap.add_argument("--app-dir", default=APP_DIR)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not os.path.isdir(args.workspace):
        print(f"OpenClaw workspace not found: {args.workspace}")
        return 1
    print(f"Importing from {args.workspace} into {args.app_dir}")
    mem = import_memory(args.workspace, args.app_dir, dry=args.dry_run)
    print(f"Imported {len(mem)} memory item(s).")
    p = import_persona(args.workspace, args.app_dir, dry=args.dry_run)
    if p:
        print(f"Persona written to {p}")
    if args.dry_run:
        print("(dry run: no files changed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
