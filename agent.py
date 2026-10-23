"""
Agent loop. Drives the model through tool calls until it stops, properly
handling structured tool outputs and the approval gate for destructive shell
commands. Tool results are returned as structured dicts (JSON-encoded), not
flat strings, so the model can reason about exit codes, errors, etc.
"""

import asyncio
import json
import os
import tools
import memory
import sessions
import integrations
from providers import build_provider, drain_key_notices

def _env_turns(default: int = 20) -> int:
    try:
        return max(1, int(os.environ.get("AGENT_MAX_TURNS", default)))
    except (TypeError, ValueError):
        return default


MAX_TURNS = _env_turns()   # tool-call rounds per message; AGENT_MAX_TURNS in .env to change

# ── identity ────────────────────────────────────────────────────────────────
# The release name lives in version.py (edit it there, once).
# The system prompt (default text, {VARIABLES}, rendering) lives in prompt_template.py.
# Per bot it is set in the control panel's Personalization tab: name, gender, pronouns
# and the prompt itself (AGENT_NAME / AGENT_GENDER / AGENT_PRONOUNS / AGENT_SYSTEM_PROMPT).
from prompt_template import DEFAULT_AGENT_NAME, DEFAULT_PROMPT_TEMPLATE, build_from_env as _build_prompt_text


def build_system_prompt(model: str = "") -> str:
    """The configured system prompt (or the default) with its {VARIABLES} filled in."""
    prompt = _build_prompt_text(model)
    if not tools.elevated_enabled():
        # Elevation is off on this box: don't mention a tool that doesn't exist.
        prompt = prompt.replace(
            "- run_shell_elevated: same but with sudo (root). ALWAYS requires user approval.\n", "")
        prompt = prompt.replace("- Both run_shell and run_shell_elevated return {stdout, exit_code, cwd}",
                                "- run_shell returns {stdout, exit_code, cwd}")
        prompt = prompt.replace(
            '- If run_shell fails with "Permission denied", retry with run_shell_elevated',
            '- If run_shell fails with "Permission denied", you have no elevated access here: '
            'tell the user what needs doing instead of trying to work around it')
    return prompt


SUBAGENT_PROMPT = """\
You are a subagent working for a parent agent on one focused task, directly on \
the owner's computer with full shell and filesystem access.

You can't talk to the user or ask questions. Make reasonable assumptions, do \
the work with your tools, then finish with a concise final report: findings, \
results, and the paths of any files you created. That report is everything \
the parent will see, so include the specifics it asked for.

Tools: run_shell, run_python, read_file, write_file, list_dir, web_search.
- Check exit_code != 0 for errors; chain commands with && not ;
- Destructive commands need user approval — don't retry if denied
- WORKDIR is your working directory; use relative paths inside it
- Anything outside WORKDIR is the owner's real data: don't delete or overwrite it
- Never claim you can't run something — try it and report the actual result.
"""

# ── Subagents ────────────────────────────────────────────────────────────────
# Env knobs: SUBAGENTS=off to disable, SUBAGENT_MODEL to use a different model,
# SUBAGENT_MAX_TURNS / SUBAGENT_MAX_PARALLEL / SUBAGENT_TIMEOUT to tune limits.
SUB_MAX_TASKS     = 5


def _subagents_enabled() -> bool:
    return os.environ.get("SUBAGENTS", "on").lower() != "off"


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default
SUB_TOOL_NAMES    = {"run_shell", "run_python", "read_file", "write_file", "list_dir"}

SUBAGENT_TOOL_SCHEMAS = [
    {
        "name": "spawn_subagent",
        "description": (
            "Delegate ONE self-contained task to a subagent (a fresh copy of you with its "
            "own context, shell, python, files and web search). It cannot see this "
            "conversation, so include all context and say exactly what to report back. "
            "Returns its final report."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Complete, self-contained instructions"},
                "name": {"type": "string", "description": "Short label, e.g. 'disk-audit'"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "spawn_subagents",
        "description": (
            "Run up to 5 independent tasks in PARALLEL, one subagent each. Same rules as "
            "spawn_subagent: every task must be self-contained. Returns all reports."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "properties": {
                            "task": {"type": "string"},
                            "name": {"type": "string"},
                        },
                        "required": ["task"],
                    },
                },
            },
            "required": ["tasks"],
        },
    },
]


async def _send(channel, content: str):
    """Send a message via the channel's platform."""
    from chat_platform import BaseChannel
    if isinstance(channel, BaseChannel):
        await channel.send(content)
    else:
        await tools.send_message(channel, content)
    return {"sent": True}


