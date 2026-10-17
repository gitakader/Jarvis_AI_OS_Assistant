"""
model_router.py — Smart Provider Router & Fallback Engine for MARK LV.

Manages:
1. Multi-provider registry (Gemini, xAI Grok, OpenRouter, DeepSeek, Ollama, LM Studio, Tavily, custom).
2. Multi-key quota pooling and automatic key rotation on 429 quota errors.
3. Configurable task-specific fallback ladders (chat, vision, fast, search).
4. Universal OpenAI-compatible, native Gemini, Ollama, and Search execution adapters.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import requests

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent

_CONFIG_PATH = _BASE / "config" / "providers.json"
_API_KEYS_LEGACY = _BASE / "config" / "api_keys.json"

_ROUTER_LOCK = threading.Lock()

# Default provider presets
DEFAULT_PROVIDERS = {
    "gemini": {
        "name": "Google Gemini",
        "type": "gemini",
        "base_url": "https://generativelanguage.googleapis.com",
        "api_keys": [],
        "models": [
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-live-preview"
        ],
        "enabled": True
    },
    "grok": {
        "name": "xAI Grok",
        "type": "openai",
        "base_url": "https://api.x.ai/v1",
        "api_keys": [],
        "models": [
            "grok-2-latest",
            "grok-2-vision-1212",
            "grok-beta"
        ],
        "enabled": True
    },
    "openrouter": {
        "name": "OpenRouter",
        "type": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "api_keys": [],
        "models": [
            "meta-llama/llama-3.3-70b-instruct",
            "anthropic/claude-3.5-sonnet",
            "deepseek/deepseek-chat",
            "google/gemini-2.0-flash-001"
        ],
        "enabled": True
    },
    "ollama": {
        "name": "Ollama (Local)",
        "type": "ollama",
        "base_url": "http://localhost:11434",
        "api_keys": [],
        "models": [
            "llama3.2:latest",
            "qwen2.5-coder:latest",
            "mistral:latest"
        ],
        "enabled": True
    },
    "lmstudio": {
        "name": "LM Studio (Local)",
        "type": "openai",
        "base_url": "http://localhost:1234/v1",
        "api_keys": [],
        "models": [
            "local-model"
        ],
        "enabled": True
    },
    "tavily": {
        "name": "Tavily Search",
        "type": "search",
        "base_url": "https://api.tavily.com",
        "api_keys": [],
        "models": [
            "tavily-search"
        ],
        "enabled": True
    },
    "ddg": {
        "name": "DuckDuckGo Search",
        "type": "search",
        "base_url": "https://duckduckgo.com",
        "api_keys": [],
        "models": [
            "duckduckgo"
        ],
        "enabled": True
    },
    "huggingface": {
        "name": "Hugging Face",
        "type": "openai",
        "base_url": "https://api-inference.huggingface.co/v1",
        "api_keys": [],
        "models": [
            "meta-llama/Llama-3.3-70B-Instruct",
            "meta-llama/Llama-3.1-8B-Instruct",
            "Qwen/Qwen2.5-72B-Instruct",
            "Qwen/Qwen2.5-Coder-32B-Instruct",
            "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B",
            "mistralai/Mistral-7B-Instruct-v0.3"
        ],
        "enabled": True
    }
}

DEFAULT_ROUTING = {
    "chat": [
        {"provider": "gemini", "model": "gemini-2.5-flash"},
        {"provider": "grok", "model": "grok-2-latest"},
        {"provider": "openrouter", "model": "meta-llama/llama-3.3-70b-instruct"},
        {"provider": "ollama", "model": "llama3.2:latest"}
    ],
    "vision": [
        {"provider": "gemini", "model": "gemini-2.5-flash"},
        {"provider": "grok", "model": "grok-2-vision-1212"},
        {"provider": "ollama", "model": "llama3.2:latest"}
    ],
    "fast": [
        {"provider": "gemini", "model": "gemini-2.5-flash-lite"},
        {"provider": "ollama", "model": "llama3.2:latest"}
    ],
    "search": [
        {"provider": "tavily", "model": "tavily-search"},
        {"provider": "ddg", "model": "duckduckgo"}
    ]
}


class ModelRouter:
    """Singleton router managing providers, key pools, and fallback ladders."""

    _instance: Optional[ModelRouter] = None

    def __new__(cls) -> ModelRouter:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_state()
        return cls._instance

    def _init_state(self) -> None:
        self._key_indices: dict[str, int] = {}       # provider -> active key index
        self._cooldowns: dict[str, float] = {}       # (provider:model or provider) -> cooldown expiry timestamp
        self._load_config()

    def _load_config(self) -> None:
        with _ROUTER_LOCK:
            if not _CONFIG_PATH.exists():
                self._config = {
                    "providers": dict(DEFAULT_PROVIDERS),
                    "routing": dict(DEFAULT_ROUTING),
                    "options": {
                        "cooldown_seconds": 300,
                        "key_rotation": "failover"  # "failover" | "round_robin"
                    }
                }
                self._sync_legacy_gemini_key()
                self._save_config_locked()
            else:
                try:
                    self._config = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
                    # Ensure all standard sections exist
                    if "providers" not in self._config:
                        self._config["providers"] = dict(DEFAULT_PROVIDERS)
                    if "routing" not in self._config:
                        self._config["routing"] = dict(DEFAULT_ROUTING)
                    if "options" not in self._config:
                        self._config["options"] = {"cooldown_seconds": 300, "key_rotation": "failover"}
                except Exception as e:
                    print(f"[ModelRouter] ⚠️ Failed reading providers.json: {e}")
                    self._config = {
                        "providers": dict(DEFAULT_PROVIDERS),
                        "routing": dict(DEFAULT_ROUTING),
                        "options": {"cooldown_seconds": 300, "key_rotation": "failover"}
                    }

    def _sync_legacy_gemini_key(self) -> None:
        """Import gemini_api_key from config/api_keys.json if present."""
        if _API_KEYS_LEGACY.exists():
            try:
                legacy = json.loads(_API_KEYS_LEGACY.read_text(encoding="utf-8"))
                k = legacy.get("gemini_api_key", "").strip()
                if k and k not in self._config["providers"].get("gemini", {}).get("api_keys", []):
                    self._config["providers"].setdefault("gemini", dict(DEFAULT_PROVIDERS["gemini"]))
                    self._config["providers"]["gemini"]["api_keys"].append(k)
            except Exception:
                pass

    def _save_config_locked(self) -> bool:
        try:
            _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            _CONFIG_PATH.write_text(json.dumps(self._config, indent=2), encoding="utf-8")
            return True
        except Exception as e:
            print(f"[ModelRouter] [FAIL] Failed saving providers.json: {e}")
            return False

    def save_config(self) -> bool:
        with _ROUTER_LOCK:
            return self._save_config_locked()

    def get_config(self) -> dict:
        with _ROUTER_LOCK:
            return json.loads(json.dumps(self._config))

    def update_config(self, new_cfg: dict) -> bool:
        with _ROUTER_LOCK:
            self._config = new_cfg
            return self._save_config_locked()

    # ── Key Pool Management ──────────────────────────────────────────────────

    def get_active_key(self, provider_id: str) -> str:
        """Retrieve the currently selected API key for a provider."""
        p = self._config.get("providers", {}).get(provider_id, {})
        keys = p.get("api_keys", [])
        if not keys:
            return ""
        idx = self._key_indices.get(provider_id, 0) % len(keys)
        return keys[idx]

    def rotate_key(self, provider_id: str, reason: str = "quota") -> bool:
        """Advance to the next available API key in the pool on 429."""
        p = self._config.get("providers", {}).get(provider_id, {})
        keys = p.get("api_keys", [])
        if len(keys) <= 1:
            return False  # No secondary key to rotate to
        
        curr_idx = self._key_indices.get(provider_id, 0)
        new_idx = (curr_idx + 1) % len(keys)
        self._key_indices[provider_id] = new_idx
        print(f"[ModelRouter] [ROTATE] Rotated {provider_id} key to index {new_idx}/{len(keys)} (reason: {reason})")
        return True

    # ── Cooldown Management ─────────────────────────────────────────────────

    def is_cooling(self, provider_id: str, model: str = "") -> bool:
        key = f"{provider_id}:{model}" if model else provider_id
        exp = self._cooldowns.get(key, 0.0)
        return time.time() < exp

    def set_cooldown(self, provider_id: str, model: str = "", seconds: float = 300.0) -> None:
        key = f"{provider_id}:{model}" if model else provider_id
        self._cooldowns[key] = time.time() + seconds
        print(f"[ModelRouter] [COOL] Set cooldown for {key} for {int(seconds)}s")

    # ── Universal Execution Adapters ────────────────────────────────────────

    def dispatch_chat(
        self,
        prompt: str,
        system: str = "",
        tier: str = "chat",
        images: list[bytes] | None = None,
        timeout: int = 40
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Dispatches request through the configured fallback ladder for 'tier'.
        Returns (response_text, answering_rung_info) or (None, None).
        """
        with _ROUTER_LOCK:
            ladder = list(self._config.get("routing", {}).get(tier, []))
            if not ladder:
                ladder = list(DEFAULT_ROUTING.get(tier, []))

        tried = []
        for rung in ladder:
            provider_id = rung.get("provider", "")
            model_name = rung.get("model", "")
            if not provider_id or not model_name:
                continue

            provider = self._config.get("providers", {}).get(provider_id, {})
            if not provider.get("enabled", True):
                continue

            if self.is_cooling(provider_id, model_name):
                continue

            ptype = provider.get("type", "openai")
            base_url = provider.get("base_url", "").rstrip("/")
            keys = provider.get("api_keys", [])

            # Attempt all keys in the pool if multiple are configured
            keys_to_try = len(keys) if keys else 1
            for key_attempt in range(keys_to_try):
                api_key = self.get_active_key(provider_id)
                try:
                    ans = self._execute_call(provider_id, ptype, base_url, api_key, model_name, prompt, system, images, timeout)
                    if ans:
                        info = f"{provider.get('name', provider_id)} ({model_name})"
                        return ans.strip(), info
                except Exception as e:
                    err_msg = str(e)
                    is_429 = "429" in err_msg or "quota" in err_msg.lower() or "RESOURCE_EXHAUSTED" in err_msg
                    
                    if is_429 and len(keys) > 1 and key_attempt < len(keys) - 1:
                        self.rotate_key(provider_id, reason="429 quota")
                        continue  # Try next key
                    else:
                        print(f"[ModelRouter] [FAIL] {provider_id}:{model_name} failed: {err_msg[:120]}")
                        if is_429:
                            self.set_cooldown(provider_id, model_name, self._config.get("options", {}).get("cooldown_seconds", 300))
                        break  # Fall down the ladder to next provider

        return None, None

    def _execute_call(
        self,
        provider_id: str,
        ptype: str,
        base_url: str,
        api_key: str,
        model: str,
        prompt: str,
        system: str,
        images: list[bytes] | None,
        timeout: int
    ) -> str:
        if ptype == "gemini":
            return self._call_gemini(api_key, model, prompt, system, images, timeout)
        elif ptype == "ollama":
            return self._call_ollama(base_url, api_key, model, prompt, system, images, timeout)
        elif ptype == "openai":
            return self._call_openai_compatible(base_url, api_key, model, prompt, system, images, timeout)
        elif ptype == "search":
            if provider_id == "ddg" or "duckduckgo" in base_url.lower():
                return self._call_ddg(prompt, timeout)
            return self._call_tavily(base_url, api_key, prompt, timeout)
        else:
            raise ValueError(f"Unknown provider type: {ptype}")

    def _call_gemini(
        self,
        api_key: str,
        model: str,
        prompt: str,
        system: str,
        images: list[bytes] | None = None,
        timeout: int = 40
    ) -> str:
        from google import genai
        from google.genai import types as gtypes
        
        cl = genai.Client(api_key=api_key, http_options=gtypes.HttpOptions(timeout=max(10000, timeout * 1000)))
        
        contents = []
        if images:
            for img in images:
                # Detect image MIME type
                mime = "image/png" if img.startswith(b"\x89PNG") else "image/jpeg"
                contents.append(gtypes.Part.from_bytes(data=img, mime_type=mime))
        contents.append(prompt)

        cfg_kwargs = {}
        if system:
            cfg_kwargs["system_instruction"] = system
            
        kwargs: dict = {"model": model, "contents": contents}
        if cfg_kwargs:
            kwargs["config"] = gtypes.GenerateContentConfig(**cfg_kwargs)

        resp = cl.models.generate_content(**kwargs)
        return (getattr(resp, "text", None) or "").strip()

    def _call_openai_compatible(
        self,
        base_url: str,
        api_key: str,
        model: str,
        prompt: str,
        system: str,
        images: list[bytes] | None,
        timeout: int
    ) -> str:
        clean_url = base_url.rstrip("/")
        if clean_url.endswith("/chat/completions"):
            endpoint = clean_url
        else:
            endpoint = f"{clean_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}" if api_key else ""
        }
        
        messages = []
        if system:
            messages.append({"role": "system", "content": system})

        if images:
            content_parts: list[dict] = [{"type": "text", "text": prompt}]
            for img in images:
                b64 = base64.b64encode(img).decode("ascii")
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
                })
            messages.append({"role": "user", "content": content_parts})
        else:
            messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.7
        }

        resp = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices", [])
        if choices and "message" in choices[0]:
            return choices[0]["message"].get("content", "")
        return ""

    def _call_ollama(
        self,
        base_url: str,
        api_key: str,
        model: str,
        prompt: str,
        system: str,
        images: list[bytes] | None,
        timeout: int
    ) -> str:
        clean_url = base_url.rstrip("/")
        for sfx in ("/api/chat", "/api/generate", "/api/tags"):
            if clean_url.endswith(sfx):
                clean_url = clean_url[:-len(sfx)]
                break

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # 1. Try /api/chat first (chat format)
        endpoint_chat = f"{clean_url}/api/chat"
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        
        user_msg: dict = {"role": "user", "content": prompt}
        if images:
            user_msg["images"] = [base64.b64encode(img).decode("ascii") for img in images]
        messages.append(user_msg)

        payload_chat = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": -1
        }
        
        try:
            resp = requests.post(endpoint_chat, json=payload_chat, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                if "message" in data and "content" in data["message"]:
                    return (data["message"].get("content") or "").strip()
                if "response" in data:
                    return (data.get("response") or "").strip()
        except Exception:
            pass

        # 2. Fallback to /api/generate
        endpoint_gen = f"{clean_url}/api/generate"
        payload_gen = {
            "model": model,
            "prompt": f"{system}\n\n{prompt}" if system else prompt,
            "stream": False
        }
        if images:
            payload_gen["images"] = [base64.b64encode(img).decode("ascii") for img in images]

        resp = requests.post(endpoint_gen, json=payload_gen, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return (data.get("response") or data.get("message", {}).get("content") or "").strip()

    def _call_tavily(self, base_url: str, api_key: str, query: str, timeout: int) -> str:
        endpoint = f"{base_url}/search"
        payload = {
            "api_key": api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": 5
        }
        resp = requests.post(endpoint, json=payload, timeout=timeout)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        lines = []
        for r in results:
            lines.append(f"• {r.get('title')}: {r.get('content')} ({r.get('url')})")
        return "\n".join(lines)

    def _call_ddg(self, query: str, timeout: int = 10) -> str:
        from ddgs import DDGS
        ddgs = DDGS(timeout=timeout)
        raw = list(ddgs.text(query, max_results=5))
        if not raw:
            return "No DuckDuckGo results found."
        lines = []
        for r in raw:
            title = r.get("title", "")
            body = r.get("body", "")
            href = r.get("href", "")
            lines.append(f"• {title}: {body} ({href})")
        return "\n".join(lines)

    # ── Provider Diagnostics ────────────────────────────────────────────────

    def test_provider(self, provider_id: str) -> tuple[bool, str]:
        """Tests connectivity and key validity for a provider."""
        p = self._config.get("providers", {}).get(provider_id, {})
        if not p:
            return False, f"Provider '{provider_id}' not found."

        ptype = p.get("type", "openai")
        base_url = p.get("base_url", "").rstrip("/")
        key = self.get_active_key(provider_id)
        models = p.get("models", [])
        test_model = models[0] if models else "default"

        start = time.time()
        try:
            if ptype == "ollama":
                clean_url = base_url
                for sfx in ("/api/chat", "/api/generate", "/api/tags"):
                    if clean_url.endswith(sfx):
                        clean_url = clean_url[:-len(sfx)]
                        break
                headers = {"Authorization": f"Bearer {key}"} if key else {}
                r = requests.get(f"{clean_url}/api/tags", headers=headers, timeout=8)
                if r.status_code == 200:
                    installed = [m.get("name") for m in r.json().get("models", [])]
                    latency = int((time.time() - start) * 1000)
                    model_hint = f" Models ({len(installed)}): {', '.join(installed[:4])}" if installed else ""
                    return True, f"Online ({latency}ms).{model_hint}"
                return False, f"Ollama returned HTTP {r.status_code}: {r.text[:80]}"
                
            elif ptype == "openai":
                if not key and "localhost" not in base_url and "127.0.0.1" not in base_url:
                    return False, "No API key configured for this remote provider."
                # Lightweight test: send 1-word generation
                ans = self._call_openai_compatible(base_url, key, test_model, "Ping. Answer with 'OK'.", "", None, 12)
                latency = int((time.time() - start) * 1000)
                return True, f"Online ({latency}ms). Model '{test_model}' answered: {ans[:30]}"
                
            elif ptype == "gemini":
                if not key:
                    return False, "No Gemini API key configured."
                ans = self._call_gemini(key, test_model or "gemini-2.5-flash", "Ping. Answer with 'OK'.", "", images=None, timeout=12)
                latency = int((time.time() - start) * 1000)
                return True, f"Online ({latency}ms). Answered: {ans[:30]}"

            elif ptype == "search":
                if provider_id == "ddg" or "duckduckgo" in base_url.lower():
                    ans = self._call_ddg("Python programming", timeout=10)
                    latency = int((time.time() - start) * 1000)
                    return True, f"Online ({latency}ms). DDG returned {len(ans.splitlines())} results."
                if not key:
                    return False, "No search API key configured."
                ans = self._call_tavily(base_url, key, "latest news", 10)
                latency = int((time.time() - start) * 1000)
                return True, f"Online ({latency}ms). Search returned {len(ans.splitlines())} results."
                
            return False, f"Unsupported provider type '{ptype}'."
        except Exception as e:
            return False, f"Connection failed: {e}"


# Singleton instance accessor
router = ModelRouter()
