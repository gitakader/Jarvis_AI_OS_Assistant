"""
Offline tests for the Skill Forge + Crucible sandbox + Skill Registry.

Everything is headless: temp directories, fake LLM/registry/speak objects,
and monkeypatched confirm/pip. No real Gemini call, no real pip install, no
UI, no config reads, and nothing outside the temp dirs is ever written.

Run:  venv/Scripts/python.exe selfheal_tests/test_skill_forge.py
"""
from __future__ import annotations

import ast
import json
import pathlib
import subprocess
import sys
import tempfile
from unittest.mock import patch

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(f"{label} — {detail}" if detail else label)
        print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""))


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — SkillCrucible.validate_ast
# ─────────────────────────────────────────────────────────────────────────────

from core.skill_crucible import SkillCrucible, _get_python_executable

VALID_SKILL = '''
def execute(**kwargs):
    name = kwargs.get("name") or "world"
    return {"message": f"hello {name}"}
'''

ASYNC_SKILL = '''
async def execute(**kwargs):
    return "async ok"
'''

NO_EXECUTE = "x = 1\n"

SYNTAX_ERR = "def execute(:\n"

BANNED_CODE = {
    "subprocess import": "import subprocess\ndef execute(**kwargs): return 'x'",
    "os import": "import os\ndef execute(**kwargs): return os.getcwd()",
    "os import-from": "from os import path\ndef execute(**kwargs): return path.exists('.')",
    "ctypes import": "import ctypes\ndef execute(**kwargs): return 'x'",
    "socket import": "import socket\ndef execute(**kwargs): return 'x'",
    "pickle import": "import pickle\ndef execute(**kwargs): return 'x'",
    "multiprocessing import": "import multiprocessing\ndef execute(**kwargs): return 'x'",
    "project import": "import core.gemini\ndef execute(**kwargs): return 'x'",
    "from project import": "from actions import notifications\ndef execute(**kwargs): return 'x'",
    "eval call": "def execute(**kwargs): return eval('1+1')",
    "exec call": "def execute(**kwargs): exec('x=1'); return 'x'",
    "compile call": "def execute(**kwargs): return compile('1','','eval')",
    "__import__ call": "def execute(**kwargs): return __import__('os')",
    "globals call": "def execute(**kwargs): return str(globals())",
    "input call": "def execute(**kwargs): return input()",
    "shutil.rmtree": "import shutil\ndef execute(**kwargs): shutil.rmtree('/tmp/x'); return 'x'",
    "importlib.import_module": "import importlib\ndef execute(**kwargs): return importlib.import_module('os')",
    "taskkill string": "def execute(**kwargs): return 'taskkill /f /im chrome.exe'",
    "del /f string": "def execute(**kwargs): return 'del /f /q C:\\\\temp\\\\*.*'",
    "rmdir /s string": "def execute(**kwargs): return 'rmdir /s /q C:\\\\temp'",
    "reg delete string": "def execute(**kwargs): return 'reg delete HKLM\\\\x /f'",
    "schtasks string": "def execute(**kwargs): return 'schtasks /create /tn x'",
    "netsh string": "def execute(**kwargs): return 'netsh interface set interface x'",
    "powercfg string": "def execute(**kwargs): return 'powercfg /hibernate off'",
    "shutdown /s string": "def execute(**kwargs): return 'shutdown /s /t 0'",
    "shutdown -h string": "def execute(**kwargs): return 'shutdown -h now'",
    "diskpart string": "def execute(**kwargs): return 'diskpart /s script.txt'",
    "vssadmin string": "def execute(**kwargs): return 'vssadmin delete shadows'",
    "bcdedit string": "def execute(**kwargs): return 'bcdedit /set bootstatuspolicy'",
    "system32 string": "def execute(**kwargs): return 'C:\\\\Windows\\\\System32\\\\cmd.exe'",
}

ALLOWED_CODE = {
    "stdlib only": "import json, pathlib\ndef execute(**kwargs): return json.dumps({'ok': True})",
    "third-party": "import requests\ndef execute(**kwargs): return 'net ok'",
    "PIL": "from PIL import Image\ndef execute(**kwargs): return 'img ok'",
    "matplotlib": "import matplotlib\ndef execute(**kwargs): return 'plot ok'",
    "pathlib makedirs": (
        "from pathlib import Path\n"
        "def execute(**kwargs):\n"
        "    d = Path('out'); d.mkdir(parents=True, exist_ok=True); return 'made'"
    ),
    "safe strings": "def execute(**kwargs): return 'the system should shutdown soon'",
    "safe del word": "def execute(**kwargs): return 'please delete the model file and deliver it'",
}


