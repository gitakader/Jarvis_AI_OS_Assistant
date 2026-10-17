"""
provider_router.py — Let the AI manage the Smart Provider Router & Fallback
config (config/providers.json) over voice and text.

This is the voice/text twin of the ⚡ PROVIDER ROUTER & FALLBACK overlay:
providers, multi-key pools, rotation strategy, cooldown, and the fallback
ladders (chat / vision / fast / search).

Safety rules baked in:
- API keys are NEVER echoed back in full — only the count and the last four
  characters, so results are safe to read aloud.
- Ladder rungs must reference a registered provider and one of its listed
  models, exactly like the overlay's pickers.
- All mutations go through the ModelRouter singleton (read-modify-write +
  persist), so the live router state and providers.json can never disagree,
  and changes take effect on the very next dispatch — no restart needed.
"""
from __future__ import annotations

from core.model_router import router

_TIERS = ("chat", "vision", "fast", "search")
_TYPES = ("openai", "gemini", "ollama", "search")

# ── Handler ──────────────────────────────────────────────────────────────────

def provider_router(parameters):
    action = (parameters.get("action") or "get").strip().lower()
    try:
        if action == "get":
            return _get(parameters)
        if action in ("enable", "disable"):
            return _set_enabled(parameters.get("provider"), action == "enable")
        if action == "add_key":
            return _add_key(parameters.get("provider"), parameters.get("key"))
        if action == "remove_key":
            return _remove_key(parameters.get("provider"),
                               parameters.get("key"),
                               parameters.get("key_index"))
        if action == "set_rotation":
            return _set_rotation(parameters.get("rotation"))
        if action == "set_cooldown":
            return _set_cooldown(parameters.get("cooldown_seconds"))
        if action == "set_ladder":
            return _set_ladder(parameters.get("tier"), parameters.get("rungs"))
        if action == "add_rung":
            return _add_rung(parameters.get("tier"),
                             parameters.get("rung_provider"),
                             parameters.get("rung_model"))
        if action == "remove_rung":
            return _remove_rung(parameters.get("tier"),
                                parameters.get("rung_provider"),
                                parameters.get("rung_model"),
                                parameters.get("rung_index"))
        if action == "move_rung":
            return _move_rung(parameters.get("tier"),
                              parameters.get("rung_provider"),
                              parameters.get("rung_model"),
                              parameters.get("rung_index"),
                              parameters.get("direction"))
        if action == "add_provider":
            return _add_provider(parameters)
        if action == "edit_provider":
            return _edit_provider(parameters)
        if action == "remove_provider":
            return _remove_provider(parameters.get("provider"))
        if action == "test":
            return _test_provider(parameters.get("provider"))
        if action == "test_ladder":
            return _test_ladder(parameters.get("tier"))
    except Exception as e:  # never crash the session over a config edit
        return f"Provider router operation failed: {e}"
    return (f"Unknown action '{action}'. Use get, enable, disable, add_key, "
            f"remove_key, set_rotation, set_cooldown, set_ladder, add_rung, "
            f"remove_rung, move_rung, add_provider, edit_provider, "
            f"remove_provider, test, or test_ladder.")


# ── Get / status ─────────────────────────────────────────────────────────────

def _mask_keys(keys) -> str:
    """'4 keys' or '4 keys (…AbCd, …EfGh)' — never the full values."""
    keys = [str(k) for k in (keys or []) if str(k).strip()]
    if not keys:
        return "no keys"
    tails = ", ".join(f"…{k[-4:]}" for k in keys)
    return f"{len(keys)} key(s) ({tails})"


