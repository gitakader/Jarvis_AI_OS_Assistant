"""
google_services.py — Google Calendar, Gmail, Drive + Contacts plugin for JARVIS.

One tool with action sub-commands (same convention as computer_control and
drive_manager). Calendar (agenda / create / natural-language quick add),
Gmail (search / read / send), Drive listing/folder browsing/name search
(drive.metadata.readonly — whole-Drive visibility, no content read), and
Contacts via the People API (list / search / create; the legacy Contacts
GData API was shut down by Google, People API v1 is its replacement).

One-time setup (google_setup.py or action='login') is required before any
action works; every other action answers with the exact next step instead of
failing silently. Adding the contacts scope means one re-consent even for
accounts that already connected before.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta

from plugins import _google_auth

PLUGIN = {
    "name": "google_services",
    "description": (
        "Google Calendar, Gmail, Google Drive and Google Contacts. Use this "
        "tool when the user asks about their schedule, email, drive or "
        "contacts: 'what's on my calendar today/tomorrow', 'add a meeting/"
        "event/reminder to my calendar', 'check my email', 'read the email "
        "from X', 'send an email to Y', 'connect my Google account', 'list "
        "my google drive files', 'what's inside my Recruitment folder', "
        "'find NID in my drive', 'what is Rahim's phone number', 'how many "
        "contacts do I have', 'list my contacts', 'contacts starting with "
        "M', 'add/save a new contact'. Sub-actions: calendar_agenda (list "
        "upcoming events), calendar_create (event with explicit title/"
        "start/end), calendar_quick_add (natural-language event, e.g. "
        "'Dinner with Rahim tomorrow 7pm'), contacts_list (first page of "
        "contacts with the TOTAL count; pass letter='M' to list only "
        "contacts whose name starts with that letter — use the letter form "
        "for browsing and contacts_search to find one specific person), "
        "contacts_search (find contacts by name/email/phone fragment), "
        "contacts_create (add a contact with name plus phone and/or "
        "email), drive_list (top level of My Drive: "
        "folders first, then recently modified files), drive_folder "
        "(contents of one folder, by name or id), drive_search (find files/"
        "folders by name anywhere in Drive, shows full folder path), "
        "gmail_search (find emails), gmail_read (read one email by id), "
        "gmail_send (send email — only after the user confirmed recipient "
        "and content in conversation), login (connect/reconnect the Google "
        "account), status (connection state)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "calendar_agenda | calendar_create | calendar_quick_add | "
                    "contacts_list | contacts_search | contacts_create | "
                    "drive_list | drive_folder | drive_search | gmail_search | "
                    "gmail_read | gmail_send | login | status"
                )
            },
            "days": {
                "type": "INTEGER",
                "description": "Days ahead for calendar_agenda (default 7, max 30)"
            },
            "title": {
                "type": "STRING",
                "description": "Event title for calendar_create"
            },
            "start": {
                "type": "STRING",
                "description": "Event start for calendar_create: 'YYYY-MM-DD HH:MM' "
                               "(local time) or RFC3339 with offset; 'YYYY-MM-DD' "
                               "alone makes an all-day event"
            },
            "end": {
                "type": "STRING",
                "description": "Event end for calendar_create, same formats as start"
            },
            "description": {
                "type": "STRING",
                "description": "Optional event description for calendar_create"
            },
            "text": {
                "type": "STRING",
                "description": "Natural-language event for calendar_quick_add, "
                               "e.g. 'Doctor appointment Friday 3pm'"
            },
            "folder_name": {
                "type": "STRING",
                "description": "Folder name for drive_folder, e.g. 'Recruitment'"
            },
            "folder_id": {
                "type": "STRING",
                "description": "Drive folder id for drive_folder — use when "
                               "drive_folder reported several folders share a "
                               "name and listed their ids"
            },
            "query": {
                "type": "STRING",
                "description": "Gmail search query for gmail_search, e.g. "
                               "'from:rahim newer_than:7d' or 'bank'; a name "
                               "fragment for drive_search, e.g. 'NID' or "
                               "'Rojoni'; or a name/email/phone fragment for "
                               "contacts_search, e.g. 'Rahim' or '01712'"
            },
            "max_results": {
                "type": "INTEGER",
                "description": "Max results for gmail_search/drive_list/"
                               "drive_folder/drive_search/contacts_list/"
                               "contacts_search (default 5; gmail max 10, "
                               "Drive and Contacts max 100)"
            },
            "letter": {
                "type": "STRING",
                "description": "For contacts_list: only contacts whose name "
                               "starts with this letter (A-Z, single letter), "
                               "e.g. letter='M' — for browsing big contact "
                               "books without dumping everything at once"
            },
            "name": {
                "type": "STRING",
                "description": "Contact's full name for contacts_create, "
                               "e.g. 'Rahim Uddin'"
            },
            "phone": {
                "type": "STRING",
                "description": "Optional phone number for contacts_create, "
                               "e.g. '+8801712...'"
            },
            "email": {
                "type": "STRING",
                "description": "Optional email address for contacts_create"
            },
            "id": {
                "type": "STRING",
                "description": "Message id for gmail_read (take it from a "
                               "gmail_search result)"
            },
            "to": {
                "type": "STRING",
                "description": "Recipient for gmail_send, e.g. 'rahim@example.com'"
            },
            "subject": {
                "type": "STRING",
                "description": "Subject line for gmail_send"
            },
            "body": {
                "type": "STRING",
                "description": "Plain-text body for gmail_send"
            },
        },
        "required": ["action"],
    },
}

_MAX_BODY = 1500  # chars of a message body spoken/returned to the model


# ── small helpers ─────────────────────────────────────────────────────────────

def _local_tz():
    return datetime.now().astimezone().tzinfo


def _b64url_decode(s: str) -> str:
    pad = "=" * (-len(s) % 4)
    try:
        return base64.urlsafe_b64decode(s + pad).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _event_time(d: dict) -> str:
    """Human, local-time rendering of a calendar event's start."""
    dt_s = d.get("dateTime")
    if dt_s:
        try:
            dt = datetime.fromisoformat(dt_s).astimezone()
            return dt.strftime("%a %d %b %H:%M")
        except ValueError:
            return str(dt_s)[:16]
    if d.get("date"):
        return f"{d['date']} (all day)"
    return "(time unknown)"


