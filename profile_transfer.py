"""
SystemC profile export / import.

A profile is exported to ONE .zip file that you can keep as a backup or move to another
computer.  What goes into it is up to whoever exports (the control panel asks):

    always     the profile's settings and personalization (name, model, platforms, prompt, ...)
    optional   bot tokens & API keys, pip dependencies, integrations (which are on, and their
               settings), long-term memory, chat history, workspace files

Never exported: settings that belong to one computer (working folder, run-as user, elevated
commands).  An imported profile gets this computer's defaults for them, so a file from
someone else can't switch on elevation or point a bot at an arbitrary folder.

Importing never runs anything and never overwrites anything: each profile is added under a
new id (name, name-2, ...), and a damaged or unsafe file is refused before anything is
written.  The two things that make a bot run code -- pip dependencies and integrations --
are only imported when explicitly asked for.

Archive layout
    manifest.json
    profiles/<id>/profile.json
    profiles/<id>/integrations.env            (optional)
    profiles/<id>/memory/...  history/...  workspace/...   (optional)

Standard library only (plus profiles.py).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import zipfile
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

import profiles as _profiles

FORMAT = "systemc-profile"
VERSION = 1

# What can be added on top of the profile's settings (which are always included).
PARTS = ("secrets", "dependencies", "integrations", "memory", "history", "workspace")
LABELS = {
    "secrets": "Bot tokens & API keys",
    "dependencies": "Pip dependencies (installed when the bot starts)",
    "integrations": "Integrations (which are on, and their settings)",
    "memory": "Long-term memory",
    "history": "Chat history",
    "workspace": "Workspace files",
}
DATA_PARTS = ("memory", "history", "workspace")

SECRET_FIELDS = _profiles.SECRET_FIELDS
# Specific to one computer; never exported, and ignored if a file contains them.
MACHINE_FIELDS = ("agent_workdir", "agent_run_as_user", "agent_account_mode", "agent_allow_elevated")
# Regenerable junk that is skipped when collecting files.
SKIP_DIRS = {".cache", ".local", "__pycache__", "node_modules", "venv", ".venv"}

MAX_FILES = 100_000
MAX_BYTES = 2 * 1024 ** 3
_MAX_JSON = 1024 * 1024
_CHUNK = 1024 * 1024
_OVERLAY_SUFFIX = ".integrations.env"
_ENV_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


class TransferError(Exception):
    """A problem worth showing to the user as-is."""


def human_size(n: int) -> str:
    n = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# ── locations ────────────────────────────────────────────────────────────────

def _app(app_dir: Optional[str]) -> str:
    return os.path.abspath(app_dir or _profiles.APP_DIR)


def _mgr(manager):
    return manager or _profiles.manager


def _overlay_path(pid: str, mgr) -> str:
    return os.path.join(mgr.profiles_dir, pid + _OVERLAY_SUFFIX)


def _inside(path: str, root: str) -> bool:
    try:
        r = os.path.realpath(root)
        return os.path.commonpath([os.path.realpath(path), r]) == r
    except ValueError:
        return False


def _new_data_dir(kind: str, pid: str, app: str) -> str:
    """Where an imported profile's `kind` data goes (always the isolated per-profile folder)."""
    if kind == "memory":
        return os.path.join(app, ".memory", "profiles", pid)
    if kind == "history":
        return os.path.join(app, ".history", "profiles", pid)
    return os.path.join(app, "workspace", "profiles", pid)


def _data_dir(kind: str, prof, app: str) -> Optional[str]:
    """The folder that holds this profile's `kind` data on this computer, or None when the
    profile keeps it somewhere that isn't SystemC's to export (a custom working folder)."""
    legacy = prof.id == "default"          # the default profile keeps the original shared folders
    if kind == "memory":
        return os.path.join(app, ".memory") if legacy else _new_data_dir(kind, prof.id, app)
    if kind == "history":
        return os.path.join(app, ".history") if legacy else _new_data_dir(kind, prof.id, app)
    ws_root = os.path.join(app, "workspace")
    wd = (prof.agent_workdir or "").strip()
    if wd:
        wd = os.path.abspath(wd)
        return wd if _inside(wd, ws_root) else None
    return ws_root if legacy else _new_data_dir("workspace", prof.id, app)