def _get(parameters) -> str:
    cfg = router.get_config()
    want = (parameters.get("provider") or "").strip().lower()

    lines = ["Provider Router status:"]
    providers = cfg.get("providers", {})
    for pid, p in providers.items():
        if want and pid != want:
            continue
        state = "ON" if p.get("enabled", True) else "OFF"
        models = ", ".join(p.get("models", []) or [])
        lines.append(
            f"- {pid} — {p.get('name', pid)} [{p.get('type', '')}] [{state}] · "
            f"{_mask_keys(p.get('api_keys'))}"
            + (f" · models: {models}" if models else ""))
    if want and not any(pid == want for pid in providers):
        return (f"Unknown provider '{want}'. "
                f"Registered: {', '.join(providers)}")

    if not want:
        lines.append("Routing ladders:")
        for tier in _TIERS:
            rungs = cfg.get("routing", {}).get(tier, [])
            if not rungs:
                lines.append(f"- {tier}: (empty)")
                continue
            steps = []
            for i, r in enumerate(rungs, 1):
                steps.append(f"{i}) {r.get('provider')}/{r.get('model')}")
            lines.append(f"- {tier}: " + "  ".join(steps))

        opts = cfg.get("options", {})
        lines.append(
            f"Options: cooldown={opts.get('cooldown_seconds', 300)}s, "
            f"key_rotation={opts.get('key_rotation', 'failover')}")
    return "\n".join(lines)


# ── Provider enable/disable ──────────────────────────────────────────────────

def _set_enabled(pid, enabled: bool) -> str:
    pid = _need_provider(pid)
    def _apply(cfg):
        cfg["providers"][pid]["enabled"] = bool(enabled)
    _commit(_apply)
    return f"Provider '{pid}' {'enabled' if enabled else 'disabled'}."


# ── Key pool ─────────────────────────────────────────────────────────────────

def _add_key(pid, key) -> str:
    pid = _need_provider(pid)
    key = str(key or "").strip()
    if not key:
        return "No key value given — add_key needs provider and key."
    def _apply(cfg):
        keys = cfg["providers"][pid].setdefault("api_keys", [])
        if key in keys:
            raise ValueError(f"that key is already in the pool for '{pid}'")
        keys.append(key)
    _commit(_apply)
    return (f"Key …{key[-4:]} added to '{pid}' "
            f"({len(router.get_config()['providers'][pid]['api_keys'])} total).")


def _remove_key(pid, key, index) -> str:
    pid = _need_provider(pid)
    idx = _as_int(index)
    key = (key or "").strip()

    def _find(keys):
        if idx is not None:
            if not 1 <= idx <= len(keys):
                raise ValueError(f"no key at position {idx} "
                                 f"(pool has {len(keys)})")
            return idx - 1
        if key:
            matches = [i for i, k in enumerate(keys)
                       if k == key or (len(key) == 4 and k.endswith(key))]
            if not matches:
                raise ValueError("no key matches — give the full key or "
                                 "its last 4 characters")
            if len(matches) > 1:
                raise ValueError("several keys end with those characters — "
                                 "give more of the key or its position")
            return matches[0]
        raise ValueError("give the key's position (key_index), the full key, "
                         "or its last 4 characters")

    removed = {}
    def _apply(cfg):
        keys = cfg["providers"][pid].setdefault("api_keys", [])
        i = _find(keys)
        removed["key"] = keys.pop(i)
    _commit(_apply)
    return (f"Key …{removed['key'][-4:]} removed from '{pid}' "
            f"({len(router.get_config()['providers'][pid]['api_keys'])} left).")


# ── Options ──────────────────────────────────────────────────────────────────

def _set_rotation(value) -> str:
    v = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if v in ("failover", "quota", "on_quota"):
        rot = "failover"
    elif v in ("round_robin", "roundrobin", "load_balance", "loadbalance"):
        rot = "round_robin"
    else:
        return f"Unknown rotation strategy '{value}'. Use 'failover' or 'round_robin'."
    def _apply(cfg):
        cfg.setdefault("options", {})["key_rotation"] = rot
    _commit(_apply)
    return f"Key rotation set to {rot}."


def _set_cooldown(seconds) -> str:
    secs = _as_int(seconds)
    if secs is None or not 0 <= secs <= 86400:
        return "Cooldown must be a number of seconds between 0 and 86400."
    def _apply(cfg):
        cfg.setdefault("options", {})["cooldown_seconds"] = secs
    _commit(_apply)
    return f"Provider cooldown set to {secs} seconds."