def _parse_when(value: str):
    """Return (kind, iso_value) for calendar_create start/end:
    kind 'dateTime' with RFC3339, or 'date' for all-day."""
    v = (value or "").strip()
    if not v:
        return None
    if "T" in v:
        return ("dateTime", v)          # already RFC3339 — pass through
    try:
        dt = datetime.strptime(v, "%Y-%m-%d %H:%M")
        return ("dateTime", dt.replace(tzinfo=_local_tz()).isoformat())
    except ValueError:
        pass
    try:
        datetime.strptime(v, "%Y-%m-%d")
        return ("date", v)              # all-day
    except ValueError:
        return None


# ── calendar actions ──────────────────────────────────────────────────────────

def _calendar_agenda(days: int) -> str:
    days = max(1, min(int(days or 7), 30))
    svc = _google_auth.build_service("calendar", "v3")
    now = datetime.utcnow()
    tmin = now.isoformat() + "Z"
    tmax = (now + timedelta(days=days)).isoformat() + "Z"
    res = svc.events().list(calendarId="primary", timeMin=tmin, timeMax=tmax,
                            singleEvents=True, orderBy="startTime",
                            maxResults=10).execute()
    items = res.get("items") or []
    if not items:
        return f"No events in the next {days} days."
    lines = [f"{len(items)} upcoming event(s):"]
    for ev in items:
        when = _event_time(ev.get("start") or {})
        what = (ev.get("summary") or "(no title)").strip()
        lines.append(f"  {when} — {what}")
    return "\n".join(lines)