def _iter_files(root: str, app: str) -> Iterator[Tuple[str, str]]:
    """(absolute path, relative posix path) of the regular files below `root`.

    Symlinks and caches are skipped, and when `root` is one of the folders every profile
    shares a top level of (.memory, .history, workspace) the profiles/ subfolder -- other
    profiles' data -- is never included."""
    root = os.path.abspath(root)
    if not os.path.isdir(root) or os.path.islink(root):
        return
    shared = {os.path.realpath(os.path.join(app, d)) for d in (".memory", ".history", "workspace")}
    top_is_shared = os.path.realpath(root) in shared
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        at_top = os.path.abspath(dirpath) == root
        keep = []
        for d in dirnames:
            if d in SKIP_DIRS or os.path.islink(os.path.join(dirpath, d)):
                continue
            if top_is_shared and at_top and d == "profiles":
                continue
            keep.append(d)
        dirnames[:] = sorted(keep)
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            if fn.endswith(".pyc") or os.path.islink(full) or not os.path.isfile(full):
                continue
            yield full, os.path.relpath(full, root).replace(os.sep, "/")


# ── what could be exported (for the dialog) ──────────────────────────────────

def describe(pid: str, *, app_dir: Optional[str] = None, manager=None, limit: int = 20000) -> dict:
    """{"name", "parts": {part: details}, "notes": {part: why it can't be included}}.
    Only parts that have something to export appear in "parts"."""
    app, mgr = _app(app_dir), _mgr(manager)
    prof = mgr.get_profile(pid)
    if prof is None:
        raise TransferError(f"Profile '{pid}' doesn't exist.")
    parts: Dict[str, dict] = {}
    notes: Dict[str, str] = {}
    n = sum(1 for k in SECRET_FIELDS if getattr(prof, k))
    if n:
        parts["secrets"] = {"count": n}
    if prof.dependencies:
        parts["dependencies"] = {"count": len(prof.dependencies)}
    ov = _overlay_path(pid, mgr)
    if prof.integrations or os.path.isfile(ov):
        parts["integrations"] = {"count": len(prof.integrations),
                                 "bytes": os.path.getsize(ov) if os.path.isfile(ov) else 0}
    for kind in DATA_PARTS:
        root = _data_dir(kind, prof, app)
        if root is None:
            notes[kind] = "its working folder is outside SystemC's folder, so it can't be included"
            continue
        files = size = 0
        capped = False
        for full, _rel in _iter_files(root, app):
            files += 1
            try:
                size += os.path.getsize(full)
            except OSError:
                pass
            if files >= limit:
                capped = True
                break
        if files:
            parts[kind] = {"files": files, "bytes": size, "capped": capped}
    return {"name": prof.name, "parts": parts, "notes": notes}


# ── export ───────────────────────────────────────────────────────────────────

def _app_version() -> str:
    try:
        import version
        return version.display()
    except Exception:
        return ""


