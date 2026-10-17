"""
_google_auth.py — shared OAuth helper for the google_services plugin.

The plugin loader deliberately skips files whose names start with '_', so this
module is a helper, never a plugin of its own. It is imported by
plugins/google_services.py (inside JARVIS) and by google_setup.py (standalone,
run from the project root) — therefore every path derives from this file's
location, never from the current working directory.

Responsibilities:
  - locate config/google/credentials.json and the token store
  - encrypt the OAuth token at rest with Windows DPAPI (CryptProtectData),
    falling back to a plain file only if DPAPI is unavailable
  - load / refresh / validate Google credentials (thread-safe refresh)
  - build api discovery service objects
  - run the one-time InstalledAppFlow browser login

Nothing here prints API keys, tokens, or client secrets — errors name files,
never contents.
"""
from __future__ import annotations

import ctypes
import json
import threading
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_GOOGLE_DIR   = _PROJECT_ROOT / "config" / "google"
_CREDS_FILE   = _GOOGLE_DIR / "credentials.json"   # from Google Cloud console
_TOKEN_FILE   = _GOOGLE_DIR / "token.json"         # DPAPI-encrypted
_TOKEN_PLAIN  = _GOOGLE_DIR / "token.plain.json"   # fallback when DPAPI is off

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/contacts",
]

# ── Windows DPAPI ─────────────────────────────────────────────────────────────
# Preferred path: win32crypt (pywin32 is a declared Windows dependency).
# Fallback: raw ctypes. The fallback deliberately does NOT call LocalFree on
# the blob CryptProtectData allocates — observed hard process crash on
# Windows 11 with ctypes LocalFree. The leak is ~230 bytes per call, and this
# code only runs at login/token-refresh time, so skipping the free is safe.

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.c_char_p)]


def _dpapi_protect(data: bytes) -> bytes | None:
    try:
        import win32crypt
        return bytes(win32crypt.CryptProtectData(data, None, None, None, None, 0))
    except Exception:
        pass
    try:
        crypt32 = ctypes.windll.crypt32
        _BLOB_P = ctypes.POINTER(_DATA_BLOB)
        crypt32.CryptProtectData.argtypes = [
            _BLOB_P, ctypes.c_wchar_p, _BLOB_P, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_uint32, _BLOB_P]
        crypt32.CryptProtectData.restype = ctypes.c_bool
        blob_in  = _DATA_BLOB(len(data), data)
        blob_out = _DATA_BLOB()
        if not crypt32.CryptProtectData(ctypes.byref(blob_in), None, None,
                                        None, None, 0, ctypes.byref(blob_out)):
            return None
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    except Exception:
        return None


def _dpapi_unprotect(blob: bytes) -> bytes | None:
    try:
        import win32crypt
        _, out = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
        return bytes(out)
    except Exception:
        pass
    try:
        crypt32 = ctypes.windll.crypt32
        _BLOB_P = ctypes.POINTER(_DATA_BLOB)
        crypt32.CryptUnprotectData.argtypes = [
            _BLOB_P, ctypes.c_wchar_p, _BLOB_P, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_uint32, _BLOB_P]
        crypt32.CryptUnprotectData.restype = ctypes.c_bool
        blob_in  = _DATA_BLOB(len(blob), blob)
        blob_out = _DATA_BLOB()
        if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None,
                                          None, None, 0, ctypes.byref(blob_out)):
            return None
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    except Exception:
        return None


# ── token storage ─────────────────────────────────────────────────────────────

def save_token(token_dict: dict) -> bool:
    """Persist the OAuth token. DPAPI-encrypted when possible, plain fallback."""
    _GOOGLE_DIR.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(token_dict).encode("utf-8")
    try:
        enc = _dpapi_protect(raw)
        if enc is not None:
            _TOKEN_FILE.write_bytes(enc)
            try:
                _TOKEN_PLAIN.unlink(missing_ok=True)
            except OSError:
                pass
            return True
    except OSError:
        pass
    try:
        _TOKEN_PLAIN.write_bytes(raw)
        return True
    except OSError:
        return False


def load_token() -> dict | None:
    """Token dict, or None when absent/undecryptable. Never raises."""
    for path, enc in ((_TOKEN_FILE, True), (_TOKEN_PLAIN, False)):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if enc:
            raw = _dpapi_unprotect(raw)
            if raw is None:
                continue
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else None
        except (ValueError, UnicodeDecodeError):
            return None
    return None


def has_credentials_file() -> bool:
    return _CREDS_FILE.is_file()


# ── credentials / services ────────────────────────────────────────────────────

class GoogleNotConnected(Exception):
    """Raised when an action needs Google but no usable token exists."""


def _credential_paths_hint() -> str:
    return (f"config{chr(92)}google{chr(92)}credentials.json "
            f"(folder: {_GOOGLE_DIR})")


def load_credentials():
    """Valid google-auth Credentials, or None when the user must log in."""
    token = load_token()
    if not token:
        return None
    try:
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_info(token, SCOPES)
        if not creds.valid:
            if creds.refresh_token:
                try:
                    with _REFRESH_LOCK:
                        creds.refresh(_request())
                        save_token(json.loads(creds.to_json()))
                except Exception:
                    return None
            else:
                return None
        # google-auth's own lock guard for concurrent refreshes
        if getattr(creds, "_lock", None) is None:
            try:
                creds._lock = threading.Lock()
            except Exception:
                pass
        return creds
    except Exception:
        return None


def _request():
    from google.auth.transport.requests import Request
    return Request()


_REFRESH_LOCK = threading.Lock()


def build_service(name: str, version: str):
    """Discovery-built service for name@version, or raises GoogleNotConnected."""
    creds = load_credentials()
    if creds is None:
        raise GoogleNotConnected(
            "Google is not connected yet. One-time setup: put credentials.json "
            f"in {_credential_paths_hint()}, then say 'connect my Google "
            "account' — or run 'venv\\Scripts\\python google_setup.py'."
        )
    from googleapiclient.discovery import build
    return build(name, version, credentials=creds, cache_discovery=False)


def account_email(creds=None) -> str:
    """Email of the connected account ('' when unknown). Uses Gmail profile."""
    try:
        from googleapiclient.discovery import build
        c = creds or load_credentials()
        if c is None:
            return ""
        svc = build("gmail", "v1", credentials=c, cache_discovery=False)
        profile = svc.users().getProfile(userId="me").execute()
        return str(profile.get("emailAddress") or "")
    except Exception:
        return ""


def login() -> str:
    """One-time browser OAuth flow. Blocks until the user approves. Returns a
    short spoken-style result string. Never raises."""
    if not has_credentials_file():
        return (
            "Google client credentials are missing. Download credentials.json "
            "from the Google Cloud console (APIs & Services -> Credentials -> "
            f"OAuth client ID, type 'Desktop app') and put it at "
            f"{_credential_paths_hint()}. Then run this again."
        )
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_secrets_file(
            str(_CREDS_FILE), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
        ok = save_token(json.loads(creds.to_json()))
        if not ok:
            return "Google login finished, but I could not save the token file."
        email = account_email(creds)
        suffix = f" ({email})" if email else ""
        return f"Google connected{suffix}."
    except Exception as e:
        return f"Google login failed: {e}"


def token_summary() -> str:
    """One-line state for the status action. Never shows secret material."""
    if has_credentials_file() and load_token() is not None:
        return "connected"
    if has_credentials_file():
        return "client credentials present, not logged in"
    return "not set up"