async def _send_file(channel, path: str, caption: str = ""):
    from chat_platform import BaseChannel
    if isinstance(channel, BaseChannel):
        await channel.send_file(path, caption)
    else:
        await tools.send_file(channel, path, caption)
    return {"sent": True}


async def _fetch_history(channel, limit: int = 20):
    from chat_platform import BaseChannel
    if isinstance(channel, BaseChannel):
        msgs = []
        async for m in channel.history(limit=limit):
            msgs.append({
                "id": m.id,
                "author": m.author,
                "content": m.content,
                "timestamp": m.created_at.isoformat(),
            })
        return {"messages": msgs, "count": len(msgs)}
    return await tools.fetch_channel_messages(channel, limit)


async def _request_approval(channel, command: str, is_elevated: bool = False, timeout: int = 60) -> bool:
    from chat_platform import BaseChannel, DiscordChannel
    if isinstance(channel, BaseChannel):
        return await channel.request_approval(command, is_elevated, timeout)
    # Legacy discord channel object
    return await tools.request_approval(channel, command, timeout)



async def _reset_session(channel, current_key: "str | None", session: "str | None" = None) -> dict:
    """The reset_session tool. No argument = this channel's session; anything
    else is a different session and needs the owner's approval first."""
    key, err = sessions.resolve(session, current_key)
    if err:
        known = [f"{s['key']} {s['label']}".strip() for s in sessions.list_sessions()[:10]]
        return {"error": err, "known_sessions": known}

    if key != current_key:
        info = next((x for x in sessions.list_sessions() if x["key"] == key), None)
        if info is None:
            return {"error": f"no saved session {key!r} — nothing to reset"}
        what = f"{key} ({info['label']})" if info["label"] else key
        approved = await _request_approval(
            channel,
            f"[reset_session] Clear the conversation history of {what} "
            f"({info['turns']} turn(s)). Long-term memory is not affected.",
            False, 60,
        )
        if not approved:
            return {"error": "reset of another session was denied by the owner", "session": key}

    result = await asyncio.to_thread(sessions.reset, key)
    if not result["ok"]:
        return {"error": "couldn't fully clear the session (check file permissions on .history/)", "session": key}
    out = {
        "reset": True,
        "session": key,
        "cleared_turns": result["cleared_turns"],
        "long_term_memory": "untouched",
    }
    if key == current_key:
        out["note"] = ("This conversation's history is cleared once your current reply is sent; "
                       "the next message starts from a blank slate.")
    return out


def _fmt(result) -> str:
    """Serialize a tool result to a JSON string for the model."""
    if isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False)
    return str(result)




async def _send_usage_summary(channel, commands_used, tool_counts, tool_order):
    """Optionally post a small-text summary of commands/tools used before the reply."""
    import tools as _tools
    if not (_tools.show_commands_used() or _tools.show_tools_used()):
        return
    lines: list[str] = []
    if _tools.show_commands_used():
        uniq = list(dict.fromkeys(commands_used))
        if uniq:
            lines.append("-# __Commands executed__")
            for c in uniq[:50]:
                lines.append(f"-# - {c}")
    if _tools.show_tools_used() and tool_counts:
        if lines:
            pass
        lines.append("-# __Tools Used__")
        for name in tool_order:
            lines.append(f"-# {name} ({tool_counts[name]})")
    if lines:
        await _send(channel, "\n".join(lines))

