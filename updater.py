"""
SystemC updater: checks a GitHub repo's releases for a newer version and
installs it over this folder.

Standard library only, so the control panel can import it before anything
else is installed.

What an update does
  1. Asks GitHub for the repo's latest release (optionally including
     pre-releases) and compares its tag with the one recorded in
     .update_state.json.
  2. Downloads the release zip (https, GitHub hosts only) and, if the release
     publishes "<zip name>.sha256", verifies it.
  3. Unpacks to a temp folder and byte-compiles every .py file. If anything is
     wrong, nothing in your install has been touched yet.
  4. Backs up every file it is about to overwrite into .backups/, then copies
     the new files in. If that fails part-way, the backup is restored.
  5. Re-runs pip if requirements.txt changed.

Never touched by an update: .env, .profiles/, .memory/, .history/, .approvals/, workspace/,
venv/, .backups/ and files the release doesn't contain (your own integrations
are safe). Files the release DOES contain are overwritten, so local edits to
those are replaced; the backup keeps the old copies.

Command line:
    python updater.py check    [--repo owner/repo] [--pre]
    python updater.py update   [--repo owner/repo] [--pre]
    python updater.py rollback
The repo defaults to UPDATE_REPO in .env (or TheRealHyperno/systemcontrol-agent if unset).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from urllib.parse import urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_NAME = ".update_state.json"
BACKUP_NAME = ".backups"
TMP_PREFIX = ".update_tmp_"
KEEP_BACKUPS = 5
MAX_DOWNLOAD = 100 * 1024 * 1024
TIMEOUT = 30
USER_AGENT = "SystemC-Updater"

# First path component of anything an update must never write to.
PROTECTED_TOP = {
    ".env", ".profiles", ".memory", ".history", ".approvals", BACKUP_NAME, STATE_NAME,
    "workspace", "venv", ".venv", ".git", "__pycache__", ".idea", ".vscode",
}
_ALLOWED_HOSTS = ("github.com", "githubusercontent.com")
DEFAULT_REPO = "TheRealHyperno/systemcontrol-agent"   # used when UPDATE_REPO is not set
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class UpdateError(Exception):
    """A problem worth showing to the user as-is."""


# ── small helpers ────────────────────────────────────────────────────────────

def _app(app_dir):
    return os.path.abspath(app_dir or APP_DIR)


def _path(app, rel):
    return os.path.join(app, *rel.split("/"))


def codename(app_dir=None) -> str:
    """SYSTEMC_VERSION, read as text from version.py (or agent.py on older releases).
    Neither file is imported here."""
    app = _app(app_dir)
    for fname in ("version.py", "agent.py"):
        try:
            with open(_path(app, fname), encoding="utf-8") as f:
                m = re.search(r'^SYSTEMC_VERSION\s*=\s*["\']([^"\']+)["\']', f.read(), re.M)
        except OSError:
            continue
        if m:
            return m.group(1)
    return "unknown"


def load_state(app_dir=None) -> dict:
    try:
        with open(_path(_app(app_dir), STATE_NAME), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict, app_dir=None) -> None:
    path = _path(_app(app_dir), STATE_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def installed_tag(app_dir=None) -> str:
    return str(load_state(app_dir).get("tag") or "")


def normalize_repo(value: str) -> str:
    """Accept 'owner/repo' or a github.com URL; return 'owner/repo'."""
    v = (value or "").strip().strip("/")
    v = re.sub(r"^(https?://)?(www\.)?github\.com/", "", v, flags=re.I)
    v = "/".join(v.split("/")[:2])
    v = re.sub(r"\.git$", "", v)
    if not _REPO_RE.match(v) or any(not part.strip(".") for part in v.split("/")):
        raise UpdateError("The update source must look like owner/repo (or a github.com link).")
    return v


def _nums(tag: str):
    return tuple(int(n) for n in re.findall(r"\d+", tag))


def _newer(latest: str, installed: str) -> bool:
    if not installed:
        return True                      # unknown install: offer the latest
    if latest == installed:
        return False
    a, b = _nums(latest), _nums(installed)
    return a >= b if (a and b) else True


def _sha256_file(path: str) -> str:
    if not os.path.isfile(path):
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ── network ──────────────────────────────────────────────────────────────────

def _check_url(url: str) -> None:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    ok = any(host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS)
    if p.scheme != "https" or not ok:
        raise UpdateError(f"Refusing to fetch {url!r}: only https GitHub links are allowed.")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Every redirect hop must also be an https GitHub URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, accept: str = "*/*"):
    _check_url(url)
    opener = urllib.request.build_opener(_SafeRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        return opener.open(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("GitHub says that doesn't exist (404). Check the repo name, that it "
                              "is public and has a published release (tick 'Include pre-releases' "
                              "if it only has pre-releases).") from None
        if e.code in (403, 429):
            raise UpdateError("GitHub refused the request (probably a rate limit). "
                              "Try again in a few minutes.") from None
        raise UpdateError(f"GitHub returned HTTP {e.code}.") from None
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"Couldn't reach GitHub: {getattr(e, 'reason', e)}") from None


def check(repo: str, prerelease: bool = False, app_dir=None) -> dict:
    """Look up the newest release. Returns a dict describing it (see keys below)."""
    repo = normalize_repo(repo)
    api = f"https://api.github.com/repos/{repo}/releases"
    accept = "application/vnd.github+json"
    try:
        if prerelease:
            with _open(api + "?per_page=15", accept) as r:
                releases = json.load(r)
            rel = next((x for x in releases if not x.get("draft")), None)
            if not rel:
                raise UpdateError("That repo has no published releases yet.")
        else:
            with _open(api + "/latest", accept) as r:
                rel = json.load(r)
    except ValueError:
        raise UpdateError("GitHub sent back something unreadable.") from None

    tag = str(rel.get("tag_name") or "")
    if not tag:
        raise UpdateError("The latest release has no tag.")
    assets = rel.get("assets") or []
    zip_asset = next((a for a in assets if str(a.get("name", "")).lower().endswith(".zip")), None)
    sha_asset = None
    if zip_asset:
        want = zip_asset["name"] + ".sha256"
        sha_asset = next((a for a in assets if a.get("name") == want), None)
        zip_url = zip_asset["browser_download_url"]
    else:
        zip_url = rel.get("zipball_url") or ""
    if not zip_url:
        raise UpdateError("The latest release has nothing to download.")

    have = installed_tag(app_dir)
    return {
        "repo": repo,
        "tag": tag,
        "name": rel.get("name") or tag,
        "notes": rel.get("body") or "",
        "prerelease": bool(rel.get("prerelease")),
        "published": rel.get("published_at") or "",
        "page": rel.get("html_url") or "",
        "zip_url": zip_url,
        "sha256_url": sha_asset["browser_download_url"] if sha_asset else "",
        "installed_tag": have,
        "update_available": _newer(tag, have),
    }


def download(url: str, dest: str, progress=None) -> str:
    """Download to dest; returns the sha256 of what was written."""
    h = hashlib.sha256()
    total = 0
    with _open(url) as r, open(dest, "wb") as f:
        size = int(r.headers.get("Content-Length") or 0)
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise UpdateError("The download is bigger than 100 MB; refusing it.")
            h.update(chunk)
            f.write(chunk)
            if progress:
                progress(total, size)
    return h.hexdigest()


def _published_sha(url: str) -> str:
    with _open(url) as r:
        text = r.read(4096).decode("utf-8", "replace")
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if not m:
        raise UpdateError("The release's .sha256 file doesn't contain a SHA-256 hash.")
    return m.group(1).lower()


# ── unpacking and installing ─────────────────────────────────────────────────

def _members(zf: zipfile.ZipFile):
    """[(ZipInfo, relative path)] for the files to install: unsafe archives are
    rejected, one shared top-level folder is stripped (GitHub zipballs have one),
    protected paths and bytecode are skipped."""
    infos = [i for i in zf.infolist() if not i.is_dir()]
    names = []
    for i in infos:
        n = i.filename.replace("\\", "/")
        parts = n.split("/")
        if n.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", parts[0]):
            raise UpdateError(f"Unsafe path in the archive: {i.filename!r}")
        if (i.external_attr >> 16) & 0o170000 == 0o120000:
            raise UpdateError(f"The archive contains a symlink ({i.filename!r}); refusing it.")
        names.append(n)
    if not names:
        raise UpdateError("The archive is empty.")
    strip = len({n.split("/")[0] for n in names}) == 1 and all("/" in n for n in names)
    out = []
    for i, n in zip(infos, names):
        rel = n.split("/", 1)[1] if strip else n
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if not parts:
            continue
        if parts[0] in PROTECTED_TOP or parts[0].startswith(TMP_PREFIX):
            continue
        if "__pycache__" in parts or parts[-1].endswith(".pyc"):
            continue
        out.append((i, "/".join(parts)))
    return out


def _make_backup(app: str, existing: list, added: list, tag_before: str) -> str:
    folder = os.path.join(app, BACKUP_NAME)
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r"[^\w.-]", "_", tag_before or "unknown")
    path = os.path.join(folder, f"{time.strftime('%Y%m%d-%H%M%S')}_{safe}.zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in existing:
            z.write(_path(app, rel), rel)
        z.writestr("__manifest__.json", json.dumps({"added": added, "tag_before": tag_before}))
    for old in list_backups(app)[KEEP_BACKUPS:]:
        try:
            os.remove(old)
        except OSError:
            pass
    return path


def list_backups(app_dir=None) -> list:
    """Backup zips, newest first."""
    folder = os.path.join(_app(app_dir), BACKUP_NAME)
    try:
        names = [n for n in os.listdir(folder) if n.endswith(".zip")]
    except OSError:
        return []
    return [os.path.join(folder, n) for n in sorted(names, reverse=True)]


def _restore(backup: str, app: str) -> str:
    """Put a backup's files back and delete the files that update added.
    Returns the tag the backup was taken at."""
    with zipfile.ZipFile(backup) as z:
        manifest = json.loads(z.read("__manifest__.json").decode("utf-8"))
        for info in z.infolist():
            if info.filename == "__manifest__.json" or info.is_dir():
                continue
            parts = info.filename.split("/")
            if ".." in parts or info.filename.startswith("/"):
                continue
            dst = _path(app, info.filename)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with z.open(info) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                os.chmod(dst, mode)
    for rel in manifest.get("added", []):
        parts = rel.split("/")
        if ".." in parts or parts[0] in PROTECTED_TOP:
            continue
        try:
            os.remove(_path(app, rel))
        except OSError:
            pass
    return manifest.get("tag_before", "")


def apply_zip(zip_path: str, tag: str, log=print, app_dir=None) -> dict:
    """Install an already-downloaded release zip. See the module docstring."""
    app = _app(app_dir)
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise UpdateError("The download isn't a valid zip file.") from None
    with zf:
        bad = zf.testzip()
        if bad:
            raise UpdateError(f"The archive is corrupt (bad file: {bad}).")
        members = _members(zf)
        rels = {rel for _, rel in members}
        if not {"agent.py", "control_panel.py"} <= rels:
            raise UpdateError("That archive doesn't look like a SystemC release "
                              "(agent.py / control_panel.py are missing).")
        stage = tempfile.mkdtemp(prefix=TMP_PREFIX, dir=app)
        try:
            log(f"Unpacking {len(members)} files…")
            for info, rel in members:
                dst = _path(stage, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with zf.open(info) as src, open(dst, "wb") as out:
                    shutil.copyfileobj(src, out)
                if (info.external_attr >> 16) & 0o111:
                    os.chmod(dst, 0o755)

            log("Checking the new code for syntax errors…")
            for rel in sorted(rels):
                if rel.endswith(".py"):
                    try:
                        with open(_path(stage, rel), "rb") as f:
                            compile(f.read(), rel, "exec")
                    except (SyntaxError, ValueError) as e:
                        raise UpdateError(f"{rel} in this update has a syntax error, so nothing "
                                          f"was changed. ({e})") from None

            have = installed_tag(app)
            req_before = _sha256_file(_path(app, "requirements.txt"))
            existing = sorted(r for r in rels if os.path.isfile(_path(app, r)))
            added = sorted(rels - set(existing))
            backup = _make_backup(app, existing, added, have)
            log(f"Backed up {len(existing)} existing files → {os.path.relpath(backup, app)}")

            log("Installing…")
            try:
                for rel in sorted(rels):
                    src, dst = _path(stage, rel), _path(app, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    tmp = dst + ".update_new"
                    shutil.copyfile(src, tmp)
                    shutil.copymode(src, tmp)
                    os.replace(tmp, dst)
            except Exception as e:
                log("Install failed part-way, restoring the previous files…")
                _restore(backup, app)
                raise UpdateError(f"The update failed ({e}); your previous version was restored.") from None

            req_changed = req_before != _sha256_file(_path(app, "requirements.txt"))
            pip_ok = True
            if req_changed:
                log("requirements.txt changed, installing dependencies…")
                r = subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                                    _path(app, "requirements.txt")],
                                   cwd=app, capture_output=True, text=True)
                pip_ok = r.returncode == 0
                if pip_ok:
                    log("Dependencies installed.")
                else:
                    tail = (r.stdout + r.stderr).strip().splitlines()[-8:]
                    log("pip reported a problem (the update itself IS installed). "
                        "Use the Dependencies tab to install what's missing:\n" + "\n".join(tail))

            save_state({"tag": tag, "previous_tag": have, "codename": codename(app),
                        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S")}, app)
            return {"tag": tag, "previous_tag": have, "files": len(rels), "added": len(added),
                    "backup": backup, "requirements_changed": req_changed, "pip_ok": pip_ok}
        finally:
            shutil.rmtree(stage, ignore_errors=True)


def install(info: dict, log=print, progress=None, app_dir=None) -> dict:
    """Download the release described by check() and apply it."""
    fd, tmp = tempfile.mkstemp(prefix="systemc_update_", suffix=".zip")
    os.close(fd)
    try:
        log(f"Downloading {info['tag']}…")
        got = download(info["zip_url"], tmp, progress)
        if info.get("sha256_url"):
            if got != _published_sha(info["sha256_url"]):
                raise UpdateError("The download doesn't match the SHA-256 published with the "
                                  "release. It was NOT installed.")
            log("SHA-256 matches the one published with the release.")
        else:
            log("This release publishes no .sha256 file, so the download couldn't be verified.")
        return apply_zip(tmp, info["tag"], log, app_dir)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def rollback(log=print, app_dir=None) -> dict:
    """Undo the most recent update using its backup (then discard that backup)."""
    app = _app(app_dir)
    backups = list_backups(app)
    if not backups:
        raise UpdateError("There's no backup to roll back to.")
    newest = backups[0]
    log(f"Restoring {os.path.basename(newest)}…")
    try:
        tag = _restore(newest, app)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as e:
        raise UpdateError(f"Couldn't restore that backup ({e}).") from None
    state = load_state(app)
    state.update({"tag": tag, "codename": codename(app),
                  "installed_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    if tag:
        save_state(state, app)
    else:
        try:
            os.remove(_path(app, STATE_NAME))
        except OSError:
            pass
    os.remove(newest)
    return {"tag": tag}


# ── command line ─────────────────────────────────────────────────────────────

def _env_value(key: str) -> str:
    try:
        with open(os.path.join(APP_DIR, ".env"), encoding="utf-8") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k.strip() == key:
                    return v.strip()
    except OSError:
        pass
    return ""


def main(argv) -> int:
    args = list(argv)
    cmd = args.pop(0) if args else "check"
    repo = _env_value("UPDATE_REPO") or DEFAULT_REPO
    pre = _env_value("UPDATE_PRERELEASE").lower() in ("1", "on", "true", "yes")
    if "--repo" in args:
        repo = args[args.index("--repo") + 1]
    if "--pre" in args:
        pre = True
    try:
        if cmd == "rollback":
            print(f"Rolled back to {rollback().get('tag') or 'the previous files'}.")
            return 0
        if cmd not in ("check", "update"):
            print(__doc__)
            return 2
        if not repo:
            raise UpdateError("No update source. Pass --repo owner/repo or set UPDATE_REPO in .env.")
        info = check(repo, pre)
        print(f"Installed: {info['installed_tag'] or '(unknown)'}   Latest: {info['tag']}")
        if not info["update_available"]:
            print("Already up to date.")
            return 0
        if cmd == "check":
            print("An update is available. Run: python updater.py update")
            return 0
        res = install(info)
        print(f"Updated to {res['tag']} ({res['files']} files). Restart the bots and control panel.")
        return 0
    except UpdateError as e:
        print(f"Update error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
