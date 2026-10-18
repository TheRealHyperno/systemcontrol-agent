"""
Two backends, one interface. The agent loop only speaks this common format:

  user message:      {"role": "user", "content": "text"}
  assistant message:  {"role": "assistant", "content": "text or None",
                        "tool_calls": [{"id", "name", "input"}]}   # tool_calls may be []
  tool result:        {"role": "tool", "tool_call_id", "name", "content": "text"}

Each provider converts this to/from whatever shape its API actually wants.
"""

import os
import re
import sys
import json
import time
import uuid
import threading
import urllib.request
import urllib.error

OLLAMA_LOCAL_HOST = "http://localhost:11434"
OLLAMA_CLOUD_HOST = "https://ollama.com"


def collect_api_keys(src=None) -> list[str]:
    """All Ollama Cloud API keys, in priority order, de-duplicated.

    OLLAMA_API_KEY   the primary key
    OLLAMA_API_KEYS  more keys, separated by commas / spaces / newlines
    """
    src = os.environ if src is None else src
    keys: list[str] = []
    for chunk in (src.get("OLLAMA_API_KEY", ""), src.get("OLLAMA_API_KEYS", "")):
        for k in re.split(r"[,\s;]+", chunk or ""):
            if k and k not in keys:
                keys.append(k)
    return keys


def ollama_settings() -> dict:
    """
    Work out where to talk to Ollama, from env vars:

      OLLAMA_MODE      local | cloud   (optional; inferred if missing)
      OLLAMA_HOST      custom host     (default: localhost, or ollama.com in cloud mode)
      OLLAMA_API_KEY   key from https://ollama.com/settings/keys (cloud only)
      OLLAMA_API_KEYS  extra keys, comma-separated — used as fallbacks, in order

    Returns {"mode", "host", "api_key" (first key), "api_keys" (all keys)}.
    """
    api_keys = collect_api_keys()
    host = os.environ.get("OLLAMA_HOST", "").strip().rstrip("/")
    mode = os.environ.get("OLLAMA_MODE", "").strip().lower()
    if mode not in ("local", "cloud"):
        # Infer: a key + (no host or an ollama.com host) means cloud
        mode = "cloud" if api_keys and (not host or "ollama.com" in host) else "local"
    if mode == "cloud":
        host = host if host and "localhost" not in host and "127.0.0.1" not in host else OLLAMA_CLOUD_HOST
    else:
        host = host or OLLAMA_LOCAL_HOST
        api_keys = []  # never send a cloud key to a local server
    return {"mode": mode, "host": host, "api_key": api_keys[0] if api_keys else "", "api_keys": api_keys}


# ── API key pool: use keys in order, fall back when one fails ───────────────
# How long (seconds) a key is skipped after it fails with each HTTP status.
KEY_COOLDOWNS = {429: 15 * 60,      # usage / rate limit hit
                 402: 60 * 60,      # no usage included / out of credits
                 401: 24 * 3600,    # key rejected
                 403: 24 * 3600}
_KEY_REASONS = {429: "hit its usage limit (429)", 402: "has no usage left for this model (402)",
                401: "was rejected (401)", 403: "was rejected (403)"}


def _mask(key: str) -> str:
    return "…" + key[-4:] if len(key) > 4 else "…"


class KeyPool:
    """Priority-ordered API keys. The first key that isn't cooling down is used;
    when one fails (402/429/401/403) it's parked for a while and the next takes
    over. The primary key is picked up again automatically once its cooldown ends.
    Thread-safe (subagents call the model from several threads)."""

    def __init__(self, keys):
        self.keys = list(keys)
        self._until: dict[int, tuple[float, int]] = {}   # idx -> (retry_at, http_code)
        self._lock = threading.Lock()
        self._notices: list[str] = []

    def acquire(self):
        now = time.time()
        with self._lock:
            for i, k in enumerate(self.keys):
                u = self._until.get(i)
                if not u or u[0] <= now:
                    return i, k
        return None

    def mark_ok(self, idx: int):
        with self._lock:
            self._until.pop(idx, None)

    def mark_bad(self, idx: int, code: int):
        now = time.time()
        with self._lock:
            cur = self._until.get(idx)
            if cur and cur[0] > now:
                return                      # another thread already parked this key
            self._until[idx] = (now + KEY_COOLDOWNS.get(code, 900), code)
            nxt = next((j for j in range(len(self.keys))
                        if j != idx and not (self._until.get(j) and self._until[j][0] > now)), None)
            why = _KEY_REASONS.get(code, f"failed (HTTP {code})")
            if nxt is not None:
                msg = (f"🔑 API key #{idx + 1} ({_mask(self.keys[idx])}) {why} — "
                       f"switching to key #{nxt + 1} ({_mask(self.keys[nxt])}).")
                self._notices.append(msg)
            else:
                msg = f"🔑 API key #{idx + 1} ({_mask(self.keys[idx])}) {why}. No other keys available."
            print(f"[ollama] {msg}", flush=True)

    def drain_notices(self) -> list[str]:
        with self._lock:
            out, self._notices = self._notices, []
        return out

    def exhausted_message(self, host: str, model: str) -> str:
        now = time.time()
        with self._lock:
            state = dict(self._until)
        if len(self.keys) == 1:
            code = state.get(0, (0, 0))[1]
            return _explain_key_failure(code, host, model)
        parts = []
        for i, k in enumerate(self.keys):
            u = state.get(i)
            if u:
                mins = max(1, int((u[0] - now) / 60))
                parts.append(f"#{i + 1} ({_mask(k)}) HTTP {u[1]}, retry in ~{mins} min")
        return (f"All {len(self.keys)} Ollama API keys are unavailable right now: " + "; ".join(parts) +
                f". Add credits or another key at https://ollama.com/settings/keys, "
                f"or pick a model included in your plan ('{model}' may not be).")


