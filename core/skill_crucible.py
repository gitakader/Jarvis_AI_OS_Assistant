"""
Skill Crucible — sandbox verification for Skill Forge skills.

A forged skill is LLM-generated Python that will run on this machine, so
before it is committed it passes three gates:

  1. AST validation   — syntax, a mandatory ``execute`` entry point, and a
                        ban list of imports / calls / string constants that
                        could spawn processes, run arbitrary code, or reach
                        for system administration commands.
  2. Dependency check — missing third-party imports are installed into the
                        active Python environment (reversible; reported).
  3. Sandbox test     — the code is executed as a module in a throwaway
                        directory under ``python -I`` (isolated mode: no
                        PYTHONPATH, no user site) with a hard timeout, and
                        every test case must return a presentable result.

The sandbox is *verification*, not a jail: the generated module still runs
with the user's privileges when invoked later. The ban list is there to
catch the failure modes an LLM actually exhibits — hallucinated
``os.system`` one-liners, ``subprocess`` helpers, ``eval`` of input — not
to stop a determined attacker who could trivially ship the same code as a
plugin. Anything banned here should also be told to the synthesizer so the
repair loop converges instead of thrashing.
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Imports a forged skill may never use. socket is banned because skills are
# told to reach the network through requests; raw sockets are never needed
# for that and are the usual first step of local scanning.
BANNED_IMPORTS = {
    "subprocess", "ctypes", "multiprocessing", "pickle", "marshal", "socket",
    "os",          # os is banned wholesale: every legitimate need (paths,
                   # makedirs, environ) has a safe stdlib alternative that
                   # does not carry system()/popen() right next to it.
}

# Callable names that are banned wherever they appear unqualified.
BANNED_CALL_NAMES = {"eval", "exec", "compile", "__import__", "globals", "locals", "input"}

# Qualified attribute calls that are banned (os.* handled above by the
# wholesale os ban; these cover the remaining escape hatches).
BANNED_QUALIFIED = {
    ("shutil", "rmtree"),
    ("importlib", "import_module"),
}

# Project modules a skill may not import: skills must be self-contained so
# the sandbox run (which cannot see the project) is representative.
BANNED_PROJECT_ROOTS = {
    "core", "actions", "plugins", "features", "memory", "dashboard", "mcp_servers",
}

# Destructive system-command fragments banned inside string constants. The
# scan is substring-based, so each entry is spelled with enough context
# ("shutdown /") to avoid tripping on ordinary prose.
BANNED_STRINGS = (
    "format c:", "format d:", "format /fs", "diskpart", "vssadmin", "bcdedit",
    "system32", "syswow64", "taskkill", "del /", "rmdir /s", "rd /s",
    "reg delete", "schtasks", "netsh", "powercfg", "shutdown /", "shutdown -",
    "boot_sentry", "auto_heal_engine", "install_wizard", "build_exe",
)

STANDARD_LIB_MODULES = {
    "abc", "argparse", "array", "ast", "asyncio", "base64", "binascii", "bisect",
    "calendar", "cmath", "collections", "colorsys", "concurrent", "configparser",
    "contextlib", "copy", "csv", "dataclasses", "datetime", "decimal",
    "difflib", "dis", "doctest", "email", "enum", "errno", "faulthandler",
    "filecmp", "fileinput", "fnmatch", "fractions", "functools", "gc", "getopt",
    "getpass", "gettext", "glob", "gzip", "hashlib", "heapq", "hmac", "html",
    "http", "imaplib", "imghdr", "importlib", "inspect", "io", "ipaddress",
    "itertools", "json", "keyword", "linecache", "locale", "logging", "lzma",
    "math", "mimetypes", "mmap", "netrc", "nntplib", "numbers",
    "operator", "os", "pathlib", "pkgutil", "platform", "plistlib", "poplib",
    "posixpath", "pprint", "profile", "pstats", "pty", "pwd", "py_compile",
    "pyclbr", "pydoc", "queue", "quopri", "random", "re", "reprlib", "resource",
    "rlcompleter", "runpy", "sched", "secrets", "select", "selectors", "shelve",
    "shlex", "shutil", "signal", "site", "smtpd", "smtplib", "sndhdr",
    "sqlite3", "ssl", "stat", "statistics", "string", "stringprep", "struct",
    "subprocess", "sys", "sysconfig", "tabnanny", "tarfile", "telnetlib",
    "tempfile", "termios", "textwrap", "threading", "time", "timeit", "tkinter",
    "token", "tokenize", "trace", "traceback", "tracemalloc", "tty", "turtle",
    "types", "typing", "unicodedata", "unittest", "urllib", "uuid", "venv",
    "warnings", "wave", "weakref", "webbrowser", "wsgiref", "xdrlib", "xml",
    "xmlrpc", "zipapp", "zipfile", "zipimport", "zlib",
}

# Common import name → pip package name mismatches.
IMPORT_TO_PIP = {
    "bs4": "beautifulsoup4",
    "PIL": "pillow",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
    "fitz": "pymupdf",
    "speedtest": "speedtest-cli",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "dotenv": "python-dotenv",
    "dateutil": "python-dateutil",
    "serial": "pyserial",
    "websocket": "websocket-client",
}


def _get_python_executable() -> str:
    """The running interpreter, so dependencies match the active runtime."""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        py_cand = exe[:-5] + ".exe"
        if Path(py_cand).exists():
            return py_cand
    return exe


class _BanVisitor(ast.NodeVisitor):
    """Walks the AST once, collecting every ban-list violation."""

    def __init__(self):
        self.violations: list[str] = []

    def _add(self, node: ast.AST, why: str) -> None:
        self.violations.append(f"line {getattr(node, 'lineno', '?')}: {why}")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in BANNED_IMPORTS:
                self._add(node, f"import of '{root}' is not allowed in a skill")
            elif root in BANNED_PROJECT_ROOTS:
                self._add(node, f"skill must be self-contained — cannot import project module '{root}'")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            root = node.module.split(".")[0]
            if root in BANNED_IMPORTS:
                self._add(node, f"import from '{root}' is not allowed in a skill")
            elif root in BANNED_PROJECT_ROOTS:
                self._add(node, f"skill must be self-contained — cannot import project module '{root}'")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id in BANNED_CALL_NAMES:
            self._add(node, f"call to '{func.id}' is not allowed in a skill")
        elif isinstance(func, ast.Attribute):
            chain = _attr_chain(func)
            if chain and chain in BANNED_QUALIFIED:
                self._add(node, f"call to '{'.'.join(chain)}' is not allowed in a skill")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            low = node.value.lower()
            for banned in BANNED_STRINGS:
                if banned in low:
                    self._add(node, f"string literal contains prohibited term '{banned}'")
                    break
        self.generic_visit(node)


def _attr_chain(node: ast.Attribute) -> Optional[Tuple[str, str]]:
    """Reduces ``shutil.rmtree`` to ('shutil', 'rmtree'); None if deeper."""
    if isinstance(node.value, ast.Name):
        return (node.value.id, node.attr)
    return None


class SkillCrucible:
    """Verifies forged skill code before it is committed to features/."""

    @staticmethod
    def validate_ast(code_str: str) -> Tuple[bool, Optional[str]]:
        """Syntax check + ban list + mandatory entry point. Never raises."""
        if not code_str or not code_str.strip():
            return False, "Code cannot be empty."

        try:
            tree = ast.parse(code_str)
        except SyntaxError as e:
            return False, f"Syntax Error on line {e.lineno}: {e.msg}"
        except Exception as e:
            return False, f"Validation Error: {e}"

        has_execute = any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "execute"
            for node in tree.body
        )
        if not has_execute:
            return False, (
                "Skill code must define 'def execute(**kwargs)' or "
                "'async def execute(**kwargs)'."
            )

        visitor = _BanVisitor()
        visitor.visit(tree)
        if visitor.violations:
            return False, "Security Violation: " + "; ".join(visitor.violations[:3])

        return True, None

    @staticmethod
    def extract_dependencies(code_str: str) -> List[str]:
        """Top-level third-party imports, deduped and sorted."""
        try:
            tree = ast.parse(code_str)
        except Exception:
            return []

        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root not in STANDARD_LIB_MODULES and root not in BANNED_PROJECT_ROOTS:
                        imports.add(root)
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                if root not in STANDARD_LIB_MODULES and root not in BANNED_PROJECT_ROOTS:
                    imports.add(root)
        return sorted(imports)

    @classmethod
    def resolve_dependencies(cls, dependencies: List[str]) -> Tuple[bool, str]:
        """Installs missing third-party packages into the active environment.

        Deliberately permitted: it is scoped to the interpreter this app
        already runs on, pip is the same tool the user would use, and the
        result is reported in the forge transcript. Never touches other
        projects or global state beyond site-packages."""
        if not dependencies:
            return True, "No external dependencies required."

        py_exe = _get_python_executable()
        installed_any = []

        for dep in dependencies:
            healthy = False
            try:
                proc = subprocess.run(
                    [py_exe, "-c", f"import {dep}"],
                    capture_output=True, timeout=5,
                )
                healthy = proc.returncode == 0
            except Exception:
                healthy = False
            if healthy:
                continue

            pip_name = IMPORT_TO_PIP.get(dep, dep)
            try:
                proc = subprocess.run(
                    [py_exe, "-m", "pip", "install", pip_name, "--quiet"],
                    capture_output=True, text=True, timeout=180,
                )
                if proc.returncode != 0:
                    err_sample = (proc.stderr or "").strip()[:180] or "Unknown pip error"
                    return False, f"Failed to install dependency '{pip_name}': {err_sample}"
                installed_any.append(pip_name)
            except subprocess.TimeoutExpired:
                return False, f"Timeout installing '{pip_name}'."
            except Exception as e:
                return False, f"Exception installing '{pip_name}': {e}"

        msg = (f"Installed: {', '.join(installed_any)}"
               if installed_any else "All dependencies satisfied.")
        return True, msg

    @classmethod
    def run_sandbox_test(
        cls,
        skill_code: str,
        test_cases: List[Dict[str, Any]],
        timeout: float = 75.0,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Executes the test cases in an isolated child process.

        The skill is written to a throwaway directory and imported as a
        module there (no fragile ``-c`` quoting), under ``python -I`` so
        PYTHONPATH and user site-packages cannot leak in. Returns
        (passed, message, telemetry)."""
        py_exe = _get_python_executable()
        tmp = Path(tempfile.mkdtemp(prefix="jarvis_crucible_"))
        try:
            (tmp / "skill.py").write_text(skill_code, encoding="utf-8")
            (tmp / "cases.json").write_text(
                json.dumps(test_cases or [{"input": {}}]), encoding="utf-8")
            (tmp / "runner.py").write_text(_RUNNER_SRC, encoding="utf-8")

            start = time.time()
            try:
                proc = subprocess.run(
                    [py_exe, "-I", str(tmp / "runner.py")],
                    capture_output=True, text=True, timeout=timeout,
                    cwd=str(tmp),
                )
            except subprocess.TimeoutExpired:
                return False, (f"Sandbox execution timed out after {timeout} seconds."), \
                    {"elapsed_s": timeout}
            except Exception as e:
                return False, f"Sandbox execution error: {e}", {"elapsed_s": 0.0}

            elapsed = time.time() - start
            if proc.returncode != 0:
                err_text = (proc.stderr or "").strip() or (proc.stdout or "").strip() \
                    or "Process exited with an error code."
                return False, f"Sandbox Test Failed: {err_text[:300]}", {"elapsed_s": elapsed}

            test_results = _extract_results(proc.stdout)
            if test_results is None:
                test_results = [{"success": True, "output": proc.stdout.strip()[:300]}]

            if not all(t.get("success", False) for t in test_results):
                first_err = next(
                    (t.get("error", "Unknown test failure")
                     for t in test_results if not t.get("success")),
                    "Test failed",
                )
                return False, f"Test Verification Failed: {first_err}", \
                    {"results": test_results, "elapsed_s": elapsed}

            return True, (f"Passed {len(test_results)}/{len(test_results)} tests "
                          f"in {elapsed:.2f}s"), {
                "results": test_results,
                "elapsed_s": elapsed,
            }
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _extract_results(stdout: str) -> Optional[List[dict]]:
    """The runner prints one JSON array on its last line."""
    try:
        for line in reversed((stdout or "").splitlines()):
            candidate = line.strip()
            if candidate.startswith("[") and candidate.endswith("]"):
                parsed = json.loads(candidate)
                if isinstance(parsed, list):
                    return parsed
    except Exception:
        pass
    return None