def _calendar_create(title, start, end, description) -> str:
    if not title or not title.strip():
        return "Sir, the event needs a title."
    s = _parse_when(start)
    if s is None:
        return ("Sir, I could not understand the start time. Use "
                "'YYYY-MM-DD HH:MM' (e.g. '2026-10-14 19:00') or "
                "'YYYY-MM-DD' for an all-day event.")
    event = {
        "summary": title.strip(),
        "start": {s[0]: s[1]},
    }
    e = _parse_when(end) if end else None
    if e is not None:
        if e[0] == s[0]:
            event["end"] = {e[0]: e[1]}
        else:
            # mixed date/dateTime — Google rejects it; drop the end rather
            # than fail the whole create
            pass
    elif s[0] == "dateTime":
        # no end given: a default 1-hour slot keeps the event visible
        try:
            dt = datetime.fromisoformat(s[1])
            event["end"] = {"dateTime": (dt + timedelta(hours=1)).isoformat()}
        except ValueError:
            pass
    if description and description.strip():
        event["description"] = description.strip()

    svc = _google_auth.build_service("calendar", "v3")
    created = svc.events().insert(calendarId="primary", body=event).execute()
    when = _event_time(created.get("start") or {})
    return f"Added to your calendar: {created.get('summary')} on {when}."


def _calendar_quick_add(text) -> str:
    if not text or not text.strip():
        return "Sir, what should I add to the calendar?"
    svc = _google_auth.build_service("calendar", "v3")
    created = svc.events().quickAdd(calendarId="primary",
                                    text=text.strip()).execute()
    when = _event_time(created.get("start") or {})
    return f"Added to your calendar: {created.get('summary')} on {when}."


# ── drive actions ─────────────────────────────────────────────────────────────

def _drive_list(max_results) -> str:
    limit = max(1, min(int(max_results or 10), 25))
    svc = _google_auth.build_service("drive", "v3")
    res = svc.files().list(q="'root' in parents and trashed=false",
                           pageSize=limit,
                           orderBy="folder,modifiedTime desc",
                           fields="files(id, name, mimeType, modifiedTime)").execute()
    files = res.get("files") or []
    if not files:
        return "Sir, your Google Drive is empty."
    lines = [f"{len(files)} item(s) at the top level of My Drive (folders first):"]
    for f in files:
        is_folder = f.get("mimeType") == "application/vnd.google-apps.folder"
        when = f.get("modifiedTime", "")[:10]
        tag = "[folder]" if is_folder else "        "
        lines.append(f"  {tag} {f['name']} (modified: {when})")
    return "\n".join(lines)


def _path_of(svc, fid: str, cache: dict) -> str:
    """Full 'folder > sub > file' path from a file id up to the root."""
    parts, seen, cur = [], set(), fid
    while cur and cur not in seen and len(parts) < 10:
        seen.add(cur)
        if cur not in cache:
            try:
                cache[cur] = svc.files().get(fileId=cur,
                                             fields="id,name,parents").execute()
            except Exception:
                cache[cur] = {"id": cur, "name": "(unreadable)", "parents": []}
        m = cache[cur]
        parts.append(m.get("name") or "(unnamed)")
        ps = m.get("parents") or []
        if not ps:
            break
        cur = ps[0]
    return " > ".join(reversed(parts))


def _drive_folder(folder_name, folder_id, max_results) -> str:
    limit = max(1, min(int(max_results or 10), 25))
    svc = _google_auth.build_service("drive", "v3")
    cache: dict = {}

    fid = str(folder_id or "").strip()
    if not fid:
        name = (folder_name or "").strip()
        if not name:
            return "Sir, which folder should I look inside?"
        term = name.replace("'", "\\'")
        cand = svc.files().list(
            q=(f"name='{term}' and "
               "mimeType='application/vnd.google-apps.folder' and trashed=false"),
            fields="files(id,name,ownedByMe)", pageSize=25).execute().get("files") or []
        if not cand:
            return f"Sir, I could not find a folder named '{name}' in your Drive."
        if len(cand) > 1:
            lines = [f"Sir, {len(cand)} folders are named '{name}'. "
                     "Which one did you mean?"]
            for c in cand:
                tag = "" if c.get("ownedByMe") else " [shared with you]"
                lines.append(f"  {c['name']}{tag}  (id: {c['id']})")
                lines.append(f"       at: {_path_of(svc, c['id'], cache)}")
            lines.append("Tell me the location (or id) and I will look inside it.")
            return "\n".join(lines)
        fid = cand[0]["id"]

    try:
        meta = svc.files().get(fileId=fid,
                               fields="id,name,mimeType,ownedByMe,owners").execute()
    except Exception:
        return "Sir, I could not read that folder id."
    if meta.get("mimeType") != "application/vnd.google-apps.folder":
        return f"Sir, '{meta.get('name')}' is a file, not a folder."
    fname = meta.get("name") or "(unnamed)"
    note = ""
    if not meta.get("ownedByMe"):
        owner = (meta.get("owners") or [{}])[0].get("emailAddress") or "another account"
        note = f" (shared with you by {owner})"

    res = svc.files().list(q=f"'{fid}' in parents and trashed=false",
                           pageSize=limit,
                           orderBy="folder,modifiedTime desc",
                           fields="files(id, name, mimeType, modifiedTime)").execute()
    items = res.get("files") or []
    if not items:
        return f"Sir, '{fname}'{note} is empty."
    lines = [f"{len(items)} item(s) in '{fname}'{note} (folders first):"]
    for f in items:
        is_folder = f.get("mimeType") == "application/vnd.google-apps.folder"
        when = f.get("modifiedTime", "")[:10]
        tag = "[folder]" if is_folder else "        "
        lines.append(f"  {tag} {f['name']} (modified: {when})")
    return "\n".join(lines)


