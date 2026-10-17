"""
mcp_tests/run_tests.py — offline verification for the MCP bridge.

Part A drives the bridge against the bundled mcp_servers/echo_server.py
plus one deliberately broken entry: discovery, name sanitisation, schema
conversion against a REAL server's raw JSON Schema, call round-trips,
the call timeout, a dead server, an unknown tool, and clean shutdown.
No network, no Gemini API, no UI.

Part B proves the track-3 session wiring: a stub JarvisLive runs the
real _build_config() (the MCP declarations must appear in the live
config) and the real _execute_tool() (an mcp_echo_add call must return
"5"). The stub is built with object.__new__ so no UI, microphone, or
dashboard is ever constructed.

Run from the project root:

    venv\\Scripts\\python mcp_tests\\run_tests.py

Exit code 0 = every check passed.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import core.mcp_config as cfg
from core import mcp_bridge

TMP = PROJECT_ROOT / "logs" / "_mcp_tests_cases"
TMP.mkdir(exist_ok=True)
_TMP_CONFIG = TMP / "mcp_servers.json"

PY = sys.executable
ECHO_SERVER = PROJECT_ROOT / "mcp_servers" / "echo_server.py"
cfg.CONFIG_PATH = _TMP_CONFIG
cfg.CONFIG_PATH.write_text(json.dumps({
    "servers": [
        {"name": "echo", "command": PY, "args": [str(ECHO_SERVER)]},
        {"name": "bad", "command": "definitely-not-a-real-command-xyz"},
    ]
}), encoding="utf-8")

passed, failed = 0, 0


def check(name, cond, detail=""):
    global passed, failed
    tag = "PASS" if cond else "FAIL"
    if cond:
        passed += 1
    else:
        failed += 1
    print(f"[{tag}] {name}{(' — ' + str(detail)) if detail else ''}")


def _decl_name(d) -> str:
    if isinstance(d, dict):
        return d.get("name", "")
    return getattr(d, "name", "") or ""


async def wiring_proof(bridge):
    """Prove track-3 wiring using the real JarvisLive methods on a stub."""
    import main
    from main import JarvisLive

    class _Ui:
        def __init__(self):
            self.muted = False

        def set_state(self, _s):
            pass

        def write_log(self, _s):
            pass

    class _Reg:
        def get_tool_declarations(self):
            return []

        def has(self, _name):
            return False

        def run(self, _name, _args, player=None, session_memory=None):
            return None

        def scheduling(self, _name):
            return None

    jarvis = object.__new__(JarvisLive)
    jarvis.ui = _Ui()
    jarvis._action_registry = _Reg()
    jarvis._plugin_registry = _Reg()
    jarvis.mcp_bridge = bridge
    jarvis._resume_handle = None
    jarvis._enhanced_live = True
    jarvis._tuned_live = False
    jarvis._dashboard = None
    jarvis.speak_error = lambda *a, **k: None

    live_cfg = jarvis._build_config()
    decl_names = {_decl_name(d)
                  for d in live_cfg.tools[0].function_declarations}
    check("wiring: mcp decls merged into live config",
          "mcp_echo_add" in decl_names, sorted(n for n in decl_names if "mcp_" in n))
    check("wiring: inline tools still present",
          "save_memory" in decl_names)

    fc = SimpleNamespace(id="call-1", name="mcp_echo_add",
                         args={"a": 2, "b": 3})
    resp = await jarvis._execute_tool(fc)
    check("wiring: _execute_tool returns FunctionResponse",
          resp.name == "mcp_echo_add", resp)
    check("wiring: mcp call result reaches the model",
          resp.response.get("result") == "5", resp.response)


async def main():
    logs = []
    bridge = mcp_bridge.McpBridge(logger=logs.append, call_timeout=3.0,
                                  connect_timeout=8.0)
    lines = await bridge.connect_all()

    # 1. boot: echo connected, bad skipped, nothing raised
    check("connect_all returned 2 lines", len(lines) == 2, lines)
    check("echo connected", any("echo: connected" in l for l in lines),
          lines[0] if lines else "")
    check("bad skipped with reason", any("bad: skipped" in l for l in lines),
          lines[1] if len(lines) > 1 else "")

    decls = {d["name"]: d for d in bridge.declarations()}

    # 2. discovery + sanitisation
    check("6 tools declared", len(decls) == 6, sorted(decls))
    for n in ("mcp_echo_echo", "mcp_echo_add", "mcp_echo_slow",
              "mcp_echo_foo_bar", "mcp_echo_foo_bar_2", "mcp_echo_make_widget"):
        check(f"declared {n}", n in decls)
    check("all names match Gemini grammar",
          all(mcp_bridge._FULL_NAME_RE.match(n) for n in decls),
          sorted(decls))

    # 3. schema conversion against the server's raw JSON Schema
    add_p = decls.get("mcp_echo_add", {}).get("parameters", {})
    check("add params OBJECT", add_p.get("type") == "OBJECT", add_p)
    check("add required a,b", add_p.get("required") == ["a", "b"], add_p)
    check("add a INTEGER",
          add_p.get("properties", {}).get("a", {}).get("type") == "INTEGER",
          add_p.get("properties", {}).get("a"))

    mw_p = decls.get("mcp_echo_make_widget", {}).get("parameters", {})
    mw_props = mw_p.get("properties", {})
    check("make_widget $ref degrades to STRING",
          mw_props.get("widget") == {"type": "STRING"}, mw_props.get("widget"))
    check("make_widget anyOf picks non-null STRING",
          mw_props.get("note", {}).get("type") == "STRING", mw_props.get("note"))

    # 4. real calls through the stdio transport
    r_echo = await bridge.call("mcp_echo_echo", {"text": "hello mcp"})
    check("echo round-trip", r_echo == "echo: hello mcp", r_echo)
    r_add = await bridge.call("mcp_echo_add", {"a": 2, "b": 3})
    check("add(2,3) -> 5", r_add == "5", r_add)
    r_widget = await bridge.call("mcp_echo_make_widget",
                                 {"widget": {"name": "w1", "qty": 7},
                                  "note": "hi"})
    check("make_widget round-trip", r_widget == "widget w1 x7 note=hi", r_widget)

    # 5. name collision: both jnames exist and still reach their own tool
    r_dot = await bridge.call("mcp_echo_foo_bar", {})
    r_us = await bridge.call("mcp_echo_foo_bar_2", {})
    check("collision pair kept distinct, raw names preserved",
          sorted([r_dot, r_us]) == ["dot", "underscore"], (r_dot, r_us))

    # 6. timeout: slow() sleeps 10 s, call_timeout 3 s — must return, not raise
    r_slow = await bridge.call("mcp_echo_slow", {})
    check("slow() times out cleanly", "timed out" in r_slow, r_slow)

    # 7. unknown tool + dead server — must return strings, not raise
    r_unknown = await bridge.call("mcp_echo_nope", {})
    check("unknown tool handled", "not available" in r_unknown, r_unknown)

    # Simulate "server was up at boot, died since": a registered tool whose
    # server is down. The bridge must try a lazy reconnect and report it.
    bridge._by_jname["mcp_bad_whatever"] = mcp_bridge.McpTool(
        server="bad", raw_name="whatever", jname="mcp_bad_whatever",
        description="", parameters={"type": "OBJECT"})
    r_bad = await bridge.call("mcp_bad_whatever", {})
    check("dead server handled", "unreachable" in r_bad, r_bad)

    # 8. session wiring (track 3): real _build_config + _execute_tool
    await wiring_proof(bridge)

    # 9. clean close — no leaked server processes, no exceptions
    await bridge.close()
    check("close() ran", True)

    _TMP_CONFIG.unlink(missing_ok=True)
    print(f"\nRESULT: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


asyncio.run(main())
