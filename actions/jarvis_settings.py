"""
jarvis_settings.py — Let the AI read and change its own settings.

The model calls this tool when the user asks to change JARVIS configuration
(morning brief, mic mute, voice, wake word, push-to-talk, HUD style, audio
devices, names, colour, plugins, etc.) or asks about the current settings.

Runtime effects (mic mute, HUD switch, colour retheme, session rebuild) are
signalled via markers in the result string and handled by the post-processing
hook in main.py's _execute_tool.
"""
from __future__ import annotations

import re

from memory.config_manager import (
    get_brief_enabled, save_brief_enabled,
    get_voice, save_voice, AVAILABLE_VOICES,
    get_wake_word_enabled, save_wake_word_enabled,
    get_push_to_talk_enabled, save_push_to_talk_enabled,
    get_hud_style, save_hud_style, HUD_STYLES,
    get_thinking_enabled, save_thinking_enabled,
    get_proactive_audio_enabled, save_proactive_audio_enabled,
    get_media_resolution, save_media_resolution, MEDIA_RESOLUTIONS,
    get_assistant_name, get_user_name, save_assistant_config,
    get_input_device, save_input_device,
    get_output_device, save_output_device,
    get_ui_color, save_ui_color,
    get_plugin_enabled, save_plugin_enabled,
    get_sensorium_speech_enabled, save_sensorium_speech_enabled,
)

from core.audio_devices import list_devices, DEFAULT_LABEL

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

# ── Setting registry ─────────────────────────────────────────────────────────
# Each entry maps a setting key to its getter, setter, type, valid values,
# and whether it requires a session rebuild to take effect.

_SETTINGS: dict[str, dict] = {
    "morning_brief": {
        "label": "Morning briefing",
        "get": get_brief_enabled,
        "set": save_brief_enabled,
        "type": "bool",
        "rebuild": False,
    },
    "wake_word": {
        "label": "Wake word detection",
        "get": get_wake_word_enabled,
        "set": save_wake_word_enabled,
        "type": "bool",
        "rebuild": False,
        "runtime": "_wake_toggle",
    },
    "push_to_talk": {
        "label": "Push-to-talk",
        "get": get_push_to_talk_enabled,
        "set": save_push_to_talk_enabled,
        "type": "bool",
        "rebuild": False,
        "runtime": "_ptt_toggle",
    },
    "voice": {
        "label": "Voice",
        "get": get_voice,
        "set": save_voice,
        "type": "choice",
        "choices": AVAILABLE_VOICES,
        "rebuild": True,
        "rebuild_keep_context": False,  # voice change needs a fresh session
    },
    "hud_style": {
        "label": "HUD style",
        "get": get_hud_style,
        "set": save_hud_style,
        "type": "choice",
        "choices": list(HUD_STYLES),
        "rebuild": False,
        "runtime": "_hud_toggle",
    },
    "thinking": {
        "label": "Thinking mode",
        "get": get_thinking_enabled,
        "set": save_thinking_enabled,
        "type": "bool",
        "rebuild": True,
    },
    "proactive_audio": {
        "label": "Proactive audio",
        "get": get_proactive_audio_enabled,
        "set": save_proactive_audio_enabled,
        "type": "bool",
        "rebuild": True,
    },
    "sensorium_speech": {
        "label": "Sensorium spoken alerts",
        "get": get_sensorium_speech_enabled,
        "set": save_sensorium_speech_enabled,
        "type": "bool",
        "rebuild": False,
    },
    "media_resolution": {
        "label": "Media resolution",
        "get": get_media_resolution,
        "set": save_media_resolution,
        "type": "choice",
        "choices": list(MEDIA_RESOLUTIONS),
        "rebuild": True,
    },
    "input_device": {
        "label": "Microphone (input device)",
        "get": get_input_device,
        "set": save_input_device,
        "type": "device",
        "kind": "input",
        "rebuild": True,
        "rebuild_keep_context": True,  # streams reopen; the conversation is kept
    },
    "output_device": {
        "label": "Speakers (output device)",
        "get": get_output_device,
        "set": save_output_device,
        "type": "device",
        "kind": "output",
        "rebuild": True,
        "rebuild_keep_context": True,
    },
    "assistant_name": {
        "label": "Assistant name",
        "get": get_assistant_name,
        "set": lambda name: save_assistant_config(str(name), get_user_name()),
        "type": "text",
        "rebuild": True,
        "rebuild_keep_context": True,
    },
    "user_name": {
        "label": "Your name",
        "get": get_user_name,
        "set": lambda name: save_assistant_config(get_assistant_name(), str(name)),
        "type": "text",
        "rebuild": True,
        "rebuild_keep_context": True,
    },
    "ui_color": {
        "label": "UI accent colour",
        "get": get_ui_color,
        "set": save_ui_color,
        "type": "color",
        "rebuild": False,
        "runtime": "_ui_color",
    },
    "microphone": {
        "label": "Microphone",
        "get": None,   # live state — resolved from the UI handle at call time
        "set": None,   # handled via runtime callback
        "type": "bool",
        "rebuild": False,
        "runtime": "_mic_toggle",
    },
}


