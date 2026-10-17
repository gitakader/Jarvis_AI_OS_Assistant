"""
google_setup.py — one-time Google login for JARVIS.

Run from the project root (E:\\Mark-LV-main):

    venv\\Scripts\\python google_setup.py

What it does: opens the browser to Google's consent screen, waits for you to
approve, then stores the OAuth token (encrypted with Windows DPAPI) under
config/google/. After this completes, restart JARVIS and say
'what's on my calendar today'.

Prerequisite: config/google/credentials.json from the Google Cloud console
(APIs & Services -> Credentials -> Create credentials -> OAuth client ID ->
Application type 'Desktop app' -> Download). Without it this script tells you
exactly where to put it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Same console hardening main.py applies: never crash on a cp1252 terminal.
for _stream_name in ("stdout", "stderr"):
    try:
        getattr(sys, _stream_name).reconfigure(encoding="utf-8")
    except Exception:
        pass

from plugins import _google_auth  # noqa: E402


def main() -> None:
    print("=" * 60)
    print("JARVIS — Google login")
    print("=" * 60)
    if not _google_auth.has_credentials_file():
        print()
        print("MISSING: config/google/credentials.json")
        print("  Google Cloud console -> APIs & Services -> Credentials ->")
        print("  Create credentials -> OAuth client ID -> 'Desktop app' -> Download.")
        print(f"  Put the downloaded file at:")
        print(f"  {_google_auth._GOOGLE_DIR / 'credentials.json'}")
        print()
        print("Then run this script again.")
        return
    print("A browser window will open. Sign in and approve the requested")
    print("scopes (calendar events, read/send Gmail, Drive metadata, and")
    print("read/write Contacts).")
    print()
    result = _google_auth.login()
    print()
    print("Result:", result)
    if result.startswith("Google connected"):
        print()
        print("Done. Restart JARVIS, then try:")
        print("  'Hey Jarvis, what's on my calendar today?'")
        print("  'Hey Jarvis, check my email.'")


if __name__ == "__main__":
    main()