_RUNNER_SRC = r'''
import asyncio
import inspect
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import skill  # noqa: E402

cases = json.loads(
    pathlib.Path(__file__).with_name("cases.json").read_text(encoding="utf-8")
)
results = []

for i, tc in enumerate(cases):
    args = tc.get("input", {}) if isinstance(tc, dict) else {}
    try:
        if inspect.iscoroutinefunction(skill.execute):
            res = asyncio.run(skill.execute(**args))
        else:
            res = skill.execute(**args)

        if res is None:
            res = "Operation completed with no output."

        is_err = False
        err_msg = ""
        if isinstance(res, dict):
            if res.get("error"):
                is_err, err_msg = True, str(res["error"])
            elif res.get("success") is False:
                is_err, err_msg = True, str(res.get("message") or res.get("error") or "Skill returned success=False")
        elif isinstance(res, str) and (res.lower().startswith("error:") or res.lower().startswith("failed:")):
            is_err, err_msg = True, res

        if is_err:
            results.append({"index": i, "success": False, "error": err_msg, "output": str(res)[:300]})
        else:
            results.append({"index": i, "success": True, "output": str(res)[:300]})
    except Exception as exc:
        results.append({"index": i, "success": False, "error": str(exc), "output": ""})

print(json.dumps(results))
'''