# ── Handler ──────────────────────────────────────────────────────────────────

def jarvis_settings(parameters, **kwargs):
    action = (parameters.get("action") or "get").strip().lower()
    setting = (parameters.get("setting") or "").strip().lower()
    value = parameters.get("value", "")

    ctx = {"player": kwargs.get("player"), "plugins": kwargs.get("plugins")}

    if action == "get":
        return _get_settings(setting, ctx)
    elif action == "set":
        return _set_setting(setting, value, ctx)
    else:
        return f"Unknown action '{action}'. Use 'get' or 'set'."


def _mic_state(player) -> str:
    """'muted' / 'active' from the live UI handle, or 'unknown' without one."""
    try:
        return "muted" if bool(player.muted) else "active"
    except Exception:
        return "unknown"


def _get_settings(setting: str, ctx: dict) -> str:
    """Return the current value of one or all settings."""
    player = ctx.get("player")

    if not setting or setting == "all":
        lines = []
        for key, cfg in _SETTINGS.items():
            if key == "microphone":
                lines.append(f"- {cfg['label']} (microphone): {_mic_state(player)}")
                continue
            try:
                val = cfg["get"]()
            except Exception:
                val = "unknown"
            if cfg["type"] == "device":
                val = val or DEFAULT_LABEL
            elif cfg["type"] == "color":
                val = val or "(default)"
            elif val == "":
                val = "(unset)"
            extras = ""
            if cfg["type"] == "choice":
                extras = f"  (options: {', '.join(cfg.get('choices', []))})"
            lines.append(f"- {cfg['label']} ({key}): {val}{extras}")
        _plugin_lines = _plugin_listing(ctx.get("plugins"))
        if _plugin_lines:
            lines.append("Plugins:")
            lines.extend(_plugin_lines)
        return "Current JARVIS settings:\n" + "\n".join(lines)

    # Live microphone state, resolved from the UI handle.
    if setting == "microphone":
        return f"Microphone: {_mic_state(player)}"

    # Plugin names are dynamic settings: get returns their enabled state.
    plugins = ctx.get("plugins")
    if plugins is not None and _plugin_name(setting, plugins):
        return (f"Plugin '{setting}': "
                f"{'enabled' if get_plugin_enabled(setting) else 'disabled'}")

    cfg = _SETTINGS.get(setting)
    if not cfg:
        return (f"Unknown setting '{setting}'. "
                f"Available: {', '.join(_SETTINGS.keys())}")
    if cfg["type"] == "device":
        val = cfg["get"]() or DEFAULT_LABEL
        options = ", ".join([DEFAULT_LABEL] + list_devices(cfg["kind"]))
        return f"{cfg['label']}: {val}  (options: {options})"
    try:
        val = cfg["get"]()
    except Exception:
        val = "unknown"
    if cfg["type"] == "choice":
        return (f"{cfg['label']}: {val}  "
                f"(options: {', '.join(cfg.get('choices', []))})")
    return f"{cfg['label']}: {val}"


def _set_setting(setting: str, value, ctx: dict) -> str:
    """Persist a setting and return a result string with runtime markers."""
    plugins = ctx.get("plugins")

    # Plugin names are dynamic settings: enable/disable that plugin.
    if plugins is not None and _plugin_name(setting, plugins):
        val = _parse_bool(value)
        if val is None:
            return f"Could not parse '{value}' as on/off."
        save_plugin_enabled(setting, val)
        # Tools are filtered at connect time, so a rebuild applies it now.
        return (f"Plugin '{setting}' {'enabled' if val else 'disabled'}. "
                f"[REBUILD:keep=True]")

    cfg = _SETTINGS.get(setting)
    if not cfg:
        return (f"Unknown setting '{setting}'. "
                f"Available: {', '.join(_SETTINGS.keys())}")

    # Parse value according to type
    if cfg["type"] == "bool":
        val = _parse_bool(value)
        if val is None:
            return f"Could not parse '{value}' as on/off."
    elif cfg["type"] == "choice":
        choices = cfg.get("choices", [])
        val = _match_choice(str(value), choices)
        if val is None:
            return (f"Invalid value '{value}' for {cfg['label']}. "
                    f"Options: {', '.join(choices)}")
    elif cfg["type"] == "device":
        val = _match_device(str(value), cfg["kind"])
        if val is None:
            return (f"Invalid value '{value}' for {cfg['label']}. "
                    f"Options: {DEFAULT_LABEL}, "
                    f"{', '.join(list_devices(cfg['kind']))}")
    elif cfg["type"] == "color":
        val = _parse_color(value)
        if val is None:
            return (f"Invalid colour '{value}'. "
                    f"Give a 6-digit hex like '#00aaff'.")
    else:  # text
        val = str(value).strip()
        if not val:
            return f"Refusing to set {cfg['label']} to an empty value."

    # Persist to config file
    setter = cfg.get("set")
    if setter:
        setter(val)

    # Build result with runtime markers for main.py's post-hook
    result_suffix = ""
    runtime_key = cfg.get("runtime")
    if runtime_key:
        result_suffix += f" [RUNTIME:{runtime_key}:{val}]"

    if cfg.get("rebuild"):
        keep = cfg.get("rebuild_keep_context", True)
        result_suffix += f" [REBUILD:keep={keep}]"

    return f"{cfg['label']} set to {val}.{result_suffix}"