def _drive_search(query, max_results) -> str:
    term = (query or "").strip()
    if not term:
        return "Sir, what should I search for in your Drive?"
    limit = max(1, min(int(max_results or 10), 25))
    svc = _google_auth.build_service("drive", "v3")
    qterm = term.replace("'", "\\'")
    res = svc.files().list(q=f"name contains '{qterm}' and trashed=false",
                           pageSize=limit,
                           orderBy="modifiedTime desc",
                           fields="files(id, name, mimeType, modifiedTime, ownedByMe)").execute()
    items = res.get("files") or []
    if not items:
        return f"Sir, nothing in your Drive matches '{term}'."
    cache: dict = {}
    lines = [f"{len(items)} match(es) for '{term}':"]
    for f in items:
        is_folder = f.get("mimeType") == "application/vnd.google-apps.folder"
        when = f.get("modifiedTime", "")[:10]
        tag = "[folder]" if is_folder else "        "
        shared = "" if f.get("ownedByMe") else " [shared with you]"
        lines.append(f"  {tag} {f['name']}{shared} (modified: {when})")
        lines.append(f"       at: {_path_of(svc, f['id'], cache)}")
    return "\n".join(lines)


# ── gmail actions ─────────────────────────────────────────────────────────────

def _gmail_search(query, max_results) -> str:
    query = (query or "").strip()
    if not query:
        return "Sir, what should I search for in your email?"
    limit = max(1, min(int(max_results or 5), 10))
    svc = _google_auth.build_service("gmail", "v1")
    res = svc.users().messages().list(userId="me", q=query,
                                      maxResults=limit).execute()
    msgs = res.get("messages") or []
    if not msgs:
        return f"No emails match '{query}'."
    lines = [f"{len(msgs)} email(s) found:"]
    for m in msgs:
        meta = svc.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"]).execute()
        heads = {h["name"]: h["value"]
                 for h in meta.get("payload", {}).get("headers", [])}
        lines.append(f"  id={m['id']} | {heads.get('From','?')} | "
                     f"{heads.get('Subject','(no subject)')} | {heads.get('Date','')[:16]}")
        lines.append(f"    {meta.get('snippet','')[:120]}")
    lines.append("Use gmail_read with the id to read one in full.")
    return "\n".join(lines)


def _gmail_read(mid) -> str:
    if not mid or not str(mid).strip():
        return "Sir, give me the email id from a gmail_search result."
    svc = _google_auth.build_service("gmail", "v1")
    msg = svc.users().messages().get(userId="me", id=str(mid).strip(),
                                     format="full").execute()
    heads = {h["name"]: h["value"]
             for h in msg.get("payload", {}).get("headers", [])}
    body = ""
    payload = msg.get("payload") or {}

    def walk(part):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return _b64url_decode(part["body"]["data"])
        for sub in part.get("parts") or []:
            got = walk(sub)
            if got:
                return got
        return ""

    body = walk(payload) or _b64url_decode(payload.get("body", {}).get("data", "")) \
        or msg.get("snippet", "")
    body = body.strip()
    if len(body) > _MAX_BODY:
        body = body[:_MAX_BODY] + "… (truncated)"
    return (f"From: {heads.get('From','?')}\n"
            f"Date: {heads.get('Date','?')}\n"
            f"Subject: {heads.get('Subject','(no subject)')}\n\n"
            f"{body or '(no readable body — it may be HTML-only)'}")


