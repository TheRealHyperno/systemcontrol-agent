"""
SystemC Drovex — Multi-Bot Profiles & Instance Manager.

Allows running multiple bot instances simultaneously (e.g. up to 3+ bots at once),
each with their own:
  - Tokens and platform configs (Discord, Stoat, or both)
  - LLM provider & API keys (different models, Ollama Cloud/Local keys, Anthropic)
  - Isolated workspace directory, system user, and permissions
  - Isolated memory (.memory/profiles/<id>/) and history (.history/profiles/<id>/)
  - Custom per-agent dependencies (automatically pulled via pip)
  - Migration utility to convert legacy single-bot .env files into structured profiles.
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import shutil
import queue
import tempfile
import threading
import subprocess
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Any, Tuple

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILES_DIR = os.path.join(APP_DIR, ".profiles")
REGISTRY_FILE = os.path.join(PROFILES_DIR, "profiles.json")
LEGACY_ENV_FILE = os.path.join(APP_DIR, ".env")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
# Fields that hold credentials: masked by the HTTP API, never logged.
SECRET_FIELDS = ("discord_token", "stoat_token", "api_key", "api_keys", "anthropic_api_key")


def valid_profile_id(pid: str) -> bool:
    """Profile ids become file names (.profiles/<id>.json) and folder names, so they are
    restricted to a-z, 0-9, '_' and '-' (no dots or slashes)."""
    return isinstance(pid, str) and bool(_ID_RE.match(pid))


def slugify(text: str, fallback: str = "bot") -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:40].strip("_")
    return s if valid_profile_id(s) else fallback


def valid_dependency(spec: str) -> bool:
    """A pip requirement, not a pip option: '--index-url ...' etc. must never get through."""
    spec = (spec or "").strip()
    # pip is run with an argument list (no shell), so the only real risk is an entry that pip
    # would read as an option (--index-url, -r file, ...) or a multi-line value.
    return bool(spec) and not spec.startswith("-") and not any(c in spec for c in "\n\r\x00")


def _write_json_private(path: str, data: Any) -> None:
    """Atomic write, owner-only (profiles hold bot tokens and API keys)."""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class LogBuffer:
    """Bounded log store for one bot instance. put() never blocks (oldest lines are dropped),
    so a bot whose logs nobody reads can't stall on a full pipe. Two views:
      history  -- last N lines, non-destructive (HTTP API)
      drain()  -- lines not yet shown, consumed once (control panel)"""

    def __init__(self, maxlen: int = 1000):
        self._lock = threading.Lock()
        self._history: deque = deque(maxlen=maxlen)
        self._pending: deque = deque(maxlen=maxlen)

    def put(self, item) -> None:
        with self._lock:
            self._history.append(item)
            self._pending.append(item)

    put_nowait = put

    def history(self, limit: int = 100) -> list:
        with self._lock:
            items = list(self._history)
        return items[-max(1, limit):]

    def drain(self) -> list:
        with self._lock:
            items = list(self._pending)
            self._pending.clear()
        return items


@dataclass
class BotProfile:
    id: str
    name: str
    description: str = ""
    color: str = "#89b4fa"
    icon: str = "🤖"
    enabled: bool = True

    # Platform — Discord
    discord_enabled: bool = True
    discord_token: str = ""
    discord_channels: str = ""
    discord_bot_mode: str = "WAKEWORD"  # WAKEWORD, MENTION, EVERYTHING
    discord_wake_word: str = "hey claw"

    # Platform — Stoat
    stoat_enabled: bool = False
    stoat_token: str = ""
    stoat_channels: str = ""
    stoat_bot_mode: str = "WAKEWORD"
    stoat_wake_word: str = "hey claw"

    # AI Model & Provider
    agent_provider: str = "ollama"  # ollama, anthropic
    agent_model: str = "llama3.2"
    ollama_mode: str = "local"  # local, cloud
    ollama_host: str = "http://localhost:11434"
    api_key: str = ""
    api_keys: List[str] = field(default_factory=list)
    anthropic_api_key: str = ""

    # Workspace & Account Execution Isolation
    agent_account_mode: str = "home_agent"  # home_agent, separate_user, custom
    agent_workdir: str = ""
    agent_run_as_user: str = ""
    agent_allow_elevated: str = "0"

    # Subagents
    subagents_enabled: bool = True
    subagent_model: str = ""
    subagent_max_parallel: int = 3

    # Personalization (the control panel's Personalization tab)
    bot_name: str = ""        # what the agent calls itself; blank = use the profile name
    gender: str = ""          # free text ("female", "Gender Non-Conforming", ...); blank = unspecified
    pronouns: str = ""        # free text ("he/him", "xe/xer", ...); blank = unspecified
    system_prompt: str = ""   # blank = the built-in default prompt (agent.DEFAULT_PROMPT_TEMPLATE)

    # Integrations. Which ones are on, and their settings, are stored per profile in
    # .profiles/<id>.integrations.env (see integration_settings.py); this list is only
    # the older way of storing "which are on" and is still honoured until that file says otherwise.
    integrations: List[str] = field(default_factory=list)

    # Per-Agent Dependencies (custom pip packages for this agent)
    dependencies: List[str] = field(default_factory=list)

    # Operational settings
    auto_restart: bool = True
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if not valid_profile_id(self.id):
            raise ValueError(f"Invalid profile id {self.id!r}: use 1-40 characters from a-z, 0-9, _ and - "
                             "(must start with a letter or digit).")
        if not str(self.name or "").strip():
            self.name = self.id
        # Personalization: names / gender / pronouns are single-line and bounded; the prompt keeps its newlines.
        for attr, cap in (("bot_name", 80), ("gender", 100), ("pronouns", 100)):
            setattr(self, attr, " ".join(str(getattr(self, attr, "") or "").split())[:cap])
        self.system_prompt = str(self.system_prompt or "").replace("\r\n", "\n").replace("\x00", "")[:60000]

    @property
    def display_bot_name(self) -> str:
        """The name the agent answers to."""
        return self.bot_name or self.name

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_public_dict(self) -> Dict[str, Any]:
        """to_dict() with credentials replaced by {"set": bool}; this is what the HTTP API returns."""
        d = self.to_dict()
        for k in SECRET_FIELDS:
            v = d.get(k)
            d[k] = {"set": bool(v)} if k != "api_keys" else {"set": bool(v), "count": len(v or [])}
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> BotProfile:
        valid = cls.__dataclass_fields__
        filtered = {k: v for k, v in data.items() if k in valid}
        filtered.setdefault("name", filtered.get("id", ""))
        for k, f in valid.items():                       # coerce what a JSON body / hand edit may get wrong
            if k not in filtered:
                continue
            v, want = filtered[k], str(f.type)
            if want == "bool":
                filtered[k] = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "on", "yes")
            elif want == "int":
                try:
                    filtered[k] = max(1, int(v))
                except (TypeError, ValueError):
                    filtered.pop(k)
            elif want == "float":
                try:
                    filtered[k] = float(v)
                except (TypeError, ValueError):
                    filtered.pop(k)
            elif want.startswith("List"):
                if isinstance(v, str):
                    v = [x.strip() for x in v.replace(";", ",").split(",")]
                filtered[k] = [str(x).strip() for x in (v if isinstance(v, (list, tuple)) else []) if str(x).strip()]
            elif want == "str":
                filtered[k] = "" if v is None else str(v)
        return cls(**filtered)

    def to_env_dict(self) -> Dict[str, str]:
        """Convert this profile into environment variables passed to the bot process."""
        env: Dict[str, str] = {
            "SYSTEMC_PROFILE_ID": self.id,
            "AGENT_NAME": self.display_bot_name,
            "AGENT_GENDER": self.gender,
            "AGENT_PRONOUNS": self.pronouns,
            "AGENT_SYSTEM_PROMPT": self.system_prompt,    # may span lines; "" = built-in default
            "BOT_MODE": self.discord_bot_mode,
            "WAKE_WORD": self.discord_wake_word,
            "AGENT_PROVIDER": self.agent_provider,
            "AGENT_MODEL": self.agent_model,
            "OLLAMA_MODE": self.ollama_mode,
            "OLLAMA_HOST": self.ollama_host,
            "AGENT_ACCOUNT_MODE": self.agent_account_mode,
            "AGENT_ALLOW_ELEVATED": self.agent_allow_elevated,
            "SUBAGENTS": "on" if self.subagents_enabled else "off",
        }

        if self.discord_enabled and self.discord_token:
            env["DISCORD_BOT_TOKEN"] = self.discord_token
            env["ALLOWED_CHANNEL_IDS"] = self.discord_channels

        if self.stoat_enabled and self.stoat_token:
            env["STOAT_BOT_TOKEN"] = self.stoat_token
            env["STOAT_ALLOWED_CHANNEL_IDS"] = self.stoat_channels
            env["STOAT_BOT_MODE"] = self.stoat_bot_mode
            env["STOAT_WAKE_WORD"] = self.stoat_wake_word

        # API Keys
        if self.api_key:
            env["OLLAMA_API_KEY"] = self.api_key
        if self.api_keys:
            env["OLLAMA_API_KEYS"] = ",".join(self.api_keys)
        if self.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = self.anthropic_api_key

        # Subagent model
        if self.subagent_model:
            env["SUBAGENT_MODEL"] = self.subagent_model
        env["SUBAGENT_MAX_PARALLEL"] = str(self.subagent_max_parallel)

        # Isolated workspace
        if self.agent_workdir:
            env["AGENT_WORKDIR"] = self.agent_workdir
        elif self.id != "default":
            # Other profiles get their own workspace folder. "default" keeps the original
            # workspace/ so a migrated single-bot install finds its files where they were.
            env["AGENT_WORKDIR"] = os.path.join(APP_DIR, "workspace", "profiles", self.id)

        if self.agent_run_as_user:
            env["AGENT_RUN_AS_USER"] = self.agent_run_as_user

        if self.integrations:
            env["INTEGRATIONS"] = ",".join(self.integrations)

        return env

    def export_env_file(self, dest_path: str) -> None:
        """Write this profile's configuration as a standalone .env file."""
        env_dict = self.to_env_dict()
        with open(dest_path, "w", encoding="utf-8") as f:
            f.write(f"# SystemC Profile: {self.name} (id: {self.id})\n")
            f.write(f"# Exported at {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            for k, v in sorted(env_dict.items()):
                if "\n" in v:        # a .env line can't hold a multi-line value
                    f.write(f"# {k} is multi-line and stays in the profile's JSON file\n")
                    continue
                f.write(f"{k}={v}\n")


class ProfileManager:
    """Manages profile definitions on disk and supervises running bot instances."""

    def __init__(self, root_dir: str = APP_DIR):
        self.root_dir = root_dir
        self.profiles_dir = os.path.join(root_dir, ".profiles")
        self.registry_file = os.path.join(self.profiles_dir, "profiles.json")
        self._lock = threading.RLock()
        os.makedirs(self.profiles_dir, exist_ok=True)
        try:
            os.chmod(self.profiles_dir, 0o700)
        except OSError:
            pass
        self._ensure_default_exists()

    def _ensure_default_exists(self):
        """Create a default profile or auto-migrate legacy .env if present."""
        if not os.path.exists(self.registry_file):
            legacy_env = os.path.join(self.root_dir, ".env")
            if os.path.exists(legacy_env):
                self.migrate_from_env(legacy_env, "default")
            else:
                default_prof = BotProfile(
                    id="default",
                    name="SystemC Prime",
                    description="Primary SystemC bot agent instance",
                    color="#89b4fa",
                    icon="🦞",
                    agent_model="qwen2.5",
                    dependencies=["requests", "httpx"],
                )
                self.save_profile(default_prof, set_active=True)

    def list_profiles(self) -> List[BotProfile]:
        profiles = []
        if not os.path.exists(self.registry_file):
            return profiles

        try:
            with open(self.registry_file, "r", encoding="utf-8") as f:
                registry = json.load(f)
            profile_ids = registry.get("profiles", [])
            for pid in profile_ids:
                prof = self.get_profile(pid)
                if prof:
                    profiles.append(prof)
        except Exception as e:
            print(f"[profiles] error reading registry: {e}")
        return profiles

    def _read_registry(self) -> Dict[str, Any]:
        registry: Dict[str, Any] = {"profiles": [], "active_profile": "default"}
        if os.path.exists(self.registry_file):
            try:
                with open(self.registry_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    registry.update(data)
            except Exception:
                pass
        return registry

    def _write_registry(self, registry: Dict[str, Any]) -> None:
        _write_json_private(self.registry_file, registry)

    def get_profile(self, profile_id: str) -> Optional[BotProfile]:
        if not valid_profile_id(profile_id):
            return None
        prof_file = os.path.join(self.profiles_dir, f"{profile_id}.json")
        if not os.path.exists(prof_file):
            return None
        try:
            with open(prof_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return BotProfile.from_dict(data)
        except Exception as e:
            print(f"[profiles] error reading profile {profile_id}: {e}")
            return None

    def save_profile(self, profile: BotProfile, set_active: bool = False) -> None:
        if not valid_profile_id(profile.id):
            raise ValueError(f"Invalid profile id {profile.id!r}.")
        profile.dependencies = [d for d in profile.dependencies if valid_dependency(d)]
        with self._lock:
            profile.updated_at = time.time()
            _write_json_private(os.path.join(self.profiles_dir, f"{profile.id}.json"), profile.to_dict())

            registry = self._read_registry()
            if profile.id not in registry.get("profiles", []):
                registry.setdefault("profiles", []).append(profile.id)
            if set_active or not registry.get("active_profile"):
                registry["active_profile"] = profile.id
            self._write_registry(registry)

    def delete_profile(self, profile_id: str) -> bool:
        if profile_id == "default" or not valid_profile_id(profile_id):
            return False  # Protect default profile from deletion
        with self._lock:
            for fname in (f"{profile_id}.json", f"{profile_id}.integrations.env"):
                fpath = os.path.join(self.profiles_dir, fname)
                if os.path.exists(fpath):
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass
            registry = self._read_registry()
            if profile_id in registry.get("profiles", []):
                registry["profiles"].remove(profile_id)
            if registry.get("active_profile") == profile_id:
                registry["active_profile"] = "default"
            try:
                self._write_registry(registry)
            except OSError:
                pass
        return True

    def get_active_profile(self) -> BotProfile:
        with self._lock:
            active_id = self._read_registry().get("active_profile") or "default"
            prof = self.get_profile(active_id) or self.get_profile("default")
            if not prof:
                prof = BotProfile(id="default", name="SystemC Prime")
                self.save_profile(prof, set_active=True)
            return prof

    def set_active_profile(self, profile_id: str) -> bool:
        with self._lock:
            if not self.get_profile(profile_id):
                return False
            registry = self._read_registry()
            registry["active_profile"] = profile_id
            try:
                self._write_registry(registry)
                return True
            except OSError:
                return False

    def migrate_from_env(self, env_path: str = LEGACY_ENV_FILE, target_id: str = "default") -> Tuple[bool, str]:
        """
        Migrates old flat .env file into the new profile system.
        Extracts Discord, Stoat, Ollama, model, workspace, and tokens.
        """
        if not valid_profile_id(target_id):
            return False, f"Invalid profile id {target_id!r}."
        if not os.path.isfile(env_path):
            return False, f"Source .env file '{env_path}' not found."

        env_vars: Dict[str, str] = {}
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                env_vars[k.strip()] = v

        # Parse API keys list
        api_keys = []
        for k_val in (env_vars.get("OLLAMA_API_KEY", ""), env_vars.get("OLLAMA_API_KEYS", "")):
            for part in k_val.replace(";", ",").split(","):
                part = part.strip()
                if part and part not in api_keys:
                    api_keys.append(part)

        integrations_list = [
            x.strip() for x in env_vars.get("INTEGRATIONS", "").replace(";", ",").split(",") if x.strip()
        ]

        profile = BotProfile(
            id=target_id,
            name=env_vars.get("AGENT_NAME") or "SystemC Migrated Bot",
            gender=env_vars.get("AGENT_GENDER", ""),
            pronouns=env_vars.get("AGENT_PRONOUNS", ""),
            system_prompt=env_vars.get("AGENT_SYSTEM_PROMPT", ""),
            description="Migrated automatically from legacy .env configuration",
            color="#89b4fa",
            icon="🔄",
            discord_enabled=bool(env_vars.get("DISCORD_BOT_TOKEN")),
            discord_token=env_vars.get("DISCORD_BOT_TOKEN", ""),
            discord_channels=env_vars.get("ALLOWED_CHANNEL_IDS", ""),
            discord_bot_mode=env_vars.get("BOT_MODE", "WAKEWORD"),
            discord_wake_word=env_vars.get("WAKE_WORD", "hey claw"),
            stoat_enabled=bool(env_vars.get("STOAT_BOT_TOKEN")),
            stoat_token=env_vars.get("STOAT_BOT_TOKEN", ""),
            stoat_channels=env_vars.get("STOAT_ALLOWED_CHANNEL_IDS", ""),
            stoat_bot_mode=env_vars.get("STOAT_BOT_MODE", "WAKEWORD"),
            stoat_wake_word=env_vars.get("STOAT_WAKE_WORD", "hey claw"),
            agent_provider=env_vars.get("AGENT_PROVIDER", "ollama"),
            agent_model=env_vars.get("AGENT_MODEL", "qwen2.5"),
            ollama_mode=env_vars.get("OLLAMA_MODE", "local"),
            ollama_host=env_vars.get("OLLAMA_HOST", "http://localhost:11434"),
            api_key=api_keys[0] if api_keys else "",
            api_keys=api_keys[1:] if len(api_keys) > 1 else [],
            anthropic_api_key=env_vars.get("ANTHROPIC_API_KEY", ""),
            agent_account_mode=env_vars.get("AGENT_ACCOUNT_MODE", "home_agent"),
            agent_workdir=env_vars.get("AGENT_WORKDIR", ""),
            agent_run_as_user=env_vars.get("AGENT_RUN_AS_USER", ""),
            agent_allow_elevated=env_vars.get("AGENT_ALLOW_ELEVATED", "0"),
            subagents_enabled=env_vars.get("SUBAGENTS", "on").lower() != "off",
            subagent_model=env_vars.get("SUBAGENT_MODEL", ""),
            integrations=integrations_list,
            dependencies=["httpx", "requests"],
        )

        # Keep what's already on the profile (name/icon/colour/deps) if one exists, and always
        # back up the .env before anything is written.
        self.save_profile(profile, set_active=True)

        # Make a backup of the old .env so nothing is ever lost
        backup_env = env_path + ".legacy.backup"
        if not os.path.exists(backup_env):
            try:
                shutil.copy2(env_path, backup_env)
            except Exception:
                pass

        return True, f"Successfully migrated legacy .env into profile '{profile.name}' (id: '{profile.id}'). Backup saved to .env.legacy.backup."

    def pull_dependencies(self, profile_id: str, log_queue: Optional[queue.Queue] = None) -> Tuple[bool, str]:
        """
        Pull / install extra pip dependencies configured for this specific agent profile.
        """
        prof = self.get_profile(profile_id)
        if not prof:
            return False, f"Profile '{profile_id}' not found."

        deps = [d.strip() for d in prof.dependencies if valid_dependency(d)]
        if not deps:
            msg = f"Profile '{prof.name}' has no custom dependencies listed."
            if log_queue:
                log_queue.put((profile_id, msg + "\n"))
            return True, msg

        cmd = [sys.executable, "-m", "pip", "install", *deps]
        if log_queue:
            log_queue.put((profile_id, f"[$ pip install {' '.join(deps)}]\n"))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            output = []
            if proc.stdout:
                for line in proc.stdout:
                    output.append(line)
                    if log_queue:
                        log_queue.put((profile_id, line))
            proc.wait()
            success = proc.returncode == 0
            summary = "".join(output[-5:]) if output else "Done."
            return success, f"Dependencies installation for '{prof.name}' finished (code {proc.returncode}):\n{summary}"
        except Exception as e:
            return False, f"Failed to pull dependencies: {e}"


# ── Multi-Instance Process Supervisor ────────────────────────────────────────

class MultiBotSupervisor:
    """Supervises multiple concurrent bot instances (e.g. up to 3+ bots running at once)."""

    def __init__(self, manager: ProfileManager):
        self.manager = manager
        self.instances: Dict[str, Dict[str, Any]] = {}
        self.log_queues: Dict[str, LogBuffer] = {}
        self.lock = threading.Lock()
        self._restarts: Dict[str, List[float]] = {}

    def get_log_queue(self, profile_id: str) -> LogBuffer:
        with self.lock:
            if profile_id not in self.log_queues:
                self.log_queues[profile_id] = LogBuffer(1000)
            return self.log_queues[profile_id]

    def drain_logs(self) -> List[Tuple[str, str]]:
        """New (profile_id, line) pairs from every instance since the last call (control panel)."""
        with self.lock:
            bufs = list(self.log_queues.values())
        out: List[Tuple[str, str]] = []
        for b in bufs:
            out.extend(b.drain())
        return out

    def is_running(self, profile_id: str) -> bool:
        with self.lock:
            inst = self.instances.get(profile_id)
            if not inst:
                return False
            proc = inst.get("process")
            return proc is not None and proc.poll() is None

    def start_instance(self, profile_id: str) -> Tuple[bool, str]:
        """Start a dedicated bot process for a given profile."""
        if not valid_profile_id(profile_id):
            return False, f"Invalid profile id {profile_id!r}."
        if self.is_running(profile_id):
            return True, f"Bot instance for profile '{profile_id}' is already running."

        profile = self.manager.get_profile(profile_id)
        if not profile:
            return False, f"Profile '{profile_id}' does not exist."

        # Check if any token is configured
        if not profile.discord_token and not profile.stoat_token:
            return False, f"Profile '{profile.name}' has no Discord or Stoat bot token configured."

        # Make sure isolated workspace exists
        legacy = profile_id == "default"      # the default profile keeps the original folders
        workdir = profile.agent_workdir or (os.path.join(APP_DIR, "workspace") if legacy
                                            else os.path.join(APP_DIR, "workspace", "profiles", profile_id))
        sub_ = [] if legacy else ["profiles", profile_id]
        try:
            os.makedirs(workdir, exist_ok=True)
            os.makedirs(os.path.join(APP_DIR, ".history", *sub_), exist_ok=True)
            os.makedirs(os.path.join(APP_DIR, ".memory", *sub_), exist_ok=True)
        except OSError as e:
            return False, f"Failed to initialize workspace for '{profile_id}': {e}"

        log_q = self.get_log_queue(profile_id)
        log_q.put((profile_id, f"🚀 Starting bot instance for profile [{profile.name}] ({profile_id})...\n"))

        # Merge environment variables
        proc_env = os.environ.copy()
        proc_env.update(profile.to_env_dict())
        proc_env["PYTHONUNBUFFERED"] = "1"
        proc_env["PYTHONIOENCODING"] = "utf-8"

        # Auto-pull dependencies before launch if specified
        if profile.dependencies:
            log_q.put((profile_id, f"📦 Checking/pulling dependencies for {profile.name}...\n"))
            self.manager.pull_dependencies(profile_id, log_q)

        args = [sys.executable, os.path.join(APP_DIR, "main.py"), "--profile", profile_id]

        try:
            proc = subprocess.Popen(
                args,
                cwd=APP_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=proc_env,
            )

            with self.lock:
                self.instances[profile_id] = {
                    "process": proc,
                    "profile_id": profile_id,
                    "started_at": time.time(),
                    "pid": proc.pid,
                    "manual_stop": False,
                }

            # Start reader thread to stream logs into queue
            reader = threading.Thread(target=self._pump_logs, args=(profile_id, proc, log_q), daemon=True)
            reader.start()

            return True, f"Successfully started bot instance [{profile.name}] (PID: {proc.pid})."
        except Exception as e:
            return False, f"Failed to start bot instance: {e}"

    def stop_instance(self, profile_id: str) -> Tuple[bool, str]:
        with self.lock:
            inst = self.instances.get(profile_id)
            if not inst or not inst.get("process"):
                return True, f"Bot instance '{profile_id}' is not running."
            inst["manual_stop"] = True
            proc = inst["process"]

        try:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
            msg = f"Bot instance '{profile_id}' stopped."
            self.get_log_queue(profile_id).put((profile_id, f"🛑 {msg}\n"))
            return True, msg
        except Exception as e:
            return False, f"Error stopping instance '{profile_id}': {e}"

    def stop_all(self) -> Dict[str, bool]:
        results = {}
        with self.lock:
            ids = list(self.instances.keys())
        for pid in ids:
            ok, _ = self.stop_instance(pid)
            results[pid] = ok
        return results

    def _pump_logs(self, profile_id: str, proc: subprocess.Popen, log_q: queue.Queue):
        try:
            if proc.stdout:
                for line in proc.stdout:
                    log_q.put((profile_id, line))
        except Exception:
            pass
        code = proc.wait()
        log_q.put((profile_id, f"\n[Bot instance '{profile_id}' exited with code {code}]\n"))

        # Check for auto-restart
        with self.lock:
            inst = self.instances.get(profile_id)
            manual = inst.get("manual_stop", False) if inst else True

        if not manual and code != 0:
            prof = self.manager.get_profile(profile_id)
            if prof and prof.auto_restart:
                # At most 5 automatic restarts in 5 minutes: a bad token or a missing package
                # would otherwise restart forever every 3 seconds.
                now = time.time()
                with self.lock:
                    recent = [t for t in self._restarts.get(profile_id, []) if now - t < 300]
                    give_up = len(recent) >= 5
                    if not give_up:
                        recent.append(now)
                    self._restarts[profile_id] = recent
                if give_up:
                    log_q.put((profile_id, f"⛔ [{profile_id}] crashed 5 times in 5 minutes — "
                                           "not restarting again. Fix the problem and start it manually.\n"))
                    return
                log_q.put((profile_id, f"⚠️ Crashed — auto-restarting [{profile_id}] in 3s...\n"))
                time.sleep(3)
                with self.lock:
                    inst = self.instances.get(profile_id)
                    if inst and inst.get("manual_stop"):
                        return                      # stopped by the user during the wait
                self.start_instance(profile_id)

    def get_status_overview(self) -> List[Dict[str, Any]]:
        profiles = self.manager.list_profiles()
        overview = []
        for p in profiles:
            running = self.is_running(p.id)
            with self.lock:
                inst = self.instances.get(p.id)
            pid = inst.get("pid") if (inst and running) else None
            started_at = inst.get("started_at") if (inst and running) else None
            overview.append({
                "id": p.id,
                "name": p.name,
                "icon": p.icon,
                "color": p.color,
                "running": running,
                "pid": pid,
                "started_at": started_at,
                "uptime": (time.time() - started_at) if started_at else 0,
                "model": p.agent_model,
                "provider": p.agent_provider,
                "dependencies": p.dependencies,
                "discord_enabled": p.discord_enabled,
                "stoat_enabled": p.stoat_enabled,
            })
        return overview


# Global singleton manager and supervisor
manager = ProfileManager()
supervisor = MultiBotSupervisor(manager)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "list":
            for p in manager.list_profiles():
                print(f"[{p.icon} {p.id}] {p.name} | Model: {p.agent_model} | Deps: {', '.join(p.dependencies) or 'none'}")
        elif cmd == "migrate":
            env_file = sys.argv[2] if len(sys.argv) > 2 else LEGACY_ENV_FILE
            ok, msg = manager.migrate_from_env(env_file)
            print(msg)
        elif cmd == "install-deps" and len(sys.argv) > 2:
            pid = sys.argv[2]
            ok, msg = manager.pull_dependencies(pid)
            print(msg)
        elif cmd == "run" and len(sys.argv) > 2:
            pid = sys.argv[2]
            ok, msg = supervisor.start_instance(pid)
            print(msg)
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                supervisor.stop_instance(pid)
        else:
            print("usage: python profiles.py [list | migrate <file> | install-deps <id> | run <id>]")
    else:
        print("SystemC Profiles Manager initialized.")