# ── Plugin dynamic settings ──────────────────────────────────────────────────

def _plugin_name(setting: str, plugins) -> bool:
    """True if `setting` names a discovered plugin (registry.has)."""
    try:
        return bool(plugins is not None and plugins.has(setting))
    except Exception:
        return False


def _plugin_listing(plugins) -> list[str]:
    """One line per discovered plugin with its enabled state."""
    try:
        entries = plugins.list_for_ui() if plugins is not None else []
    except Exception:
        return []
    out = []
    for p in entries:
        if not p.get("valid"):
            continue
        state = "on" if get_plugin_enabled(p["name"]) else "off"
        out.append(f"- {p['name']} ({state})")
    return out


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_bool(value) -> bool | None:
    """Parse a variety of natural-language boolean strings."""
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "on", "1", "yes", "enable", "enabled",
             "activate", "activated", "unmute", "unmuted", "active"):
        return True
    if s in ("false", "off", "0", "no", "disable", "disabled",
             "deactivate", "deactivated", "mute", "muted", "inactive"):
        return False
    return None


def _match_choice(value: str, choices: list) -> str | None:
    """Case-insensitive exact or prefix match against valid choices."""
    v = value.strip().lower()
    # Exact match first
    for c in choices:
        if str(c).lower() == v:
            return c
    # Prefix match
    for c in choices:
        if str(c).lower().startswith(v):
            return c
    return None


def _match_device(value: str, kind: str) -> str | None:
    """Match a spoken device name. 'default' / 'system' means the OS default,
    stored as the empty string — same convention as the settings UI."""
    v = value.strip().lower()
    if v in ("default", "system", "system default", DEFAULT_LABEL.lower()):
        return ""
    return _match_choice(value, list_devices(kind))


def _parse_color(value) -> str | None:
    """Validate a hex colour; returns '#rrggbb' or None."""
    s = str(value).strip()
    if not _HEX_RE.match(s):
        return None
    return s if s.startswith("#") else f"#{s}"


# ── Tool declaration (auto-discovered by action_loader) ──────────────────────

TOOL = {
    "name": "jarvis_settings",
    "description": (
        "Read or change JARVIS's own settings. "
        "Use action='get' to see current settings (all or a specific one). "
        "Use action='set' with a setting name and value to change a setting. "
        "Settings: morning_brief (on/off), wake_word (on/off), "
        "push_to_talk (on/off), voice (Charon/Puck/Kore/Fenrir/Aoede), "
        "hud_style (face/core), thinking (on/off), proactive_audio (on/off), "
        "media_resolution (default/low/medium/high), microphone (mute/unmute), "
        "input_device (a microphone name, or 'default'), "
        "output_device (a speaker name, or 'default'), "
        "assistant_name (any text), user_name (any text), "
        "ui_color (a 6-digit hex colour like #00aaff), "
        "and plugins: use a discovered plugin's name as the setting to "
        "enable/disable it (e.g. setting='drive_manager', value='off'). "
        "Provider/router/API-key configuration is handled by the separate "
        "provider_router tool. "
        "Call when the user says things like 'turn off morning brief', "
        "'change voice to Puck', 'mute yourself', 'enable wake word', "
        "'switch to the reactor core HUD', 'use my headset microphone', "
        "'call me Steve', 'disable the drive plugin', "
        "'what are my settings', 'disable thinking', etc."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "get — read current settings | set — change a setting",
            },
            "setting": {
                "type": "STRING",
                "description": (
                    "Setting key: morning_brief, wake_word, push_to_talk, "
                    "voice, hud_style, thinking, proactive_audio, "
                    "media_resolution, microphone, input_device, "
                    "output_device, assistant_name, user_name, ui_color, "
                    "a plugin name, or 'all' to list everything"
                ),
            },
            "value": {
                "type": "STRING",
                "description": (
                    "New value. For booleans: on/off/true/false/enable/disable/mute/unmute. "
                    "For choices: one of the valid options (e.g. Puck, core, medium). "
                    "For devices: a device name or 'default'. "
                    "For ui_color: a 6-digit hex like '#00aaff'. "
                    "For assistant_name/user_name: any text."
                ),
            },
        },
        "required": ["action"],
    },
    "handler": jarvis_settings,
}