def export_profiles(ids: Iterable[str], parts: Iterable[str], dest: str, *,
                    app_dir: Optional[str] = None, manager=None) -> dict:
    """Write the chosen profiles (and the chosen optional `parts`) to the zip `dest`.
    Returns {"path", "profiles": [ids], "files", "bytes"}."""
    app, mgr = _app(app_dir), _mgr(manager)
    ids = list(dict.fromkeys(ids))
    parts = set(parts)
    if not ids:
        raise TransferError("Pick at least one profile to export.")
    unknown = parts - set(PARTS)
    if unknown:
        raise TransferError(f"Unknown part(s): {', '.join(sorted(unknown))}.")

    tmp = dest + ".part"
    total_files = total_bytes = 0
    manifest = {"format": FORMAT, "version": VERSION, "app": _app_version(),
                "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"), "profiles": {}}
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as z:
            for pid in ids:
                prof = mgr.get_profile(pid)
                if prof is None:
                    raise TransferError(f"Profile '{pid}' doesn't exist.")
                data = prof.to_dict()
                for k in MACHINE_FIELDS:
                    data.pop(k, None)
                included: List[str] = []

                if "secrets" in parts and any(data.get(k) for k in SECRET_FIELDS):
                    included.append("secrets")
                else:
                    for k in SECRET_FIELDS:
                        data[k] = [] if k == "api_keys" else ""
                if "dependencies" in parts and data.get("dependencies"):
                    included.append("dependencies")
                else:
                    data["dependencies"] = []

                ov = _overlay_path(pid, mgr)
                has_overlay = os.path.isfile(ov)
                if "integrations" in parts and (has_overlay or data.get("integrations")):
                    included.append("integrations")
                    if has_overlay:
                        z.write(ov, f"profiles/{pid}/integrations.env")
                else:
                    data["integrations"] = []

                z.writestr(f"profiles/{pid}/profile.json",
                           json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"))

                for kind in DATA_PARTS:
                    if kind not in parts:
                        continue
                    root = _data_dir(kind, prof, app)
                    if root is None:
                        continue
                    n = 0
                    for full, rel in _iter_files(root, app):
                        try:
                            size = os.path.getsize(full)
                        except OSError:
                            continue
                        total_files += 1
                        total_bytes += size
                        if total_files > MAX_FILES or total_bytes > MAX_BYTES:
                            raise TransferError(
                                f"That's too much to put in one file (limit {MAX_FILES:,} files / "
                                f"{human_size(MAX_BYTES)}). Untick workspace files or chat history.")
                        z.write(full, f"profiles/{pid}/{kind}/{rel}")
                        n += 1
                    if n:
                        included.append(kind)
                manifest["profiles"][pid] = {"name": prof.name, "parts": included}
            z.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"))
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return {"path": dest, "profiles": list(manifest["profiles"]), "files": total_files, "bytes": total_bytes}


# ── reading an archive safely ────────────────────────────────────────────────

def _clean_name(name: str) -> str:
    n = name.replace("\\", "/")
    parts = n.split("/")
    if n.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", parts[0]) or "\x00" in n:
        raise TransferError(f"The file contains an unsafe path ({name!r}), so it was refused.")
    return n


def _open_archive(path: str) -> Tuple[zipfile.ZipFile, Dict[str, zipfile.ZipInfo]]:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise TransferError("That isn't a SystemC profile file (it isn't a valid zip).") from None
    try:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_FILES:
            raise TransferError(f"The file contains too many files (limit {MAX_FILES:,}).")
        if sum(i.file_size for i in infos) > MAX_BYTES:
            raise TransferError(f"The file is too large when unpacked (limit {human_size(MAX_BYTES)}).")
        names: Dict[str, zipfile.ZipInfo] = {}
        for i in infos:
            n = _clean_name(i.filename)
            if (i.external_attr >> 16) & 0o170000 == 0o120000:
                raise TransferError(f"The file contains a symlink ({i.filename!r}), so it was refused.")
            names[n] = i
        bad = zf.testzip()
        if bad:
            raise TransferError(f"The file is damaged (bad entry: {bad}).")
        return zf, names
    except BaseException:
        zf.close()
        raise


def _read_json(zf: zipfile.ZipFile, info: zipfile.ZipInfo, what: str) -> dict:
    if info.file_size > _MAX_JSON:
        raise TransferError(f"{what} is unreasonably large; the file was refused.")
    try:
        data = json.loads(zf.read(info).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise TransferError(f"{what} in the file is damaged.") from None
    if not isinstance(data, dict):
        raise TransferError(f"{what} in the file is damaged.")
    return data


def inspect_archive(path: str) -> dict:
    """Validate a profile file and describe it for the import dialog:
    {"exported_at", "app", "profiles": {source_id: {"name", "model", "platforms", "available"}}}
    where "available" maps each optional part present in the file to its details."""
    zf, names = _open_archive(path)
    with zf:
        if "manifest.json" not in names:
            raise TransferError("That isn't a SystemC profile file (no manifest.json inside).")
        manifest = _read_json(zf, names["manifest.json"], "The manifest")
        if manifest.get("format") != FORMAT:
            raise TransferError("That isn't a SystemC profile file.")
        ver = manifest.get("version")
        if not isinstance(ver, int) or ver < 1 or ver > VERSION:
            raise TransferError("This file was made by a newer version of SystemC; update SystemC to import it.")

        src_ids = sorted({n.split("/")[1] for n in names if n.startswith("profiles/") and n.count("/") >= 2})
        out: Dict[str, dict] = {}
        for sid in src_ids:
            if not _profiles.valid_profile_id(sid):
                raise TransferError(f"The file contains a profile with an invalid id ({sid!r}).")
            pj = names.get(f"profiles/{sid}/profile.json")
            if pj is None:
                raise TransferError(f"Profile '{sid}' in the file has no profile.json.")
            data = _read_json(zf, pj, f"Profile '{sid}'")
            for k in MACHINE_FIELDS:
                data.pop(k, None)
            try:
                prof = _profiles.BotProfile.from_dict({**data, "id": sid})
            except Exception as e:
                raise TransferError(f"Profile '{sid}' in the file is damaged ({e}).") from None

            avail: Dict[str, dict] = {}
            n = sum(1 for k in SECRET_FIELDS if getattr(prof, k))
            if n:
                avail["secrets"] = {"count": n}
            if prof.dependencies:
                avail["dependencies"] = {"count": len(prof.dependencies), "items": list(prof.dependencies)}
            ov = names.get(f"profiles/{sid}/integrations.env")
            if ov is not None or prof.integrations:
                avail["integrations"] = {"count": len(prof.integrations), "bytes": ov.file_size if ov else 0}
            for kind in DATA_PARTS:
                prefix = f"profiles/{sid}/{kind}/"
                members = [i for nme, i in names.items() if nme.startswith(prefix)]
                if members:
                    avail[kind] = {"files": len(members), "bytes": sum(m.file_size for m in members)}
            platforms = [n for n, on in (("Discord", prof.discord_enabled), ("Stoat", prof.stoat_enabled)) if on]
            out[sid] = {"name": prof.name, "model": prof.agent_model, "platforms": platforms, "available": avail}
        if not out:
            raise TransferError("The file doesn't contain any profiles.")
        return {"exported_at": str(manifest.get("exported_at", "")), "app": str(manifest.get("app", "")),
                "profiles": out}


# ── import ───────────────────────────────────────────────────────────────────

def _id_taken(pid: str, app: str, mgr) -> bool:
    if pid == "default" or os.path.exists(os.path.join(mgr.profiles_dir, pid + ".json")):
        return True
    if os.path.exists(_overlay_path(pid, mgr)):
        return True
    # a deleted profile can leave its folders behind; never merge into them
    return any(os.path.exists(_new_data_dir(k, pid, app)) for k in DATA_PARTS)


def unique_id(base: str, *, app_dir: Optional[str] = None, manager=None) -> str:
    """`base` if it's free, else base-2, base-3, ... ("free" = no profile, no leftover folders)."""
    app, mgr = _app(app_dir), _mgr(manager)
    base = base if _profiles.valid_profile_id(base) else "bot"
    if not _id_taken(base, app, mgr):
        return base
    n = 2
    while True:
        cand = f"{base[:40 - len(str(n)) - 1]}-{n}"
        if not _id_taken(cand, app, mgr):
            return cand
        n += 1


def _clean_overlay(raw: bytes) -> str:
    """Keep only KEY=value lines, and never take an API token from a file."""
    lines = [ln for ln in raw.decode("utf-8", "replace").splitlines()
             if _ENV_LINE.match(ln) and "\x00" not in ln and not ln.startswith("SYSTEMC_API_TOKEN=")]
    return "\n".join(lines) + "\n" if lines else ""


def _write_private(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _extract(zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: str, budget: dict) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    left = info.file_size
    with zf.open(info) as src, open(dest, "xb") as out:
        while True:
            chunk = src.read(_CHUNK)
            if not chunk:
                break
            left -= len(chunk)
            budget["bytes"] -= len(chunk)
            if left < 0 or budget["bytes"] < 0:
                raise TransferError("A file inside is bigger than it claims to be; the file looks damaged.")
            out.write(chunk)            # permissions stay at the defaults: nothing is made executable


def _safe_dest(root: str, rel: str) -> str:
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise TransferError(f"The file contains an unsafe path ({rel!r}), so it was refused.")
    dest = os.path.join(root, *parts)
    if not _inside(dest, root):
        raise TransferError(f"The file contains an unsafe path ({rel!r}), so it was refused.")
    return dest


def import_archive(path: str, selections: Dict[str, dict], *,
                   app_dir: Optional[str] = None, manager=None) -> List[dict]:
    """Import profiles from `path`.

    selections: {source_id: {"name": str, "parts": iterable of optional parts to take}}
    Every profile is added under a fresh id.  All or nothing: if anything fails, whatever was
    created is removed again.  Returns [{"id", "name", "parts"}, ...]."""
    info = inspect_archive(path)
    app, mgr = _app(app_dir), _mgr(manager)
    if not selections:
        raise TransferError("Pick at least one profile to import.")
    for sid in selections:
        if sid not in info["profiles"]:
            raise TransferError(f"Profile '{sid}' isn't in that file.")

    created_dirs: List[str] = []
    created_files: List[str] = []
    saved_ids: List[str] = []
    results: List[dict] = []
    budget = {"bytes": MAX_BYTES}
    zf, names = _open_archive(path)
    try:
        with zf:
            for sid, sel in selections.items():
                pinfo = info["profiles"][sid]
                want = set(sel.get("parts") or ()) & set(pinfo["available"])
                name = " ".join(str(sel.get("name") or pinfo["name"]).split())[:80] or pinfo["name"]
                new_id = unique_id(_profiles.slugify(name, fallback=sid), app_dir=app, manager=mgr)

                # 1. data folders
                for kind in DATA_PARTS:
                    if kind not in want:
                        continue
                    dest_root = _new_data_dir(kind, new_id, app)
                    os.makedirs(dest_root)
                    created_dirs.append(dest_root)
                    prefix = f"profiles/{sid}/{kind}/"
                    for nme, member in names.items():
                        if nme.startswith(prefix):
                            _extract(zf, member, _safe_dest(dest_root, nme[len(prefix):]), budget)

                # 2. integration settings
                ov_member = names.get(f"profiles/{sid}/integrations.env")
                if "integrations" in want and ov_member is not None:
                    if ov_member.file_size > _MAX_JSON:
                        raise TransferError("The integration settings in the file are unreasonably large.")
                    text = _clean_overlay(zf.read(ov_member))
                    if text:
                        ov_dest = _overlay_path(new_id, mgr)
                        _write_private(ov_dest, text)
                        created_files.append(ov_dest)

                # 3. the profile itself, last, so a failure above never leaves it half-registered
                data = _read_json(zf, names[f"profiles/{sid}/profile.json"], f"Profile '{sid}'")
                for k in MACHINE_FIELDS:
                    data.pop(k, None)
                if "secrets" not in want:
                    for k in SECRET_FIELDS:
                        data[k] = [] if k == "api_keys" else ""
                if "dependencies" not in want:
                    data["dependencies"] = []
                if "integrations" not in want:
                    data["integrations"] = []
                data.update({"id": new_id, "name": name, "created_at": time.time()})
                prof = _profiles.BotProfile.from_dict(data)
                mgr.save_profile(prof)
                saved_ids.append(new_id)
                results.append({"id": new_id, "name": name, "parts": sorted(want)})
    except BaseException as e:
        for pid in saved_ids:
            mgr.delete_profile(pid)
        for p in created_files:
            try:
                os.remove(p)
            except OSError:
                pass
        for d in created_dirs:
            shutil.rmtree(d, ignore_errors=True)
        if isinstance(e, (TransferError, KeyboardInterrupt)):
            raise
        raise TransferError(f"The import failed and was rolled back: {e}") from None
    return results
