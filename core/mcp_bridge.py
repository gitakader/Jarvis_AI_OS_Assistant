"""
core/mcp_bridge.py — JARVIS as an MCP client.

Connects to external Model Context Protocol servers (stdio processes and
streamable-HTTP endpoints, configured in config/mcp_servers.json) and exposes
their tools to the live session as Gemini function declarations named
mcp_<server>_<tool>.

Design:
  * One connection per server, held open for the app lifetime. The mcp SDK
    exposes both transports as async context managers, so a connection is the
    live generator plus a ClientSession over the yielded streams.
  * All connection lifecycle runs on one dedicated supervisor task. The SDK is
    anyio-based, and anyio cancel scopes must be entered and exited in the same
    task — resuming the transport generator from a second task raises
    "Attempted to exit cancel scope in a different task". The supervisor keeps
    every __aenter__/__aexit__ in one task; public methods submit work to it.
  * Timeouts: spawn uses asyncio.timeout; initialize/list_tools/call_tool use
    anyio.fail_after (4.15 sync form) as short-lived scopes fully inside one
    await. Any anyio scope held across the transport generator's yield breaks
    LIFO scope ordering ("isn't the current task's current cancel scope") and
    corrupts the scope chain for every later connection — asyncio.wait_for
    fails the same way (it runs the coroutine in a throwaway task), so neither
    may wrap __aenter__/__aexit__.
  * Never fatal. A server that fails to connect at boot is logged and its
    tools simply don't appear; a call to it later tries a lazy reconnect.
    A call failure returns an error string to the model — it never raises
    into the session loop.
  * Tool names are sanitised to Gemini's function-name grammar
    (^[a-zA-Z_][a-zA-Z0-9_]{0,63}$) and deduplicated; the original MCP name
    is kept for the actual call.
  * MCP's JSON Schema inputSchema is converted to the small subset the
    Gemini API accepts (STRING/NUMBER/INTEGER/BOOLEAN/ARRAY/OBJECT,
    properties, required, enum, description). Anything exotic degrades to
    STRING rather than failing the whole server.
  * Each stdio server's stderr goes to logs/mcp_<server>.log — a crashed
    or chatty server is diagnosable without polluting jarvis.log.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import anyio

from core import mcp_config

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOGS = _PROJECT_ROOT / "logs"

_CALL_TIMEOUT = 60.0          # seconds per tool call
_CONNECT_TIMEOUT = 30.0       # seconds per server connect/initialize
                              # (npx-wrapped servers need a cold-start budget)
_TEARDOWN_TIMEOUT = 5.0       # seconds per teardown step
_MAX_TEXT = 4000              # result text cap (matches tool_result broadcast)
_MAX_DESC = 500
_MAX_SCHEMA_DEPTH = 4

_TOOL_RE = re.compile(r"[^A-Za-z0-9_]+")
_FULL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_DEVNULL = open(os.devnull, "w")   # module-lifetime fallback errlog


@dataclass
class McpTool:
    server: str
    raw_name: str
    jname: str
    description: str
    parameters: dict


@dataclass
class _Conn:
    spec: mcp_config.ServerSpec
    session: object = None
    _gen: object = None            # the live transport generator
    _http_client: object = None    # httpx client when headers were supplied
    _errlog: object = None         # stdio server stderr capture
    ok: bool = False


class McpBridge:
    """All MCP servers for one JARVIS process. Async — runs on the session loop."""

    def __init__(self, logger=None, call_timeout: float = _CALL_TIMEOUT,
                 connect_timeout: float = _CONNECT_TIMEOUT):
        self._log = logger or (lambda msg: None)
        self.call_timeout = call_timeout
        self.connect_timeout = connect_timeout
        self._conns: dict[str, _Conn] = {}
        self._by_jname: dict[str, McpTool] = {}
        self._config_errors: list[str] = []
        self._queue: asyncio.Queue | None = None
        self._sup_task: asyncio.Task | None = None

    # ── startup ────────────────────────────────────────────────────────────

    async def connect_all(self) -> list[str]:
        """Connect every configured server. Returns boot-transcript lines.
        Never raises — per-server failures are reported, not propagated."""
        return await self._submit("connect_all")

    def declarations(self) -> list[dict]:
        """Gemini function declarations for all currently-known MCP tools."""
        return [
            {"name": t.jname, "description": t.description,
             "parameters": t.parameters}
            for t in self._by_jname.values()
        ]

    def status(self) -> list[str]:
        """One line per server for /api/health-style reporting."""
        out = []
        for name, conn in self._conns.items():
            n = sum(1 for t in self._by_jname.values() if t.server == name)
            out.append(f"{name}: {'up' if conn.ok else 'down'} ({n} tools)")
        return out

    # ── tool calls ─────────────────────────────────────────────────────────

    async def call(self, jname: str, arguments: dict | None) -> str:
        return await self._submit("call", jname, arguments)

    async def close(self) -> None:
        if self._sup_task is not None and not self._sup_task.done():
            await self._submit("close")

    # ── supervisor plumbing ────────────────────────────────────────────────

    def _ensure_supervisor(self) -> None:
        if self._sup_task is not None and self._sup_task.done():
            # A dead supervisor means its anyio task world is gone; the
            # connections it owned are unusable. Drop them — a later
            # connect_all rebuilds.
            self._conns.clear()
            self._by_jname.clear()
        if self._sup_task is None or self._sup_task.done():
            self._queue = asyncio.Queue()
            self._sup_task = asyncio.create_task(self._supervisor())

    async def _submit(self, cmd: str, *args):
        self._ensure_supervisor()
        fut = asyncio.get_running_loop().create_future()
        await self._queue.put((cmd, args, fut))
        return await fut

    async def _supervisor(self) -> None:
        try:
            while True:
                cmd, args, fut = await self._queue.get()
                if cmd == "close":
                    try:
                        await self._do_close()
                        fut.set_result(None)
                    except BaseException as e:
                        if not fut.done():
                            fut.set_exception(e)
                    break
                try:
                    if cmd == "connect_all":
                        fut.set_result(await self._do_connect_all())
                    elif cmd == "call":
                        fut.set_result(await self._do_call(*args))
                    else:
                        fut.set_result(None)
                except BaseException as e:
                    if not fut.done():
                        fut.set_exception(e)
        finally:
            try:
                await self._do_close()
            except BaseException:
                pass

    # ── supervisor-side implementations ────────────────────────────────────

    async def _do_connect_all(self) -> list[str]:
        lines: list[str] = []
        specs, self._config_errors = mcp_config.load_servers()
        for err in self._config_errors:
            self._log(f"[MCP] config: {err}")
            lines.append(f"[MCP] config error: {err}")
        if not specs:
            return lines
        for spec in specs:
            old = self._conns.get(spec.name)
            if old is not None and (old._gen is not None or old.session is not None):
                await self._teardown_conn(old)
            self._conns[spec.name] = _Conn(spec=spec)
            ok, err = await self._connect(spec)
            if ok:
                n = sum(1 for t in self._by_jname.values() if t.server == spec.name)
                msg = f"[MCP] {spec.name}: connected ({n} tool{'s' if n != 1 else ''})"
                lines.append(msg)
            else:
                msg = f"[MCP] {spec.name}: skipped — {err}"
                lines.append(msg)
            self._log(msg)
        return lines

    async def _do_call(self, jname: str, arguments: dict | None) -> str:
        tool = self._by_jname.get(jname)
        if tool is None:
            return f"MCP tool '{jname}' is not available."
        conn = self._conns.get(tool.server)
        if conn is None:
            conn = _Conn(spec=mcp_config.ServerSpec(
                name=tool.server, kind="stdio", command=""))
            self._conns[tool.server] = conn
        if not conn.ok:
            ok, err = await self._connect(conn.spec)
            if not ok:
                return f"MCP server '{tool.server}' is unreachable: {err}"
        try:
            with anyio.fail_after(self.call_timeout + 5):
                res = await conn.session.call_tool(
                    tool.raw_name, arguments or {},
                    read_timeout_seconds=self.call_timeout)
            return self._result_text(tool, res)
        except TimeoutError:
            conn.ok = False
            return self._timeout_msg(tool)
        except Exception as e:
            conn.ok = False
            if "timed out" in str(e).lower():
                # The SDK raises MCPError("Request 'tools/call' timed out")
                # on its own read_timeout — report it as a timeout, not a crash.
                return self._timeout_msg(tool)
            return f"MCP tool '{tool.raw_name}' on '{tool.server}' failed: {e}"

    def _timeout_msg(self, tool: McpTool) -> str:
        return (f"MCP tool '{tool.raw_name}' on '{tool.server}' timed out "
                f"after {self.call_timeout:.0f}s.")

    async def _connect(self, spec: mcp_config.ServerSpec) -> tuple[bool, str]:
        conn = self._conns[spec.name]
        if conn._gen is not None or conn.session is not None:
            # Reconnect after a dropped connection: close the old transport
            # first so the server process doesn't leak.
            await self._teardown_conn(conn)
        try:
            if spec.kind == "stdio":
                import mcp.client.stdio as _mcp_stdio
                from mcp import StdioServerParameters
                env = {**os.environ, **spec.env}
                params = StdioServerParameters(
                    command=spec.command,
                    args=[str(a) for a in spec.args],
                    env=env,
                    cwd=spec.cwd or str(_PROJECT_ROOT),
                )
                try:
                    _LOGS.mkdir(parents=True, exist_ok=True)
                    conn._errlog = open(_LOGS / f"mcp_{spec.name}.log", "w",
                                        encoding="utf-8", errors="replace")
                except OSError:
                    conn._errlog = None
                gen = _mcp_stdio.stdio_client(params, errlog=conn._errlog or _DEVNULL)
                conn._gen = gen
                # Spawn timeout via asyncio.timeout, not anyio.fail_after: the
                # generator suspends at yield with its internal task-group
                # cancel scope still open, and any anyio scope held across the
                # yield breaks LIFO scope ordering on exit ("isn't the current
                # task's current cancel scope") — corrupting the scope chain
                # for every later connection in the task.
                async with asyncio.timeout(self.connect_timeout):
                    read, write = await gen.__aenter__()
            else:
                from mcp.client.streamable_http import streamable_http_client
                hc = None
                if spec.headers:
                    import httpx
                    hc = httpx.AsyncClient(headers=dict(spec.headers))
                    conn._http_client = hc
                gen = streamable_http_client(spec.url, http_client=hc,
                                             terminate_on_close=False)
                conn._gen = gen
                async with asyncio.timeout(self.connect_timeout):
                    read, write = await gen.__aenter__()

            from mcp import ClientSession
            session = ClientSession(read, write)
            # ClientSession's __aenter__ starts the dispatcher run loop —
            # using the session without entering it fails every request with
            # "JSONRPCDispatcher.send_raw_request called before run()".
            if hasattr(session, "__aenter__"):
                await session.__aenter__()
            conn.session = session   # teardown must close it even if init fails
            with anyio.fail_after(self.connect_timeout):
                await session.initialize()
            with anyio.fail_after(self.connect_timeout):
                listing = await session.list_tools()
            self._register_tools(spec.name, listing.tools)
            conn.ok = True
            return True, ""
        except BaseException as e:
            conn.ok = False
            await self._teardown_conn(conn)
            if isinstance(e, Exception):
                return False, f"{type(e).__name__}: {e}"
            raise

    def _register_tools(self, server: str, tools) -> None:
        for t in tools:
            raw = getattr(t, "name", "")
            if not isinstance(raw, str) or not raw:
                continue
            jname = self._sanitize_name(server, raw)
            if jname is None:
                self._log(f"[MCP] {server}: tool {raw!r} skipped "
                          f"(name unusable after sanitising)")
                continue
            desc = (getattr(t, "description", "") or f"MCP tool {raw} "
                    f"from server {server}")
            params = _to_gemini_params(getattr(t, "input_schema", None) or {})
            self._by_jname[jname] = McpTool(
                server=server, raw_name=raw, jname=jname,
                description=f"[MCP:{server}] {desc}"[:_MAX_DESC + 11],
                parameters=params)

    def _sanitize_name(self, server: str, raw: str) -> str | None:
        clean = _TOOL_RE.sub("_", raw).strip("_")
        clean = re.sub(r"_+", "_", clean)
        if not clean:
            return None
        if clean[0].isdigit():
            clean = "t" + clean
        base = f"mcp_{server}_{clean}"
        max_tool_part = 63 - len(f"mcp_{server}_")
        if max_tool_part < 1:
            return None
        base = base[:63]
        jname, i = base, 2
        while jname in self._by_jname:
            suffix = f"_{i}"
            jname = base[: 63 - len(suffix)] + suffix
            i += 1
        return jname if _FULL_NAME_RE.match(jname) else None

    def _result_text(self, tool: McpTool, res) -> str:
        parts: list[str] = []
        try:
            for block in getattr(res, "content", None) or []:
                btype = getattr(block, "type", "")
                text = getattr(block, "text", None)
                if btype == "text" and isinstance(text, str):
                    parts.append(text)
                else:
                    parts.append(f"[{btype or 'content'} content]")
        except Exception:
            pass
        if not parts:
            try:
                sc = getattr(res, "structured_content", None)
                if sc:
                    parts.append(json.dumps(sc, ensure_ascii=False)[:_MAX_TEXT])
            except Exception:
                pass
        text = "\n".join(p.strip() for p in parts if p and p.strip())[:_MAX_TEXT]
        if getattr(res, "is_error", False):
            return f"MCP tool '{tool.raw_name}' failed: {text or 'unknown error'}"
        return text or "Done."

    async def _do_close(self) -> None:
        conns = list(self._conns.values())
        self._conns.clear()
        self._by_jname.clear()
        for conn in conns:
            await self._teardown_conn(conn)

    async def _teardown_conn(self, conn: _Conn) -> None:
        """Close one connection, never raising and never hanging close().

        Resuming the transport generator must happen in the supervisor task
        (the one that entered it) — which this always runs in. The session and
        generator exits must NOT be wrapped in anyio timeouts: they exit their
        own cancel scopes, and a scope entered above those would break LIFO
        ordering ("isn't the current task's current cancel scope"). Their
        internal shutdowns are bounded by the SDK itself.
        """

        async def _quiet(coro) -> None:
            try:
                with anyio.fail_after(_TEARDOWN_TIMEOUT):
                    await coro
            except BaseException:
                pass

        try:
            if conn.session is not None and hasattr(conn.session, "__aexit__"):
                await conn.session.__aexit__(None, None, None)
        except BaseException:
            pass
        try:
            if conn._gen is not None:
                await conn._gen.__aexit__(None, None, None)
        except BaseException:
            pass
        try:
            if conn._http_client is not None:
                await _quiet(conn._http_client.aclose())
        except BaseException:
            pass
        try:
            if conn._errlog is not None:
                conn._errlog.close()
        except BaseException:
            pass
        conn._gen = None
        conn._http_client = None
        conn._errlog = None
        conn.session = None


# ── JSON Schema → Gemini parameters ─────────────────────────────────────────

_SIMPLE_TYPES = {"string": "STRING", "number": "NUMBER",
                 "integer": "INTEGER", "boolean": "BOOLEAN"}


def _to_gemini_params(schema: dict) -> dict:
    out: dict = {"type": "OBJECT"}
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return out
    props: dict = {}
    for key, sub in (schema.get("properties") or {}).items():
        if isinstance(key, str):
            props[key] = _convert_prop(sub, 1)
    if props:
        out["properties"] = props
    req = schema.get("required")
    if isinstance(req, list):
        names = [r for r in req if isinstance(r, str) and r in props]
        if names:
            out["required"] = names
    return out


def _convert_prop(schema, depth: int) -> dict:
    if depth > _MAX_SCHEMA_DEPTH or not isinstance(schema, dict):
        return {"type": "STRING"}
    if "$ref" in schema:
        return {"type": "STRING"}          # refs unresolved by design
    if "anyOf" in schema or "oneOf" in schema:
        key = "anyOf" if "anyOf" in schema else "oneOf"
        for opt in schema[key] or []:
            if isinstance(opt, dict) and opt.get("type") != "null":
                return _convert_prop(opt, depth + 1)
        return {"type": "STRING"}

    stype = schema.get("type")
    if stype == "array":
        out = {"type": "ARRAY",
               "items": _convert_prop(schema.get("items") or {}, depth + 1)}
    elif stype == "object":
        out: dict = {"type": "OBJECT"}
        props: dict = {}
        for key, sub in (schema.get("properties") or {}).items():
            if isinstance(key, str):
                props[key] = _convert_prop(sub, depth + 1)
        if props:
            out["properties"] = props
    elif stype in _SIMPLE_TYPES:
        out = {"type": _SIMPLE_TYPES[stype]}
    else:
        out = {"type": "STRING"}

    desc = schema.get("description")
    if isinstance(desc, str) and desc:
        out["description"] = desc[:_MAX_DESC]
    enum = schema.get("enum")
    if isinstance(enum, list) and out.get("type") == "STRING":
        vals = [e for e in enum if isinstance(e, (str, int, float, bool))]
        if vals:
            out["enum"] = [str(v) for v in vals][:50]
    return out