def section1() -> None:
    print("SECTION 1 — crucible validate_ast")
    ok, err = SkillCrucible.validate_ast(VALID_SKILL)
    check("1.1 valid sync skill passes", ok and err is None, str(err))
    ok, err = SkillCrucible.validate_ast(ASYNC_SKILL)
    check("1.2 valid async skill passes", ok and err is None, str(err))
    ok, err = SkillCrucible.validate_ast("")
    check("1.3 empty code rejected", not ok and "empty" in str(err).lower(), str(err))
    ok, err = SkillCrucible.validate_ast(NO_EXECUTE)
    check("1.4 missing execute rejected", not ok and "execute" in str(err), str(err))
    ok, err = SkillCrucible.validate_ast(SYNTAX_ERR)
    check("1.5 syntax error rejected", not ok and "Syntax Error" in str(err), str(err))
    for label, code in BANNED_CODE.items():
        ok, err = SkillCrucible.validate_ast(code)
        check(f"1.6 banned: {label}", not ok, f"unexpectedly passed ({err})")
    for label, code in ALLOWED_CODE.items():
        ok, err = SkillCrucible.validate_ast(code)
        check(f"1.7 allowed: {label}", ok, str(err))


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — extract_dependencies + resolve_dependencies (pip faked)
# ─────────────────────────────────────────────────────────────────────────────

def section2() -> None:
    print("SECTION 2 — dependencies")
    code = ("import requests\nfrom bs4 import BeautifulSoup\nimport json\n"
            "import core.gemini\nfrom actions import notifications\n"
            "import PIL\nimport os\n"
            "def execute(**kwargs): return 'x'")
    deps = SkillCrucible.extract_dependencies(code)
    check("2.1 third-party roots found", set(deps) == {"requests", "bs4", "PIL"}, str(deps))
    check("2.2 stdlib+project excluded", "json" not in deps and "core" not in deps
          and "actions" not in deps and "os" not in deps)

    import core.skill_crucible as sc

    class _Proc:
        def __init__(self, returncode, stderr="", stdout=""):
            self.returncode, self.stderr, self.stdout = returncode, stderr, stdout

    with patch.object(sc.subprocess, "run", return_value=_Proc(0)):
        ok, msg = SkillCrucible.resolve_dependencies(["json"])
        check("2.3 healthy dep skips pip", ok and "satisfied" in msg, msg)
    with patch.object(sc.subprocess, "run", side_effect=[_Proc(1), _Proc(0)]) as m:
        ok, msg = SkillCrucible.resolve_dependencies(["nonexistent_pkg_xyz"])
        check("2.4 missing dep pip-installed", ok and "Installed:" in msg, msg)
        check("2.5 pip invoked", m.called and "pip" in " ".join(m.call_args_list[1][0][0]))
    with patch.object(sc.subprocess, "run", return_value=_Proc(1, stderr="No matching distribution")):
        ok, msg = SkillCrucible.resolve_dependencies(["nonexistent_pkg_xyz"])
        check("2.6 pip failure reported", not ok and "No matching distribution" in msg, msg)
    ok, msg = SkillCrucible.resolve_dependencies([])
    check("2.7 empty deps trivially ok", ok, msg)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — run_sandbox_test (real isolated subprocess, temp dir)
# ─────────────────────────────────────────────────────────────────────────────

SIMPLE = "def execute(**kwargs):\n    return {'answer': kwargs.get('q', 'none')}"
FAILING = "def execute(**kwargs):\n    return {'error': 'nope'}"
RAISING = "def execute(**kwargs):\n    raise RuntimeError('boom')"
HANGING = "import time\ndef execute(**kwargs):\n    time.sleep(60)\n    return 'late'"
PROJECT_IMPORT = "import core.gemini\ndef execute(**kwargs): return 'x'"
COROUTINE = "import asyncio\nasync def execute(**kwargs):\n    await asyncio.sleep(0.01)\n    return 'async result'"