# ── Ladders ──────────────────────────────────────────────────────────────────

def _set_ladder(tier, rungs) -> str:
    tier = _need_tier(tier)
    ladder = []
    if not isinstance(rungs, list) or not rungs:
        return "set_ladder needs a 'rungs' list (provider + model per rung)."
    for r in rungs:
        p = str((r or {}).get("provider", "")).strip().lower()
        m = str((r or {}).get("model", "")).strip()
        _check_rung(p, m)
        ladder.append({"provider": p, "model": m})
    def _apply(cfg):
        cfg.setdefault("routing", {})[tier] = ladder
    _commit(_apply)
    steps = ", ".join(f"{p}/{m}" for p, m in
                      [(r["provider"], r["model"]) for r in ladder])
    return f"{tier} ladder set: {steps}."


def _add_rung(tier, p, m) -> str:
    tier = _need_tier(tier)
    p = _need_provider(p)
    models = router.get_config()["providers"][p].get("models", []) or []
    if m:
        m = str(m).strip()
        if models and m not in models:
            return f"'{m}' is not a listed model for '{p}'. Options: {', '.join(models)}"
    else:
        if not models:
            return f"Provider '{p}' lists no models — give one explicitly."
        m = models[0]
    def _apply(cfg):
        cfg.setdefault("routing", {}).setdefault(tier, []).append(
            {"provider": p, "model": m})
    _commit(_apply)
    return f"Added {p}/{m} to the {tier} ladder."


def _find_rung_index(cfg, tier, p, m, index) -> int:
    ladder = cfg.get("routing", {}).get(tier, [])
    idx = _as_int(index)
    if idx is not None:
        if not 1 <= idx <= len(ladder):
            raise ValueError(f"no rung at position {idx} "
                             f"(ladder has {len(ladder)})")
        return idx - 1
    p = (p or "").strip().lower()
    for i, r in enumerate(ladder):
        if r.get("provider") == p and (not m or r.get("model") == m):
            return i
    raise ValueError(f"no rung found for provider '{p}'"
                     + (f"/model '{m}'" if m else ""))


def _remove_rung(tier, p, m, index) -> str:
    tier = _need_tier(tier)
    removed = {}
    def _apply(cfg):
        i = _find_rung_index(cfg, tier, p, m, index)
        removed["rung"] = cfg["routing"][tier].pop(i)
    _commit(_apply)
    r = removed["rung"]
    return f"Removed {r.get('provider')}/{r.get('model')} from the {tier} ladder."


def _move_rung(tier, p, m, index, direction) -> str:
    tier = _need_tier(tier)
    d = str(direction or "up").strip().lower()
    if d not in ("up", "down"):
        return "direction must be 'up' or 'down'."
    def _apply(cfg):
        ladder = cfg["routing"].get(tier, [])
        i = _find_rung_index(cfg, tier, p, m, index)
        j = i - 1 if d == "up" else i + 1
        if j < 0 or j >= len(ladder):
            raise ValueError("that rung is already at the "
                             + ("top" if d == "up" else "bottom"))
        ladder[i], ladder[j] = ladder[j], ladder[i]
    _commit(_apply)
    return f"Moved rung {d} on the {tier} ladder."


# ── Provider registry ────────────────────────────────────────────────────────

def _add_provider(par) -> str:
    pid = _new_id(par.get("provider"))
    ptype = str(par.get("type") or "openai").strip().lower()
    if ptype not in _TYPES:
        return f"Invalid type '{ptype}'. Options: {', '.join(_TYPES)}"
    models = [m.strip() for m in str(par.get("models") or "").split(",")
              if m.strip()]
    key = str(par.get("key") or "").strip()
    def _apply(cfg):
        if pid in cfg.get("providers", {}):
            raise ValueError(f"provider '{pid}' already exists — use edit_provider")
        cfg.setdefault("providers", {})[pid] = {
            "name": str(par.get("name") or pid).strip(),
            "type": ptype,
            "base_url": str(par.get("base_url") or "").strip(),
            "api_keys": [key] if key else [],
            "models": models,
            "enabled": True,
        }
    _commit(_apply)
    return (f"Provider '{pid}' added"
            + (f" with key …{key[-4:]}" if key else "")
            + (f" and {len(models)} model(s)" if models else "")
            + ".")


