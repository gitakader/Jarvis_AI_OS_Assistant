"""
core/mcp_config.py — validated loader for config/mcp_servers.json.

The MCP bridge connects JARVIS to external Model Context Protocol servers.
Each entry is either a stdio server (a local process JARVIS spawns) or an
HTTP server (streamable HTTP URL). Everything here is validation only —
no connections, no imports of the mcp package, so the loader works (and
reports) even before the SDK is installed.

Schema:

    {
      "servers": [
        {"name": "echo",
         "command": "venv\\Scripts\\python.exe",
         "args": ["mcp_servers/echo_server.py"],
         "env": {},                    # optional, str -> str
         "cwd": null},                 # optional; null/absent = project root
        {"name": "remote_thing",
         "url": "http://127.0.0.1:9100/mcp",
         "headers": {}}                # optional
      ]
    }

Rules enforced here:
  * name required, unique, ^[a-z][a-z0-9_]{0,31}$ — the Gemini function name
    is mcp_<server>_<tool>, so the server name must stay within identifier
    bounds and snake_case keeps prefixes predictable.
  * exactly one transport per entry: command (stdio) or url (http/https).
  * args: list[str], env/headers: dict[str,str], cwd: str or null.
  * unknown keys are rejected — a typo like "commad" must not silently
    drop the server's command.

The file is trusted the same way plugins are: it can run whatever command
it names, so it is local-only by convention. Missing file = no servers,
not an error. load_servers() never raises; problems come back as strings.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = _PROJECT_ROOT / "config" / "mcp_servers.json"

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

_ALLOWED_KEYS = {"name", "command", "args", "env", "cwd", "url", "headers"}


@dataclass
class ServerSpec:
    name: str
    kind: str                      # "stdio" | "http"
    command: str = ""              # stdio
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str = ""                  # http
    headers: dict[str, str] = field(default_factory=dict)


def load_servers() -> tuple[list[ServerSpec], list[str]]:
    """(valid servers, error strings). Never raises."""
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        return [], []              # not configured — fine
    try:
        data = json.loads(raw)
    except ValueError as e:
        return [], [f"{CONFIG_PATH.name} is not valid JSON: {e}"]

    if not isinstance(data, dict) or "servers" not in data:
        return [], [f"{CONFIG_PATH.name} must be an object with a 'servers' list"]
    entries = data["servers"]
    if not isinstance(entries, list):
        return [], [f"'servers' must be a list, got {type(entries).__name__}"]

    specs: list[ServerSpec] = []
    errors: list[str] = []
    seen: set[str] = set()

    for i, entry in enumerate(entries):
        where = f"servers[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be an object, got {type(entry).__name__}")
            continue

        unknown = set(entry) - _ALLOWED_KEYS
        if unknown:
            errors.append(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
            continue

        name = entry.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            errors.append(
                f"{where}: 'name' must match ^[a-z][a-z0-9_]{{0,31}}$ "
                f"(got {name!r}) — it prefixes every tool as mcp_<name>_<tool>")
            continue
        if name in seen:
            errors.append(f"{where}: duplicate server name {name!r}")
            continue
        seen.add(name)

        command, url = entry.get("command"), entry.get("url")
        if (command is not None) == (url is not None):
            errors.append(f"{where} ({name!r}): needs exactly one of "
                          f"'command' (stdio) or 'url' (http)")
            continue

        if command is not None:
            if not isinstance(command, str) or not command.strip():
                errors.append(f"{where} ({name!r}): 'command' must be a non-empty string")
                continue
            args = entry.get("args", [])
            env = entry.get("env", {})
            cwd = entry.get("cwd")
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                errors.append(f"{where} ({name!r}): 'args' must be a list of strings")
                continue
            if not isinstance(env, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
                errors.append(f"{where} ({name!r}): 'env' must be an object of strings")
                continue
            if cwd is not None and not isinstance(cwd, str):
                errors.append(f"{where} ({name!r}): 'cwd' must be a string or null")
                continue
            specs.append(ServerSpec(name=name, kind="stdio", command=command,
                                    args=args, env=env, cwd=cwd))
        else:
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                errors.append(f"{where} ({name!r}): 'url' must start with http:// or https://")
                continue
            headers = entry.get("headers", {})
            if not isinstance(headers, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
                errors.append(f"{where} ({name!r}): 'headers' must be an object of strings")
                continue
            specs.append(ServerSpec(name=name, kind="http", url=url, headers=headers))

    return specs, errors