def section3() -> None:
    print("SECTION 3 — sandbox execution")
    ok, msg, tel = SkillCrucible.run_sandbox_test(SIMPLE, [{"input": {}}, {"input": {"q": "hi"}}])
    check("3.1 simple skill passes all cases", ok, msg)
    check("3.2 two results returned", ok and len(tel.get("results", [])) == 2, str(tel))

    ok, msg, tel = SkillCrucible.run_sandbox_test(ASYNC_SKILL, [{"input": {}}])
    check("3.3 async skill passes", ok, msg)

    ok, msg, tel = SkillCrucible.run_sandbox_test(FAILING, [{"input": {}}])
    check("3.4 error-dict result fails", not ok and "nope" in msg, msg)

    ok, msg, tel = SkillCrucible.run_sandbox_test(RAISING, [{"input": {}}])
    check("3.5 exception fails", not ok and "boom" in msg, msg)

    ok, msg, tel = SkillCrucible.run_sandbox_test(HANGING, [{"input": {}}], timeout=3.0)
    check("3.6 hang hits timeout", not ok and "timed out" in msg, msg)

    ok, msg, tel = SkillCrucible.run_sandbox_test(PROJECT_IMPORT, [{"input": {}}])
    check("3.7 isolated mode blocks project imports", not ok and "core" in msg, msg)

    # No temp litter: the crucible cleans its temp dirs.
    import tempfile as _tf
    leftovers = [p for p in pathlib.Path(_tf.gettempdir()).iterdir()
                 if p.name.startswith("jarvis_crucible_")]
    check("3.8 temp dirs cleaned up", not leftovers, str(leftovers))


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — SkillRegistry (temp features dir)
# ─────────────────────────────────────────────────────────────────────────────

from core.skill_registry import SkillRegistry, EXEC_TIMEOUT_SECONDS

GOOD_CODE = "def execute(**kwargs):\n    return {'ok': True, 'name': kwargs.get('name', '?')}"
SLOW_CODE = "import time\ndef execute(**kwargs):\n    time.sleep(60)\n    return 'late'"
ERR_CODE = "def execute(**kwargs):\n    return {'error': 'skill says no'}"
ASYNC_CODE = "async def execute(**kwargs):\n    return 'async done'"
NONE_CODE = "def execute(**kwargs):\n    return None"


def _mk_registry(tmp: pathlib.Path, **kw):
    reg = SkillRegistry(features_dir=tmp / "features", **kw)
    reg.initialize()
    return reg