def _edit_provider(par) -> str:
    pid = _need_provider(par.get("provider"))
    fields = {k: par.get(k) for k in ("name", "type", "base_url", "models", "enabled")}
    if not any(v is not None for v in fields.values()):
        return "edit_provider needs at least one field to change."
    ptype = fields.get("type")
    if ptype is not None:
        ptype = str(ptype).strip().lower()
        if ptype not in _TYPES:
            return f"Invalid type '{ptype}'. Options: {', '.join(_TYPES)}"
    models = None
    if fields.get("models") is not None:
        models = [m.strip() for m in str(fields["models"]).split(",")
                  if m.strip()]
    enabled = fields.get("enabled")
    if enabled is not None:
        enabled = bool(enabled)

    def _apply(cfg):
        p = cfg["providers"][pid]
        if fields.get("name") is not None:
            p["name"] = str(fields["name"]).strip() or pid
        if ptype is not None:
            p["type"] = ptype
        if fields.get("base_url") is not None:
            p["base_url"] = str(fields["base_url"]).strip()
        if models is not None:
            p["models"] = models
        if enabled is not None:
            p["enabled"] = enabled
    _commit(_apply)
    return f"Provider '{pid}' updated."


def _remove_provider(pid) -> str:
    pid = _need_provider(pid)
    refs = 0
    for tier in _TIERS:
        refs += sum(1 for r in router.get_config().get("routing", {}).get(tier, [])
                    if r.get("provider") == pid)
    def _apply(cfg):
        del cfg["providers"][pid]
    _commit(_apply)
    warn = (f" Note: {refs} ladder rung(s) still referenced it — "
            f"they will be skipped until removed." if refs else "")
    return f"Provider '{pid}' removed.{warn}"


# ── Diagnostics ──────────────────────────────────────────────────────────────

def _test_provider(pid) -> str:
    pid = _need_provider(pid)
    ok, msg = router.test_provider(pid)
    return f"Test {'passed' if ok else 'failed'} for '{pid}': {msg}"


def _test_ladder(tier) -> str:
    tier = _need_tier(tier)
    reply, info = router.dispatch_chat(
        "Test prompt. Respond with OK.", tier=tier, timeout=20)
    if reply:
        return (f"{tier} ladder OK — handled by {info}. "
                f"Reply: {reply[:120]}")
    return f"All rungs on the {tier} ladder failed or timed out."


# ── Helpers ──────────────────────────────────────────────────────────────────

def _need_provider(pid) -> str:
    pid = str(pid or "").strip().lower()
    if not pid:
        raise ValueError("no provider given")
    if pid not in router.get_config().get("providers", {}):
        raise ValueError(f"unknown provider '{pid}' — registered: "
                         + ", ".join(router.get_config()["providers"]))
    return pid


def _need_tier(tier) -> str:
    tier = str(tier or "").strip().lower()
    if tier not in _TIERS:
        raise ValueError(f"unknown tier '{tier}'. Use {', '.join(_TIERS)}")
    return tier


def _new_id(pid) -> str:
    pid = str(pid or "").strip().lower()
    if not pid or not all(c.isalnum() or c in "_-" for c in pid):
        raise ValueError("provider id must be letters, digits, '-' or '_'")
    return pid


def _check_rung(p, m) -> None:
    if p not in router.get_config().get("providers", {}):
        raise ValueError(f"unknown provider '{p}' in rung")
    models = router.get_config()["providers"][p].get("models", []) or []
    if models and m not in models:
        raise ValueError(f"'{m}' is not a listed model for '{p}'. "
                         f"Options: {', '.join(models)}")


