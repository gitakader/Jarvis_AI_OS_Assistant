"""
smart_home.py — Smart Home & IoT Automation for MARK LV.

Provides voice and tool-driven control for:
1. Tuya smart devices (bulbs, smart plugs, switches) using tinytuya (local LAN control, zero cloud latency).
2. MQTT integration (Home Assistant, Tasmota, Zigbee2MQTT) using paho-mqtt.
3. Local network device discovery and state monitoring.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent

_CONFIG_PATH = _BASE / "config" / "api_keys.json"
_DEVICES_PATH = _BASE / "config" / "smart_devices.json"

try:
    import tinytuya
    _TINYTUYA = True
except ImportError:
    _TINYTUYA = False

try:
    import paho.mqtt.client as mqtt
    _MQTT = True
except ImportError:
    _MQTT = False


def _load_devices() -> dict:
    """Load user's configured smart home devices."""
    if _DEVICES_PATH.exists():
        try:
            return json.loads(_DEVICES_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {
        "tuya_devices": {},
        "mqtt_settings": {
            "broker": "localhost",
            "port": 1883,
            "username": "",
            "password": "",
            "prefix": "homeassistant"
        }
    }


def _save_devices(data: dict) -> bool:
    try:
        _DEVICES_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DEVICES_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        print(f"[SmartHome] ⚠️ Failed saving smart_devices.json: {e}")
        return False


def tuya_scan(poll: bool = True) -> str:
    """Scan the local network for Tuya smart devices."""
    if not _TINYTUYA:
        return "tinytuya is not installed in the environment."
    try:
        devices = tinytuya.deviceScan(verbose=False, maxretry=2)
        if not devices:
            return "No Tuya devices found on the local subnet."
        
        summary = []
        cfg = _load_devices()
        known = cfg.setdefault("tuya_devices", {})
        
        for ip, dev in devices.items():
            dev_id = dev.get("gwId", dev.get("ip", ip))
            name = dev.get("name", f"Device-{ip}")
            summary.append(f"• IP: {ip} | ID: {dev_id} | Product: {dev.get('productKey', 'N/A')}")
            if dev_id not in known:
                known[dev_id] = {
                    "ip": ip,
                    "name": name,
                    "version": dev.get("version", "3.3"),
                    "key": ""
                }
        _save_devices(cfg)
        return f"Found {len(devices)} Tuya device(s) on local network:\n" + "\n".join(summary)
    except Exception as e:
        return f"Tuya device scan failed: {e}"


def tuya_control(device_name_or_id: str, command: str, value: Any = None) -> str:
    """Control a Tuya device locally (on, off, toggle, status, brightness, color)."""
    if not _TINYTUYA:
        return "tinytuya is not installed in the environment."
    
    cfg = _load_devices()
    devices = cfg.get("tuya_devices", {})
    
    target_info = None
    target_id = None
    
    # Match by name or ID
    for dev_id, info in devices.items():
        if dev_id.lower() == device_name_or_id.lower() or info.get("name", "").lower() == device_name_or_id.lower():
            target_info = info
            target_id = dev_id
            break
            
    if not target_info:
        return f"Device '{device_name_or_id}' not found in smart_devices.json. Run tuya_scan first or register it."

    ip = target_info.get("ip")
    key = target_info.get("key", "")
    version = float(target_info.get("version", 3.3))
    
    try:
        dev = tinytuya.OutletDevice(target_id, ip, key)
        dev.set_version(version)
        dev.set_socketPersistent(False)
        
        cmd = command.lower().strip()
        if cmd == "on":
            dev.turn_on()
            return f"Turned ON {target_info.get('name', target_id)}."
        elif cmd == "off":
            dev.turn_off()
            return f"Turned OFF {target_info.get('name', target_id)}."
        elif cmd == "status":
            st = dev.status()
            return f"Status for {target_info.get('name', target_id)}: {json.dumps(st)}"
        elif cmd == "toggle":
            st = dev.status()
            dps = st.get("dps", {})
            current = dps.get("1", False)
            if current:
                dev.turn_off()
                return f"Toggled OFF {target_info.get('name', target_id)}."
            else:
                dev.turn_on()
                return f"Toggled ON {target_info.get('name', target_id)}."
        else:
            return f"Unsupported command '{command}'. Use on | off | toggle | status."
    except Exception as e:
        return f"Failed controlling {device_name_or_id}: {e}"


def mqtt_publish(topic: str, payload: str) -> str:
    """Publish a command to an MQTT topic for Home Assistant or IoT devices."""
    if not _MQTT:
        return "paho-mqtt is not installed in the environment."
    
    cfg = _load_devices()
    mcfg = cfg.get("mqtt_settings", {})
    broker = mcfg.get("broker", "localhost")
    port = int(mcfg.get("port", 1883))
    username = mcfg.get("username", "")
    password = mcfg.get("password", "")

    try:
        client = mqtt.Client()
        if username:
            client.username_pw_set(username, password)
        client.connect(broker, port, keepalive=10)
        client.publish(topic, payload)
        client.disconnect()
        return f"Published to MQTT '{topic}': {payload}"
    except Exception as e:
        return f"MQTT publish failed: {e}"


def list_devices() -> str:
    """List all registered smart home devices."""
    cfg = _load_devices()
    tuya = cfg.get("tuya_devices", {})
    mqtt_s = cfg.get("mqtt_settings", {})
    
    lines = [f"Registered Tuya Devices ({len(tuya)}):"]
    for dev_id, info in tuya.items():
        name = info.get("name", "Unnamed")
        ip = info.get("ip", "Unknown IP")
        has_key = "✓ Key Configured" if info.get("key") else "⚠️ No Local Key"
        lines.append(f"  • {name} [{dev_id}] @ {ip} ({has_key})")
    
    broker = mqtt_s.get("broker", "localhost")
    lines.append(f"\nMQTT Broker: {broker}:{mqtt_s.get('port', 1883)}")
    return "\n".join(lines)


def smart_home(params: dict, player=None, session_memory=None) -> str:
    action = params.get("action", "").lower().strip()
    
    if action == "tuya_scan":
        return tuya_scan()
    elif action in ("tuya_control", "tuya_toggle", "turn_on", "turn_off"):
        dev = params.get("device", "")
        cmd = params.get("command", "")
        if action == "turn_on":
            cmd = "on"
        elif action == "turn_off":
            cmd = "off"
        elif action == "tuya_toggle":
            cmd = "toggle"
        return tuya_control(dev, cmd)
    elif action == "mqtt_publish":
        topic = params.get("topic", "")
        payload = params.get("payload", "")
        return mqtt_publish(topic, payload)
    elif action in ("list_devices", "list"):
        return list_devices()
    else:
        return f"Unknown smart_home action: '{action}'. Available: tuya_scan | tuya_control | mqtt_publish | list_devices"


TOOL = {
    "name": "smart_home",
    "description": "Smart Home & IoT automation: control Tuya smart lights, plugs, relays, query state, scan local devices, and publish to Home Assistant via MQTT.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "tuya_scan | tuya_control | turn_on | turn_off | tuya_toggle | mqtt_publish | list_devices"
            },
            "device": {
                "type": "STRING",
                "description": "Name or ID of the smart device (e.g. 'desk_lamp', 'office_plug')"
            },
            "command": {
                "type": "STRING",
                "description": "Command for device: on | off | toggle | status"
            },
            "topic": {
                "type": "STRING",
                "description": "MQTT topic (e.g. 'homeassistant/light/office/set')"
            },
            "payload": {
                "type": "STRING",
                "description": "MQTT payload to send (e.g. '{\"state\": \"ON\"}')"
            }
        },
        "required": ["action"]
    },
    "handler": smart_home,
}