def section4() -> None:
    print("SECTION 4 — registry")
    with tempfile.TemporaryDirectory(prefix="jv_forge_t_") as td:
        tmp = pathlib.Path(td)
        reg = _mk_registry(tmp)
        rec = reg.register("hello_skill", "Says hello", {"type": "OBJECT", "properties": {}}, GOOD_CODE)
        check("4.1 register returns valid record", rec.valid and rec.active)
        check("4.2 file written", (tmp / "features" / "hello_skill.py").exists())
        check("4.3 sidecar manifest written", (tmp / "features" / "hello_skill" / "manifest.json").exists())
        check("4.4 has()", reg.has("hello_skill"))

        try:
            reg.register("hello_skill", "dup", {}, GOOD_CODE)
            check("4.5 duplicate register raises", False, "no exception")
        except ValueError as e:
            check("4.5 duplicate register raises", True)

        for bad in ("", "1bad", "has space", "mcp_evil", "x" * 65):
            ok, reason = reg.check_name(bad)
            check(f"4.6 bad name rejected: {bad[:12]!r}", not ok, reason)

        ok, reason = reg.check_name("fresh_name")
        check("4.7 good name accepted", ok, reason)

        # reserved-name callback from main.py's perspective
        reg2 = _mk_registry(tmp, name_available=lambda n: n != "system_status")
        ok, _ = reg2.check_name("system_status")
        check("4.8 name_available callback enforced", not ok)

        # execution paths
        h = reg.make_handler("hello_skill")
        out = h({"name": "sir"})
        check("4.9 sync handler runs", "ok" in out and "sir" in out, out)
        check("4.10 invocations counted", reg.get("hello_skill").invocations == 1)
        sidecar = json.loads((tmp / "features" / "hello_skill" / "manifest.json")
                             .read_text(encoding="utf-8"))
        check("4.10b invocations persisted to sidecar", sidecar.get("invocations") == 1)

        reg.register("none_skill", "none", {}, NONE_CODE)
        out = reg.make_handler("none_skill")({})
        check("4.11 None result → Done.", out == "Done.", out)

        reg.register("err_skill", "err", {}, ERR_CODE)
        out = reg.make_handler("err_skill")({})
        check("4.12 error dict surfaced", "skill says no" in out, out)
        check("4.13 last_error recorded", "skill says no" in reg.get("err_skill").last_error)

        reg.register("async_skill", "async", {}, ASYNC_CODE)
        out = reg.make_handler("async_skill")({})
        check("4.14 async execute runs", "async done" in out, out)

        reg.register("slow_skill", "slow", {}, SLOW_CODE)
        old = EXEC_TIMEOUT_SECONDS
        import core.skill_registry as sr
        sr.EXEC_TIMEOUT_SECONDS = 2.0
        try:
            out = reg.make_handler("slow_skill")({})
            check("4.15 hang → timeout message", "timed out" in out, out)
            check("4.16 timeout recorded", "timed out" in reg.get("slow_skill").last_error)
        finally:
            sr.EXEC_TIMEOUT_SECONDS = old

        # persistence + toggle + delete
        reg.toggle("hello_skill", False)
        check("4.17 toggle off", reg.get("hello_skill").active is False)
        reg3 = _mk_registry(tmp)
        check("4.18 toggle persisted across reload", reg3.get("hello_skill").active is False)
        reg3.toggle("hello_skill", True)
        check("4.19 toggle back on", reg3.get("hello_skill").active is True)
        check("4.20 disabled skill still listed", any(s["name"] == "hello_skill" for s in reg.list()))

        reg.delete("hello_skill")
        check("4.21 delete removes record", not reg.has("hello_skill"))
        check("4.22 delete removes files",
              not (tmp / "features" / "hello_skill.py").exists()
              and not (tmp / "features" / "hello_skill").exists())
        reg4 = _mk_registry(tmp)
        check("4.23 delete persists across reload", not reg4.has("hello_skill"))
        check("4.24 other skills untouched", reg4.has("none_skill") and reg4.has("async_skill"))

        # active_skills on reload excludes disabled
        reg4.toggle("none_skill", False)
        reg5 = _mk_registry(tmp)
        actives = {s.name for s in reg5.active_skills()}
        check("4.25 active_skills respects state", "none_skill" not in actives and "async_skill" in actives)

        # corrupt skill file is skipped, not fatal
        (tmp / "features" / "broken_skill.py").write_text("FEATURE_METADATA = {", encoding="utf-8")
        reg6 = _mk_registry(tmp)
        check("4.26 broken skill file skipped gracefully", not reg6.has("broken_skill"))

        # unknown handler
        out = reg6.make_handler("ghost")({})
        check("4.27 unknown skill → unavailable", "not available" in out, out)

        # on_change callback: fired on register/toggle/delete
        events: list = []
        reg7 = SkillRegistry(features_dir=tmp / "features_cb",
                             on_change=lambda n, r: events.append((n, r.active if r else None)))
        reg7.initialize()
        rec = reg7.register("cb_skill", "cb", {"type": "OBJECT", "properties": {}}, GOOD_CODE)
        check("4.28 on_change fired on register", events == [("cb_skill", True)], str(events))
        reg7.toggle("cb_skill", False)
        check("4.29 on_change fired on toggle", events[-1] == ("cb_skill", False), str(events))
        reg7.toggle("cb_skill", True)
        check("4.30 on_change fired on re-enable", events[-1] == ("cb_skill", True), str(events))
        reg7.delete("cb_skill")
        check("4.31 on_change fired on delete with None", events[-1] == ("cb_skill", None), str(events))
        reg7.initialize()
        check("4.32 initialize fires no callbacks", len(events) == 4, str(events))


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — SkillForge core (synthesizer faked)
# ─────────────────────────────────────────────────────────────────────────────

from core.skill_forge import SkillForge

GOOD_PACKAGE = {
    "success": True,
    "manifest": {"name": "hello_forge", "description": "Says hello.",
                 "parameters": {"type": "OBJECT", "properties": {}}},
    "code": GOOD_CODE,
    "test_cases": [{"input": {}}],
}
BAD_AST_PACKAGE = {
    "success": True,
    "manifest": {"name": "bad_ast", "description": "bad",
                 "parameters": {"type": "OBJECT", "properties": {}}},
    "code": "import subprocess\ndef execute(**kwargs): return 'x'",
    "test_cases": [{"input": {}}],
}
BAD_RUNTIME_PACKAGE = {
    "success": True,
    "manifest": {"name": "bad_run", "description": "bad",
                 "parameters": {"type": "OBJECT", "properties": {}}},
    "code": "def execute(**kwargs):\n    return {'error': 'runtime no'}",
    "test_cases": [{"input": {}}],
}
FIXED_CODE = "def execute(**kwargs):\n    return {'fixed': True}"