def _explain_key_failure(code: int, host: str, model: str) -> str:
    if code in (401, 403):
        return (f"{host} rejected the API key (HTTP {code}). "
                "Check your key — keys live at https://ollama.com/settings/keys")
    if code == 402:
        return (f"'{model}' isn't included in your free Ollama Cloud usage (HTTP 402). "
                "Pick another model (🔎 Fetch available models), add another API key, or add "
                "usage credits / upgrade at https://ollama.com/settings")
    if code == 429:
        return ("Ollama Cloud usage limit reached (HTTP 429). Wait a bit, use another model or "
                "another API key, or upgrade at https://ollama.com/upgrade")
    return f"Ollama request failed (HTTP {code})"


_POOL: "KeyPool | None" = None
_POOL_LOCK = threading.Lock()


def _get_pool(keys) -> KeyPool:
    """One pool shared by the main agent and its subagents, so a failed key is
    skipped everywhere. Rebuilt only if the list of keys changes."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None or _POOL.keys != list(keys):
            _POOL = KeyPool(keys)
        return _POOL


def drain_key_notices() -> list[str]:
    """Chat-ready messages about key switches since the last call (used by agent.py)."""
    return _POOL.drain_notices() if _POOL else []


def list_ollama_models(host: str = "", api_key: str = "", timeout: float = 15.0) -> list[str]:
    """
    Ask an Ollama server which model IDs it offers (GET /api/tags).
    Works for a local server and for Ollama Cloud (pass the API key).
    Raises RuntimeError with a human-readable message on failure.
    """
    host = (host or OLLAMA_CLOUD_HOST).rstrip("/")
    req = urllib.request.Request(f"{host}/api/tags")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError(f"{host} rejected the API key (HTTP {e.code}). "
                               "Create one at https://ollama.com/settings/keys") from e
        raise RuntimeError(f"{host} returned HTTP {e.code}") from e
    except Exception as e:
        raise RuntimeError(f"Couldn't reach {host}: {e}") from e
    names = [m.get("model") or m.get("name") for m in data.get("models", [])]
    return sorted(n for n in names if n)


def _cloud_model_id(model: str) -> str:
    """The hosted API (ollama.com) uses plain IDs like 'gpt-oss:120b'; the
    '-cloud' / ':cloud' suffix is only for cloud models run via a local server."""
    for suffix in ("-cloud", ":cloud"):
        if model.endswith(suffix):
            return model[: -len(suffix)]
    return model


def _to_openai_tool_schema(schema: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": schema["name"],
            "description": schema.get("description", ""),
            "parameters": schema.get("input_schema", {"type": "object", "properties": {}}),
        },
    }


class AnthropicProvider:
    def __init__(self, model: str, api_key: str, system_prompt: str):
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.system_prompt = system_prompt

    def _to_native(self, history: list) -> list:
        native = []
        for m in history:
            if m["role"] == "user":
                native.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                blocks = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls", []):
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc["input"],
                        }
                    )
                native.append({"role": "assistant", "content": blocks})
            elif m["role"] == "tool":
                native.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m["tool_call_id"],
                                "content": m["content"],
                            }
                        ],
                    }
                )
        return native

    def step(self, history: list, tool_schemas: list) -> dict:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=1500,
            system=self.system_prompt,
            tools=tool_schemas,
            messages=self._to_native(history),
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip() or None
        tool_calls = [
            {"id": b.id, "name": b.name, "input": b.input}
            for b in resp.content
            if b.type == "tool_use"
        ]
        return {"role": "assistant", "content": text, "tool_calls": tool_calls}


class OllamaProvider:
    """Talks to Ollama — a local server, or Ollama Cloud (https://ollama.com)
    with one or more API keys (tried in order; see KeyPool). Needs a model that
    supports tool calling (e.g. llama3.1, qwen3, gpt-oss, mistral-nemo, ...)."""

    def __init__(self, model: str, host: str, system_prompt: str,
                 api_key: str = "", api_keys: "list[str] | None" = None):
        import ollama
        self._ollama = ollama
        self.host = host
        keys = list(api_keys) if api_keys else ([api_key] if api_key else [])
        self.keys = keys
        self.is_cloud = bool(keys)
        self.pool = _get_pool(keys) if keys else None
        self._clients: dict = {}
        self._clients_lock = threading.Lock()
        self.client = self._client_for(keys[0] if keys else "")   # kept for compatibility
        self.model = _cloud_model_id(model) if keys else model
        self.system_prompt = system_prompt

    def _client_for(self, key: str):
        with self._clients_lock:
            if key not in self._clients:
                headers = {"Authorization": f"Bearer {key}"} if key else None
                self._clients[key] = self._ollama.Client(host=self.host, headers=headers, timeout=240)
            return self._clients[key]

    def _to_native(self, history: list) -> list:
        native = [{"role": "system", "content": self.system_prompt}]
        for m in history:
            if m["role"] == "user":
                native.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msg = {"role": "assistant", "content": m.get("content") or ""}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [
                        {"function": {"name": tc["name"], "arguments": tc["input"]}}
                        for tc in m["tool_calls"]
                    ]
                native.append(msg)
            elif m["role"] == "tool":
                native.append(
                    {"role": "tool", "content": m["content"], "name": m["name"]}
                )
        return native

    def step(self, history: list, tool_schemas: list) -> dict:
        oa_tools = [_to_openai_tool_schema(s) for s in tool_schemas]
        messages = self._to_native(history)
        while True:
            idx, key = None, ""
            if self.pool:
                slot = self.pool.acquire()
                if slot is None:
                    raise RuntimeError(self.pool.exhausted_message(self.host, self.model))
                idx, key = slot
            try:
                resp = self._client_for(key).chat(model=self.model, messages=messages, tools=oa_tools)
                if self.pool:
                    self.pool.mark_ok(idx)
                break
            except self._ollama.ResponseError as e:
                code = getattr(e, "status_code", None)
                if self.pool and code in KEY_COOLDOWNS:
                    self.pool.mark_bad(idx, code)   # park this key; loop picks the next one
                    continue
                if code == 404:
                    raise RuntimeError(
                        f"Model '{self.model}' not found on {self.host}. "
                        "List valid IDs with:  python providers.py models"
                    ) from e
                raise
            except ConnectionError as e:
                raise RuntimeError(f"Couldn't connect to Ollama at {self.host}: {e}") from e
        msg = resp["message"]
        text = (msg.get("content") or "").strip() or None
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc["function"]
            tool_calls.append(
                {
                    "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "name": fn["name"],
                    "input": fn.get("arguments") or {},
                }
            )
        return {"role": "assistant", "content": text, "tool_calls": tool_calls}


def build_provider(system_prompt: str, model: str | None = None):
    """AGENT_PROVIDER=ollama (default; local or Ollama Cloud) or anthropic, picked via env vars."""
    kind = os.environ.get("AGENT_PROVIDER", "ollama").lower()
    if kind == "anthropic":
        return AnthropicProvider(
            model=model or os.environ.get("AGENT_MODEL", "claude-sonnet-4-6"),
            api_key=os.environ["ANTHROPIC_API_KEY"],
            system_prompt=system_prompt,
        )
    cfg = ollama_settings()
    if cfg["mode"] == "cloud" and not cfg["api_keys"]:
        raise RuntimeError("Ollama Cloud mode needs OLLAMA_API_KEY (or OLLAMA_API_KEYS) "
                           "(create one at https://ollama.com/settings/keys)")
    return OllamaProvider(
        model=model or os.environ.get("AGENT_MODEL", "llama3.2"),
        host=cfg["host"],
        system_prompt=system_prompt,
        api_keys=cfg["api_keys"],
    )


if __name__ == "__main__":
    # python providers.py models   -> print the model IDs your configured Ollama offers
    here = os.path.dirname(os.path.abspath(__file__))
    envp = os.path.join(here, ".env")
    if os.path.exists(envp):
        for line in open(envp):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    if len(sys.argv) > 1 and sys.argv[1] == "models":
        cfg = ollama_settings()
        print(f"[{cfg['mode']}] {cfg['host']}")
        last_err = None
        for key in (cfg["api_keys"] or [""]):          # first key that works wins
            try:
                for name in list_ollama_models(cfg["host"], key):
                    print(" ", name)
                last_err = None
                break
            except RuntimeError as e:
                last_err = e
        if last_err:
            print("Error:", last_err)
            sys.exit(1)
    else:
        print("usage: python providers.py models")
