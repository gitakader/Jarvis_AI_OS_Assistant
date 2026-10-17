"""
mcp_servers/echo_server.py — bundled example MCP server.

Exists so the MCP bridge can be proven end-to-end without any external
dependency: mcp_tests/run_tests.py drives it headlessly, and once it is
listed in config/mcp_servers.json the live session can call its tools.

Tools (and why each one exists):

  echo          — plain round-trip call
  add           — two required ints; the classic "prove the schema works" call
  slow          — sleeps 10 s, so the bridge's call timeout can be exercised
  foo.bar       ┐ deliberately colliding names: after the bridge sanitises
  foo_bar       ┘ them to Gemini function names both become foo_bar, and the
                  bridge must keep them distinct (mcp_echo_foo_bar / _2) while
                  still calling the right original name
  make_widget   — a nested pydantic model + Optional param: the raw JSON
                  schema uses $ref and anyOf, which the bridge's converter
                  degrades to STRING instead of failing the whole server

Keep this file dependency-free beyond mcp + pydantic (installed in venv)
so it doubles as the template for user-added servers.
"""
import time
from typing import Optional

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

mcp = MCPServer("echo")


class Widget(BaseModel):
    name: str
    qty: int


@mcp.tool()
def echo(text: str) -> str:
    return f"echo: {text}"


@mcp.tool()
def add(a: int, b: int) -> int:
    return a + b


@mcp.tool()
def slow() -> str:
    time.sleep(10)
    return "too late"


@mcp.tool(name="foo.bar")
def foo_dot_bar() -> str:
    return "dot"


@mcp.tool(name="foo_bar")
def foo_underscore_bar() -> str:
    return "underscore"


@mcp.tool()
def make_widget(widget: Widget, note: Optional[str] = None) -> str:
    return f"widget {widget.name} x{widget.qty} note={note}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