def section5() -> None:
    print("SECTION 5 — forge core (faked LLM)")
    with tempfile.TemporaryDirectory(prefix="jv_forge_t_") as td:
        tmp = pathlib.Path(td)
        reg = _mk_registry(tmp)

        # 5.1 happy path
        with patch.object(SkillForge, "_call_synthesizer", return_value=dict(GOOD_PACKAGE)):
            res = SkillForge.forge("say hello", registry=reg)
        check("5.1 forge happy path", res.get("success") and res.get("name") == "hello_forge", str(res))
        check("5.2 registered", reg.has("hello_forge"))
        check("5.3 message mentions tests", "tests" in res.get("message", ""), res.get("message", ""))

        # 5.4 no goal
        res = SkillForge.forge("  ", registry=reg)
        check("5.4 blank goal rejected", not res.get("success"))

        # 5.5 no registry
        with patch.object(SkillForge, "_call_synthesizer", return_value=dict(GOOD_PACKAGE)):
            res = SkillForge.forge("say hello", registry=None)
        check("5.5 missing registry rejected", not res.get("success"))

        # 5.6 AST fail → repair → pass (one repair)
        with patch.object(SkillForge, "_call_synthesizer", return_value=dict(BAD_AST_PACKAGE)), \
             patch.object(SkillForge, "_repair_code", return_value={"success": True, "code": FIXED_CODE}) as m_repair:
            res = SkillForge.forge("bad ast skill", registry=reg)
        check("5.6 AST-fail repaired to success", res.get("success") and res.get("name") == "bad_ast", str(res))
        check("5.7 exactly one repair call", m_repair.call_count == 1, str(m_repair.call_count))
        check("5.8 repaired skill registered", reg.has("bad_ast"))

        # 5.9 AST fail → repair fails → clean failure
        with patch.object(SkillForge, "_call_synthesizer", return_value=dict(BAD_AST_PACKAGE)), \
             patch.object(SkillForge, "_repair_code", return_value={"success": False, "error": "no idea"}):
            res = SkillForge.forge("unfixable", registry=reg)
        check("5.9 unfixable AST → failure", not res.get("success") and "AST" in res.get("message", ""), str(res))
        check("5.10 nothing registered", not reg.has("unfixable"))

        # 5.11 runtime fail → repair → pass
        with patch.object(SkillForge, "_call_synthesizer", return_value=dict(BAD_RUNTIME_PACKAGE)), \
             patch.object(SkillForge, "_repair_code", return_value={"success": True, "code": FIXED_CODE}):
            res = SkillForge.forge("runtime fix", registry=reg)
        check("5.11 runtime-fail repaired to success", res.get("success") and res.get("name") == "bad_run", str(res))

        # 5.12 name collision → auto-suffix
        with patch.object(SkillForge, "_call_synthesizer",
                          return_value=dict(GOOD_PACKAGE)):  # name hello_forge already taken
            res = SkillForge.forge("say hello again", registry=reg)
        check("5.12 taken name auto-suffixed", res.get("success") and res.get("name") == "hello_forge_skill", str(res))

        # 5.13 unparseable model output
        with patch.object(SkillForge, "_llm_json", return_value="not json at all"):
            res = SkillForge.forge("junk output", registry=reg)
        check("5.13 unparseable LLM output → failure", not res.get("success") and "Skill synthesis failed" in res.get("message", ""), str(res))

        # 5.14 LLM returns None (ladder exhausted)
        with patch.object(SkillForge, "_llm_json", return_value=None):
            res = SkillForge.forge("no answer", registry=reg)
        check("5.14 exhausted ladder → failure", not res.get("success") and "ladder" in res.get("message", ""), str(res))

        # 5.15 name hint normalization + collision-free use
        with patch.object(SkillForge, "_call_synthesizer", return_value=dict(GOOD_PACKAGE)):
            pass  # covered by 5.12


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — actions/skill_forge.py handler (registry + confirm faked)
# ─────────────────────────────────────────────────────────────────────────────

