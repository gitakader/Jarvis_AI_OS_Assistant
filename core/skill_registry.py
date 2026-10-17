"""
Skill Registry — the home of Skill Forge skills.

Forged skills live in ``features/`` as one self-contained Python file each
(``features/<name>.py``) carrying an embedded ``FEATURE_METADATA`` dict, with
an optional sidecar directory ``features/<name>/manifest.json`` holding the
mutable state (active flag, invocation count, last error).

Boot:      ``initialize()`` scans the directory, parses metadata, and makes
           every valid, active skill available.
Dispatch:  ``make_handler(name)`` returns a plain synchronous callable for
           ActionRegistry.register_dynamic — the action registry in main.py
           then routes, declares, and logs the skill like any other tool.
           No dispatch code in main.py ever needs to know a skill is dynamic.
Hot-plug:  the Forge calls ``register()`` when it commits a new skill; it is
           visible to new sessions immediately and to the running session on
           its next reconnect.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import re
import shutil
import threading
import traceback
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_DEFAULT_PARAMS = {"type": "OBJECT", "properties": {}}

# A generated skill must answer within this long at runtime. The forge-time
# sandbox already bounded it at 75s; this is the second line of defence for
# drift introduced later.
EXEC_TIMEOUT_SECONDS = 120.0

# Fields that the sidecar manifest may override on load (everything else is
# authoritative in the .py metadata).
_MUTABLE_KEYS = ("active", "invocations", "last_error")


@dataclass
class SkillRecord:
    name: str
    description: str = ""
    parameters: dict = field(default_factory=lambda: dict(_DEFAULT_PARAMS))
    file: str = ""
    manifest_file: str = ""
    active: bool = True
    invocations: int = 0
    last_error: str = ""
    valid: bool = False
    error: str = ""


class SkillRegistry:
    def __init__(
        self,
        features_dir: Path,
        logger: Callable[[str], None] = print,
        notify: Callable[[str], None] = lambda m: None,
        name_available: Optional[Callable[[str], bool]] = None,
        on_change: Optional[Callable[[str, Optional["SkillRecord"]], None]] = None,
    ):
        self._dir = features_dir
        self._skills: Dict[str, SkillRecord] = {}
        self._modules: Dict[str, Any] = {}
        self._logger = logger
        self._notify = notify
        # Extra name check supplied by main.py (inline tools / actions /
        # plugins / MCP). The registry itself always checks its own names.
        self._name_available = name_available or (lambda n: True)
        # Fired after register/toggle/delete with (name, record) — or
        # (name, None) on delete. main.py uses it to keep the live action
        # registry in step, so a hot-forged skill is dispatchable immediately
        # and enters the tool declarations on the next reconnect.
        self._on_change = on_change
        self._lock = threading.Lock()

    # ── lifecycle ──────────────────────────────────────────────────────────

    def initialize(self) -> int:
        """Scans features/*.py. Returns the number of active skills loaded.

        Never raises: a broken file is logged and skipped."""
        self._dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._skills.clear()
            self._modules.clear()

            for path in sorted(self._dir.glob("*.py"), key=lambda p: p.name):
                if path.name.startswith("_"):
                    continue
                rec = self._load_file(path)
                if rec is None:
                    continue
                if not rec.valid:
                    # Rejected records never enter the dict: a broken file must
                    # not shadow a future forge of the same name.
                    self._logger(f"Skill rejected: {path.name} — {rec.error}")
                    continue
                self._skills[rec.name] = rec
                if rec.active:
                    self._logger(f"Skill loaded: {rec.name} ({path.name})")
                else:
                    self._logger(f"Skill disabled: {rec.name}")

        active = sum(1 for s in self._skills.values() if s.valid and s.active)
        self._logger(f"Skill discovery complete: {active} active.")
        return active

    def _load_file(self, path: Path) -> Optional[SkillRecord]:
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            meta: Dict[str, Any] = {}
            for node in tree.body:
                targets = (getattr(node, "targets", []) if isinstance(node, ast.Assign)
                           else [getattr(node, "target", None)] if isinstance(node, ast.AnnAssign)
                           else [])
                if any(isinstance(t, ast.Name) and t.id == "FEATURE_METADATA" for t in targets):
                    value = node.value
                    parsed = ast.literal_eval(value)
                    if isinstance(parsed, dict):
                        meta = parsed
                    break

            if not meta:
                return None  # not a skill file (helper module)

            name = meta.get("name")
            if not isinstance(name, str) or not _NAME_RE.match(name):
                return SkillRecord(name=str(name or path.stem), file=path.name,
                                   error="FEATURE_METADATA['name'] missing or invalid.")

            description = str(meta.get("description") or "").strip()
            parameters = meta.get("parameters", _DEFAULT_PARAMS)
            if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
                parameters = dict(_DEFAULT_PARAMS)

            # Sidecar manifest (features/<name>/manifest.json) holds the
            # mutable state; its absence means defaults.
            manifest_file = self._dir / name / "manifest.json"
            active, invocations, last_error = True, 0, ""
            if manifest_file.exists():
                try:
                    with open(manifest_file, "r", encoding="utf-8") as f:
                        sidecar = json.load(f)
                    active = bool(sidecar.get("active", True))
                    invocations = int(sidecar.get("invocations", 0) or 0)
                    last_error = str(sidecar.get("last_error") or "")
                except Exception as e:
                    self._logger(f"Skill '{name}': unreadable manifest — {e}")

            return SkillRecord(name=name, description=description, parameters=parameters,
                               file=path.name, manifest_file=str(manifest_file),
                               active=active, invocations=invocations,
                               last_error=last_error, valid=True)
        except Exception as e:
            traceback.print_exc()
            return SkillRecord(name=path.stem, file=path.name,
                               error=f"Failed to load: {e}")

    # ── queries ────────────────────────────────────────────────────────────

    def names(self) -> set[str]:
        with self._lock:
            return set(self._skills.keys())

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._skills

    def get(self, name: str) -> Optional[SkillRecord]:
        with self._lock:
            return self._skills.get(name)

    def active_skills(self) -> List[SkillRecord]:
        with self._lock:
            return [s for s in self._skills.values() if s.valid and s.active]

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [{
                "name": s.name,
                "description": s.description,
                "active": s.active and s.valid,
                "invocations": s.invocations,
                "last_error": s.last_error or "",
                "file": s.file,
            } for s in sorted(self._skills.values(), key=lambda s: s.name)]

    def check_name(self, name: str) -> tuple[bool, str]:
        """(ok, reason) — whether a NEW skill may use this name."""
        if not name or not _NAME_RE.match(name):
            return False, ("Skill name must be 1-64 characters of letters, digits, "
                           "and underscores, starting with a letter or underscore.")
        if name.startswith("mcp_"):
            return False, "Skill names cannot start with 'mcp_'."
        if name in self._skills:
            return False, f"A skill named '{name}' already exists."
        try:
            if not self._name_available(name):
                return False, f"The name '{name}' is already used by a built-in tool."
        except Exception:
            pass
        return True, ""

    # ── execution ──────────────────────────────────────────────────────────

    def make_handler(self, name: str) -> Callable[[dict], str]:
        """A synchronous handler for ActionRegistry.register_dynamic.

        Runs the skill's ``execute`` in a fresh worker thread with a hard
        timeout, so a generated skill that hangs can never wedge the
        assistant's tool-executor pool."""
        def _handler(parameters: dict, **ctx) -> str:
            return self._execute(name, parameters or {})
        return _handler

    def _execute(self, name: str, params: dict) -> str:
        rec = self.get(name)
        if rec is None or not rec.valid:
            return f"Skill '{name}' is not available."
        if not rec.active:
            return f"Skill '{name}' is currently disabled."

        module = self._load_module(rec)
        if module is None:
            return f"Skill '{name}' could not be loaded: {rec.last_error}"

        fn = getattr(module, "execute", None)
        if not callable(fn):
            return f"Skill '{name}' has no execute function."

        with self._lock:
            rec.invocations += 1

        try:
            ok, result = _invoke_with_timeout(fn, params, EXEC_TIMEOUT_SECONDS)
            if not ok:
                rec.last_error = str(result)
                return str(result)
            rec.last_error = ""
            if result is None:
                return "Done."
            if isinstance(result, dict) and result.get("error"):
                msg = str(result["error"])
                rec.last_error = msg
                return f"Skill '{name}' reported an error: {msg}"
            return str(result)
        except Exception as e:
            rec.last_error = str(e)
            self._logger(f"Skill '{name}' crashed: {e}")
            return f"Skill '{name}' failed: {e}"
        finally:
            self._persist_invocation(rec)

    def _persist_invocation(self, rec: SkillRecord) -> None:
        """Best-effort write of the counter (and any last_error) to the sidecar."""
        if self.get(rec.name) is not rec:
            return  # deleted mid-call; don't resurrect the sidecar
        try:
            side_dir = self._dir / rec.name
            side_dir.mkdir(parents=True, exist_ok=True)
            sidecar_path = side_dir / "manifest.json"
            sidecar = {}
            if sidecar_path.exists():
                sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            sidecar["invocations"] = rec.invocations
            sidecar["last_error"] = rec.last_error or ""
            sidecar_path.write_text(json.dumps(sidecar, indent=4), encoding="utf-8")
        except Exception as e:
            self._logger(f"Skill '{rec.name}': could not persist invocation — {e}")

    def _load_module(self, rec: SkillRecord):
        if rec.name in self._modules:
            return self._modules[rec.name]
        code_path = self._dir / rec.file
        if not code_path.exists():
            rec.last_error = f"missing file {rec.file}"
            return None
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                f"jarvis_skill_{rec.name}", code_path)
            if spec is None or spec.loader is None:
                raise ImportError("could not build import spec")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self._modules[rec.name] = module
            return module
        except Exception as e:
            rec.last_error = str(e)
            self._logger(f"Skill '{rec.name}' failed to import: {e}")
            return None

    # ── mutation (called by the forge action) ──────────────────────────────

    def register(self, name: str, description: str, parameters: dict,
                 code: str) -> SkillRecord:
        """Commits a freshly forged skill to features/ and loads it.

        Refuses to overwrite an existing skill file."""
        ok, reason = self.check_name(name)
        if not ok:
            raise ValueError(reason)

        py_file = self._dir / f"{name}.py"
        side_dir = self._dir / name
        if py_file.exists() or side_dir.exists():
            raise ValueError(f"A skill named '{name}' already exists on disk.")

        meta = {
            "name": name,
            "description": description.strip(),
            "parameters": parameters if isinstance(parameters, dict) else dict(_DEFAULT_PARAMS),
            "created_at": time.time(),
            "version": "1.0.0",
            "author": "JARVIS Skill Forge",
            "active": True,
        }
        header = (
            f'"""\n'
            f'Skill: {name}\n'
            f'Description: {description.strip()}\n'
            f'Forged by JARVIS Skill Forge (sandbox-verified).\n'
            f'"""\n\n'
            f'FEATURE_METADATA = {repr(meta)}\n\n'
        )
        side_dir.mkdir(parents=True, exist_ok=True)
        try:
            py_file.write_text(header + code, encoding="utf-8")
            with open(side_dir / "manifest.json", "w", encoding="utf-8") as f:
                json.dump({**meta, "invocations": 0, "last_error": ""}, f, indent=4)
        except Exception as e:
            shutil.rmtree(side_dir, ignore_errors=True)
            py_file.unlink(missing_ok=True)
            raise ValueError(f"Failed to write skill files: {e}")

        rec = self._load_file(py_file)
        if rec is None or not rec.valid:
            raise ValueError(f"Skill '{name}' was written but did not load: "
                             f"{rec.error if rec else 'unknown'}")
        with self._lock:
            self._skills[name] = rec
        self._notify(f"SYS: New skill forged and activated: {name}")
        self._fire_on_change(name, rec)
        return rec

    def toggle(self, name: str, active: Optional[bool] = None) -> bool:
        """Enable/disable; the new state persists in the sidecar manifest."""
        rec = self.get(name)
        if rec is None or not rec.valid:
            return False
        new_status = (not rec.active) if active is None else bool(active)
        with self._lock:
            rec.active = new_status
        try:
            side_dir = self._dir / name
            side_dir.mkdir(parents=True, exist_ok=True)
            sidecar_path = side_dir / "manifest.json"
            sidecar = {}
            if sidecar_path.exists():
                sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            sidecar["active"] = new_status
            sidecar_path.write_text(json.dumps(sidecar, indent=4), encoding="utf-8")
        except Exception as e:
            self._logger(f"Skill '{name}': could not persist toggle — {e}")
            return False
        self._notify(f"SYS: Skill '{name}' {'enabled' if new_status else 'disabled'}.")
        self._fire_on_change(name, rec)
        return True

    def delete(self, name: str) -> bool:
        """Removes the skill file, its sidecar directory, and the live record."""
        rec = self.get(name)
        if rec is None:
            return False
        py_file = self._dir / f"{name}.py"
        side_dir = self._dir / name
        try:
            py_file.unlink(missing_ok=True)
            shutil.rmtree(side_dir, ignore_errors=True)
        except Exception as e:
            self._logger(f"Skill '{name}': delete failed — {e}")
            return False
        with self._lock:
            self._skills.pop(name, None)
            self._modules.pop(name, None)
        self._notify(f"SYS: Skill deleted: {name}")
        self._fire_on_change(name, None)
        return True

    def _fire_on_change(self, name: str, rec: Optional[SkillRecord]) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(name, rec)
        except Exception as e:
            self._logger(f"Skill change callback failed for '{name}': {e}")


def _invoke_with_timeout(fn: Callable, params: dict, timeout: float):
    """Runs the skill on a daemon thread and joins with a hard deadline.

    A daemon thread (rather than a thread pool) because a timed-out skill
    cannot be killed — the thread is simply abandoned, and being a daemon it
    can never keep the process alive on exit. Returns (ok, result_or_error)."""
    box: Dict[str, Any] = {}

    def _run() -> None:
        try:
            box["result"] = _invoke(fn, params)
        except Exception as e:  # surfaced to the caller, not swallowed
            box["error"] = e

    thread = threading.Thread(target=_run, daemon=True, name="jarvis-skill-exec")
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return False, f"timed out after {timeout:.0f} seconds"
    if "error" in box:
        raise box["error"]
    return True, box.get("result")


def _invoke(fn: Callable, params: dict) -> Any:
    """Runs the skill's execute on this (worker) thread; async skills get a
    private event loop, matching Brahma's execute_sync contract."""
    if inspect.iscoroutinefunction(fn):
        return asyncio.run(fn(**params))
    return fn(**params)