class Agent:
    def __init__(self):
        self.provider = build_provider(build_system_prompt())
        self.sub_provider = None   # created on first use (honours SUBAGENT_MODEL)

    async def _run_subagents(self, channel, tasks) -> dict:
        """Run 1-5 subagents concurrently; each gets a fresh history and a limited
        toolset (no messaging, no further spawning). Returns their final reports."""
        if isinstance(tasks, str):
            try:
                tasks = json.loads(tasks)
            except Exception:
                tasks = []
        tasks = [t for t in (tasks or []) if isinstance(t, dict) and str(t.get("task") or "").strip()]
        tasks = tasks[:SUB_MAX_TASKS]
        if not tasks:
            return {"error": "no tasks given — pass tasks=[{\"name\": ..., \"task\": ...}, ...]"}

        if self.sub_provider is None:
            self.sub_provider = build_provider(
                SUBAGENT_PROMPT, model=os.environ.get("SUBAGENT_MODEL") or None
            )

        names = [str(t.get("name") or f"agent-{i + 1}") for i, t in enumerate(tasks)]
        await _send(channel, f"🧩 Spawning {len(tasks)} subagent(s): " + ", ".join(names))

        SUB_MAX_TURNS    = _env_int("SUBAGENT_MAX_TURNS", 12)
        SUB_MAX_PARALLEL = _env_int("SUBAGENT_MAX_PARALLEL", 3)
        SUB_TIMEOUT      = _env_int("SUBAGENT_TIMEOUT", 600)

        schemas = [s for s in tools.LOCAL_TOOL_SCHEMAS if s["name"] in SUB_TOOL_NAMES] + tools.WEB_TOOL_SCHEMAS
        approval_lock = asyncio.Lock()      # one approval prompt at a time (Stoat replies are plain yes/no)
        sem = asyncio.Semaphore(SUB_MAX_PARALLEL)

        async def run_one(name: str, task: str) -> dict:
            async def shell(command: str):
                if tools.is_destructive(command):
                    async with approval_lock:
                        ok = await tools.request_approval(channel, f"[subagent: {name}]\n{command}")
                    if not ok:
                        return {"error": "command was denied by user", "exit_code": -1}
                return await asyncio.to_thread(tools.run_shell, command)

            sync_fns = {
                "run_shell":  lambda **kw: shell(kw["command"]),
                "run_python": lambda **kw: tools.run_python(kw["code"]),
                "read_file":  lambda **kw: tools.read_file(kw["path"]),
                "write_file": lambda **kw: tools.write_file(kw["path"], kw["content"]),
                "list_dir":   lambda **kw: tools.list_dir(kw.get("path", ".")),
            }
            async_fns = {
                "web_search": lambda **kw: tools.web_search(kw["query"], kw.get("max_results", 5)),
            }

            history = [{"role": "user", "content": task}]
            last_text = ""
            for turn in range(SUB_MAX_TURNS):
                msg = await asyncio.to_thread(self.sub_provider.step, history, schemas)
                for note in drain_key_notices():
                    await _send(channel, note)
                history.append(msg)
                last_text = msg.get("content") or last_text
                if not msg["tool_calls"]:
                    return {"name": name, "status": "ok", "turns": turn + 1,
                            "report": (msg.get("content") or "(empty report)")[:6000]}
                for tc in msg["tool_calls"]:
                    tname, targs = tc["name"], tc["input"]
                    try:
                        if tname in sync_fns:
                            result = await asyncio.to_thread(sync_fns[tname], **targs)
                            if hasattr(result, "__await__"):
                                result = await result
                        elif tname in async_fns:
                            result = await async_fns[tname](**targs)
                        else:
                            result = {"error": f"unknown tool: {tname}"}
                    except Exception as e:
                        result = {"error": str(e)}
                    history.append({"role": "tool", "tool_call_id": tc["id"], "name": tname,
                                    "content": _fmt(result)})
            return {"name": name, "status": "incomplete", "turns": SUB_MAX_TURNS,
                    "report": (last_text or "hit the turn limit before finishing")[:6000]}

        async def guarded(name: str, task: str) -> dict:
            async with sem:
                try:
                    return await asyncio.wait_for(run_one(name, task), SUB_TIMEOUT)
                except asyncio.TimeoutError:
                    return {"name": name, "status": "timeout", "report": f"timed out after {SUB_TIMEOUT}s"}
                except Exception as e:
                    return {"name": name, "status": "error", "report": f"{type(e).__name__}: {e}"}

        results = await asyncio.gather(*(guarded(n, str(t["task"])) for n, t in zip(names, tasks)))
        return {"results": results}

    async def run_turn(self, channel, user_text: str, history: list, author: dict | None = None) -> list:
        """`author` = {"id": ..., "name": ...} as reported by the platform itself
        (never parsed from message text). Integrations use it for permission checks."""
        history.append({"role": "user", "content": user_text})

        # Integrations (integrations/*.py): extra tools + prompt text for THIS turn.
        from chat_platform import BaseChannel
        author = author or {}
        ictx = integrations.Context(
            channel=channel,
            platform=channel.platform if isinstance(channel, BaseChannel) else "discord",  # raw channel = legacy Discord path
            author_id=str(author["id"]) if author.get("id") is not None else None,
            author_name=author.get("name"),
            confirm=lambda text: _request_approval(channel, text, False, 60),
            send=lambda text: _send(channel, text),
        )

        # Re-inject memory (MEMORY.md + last day or two of daily logs) fresh
        # on every turn, since it can change between turns (another channel's
        # turn may have written to it, or the `remember` tool may fire below).
        mem_context = memory.load_context()
        base_schemas = (
            tools.LOCAL_TOOL_SCHEMAS
            + tools.DISCORD_TOOL_SCHEMAS
            + tools.WEB_TOOL_SCHEMAS
            + memory.TOOL_SCHEMAS
            + sessions.TOOL_SCHEMAS
            + (SUBAGENT_TOOL_SCHEMAS if _subagents_enabled() else [])
        )
        toolset = integrations.build(ictx, reserved={t["name"] for t in base_schemas})
        self.provider.system_prompt = "\n\n".join(
            part for part in (build_system_prompt(self.provider.model), toolset.prompt, mem_context) if part
        )

        async def exec_shell(command: str) -> dict:
            commands_used.append(command)
            if tools.is_destructive(command):
                approved = await tools.request_approval(channel, command)
                if not approved:
                    return {"error": "command was denied by user", "exit_code": -1}
            return await asyncio.to_thread(tools.run_shell, command)

        async def exec_shell_elevated(command: str) -> dict:
            if not tools.elevated_enabled():
                return {"error": "elevated commands are turned off on this machine", "exit_code": -1}
            # Elevated always requires approval — no exceptions
            approved = await _request_approval(channel, f"[ELEVATED / sudo]\n{command}", True, 90)
            if not approved:
                return {"error": "elevated command was denied by user", "exit_code": -1}
            return await asyncio.to_thread(tools.run_shell_elevated, command)

        local_fns = {
            "run_shell":          lambda **kw: exec_shell(kw["command"]),
            **({"run_shell_elevated": lambda **kw: exec_shell_elevated(kw["command"])}
               if tools.elevated_enabled() else {}),
            "run_python":         lambda **kw: tools.run_python(kw["code"]),
            "read_file":          lambda **kw: tools.read_file(kw["path"]),
            "write_file":         lambda **kw: tools.write_file(kw["path"], kw["content"]),
            "list_dir":           lambda **kw: tools.list_dir(kw.get("path", ".")),
            "remember":           lambda **kw: memory.remember(kw["fact"]),
        }
        async_fns = {
            "fetch_channel_messages": lambda **kw: _fetch_history(channel, kw.get("limit", 20)),
            "send_message": lambda **kw: _send(channel, kw["content"]),
            "send_file": lambda **kw: _send_file(channel, kw["path"], kw.get("caption", "")),
            "web_search": lambda **kw: tools.web_search(
                kw["query"], kw.get("max_results", 5)
            ),
        }
        current_session = sessions.key_for(channel)
        async_fns["reset_session"] = lambda **kw: _reset_session(
            channel, current_session, kw.get("session")
        )
        if _subagents_enabled():
            async_fns["spawn_subagent"] = lambda **kw: self._run_subagents(
                channel, [{"name": kw.get("name"), "task": kw.get("task")}]
            )
            async_fns["spawn_subagents"] = lambda **kw: self._run_subagents(channel, kw.get("tasks"))

        all_schemas = base_schemas + toolset.schemas

        commands_used: list[str] = []
        tool_counts: dict[str, int] = {}
        tool_order: list[str] = []

        final_text = None
        for _ in range(MAX_TURNS):
            # Network call to the model: run in a thread so the bot's event loop
            # (Discord heartbeat, Stoat websocket) keeps breathing while we wait.
            assistant_msg = await asyncio.to_thread(self.provider.step, history, all_schemas)
            for note in drain_key_notices():          # e.g. "API key #1 hit its usage limit — switching…"
                await _send(channel, note)
            history.append(assistant_msg)

            if not assistant_msg["tool_calls"]:
                if assistant_msg["content"]:
                    final_text = assistant_msg["content"]
                    await _send_usage_summary(channel, commands_used, tool_counts, tool_order)
                    await _send(channel, assistant_msg["content"])
                break

            for tc in assistant_msg["tool_calls"]:
                name, args, call_id = tc["name"], tc["input"], tc["id"]
                if name not in tool_counts:
                    tool_counts[name] = 0
                    tool_order.append(name)
                tool_counts[name] += 1
                try:
                    if name in local_fns:
                        # Sync tools (subprocess, file I/O) run in a thread too
                        result = await asyncio.to_thread(local_fns[name], **args)
                        # local_fns may return coroutines (exec_shell does)
                        if hasattr(result, "__await__"):
                            result = await result
                    elif name in async_fns:
                        result = await async_fns[name](**args)
                    elif toolset.owns(name):
                        result = await toolset.call(name, args)
                    else:
                        result = {"error": f"unknown tool: {name}"}
                except Exception as e:
                    result = {"error": str(e)}

                history.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": _fmt(result),
                })
        else:
            await _send(channel, "_(hit the max tool-call limit for this turn — stopping)_")

        channel_label = getattr(channel, "name", None) or str(getattr(channel, "id", "unknown"))
        memory.log_turn(channel_label, user_text, final_text)

        return history