import actions.skill_forge as sf

class _Speak:
    def __init__(self):
        self.said: list[str] = []
    def __call__(self, text: str):
        self.said.append(text)


def section6() -> None:
    print("SECTION 6 — skill_forge action")
    check("6.1 TOOL name", sf.TOOL.get("name") == "skill_forge")
    check("6.2 handler callable", callable(sf.TOOL.get("handler")))
    check("6.3 NON_BLOCKING declared", sf.TOOL.get("behavior") == "NON_BLOCKING")
    check("6.4 SILENT scheduling", sf.TOOL.get("scheduling") == "SILENT")
    schema = sf.TOOL.get("parameters", {}).get("properties", {})
    check("6.5 schema covers forge/list/toggle/delete", "action" in schema and "goal" in schema
          and "name" in schema and "enable" in schema, str(schema))

    with tempfile.TemporaryDirectory(prefix="jv_forge_t_") as td:
        tmp = pathlib.Path(td)
        reg = _mk_registry(tmp)
        reg.register("demo_skill", "Does demo things.", {"type": "OBJECT", "properties": {}},
                     GOOD_CODE)
        speak = _Speak()

        # list
        out = sf.skill_forge({"action": "list"}, skills=reg)
        check("6.6 list shows skill", "demo_skill" in out and "enabled" in out, out)
        out = sf.skill_forge({"action": "list"}, skills=_mk_registry(tmp / "empty"))
        check("6.7 empty list message", "no forged skills" in out, out)

        # toggle
        out = sf.skill_forge({"action": "toggle", "name": "demo_skill"}, skills=reg)
        check("6.8 toggle flips", "disabled" in out and reg.get("demo_skill").active is False, out)
        out = sf.skill_forge({"action": "toggle", "name": "demo_skill", "enable": True}, skills=reg)
        check("6.9 toggle enable", "enabled" in out and reg.get("demo_skill").active is True, out)
        out = sf.skill_forge({"action": "toggle", "name": "ghost"}, skills=reg)
        check("6.10 toggle unknown name", "don't have" in out, out)
        out = sf.skill_forge({"action": "toggle"}, skills=reg)
        check("6.11 toggle without name", "name" in out, out)

        # delete → confirm gate
        with patch.object(sf.confirm, "request", return_value="[CONFIRMATION_PENDING] gate") as m_req:
            out = sf.skill_forge({"action": "delete", "name": "demo_skill"}, skills=reg)
        check("6.12 delete goes through confirm gate", out == "[CONFIRMATION_PENDING] gate", out)
        check("6.13 gate key", m_req.call_args[1].get("key") == "skill_delete", str(m_req.call_args))
        check("6.14 skill still there before confirm", reg.has("demo_skill"))
        run_fn = m_req.call_args[1]["run"]
        result = run_fn()
        check("6.15 gate callable deletes", result == "Deleted skill 'demo_skill'." and not reg.has("demo_skill"), result)
        out = sf.skill_forge({"action": "delete", "name": "ghost"}, skills=reg)
        check("6.16 delete unknown name", "don't have" in out, out)

        # forge (SkillForge.forge faked)
        with patch.object(sf.SkillForge, "forge", return_value={
                "success": True, "name": "new_skill", "description": "Fetches cricket scores.",
                "message": "Forged and activated skill 'new_skill'. Passed 2/2 tests in 0.50s."}):
            out = sf.skill_forge({"action": "forge", "goal": "show cricket scores"},
                                 skills=reg, speak=speak)
        check("6.17 forge success text", "next reconnect" in out and "new_skill" in out, out)
        check("6.18 forge announces via speak", any("new_skill" in s for s in speak.said), str(speak.said))

        out = sf.skill_forge({"action": "forge", "goal": ""}, skills=reg, speak=speak)
        check("6.19 forge without goal", "Tell me what the skill should do" in out, out)

        with patch.object(sf.SkillForge, "forge", return_value={"success": False, "message": "Crucible rejected it"}):
            out = sf.skill_forge({"action": "forge", "goal": "do something"}, skills=reg, speak=speak)
        check("6.20 forge failure text", "Skill forge failed" in out and "Crucible" in out, out)
        check("6.21 forge failure announced", any("could not build" in s for s in speak.said), str(speak.said))

        # registry missing
        out = sf.skill_forge({"action": "forge", "goal": "x"}, skills=None)
        check("6.22 forge without registry", "unavailable" in out, out)
        out = sf.skill_forge({"action": "list"}, skills=None)
        check("6.23 list without registry", "unavailable" in out, out)
        out = sf.skill_forge({"action": "toggle", "name": "x"}, skills=None)
        check("6.24 toggle without registry", "unavailable" in out, out)

        # speak=None must not crash forge success
        with patch.object(sf.SkillForge, "forge", return_value={
                "success": True, "name": "new_skill", "description": "d",
                "message": "ok"}):
            out = sf.skill_forge({"action": "forge", "goal": "x"}, skills=reg, speak=None)
        check("6.25 forge with speak=None", "next reconnect" in out, out)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — action registry dynamic registration