def _commit(apply_fn) -> None:
    """Read-modify-write through the router singleton: the live state and
    providers.json are updated together, under the router's own lock."""
    cfg = router.get_config()
    apply_fn(cfg)
    if not router.update_config(cfg):
        raise RuntimeError("failed to save providers.json")


def _as_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── Tool declaration (auto-discovered by action_loader) ──────────────────────

TOOL = {
    "name": "provider_router",
    "description": (
        "Manage JARVIS's smart provider router and fallback configuration: "
        "which AI providers are enabled, their API key pools, key rotation "
        "strategy, cooldown, and the fallback order (ladders) for chat, "
        "vision, fast and search tasks. API keys are never shown in full — "
        "only counts and the last 4 characters. "
        "Use action='get' to see the current setup. "
        "Other actions: enable/disable (provider), add_key/remove_key "
        "(provider + key or key_index), set_rotation (failover/round_robin), "
        "set_cooldown (seconds), set_ladder (tier + rungs list), "
        "add_rung/remove_rung/move_rung (tier + rung_provider + rung_model "
        "or rung_index), add_provider/edit_provider/remove_provider, "
        "test (a provider connection), test_ladder (a tier). "
        "Call when the user says things like 'disable grok', "
        "'what providers are configured', 'put openrouter first on the chat "
        "ladder', 'add this key to openrouter: sk-…', 'remove the second "
        "grok key', 'switch to round robin key rotation', 'set the cooldown "
        "to 10 minutes', 'test the gemini provider', 'test my chat ladder', "
        "'add a provider called deepseek at api.deepseek.com', etc."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "The operation to perform (see the tool description).",
            },
            "provider": {
                "type": "STRING",
                "description": "Provider id (gemini, grok, openrouter, ollama, "
                               "ollama_cloud, lmstudio, tavily, ddg, huggingface, "
                               "or a custom one).",
            },
            "tier": {
                "type": "STRING",
                "description": "Capability tier: chat, vision, fast, or search.",
            },
            "key": {
                "type": "STRING",
                "description": "An API key to add, or the key (full value or last "
                               "4 characters) to remove.",
            },
            "key_index": {
                "type": "INTEGER",
                "description": "1-based position of the key to remove.",
            },
            "rungs": {
                "type": "ARRAY",
                "description": "For set_ladder: the ordered ladder as a list of "
                               "{provider, model} objects.",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "provider": {"type": "STRING"},
                        "model": {"type": "STRING"},
                    },
                    "required": ["provider", "model"],
                },
            },
            "rung_provider": {
                "type": "STRING",
                "description": "Provider of the ladder rung to add/remove/move.",
            },
            "rung_model": {
                "type": "STRING",
                "description": "Model of the ladder rung (defaults to the "
                               "provider's first listed model when adding).",
            },
            "rung_index": {
                "type": "INTEGER",
                "description": "1-based position of the rung to remove or move "
                               "(alternative to provider/model).",
            },
            "direction": {
                "type": "STRING",
                "description": "For move_rung: 'up' or 'down'.",
            },
            "name": {
                "type": "STRING",
                "description": "Display name (add_provider/edit_provider).",
            },
            "type": {
                "type": "STRING",
                "description": "Provider protocol: openai, gemini, ollama, or search.",
            },
            "base_url": {
                "type": "STRING",
                "description": "Endpoint URL, e.g. https://api.deepseek.com/v1",
            },
            "models": {
                "type": "STRING",
                "description": "Comma-separated model names.",
            },
            "enabled": {
                "type": "BOOLEAN",
                "description": "Provider on/off (edit_provider).",
            },
            "rotation": {
                "type": "STRING",
                "description": "Key rotation strategy: failover or round_robin.",
            },
            "cooldown_seconds": {
                "type": "INTEGER",
                "description": "How long a provider stays cooled down after a "
                               "quota error, in seconds.",
            },
        },
        "required": ["action"],
    },
    "handler": provider_router,
}