def _gmail_send(to, subject, body) -> str:
    to = (to or "").strip()
    if not to or "@" not in to:
        return "Sir, the recipient address does not look right."
    subject = subject or "(no subject)"
    body = body or ""

    from email.mime.text import MIMEText
    msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to
    msg["Subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    svc = _google_auth.build_service("gmail", "v1")
    sent = svc.users().messages().send(userId="me",
                                       body={"raw": raw}).execute()
    return (f"Email sent to {to} (subject: {subject}) — "
            f"id {sent.get('id', 'unknown')}.")


# ── contacts actions (People API) ────────────────────────────────────────────

def _fmt_person(p: dict) -> str:
    names = p.get("names") or []
    if names:
        n = names[0]
        name = n.get("displayName") or " ".join(
            x for x in (n.get("givenName"), n.get("familyName")) if x)
    else:
        name = ""
    emails = [e["value"] for e in (p.get("emailAddresses") or [])
              if e.get("value")]
    phones = [x["value"] for x in (p.get("phoneNumbers") or [])
              if x.get("value")]
    bits = [name or "(no name)"]
    if emails:
        bits.append(" | ".join(emails[:2]))
    if phones:
        bits.append(" | ".join(phones[:2]))
    return " — ".join(bits)


def _contacts_list(max_results, letter) -> str:
    limit = max(1, min(int(max_results or 25), 100))
    letter = str(letter or "").strip().upper()
    svc = _google_auth.build_service("people", "v1")

    if letter:
        # Letter browsing walks every page once, then filters locally —
        # the People API has no server-side letter filter.
        people, token = [], None
        total = 0
        while True:
            kw = {"resourceName": "people/me",
                  "personFields": "names,emailAddresses,phoneNumbers",
                  "pageSize": 1000}
            if token:
                kw["pageToken"] = token
            res = svc.people().connections().list(**kw).execute()
            total = int(res.get("totalPeople") or 0)
            people.extend(res.get("connections") or [])
            token = res.get("nextPageToken")
            if not token:
                break
        filtered = [p for p in people
                    if _person_sort_key(p).startswith(letter.lower())]
        filtered.sort(key=_person_sort_key)
        shown = filtered[:limit]
        if not shown:
            return f"Sir, no contacts start with '{letter}'."
        head = (f"{len(filtered)} contact(s) start with '{letter}' "
                f"(of {total} total)")
        if len(filtered) > len(shown):
            head += f" — showing {len(shown)}"
        lines = [head + ":"]
        for p in shown:
            lines.append(f"  {_fmt_person(p)}")
        return "\n".join(lines)

    res = svc.people().connections().list(
        resourceName="people/me",
        personFields="names,emailAddresses,phoneNumbers",
        pageSize=min(limit, 100)).execute()
    people = res.get("connections") or []
    total = int(res.get("totalPeople") or len(people))
    if not people:
        return "Sir, your Google contacts list is empty."
    people.sort(key=_person_sort_key)
    lines = [f"{total} contact(s) in total — showing the first "
             f"{len(people)}:"]
    for p in people:
        lines.append(f"  {_fmt_person(p)}")
    if total > len(people):
        lines.append("Ask me for a specific name or number, or say "
                     "'contacts starting with M' for one letter.")
    return "\n".join(lines)


def _person_sort_key(p: dict) -> str:
    names = p.get("names") or []
    if not names:
        return ""
    n = names[0]
    name = n.get("displayName") or " ".join(
        x for x in (n.get("givenName"), n.get("familyName")) if x)
    return (name or "").lower()


def _contacts_search(query, max_results) -> str:
    term = (query or "").strip()
    if not term:
        return "Sir, who should I look up in your contacts?"
    limit = max(1, min(int(max_results or 5), 25))
    svc = _google_auth.build_service("people", "v1")
    res = svc.people().searchContacts(
        query=term, readMask="names,emailAddresses,phoneNumbers",
        pageSize=limit).execute()
    hits = [r.get("person") for r in (res.get("results") or [])
            if r.get("person")]
    if not hits:
        return f"Sir, no contacts match '{term}'."
    lines = [f"Found {len(hits)} contact(s) matching '{term}':"]
    for p in hits:
        lines.append(f"  {_fmt_person(p)}")
    return "\n".join(lines)


def _contacts_create(name, phone, email) -> str:
    name = (name or "").strip()
    phone = (phone or "").strip()
    email = (email or "").strip()
    if not name:
        return "Sir, the contact needs at least a name."
    if not phone and not email:
        return "Sir, give me a phone number or an email address for the contact."
    parts = name.split()
    body = {"names": [({"givenName": parts[0],
                        "familyName": " ".join(parts[1:])}
                       if len(parts) > 1 else {"givenName": name})]}
    if phone:
        body["phoneNumbers"] = [{"value": phone}]
    if email:
        body["emailAddresses"] = [{"value": email}]
    svc = _google_auth.build_service("people", "v1")
    created = svc.people().createContact(body=body).execute()
    rid = created.get("resourceName", "")
    return f"Contact added: {_fmt_person(created)} (id: {rid})"


# ── plugin entry ──────────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    action = str(parameters.get("action", "") or "").strip().lower()
    if not action:
        return "Sir, google_services needs an action."

    if player:
        try:
            player.write_log(f"[Google] {action}")
        except Exception:
            pass

    try:
        if action == "status":
            state = _google_auth.token_summary()
            email = _google_auth.account_email() if state == "connected" else ""
            who = f" as {email}" if email else ""
            return f"Google {state}{who}."

        if action == "login":
            result = _google_auth.login()
            if result.startswith("Google connected"):
                email = _google_auth.account_email()
                if email:
                    try:
                        from memory.config_manager import save_plugin_config
                        save_plugin_config("google_services",
                                           {"account": email})
                        return f"Google connected as {email}."
                    except Exception:
                        pass
            return result

        if action == "calendar_agenda":
            return _calendar_agenda(parameters.get("days"))
        if action == "calendar_create":
            return _calendar_create(parameters.get("title"),
                                    parameters.get("start"),
                                    parameters.get("end"),
                                    parameters.get("description"))
        if action == "calendar_quick_add":
            return _calendar_quick_add(parameters.get("text"))
        if action == "contacts_list":
            return _contacts_list(parameters.get("max_results"),
                                  parameters.get("letter"))
        if action == "contacts_search":
            return _contacts_search(parameters.get("query"),
                                    parameters.get("max_results"))
        if action == "contacts_create":
            return _contacts_create(parameters.get("name"),
                                    parameters.get("phone"),
                                    parameters.get("email"))
        if action == "drive_list":
            return _drive_list(parameters.get("max_results"))
        if action == "drive_folder":
            return _drive_folder(parameters.get("folder_name"),
                                 parameters.get("folder_id"),
                                 parameters.get("max_results"))
        if action == "drive_search":
            return _drive_search(parameters.get("query"),
                                 parameters.get("max_results"))
        if action == "gmail_search":
            return _gmail_search(parameters.get("query"),
                                 parameters.get("max_results"))
        if action == "gmail_read":
            return _gmail_read(parameters.get("id"))
        if action == "gmail_send":
            return _gmail_send(parameters.get("to"),
                               parameters.get("subject"),
                               parameters.get("body"))

        return ("Sir, unknown action "
                f"'{action}'. Valid actions are calendar_agenda, "
                "calendar_create, calendar_quick_add, contacts_list, "
                "contacts_search, contacts_create, drive_list, drive_folder, "
                "drive_search, gmail_search, gmail_read, gmail_send, login, "
                "status.")

    except _google_auth.GoogleNotConnected as e:
        return f"Sir, {e}"
    except Exception as e:
        try:
            from google.auth.exceptions import RefreshError
            if isinstance(e, RefreshError) or (
                    e.__class__.__name__ == "RefreshError"):
                return ("Sir, the Google login has expired. Say 'connect my "
                        "Google account' to refresh it.")
        except Exception:
            pass
        msg = str(e)
        if ("insufficient" in msg.lower()
                and ("scope" in msg.lower() or "permission" in msg.lower())):
            return ("Sir, your Google login does not cover this action. "
                    "Re-connect once to grant the newer permissions: say "
                    "'connect my Google account' (or run "
                    "venv\\Scripts\\python google_setup.py), approve the "
                    "requested scope, then try again.")
        return f"Sir, google_services failed: {e}"