# ─────────────────────────────────────────────────────────────────────────────

from core.action_loader import ActionRegistry, _CTX_KEYS

def section7() -> None:
    print("SECTION 7 — ActionRegistry dynamic registration")
    reg = ActionRegistry({}, lambda m: None)
    ok = reg.register_dynamic("dyn_tool", "A dynamic tool.",
                              {"type": "OBJECT", "properties": {}}, lambda parameters: "ran")
    check("7.1 register_dynamic succeeds", ok and reg.has("dyn_tool"))
    check("7.2 duplicate rejected", reg.register_dynamic("dyn_tool", "x", {}, lambda p: "y") is False)
    decls = reg.get_tool_declarations()
    check("7.3 declaration present", any(d["name"] == "dyn_tool" for d in decls), str(decls))
    out = reg.run("dyn_tool", {}, {"player": None, "speak": None, "skills": None})
    check("7.4 run() dispatches dynamic tool", out == "ran", out)
    check("7.5 unregister dynamic tool", reg.unregister("dyn_tool") and not reg.has("dyn_tool"))
    check("7.6 unregister unknown → False", reg.unregister("dyn_tool") is False)
    from core.action_loader import ActionRecord
    ar7 = ActionRegistry({"bundled": ActionRecord(name="bundled", description="b",
                           parameters={"type": "OBJECT", "properties": {}},
                           handler=lambda p: "b", file="actions/bundled.py", valid=True)},
                         lambda m: None)
    check("7.7 bundled action never unregistered", ar7.unregister("bundled") is False)

    with tempfile.TemporaryDirectory(prefix="jv_forge_t_") as td:
        tmp = pathlib.Path(td)
        reg2 = _mk_registry(tmp)
        reg2.register("ctx_skill", "ctx", {"type": "OBJECT", "properties": {}},
                      "def execute(**kwargs):\n    return {'seen': True}")
        ar = ActionRegistry({}, lambda m: None)
        ar.register_dynamic("ctx_skill", "ctx", {"type": "OBJECT", "properties": {}},
                            reg2.make_handler("ctx_skill"))
        out = ar.run("ctx_skill", {"x": 1}, {"skills": reg2})
        check("7.5 dynamic handler via full ctx", "True" in out, out)

    check("7.6 'skills' in _CTX_KEYS", "skills" in _CTX_KEYS, str(_CTX_KEYS))


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8 — real discovery: action loads alongside the real actions dir
# ─────────────────────────────────────────────────────────────────────────────

def section8() -> None:
    print("SECTION 8 — real action discovery")
    from core.action_loader import discover_actions
    reg = discover_actions(BASE / "actions", reserved_names={"skill_forge"})
    rec = None
    for r in reg._all_records:
        if r.name == "skill_forge":
            rec = r
            break
    check("8.1 skill_forge present in discovery", rec is not None)
    if rec is not None:
        check("8.2 rejected as reserved name (no shadowing)", not rec.valid and "reserved" in rec.error, rec.error)
    reg2 = discover_actions(BASE / "actions", reserved_names=set())
    rec2 = None
    for r in reg2._all_records:
        if r.name == "skill_forge":
            rec2 = r
            break
    check("8.3 skill_forge loads when unreserved", rec2 is not None and rec2.valid, rec2.error if rec2 else "")
    if rec2 is not None:
        check("8.4 behavior/scheduling survive discovery",
              rec2.behavior == "NON_BLOCKING" and rec2.scheduling == "SILENT",
              f"{rec2.behavior}/{rec2.scheduling}")


def main() -> None:
    section1()
    section2()
    section3()
    section4()
    section5()
    section6()
    section7()
    section8()
    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    if FAILURES:
        print("Failures:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
