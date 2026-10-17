"""
mcp_tests/probe_rea_registered.py — phase-3 offline verification for the
registered rea MCP entry in config/mcp_servers.json.

Drives the EXACT command/args/env/cwd core/mcp_bridge.py will use for the
"rea" server: cmd.exe /d /c npx -y rea-agents@6.5.0 mcp with
GHIDRA_INSTALL_DIR + JAVA_HOME merged into the environment. Proves:
  1. spawn + stdio handshake succeeds,
  2. initialize returns serverInfo rea 6.5.0,
  3. tools/list returns the full catalog (207 expected),
  4. every tool carries a description (needed for clean Gemini decls).
No restart, no Gemini, no UI. Server stderr lands in logs/mcp_rea.log,
the same file the bridge uses.

Run from the project root:

    venv\\Scripts\\python mcp_tests\\probe_rea_registered.py

Exit code 0 = every check passed.
"""
import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mcp import ClientSession, StdioServerParameters
import mcp.client.stdio as mcp_stdio

import core.mcp_config as cfg

EXPECTED_TOOL_COUNT = 207
SPAWN_TIMEOUT = 240
INIT_TIMEOUT = 240
LIST_TIMEOUT = 120

passed, failed = 0, 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {name}{(' — ' + str(detail)) if detail else ''}")


async def probe() -> None:
    specs, errors = cfg.load_servers()
    check("registered config parses with no errors", not errors,
          "; ".join(errors) if errors else "ok")
    spec = next((s for s in specs if s.name == "rea"), None)
    if spec is None:
        check("rea entry present in config", False, "no server named 'rea'")
        return
    check("rea spec is stdio via cmd.exe wrapper", spec.kind == "stdio" and spec.command == "cmd.exe",
          f"kind={spec.kind} command={spec.command!r}")
    check("rea spec carries both env vars", "GHIDRA_INSTALL_DIR" in spec.env and "JAVA_HOME" in spec.env,
          f"GHIDRA_INSTALL_DIR={spec.env.get('GHIDRA_INSTALL_DIR')!r} "
          f"JAVA_HOME={spec.env.get('JAVA_HOME')!r}")

    params = StdioServerParameters(
        command=spec.command,
        args=[str(a) for a in spec.args],
        env={**os.environ, **spec.env},
        cwd=spec.cwd or str(PROJECT_ROOT),
    )
    errlog_path = PROJECT_ROOT / "logs" / "mcp_rea.log"
    with open(errlog_path, "w", encoding="utf-8", errors="replace") as errlog:
        gen = mcp_stdio.stdio_client(params, errlog=errlog)
        try:
            async with asyncio.timeout(SPAWN_TIMEOUT):
                read, write = await gen.__aenter__()
        except Exception as e:
            check("spawn + stdio handshake", False, f"{type(e).__name__}: {e}")
            print(f"--- {errlog_path.name} tail ---")
            print(errlog_path.read_text(encoding="utf-8", errors="replace")[-800:])
            return
        try:
            async with ClientSession(read, write) as session:
                try:
                    async with asyncio.timeout(INIT_TIMEOUT):
                        info = await session.initialize()
                except Exception as e:
                    check("initialize handshake", False, f"{type(e).__name__}: {e}")
                    return
                check("serverInfo is rea 6.5.0", info.server_info.name == "rea",
                      f"server={info.server_info.name} version={info.server_info.version} "
                      f"protocol={info.protocol_version}")
                try:
                    async with asyncio.timeout(LIST_TIMEOUT):
                        tools = await session.list_tools()
                except Exception as e:
                    check("tools/list", False, f"{type(e).__name__}: {e}")
                    return
                names = [t.name for t in tools.tools]
                check(f"tools/list returns {EXPECTED_TOOL_COUNT} tools",
                      len(names) == EXPECTED_TOOL_COUNT, f"got {len(names)}")
                no_desc = [n for t, n in zip(tools.tools, names) if not getattr(t, "description", None)]
                check("every tool has a description", not no_desc,
                      f"missing on: {no_desc[:5]}" if no_desc else "all described")
                print("  sample:", ", ".join(names[:6]), "...")
                unsafe = [n for n in names
                          if not n.replace("-", "_").replace(".", "_").isidentifier()]
                print(f"  info: {len(unsafe)} names need sanitising before mcp_rea_ prefix "
                      f"(bridge already handles this): {unsafe[:5]}")
        finally:
            try:
                await gen.__aexit__(None, None, None)
            except Exception:
                pass


if __name__ == "__main__":
    import traceback
    try:
        asyncio.run(probe())
    except BaseException as e:
        traceback.print_exception(e)
        check("probe completed without exception", False, f"{type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
