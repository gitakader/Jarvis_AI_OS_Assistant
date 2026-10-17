"""
selfheal_tests/test_google_contacts.py — offline verification of the
contacts sub-actions in plugins/google_services.py.

Phase-mission style: each check prints an evidence block; STOPS at the first
failure. No network: the People API service is faked, so this runs with no
Google token present.

    venv\\Scripts\\python.exe -u selfheal_tests\\test_google_contacts.py
"""
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from plugins import _google_auth
from plugins import google_services as gs

PASSED = 0


def ok(name, evidence):
    global PASSED
    PASSED += 1
    print(f"\nPASS {name}")
    for line in evidence:
        print(f"  {line}")


def fail(name, evidence):
    print(f"\nFAIL {name}")
    for line in evidence:
        print(f"  {line}")
    sys.exit(1)


# ── fakes for the People API service ─────────────────────────────────────────

class FakeListSvc:
    """Shape of svc.people().connections().list(**kw).execute()

    Takes a LIST of payloads — one per list() call (letter browsing walks
    pages, each execute() consuming the next payload)."""

    def __init__(self, payloads):
        if not isinstance(payloads, list):
            payloads = [payloads]
        self.payloads = list(payloads)
        self.captured = []

    def people(self):
        return self

    def connections(self):
        return self

    def list(self, **kw):
        self.captured.append(kw)
        return self

    def execute(self):
        return self.payloads.pop(0)


class FakeSearchSvc:
    """Shape of svc.people().searchContacts(**kw).execute()"""

    def __init__(self, payload):
        self.payload = payload
        self.captured = None

    def people(self):
        return self

    def searchContacts(self, **kw):
        self.captured = kw
        return self

    def execute(self):
        return self.payload


class FakeCreateSvc:
    """Shape of svc.people().createContact(**kw).execute()"""

    def __init__(self, person):
        self.person = person
        self.captured = None

    def people(self):
        return self

    def createContact(self, **kw):
        self.captured = kw
        return self

    def execute(self):
        return self.person


P1 = {
    "resourceName": "people/c1",
    "names": [{"displayName": "Rahim Uddin", "givenName": "Rahim",
               "familyName": "Uddin"}],
    "emailAddresses": [{"value": "rahim@example.com"}],
    "phoneNumbers": [{"value": "+8801712345678"}],
}
P2 = {
    "resourceName": "people/c2",
    "names": [{"givenName": "Amina"}],
    "emailAddresses": [{"value": "amina@example.com"}],
}
P3 = {"resourceName": "people/c3"}  # no readable fields at all


# ── 1. compile + scope + PLUGIN dict ─────────────────────────────────────────

def test_compile_and_scope():
    import py_compile
    py_compile.compile(str(ROOT / "plugins" / "_google_auth.py"), doraise=True)
    py_compile.compile(str(ROOT / "plugins" / "google_services.py"), doraise=True)
    contacts = [s for s in _google_auth.SCOPES if "contacts" in s]
    if not contacts:
        fail("compile + scope", ["no contacts scope found in SCOPES"])
    ok("compile + scope", [
        "both plugin files compile",
        f"contacts scope present: {contacts[0]}",
    ])


def test_plugin_dict():
    p = gs.PLUGIN
    desc = p["description"]
    props = p["parameters"]["properties"]
    act = props["action"]["description"]
    missing = [a for a in ("contacts_list", "contacts_search", "contacts_create")
               if a not in act]
    if missing:
        fail("PLUGIN dict", [f"missing actions in declaration: {missing}"])
    params_ok = all(k in props for k in ("name", "phone", "email", "letter"))
    if not params_ok:
        fail("PLUGIN dict", ["name/phone/email/letter params missing"])
    if "contacts" not in desc.lower():
        fail("PLUGIN dict", ["description does not mention contacts"])
    ok("PLUGIN dict", [
        "action declaration lists contacts_list/search/create",
        "name/phone/email/letter parameters declared",
        "description mentions contacts",
    ])


# ── 2. rendering ─────────────────────────────────────────────────────────────

def test_fmt_person():
    full = gs._fmt_person(P1)
    expect = "Rahim Uddin — rahim@example.com — +8801712345678"
    if full != expect:
        fail("fmt_person full", [f"got: {full!r}", f"want: {expect!r}"])
    no_phone = gs._fmt_person(P2)
    if "Amina" not in no_phone or "amina@example.com" not in no_phone:
        fail("fmt_person no phone", [f"got: {no_phone!r}"])
    empty = gs._fmt_person(P3)
    if "(no name)" not in empty:
        fail("fmt_person empty", [f"got: {empty!r}"])
    ok("fmt_person", [
        f"full: {full}",
        f"no phone: {no_phone}",
        f"no fields: {empty}",
    ])


# ── 3. contacts_list ─────────────────────────────────────────────────────────

def test_contacts_list():
    svc = FakeListSvc({"connections": [P2, P1]})
    with patch.object(gs._google_auth, "build_service", return_value=svc):
        out = gs._contacts_list(25, None)
    if "2 contact(s) in total" not in out or "Rahim Uddin" not in out:
        fail("contacts_list content", [out])
    if svc.captured[0].get("resourceName") != "people/me":
        fail("contacts_list resourceName", [str(svc.captured)])
    if out.index("Amina") > out.index("Rahim Uddin"):
        fail("contacts_list sort", ["not sorted by display name", out])
    ok("contacts_list", [
        "total-count line + names/emails/phones rendered",
        "resourceName=people/me passed",
        "sorted by display name",
        f"captured kwargs: {svc.captured}",
    ])


def test_contacts_list_total_hint():
    svc = FakeListSvc({"connections": [P1], "totalPeople": 1955})
    with patch.object(gs._google_auth, "build_service", return_value=svc):
        out = gs._contacts_list(None, None)
    if "1955 contact(s) in total — showing the first 1" not in out:
        fail("contacts_list total", [out])
    if "Ask me for a specific name" not in out:
        fail("contacts_list hint", [out])
    ok("contacts_list total hint", [
        out.splitlines()[0],
        f"hint present: {out.splitlines()[-1]}",
    ])


def test_contacts_list_empty():
    svc = FakeListSvc({"connections": []})
    with patch.object(gs._google_auth, "build_service", return_value=svc):
        out = gs._contacts_list(10, None)
    if "empty" not in out:
        fail("contacts_list empty", [out])
    ok("contacts_list empty", [out])


def test_contacts_list_letter_pages():
    page1 = {"connections": [P1, P2], "totalPeople": 1955,
             "nextPageToken": "tok1"}
    page2 = {"connections": [
        {"names": [{"displayName": "Abul"}],
         "phoneNumbers": [{"value": "011"}]},
        {"names": [{"displayName": "Babu"}]},
    ], "totalPeople": 1955}
    svc = FakeListSvc([page1, page2])
    with patch.object(gs._google_auth, "build_service", return_value=svc):
        out = gs._contacts_list(25, "a")
    if "2 contact(s) start with 'A' (of 1955 total)" not in out:
        fail("contacts_list letter", [out])
    if "Abul" not in out or "Amina" not in out:
        fail("contacts_list letter names", [out])
    if "Rahim" in out or "Babu" in out:
        fail("contacts_list letter filter leaked", [out])
    if len(svc.captured) != 2 or svc.captured[1].get("pageToken") != "tok1":
        fail("contacts_list letter paging", [str(svc.captured)])
    if svc.captured[0].get("pageSize") != 1000:
        fail("contacts_list letter pageSize", [str(svc.captured)])
    ok("contacts_list letter", [
        f"header: {out.splitlines()[0]}",
        "A-names from both pages, sorted",
        f"paged twice: {svc.captured}",
    ])


def test_contacts_list_letter_miss():
    svc = FakeListSvc({"connections": [P1], "totalPeople": 5})
    with patch.object(gs._google_auth, "build_service", return_value=svc):
        out = gs._contacts_list(None, "z")
    if "no contacts start with 'Z'" not in out:
        fail("contacts_list letter miss", [out])
    ok("contacts_list letter miss", [out])


# ── 4. contacts_search ───────────────────────────────────────────────────────

def test_contacts_search():
    svc = FakeSearchSvc({"results": [{"person": P1}]})
    with patch.object(gs._google_auth, "build_service", return_value=svc):
        out = gs._contacts_search("Rahim", 5)
    if "Found 1 contact(s) matching 'Rahim'" not in out:
        fail("contacts_search content", [out])
    if svc.captured.get("query") != "Rahim":
        fail("contacts_search query", [str(svc.captured)])
    if "emailAddresses" not in str(svc.captured.get("readMask", "")):
        fail("contacts_search readMask", [str(svc.captured)])
    ok("contacts_search", [
        f"result rendered: {out.splitlines()[0]}",
        f"captured kwargs: {svc.captured}",
    ])


def test_contacts_search_miss_and_blank():
    svc = FakeSearchSvc({"results": []})
    with patch.object(gs._google_auth, "build_service", return_value=svc):
        miss = gs._contacts_search("nobody", 5)
        blank = gs._contacts_search("", 5)
    if "no contacts match 'nobody'" not in miss:
        fail("contacts_search miss", [miss])
    if "who should I look up" not in blank:
        fail("contacts_search blank", [blank])
    ok("contacts_search miss/blank", [miss, blank])


# ── 5. contacts_create ───────────────────────────────────────────────────────

def test_contacts_create():
    person = dict(P1, resourceName="people/cNEW")
    svc = FakeCreateSvc(person)
    with patch.object(gs._google_auth, "build_service", return_value=svc):
        out = gs._contacts_create("Rahim Uddin", "+8801712345678",
                                  "rahim@example.com")
    body = svc.captured.get("body") or {}
    names = body.get("names") or [{}]
    if names[0].get("givenName") != "Rahim" or names[0].get("familyName") != "Uddin":
        fail("contacts_create name split", [str(body)])
    if (body.get("phoneNumbers") or [{}])[0].get("value") != "+8801712345678":
        fail("contacts_create phone", [str(body)])
    if (body.get("emailAddresses") or [{}])[0].get("value") != "rahim@example.com":
        fail("contacts_create email", [str(body)])
    if "Contact added" not in out or "Rahim Uddin" not in out:
        fail("contacts_create result", [out])
    ok("contacts_create", [
        f"name split into given/family: {names[0]}",
        "phone + email carried in body",
        f"result: {out}",
    ])


def test_contacts_create_errors():
    svc = FakeCreateSvc(P1)
    with patch.object(gs._google_auth, "build_service", return_value=svc):
        noname = gs._contacts_create("", "123", "")
        nocontact = gs._contacts_create("Rahim", "", "")
    if "needs at least a name" not in noname:
        fail("contacts_create no name", [noname])
    if "phone number or an email" not in nocontact:
        fail("contacts_create no phone/email", [nocontact])
    ok("contacts_create errors", [noname, nocontact])


# ── 6. run() dispatch + not-connected path ───────────────────────────────────

def test_run_dispatch():
    svc = FakeSearchSvc({"results": [{"person": P2}]})
    with patch.object(gs._google_auth, "build_service", return_value=svc):
        out = gs.run({"action": "contacts_search", "query": "Amina"})
    if "Amina" not in out:
        fail("run dispatch search", [out])
    svc2 = FakeCreateSvc(P1)
    with patch.object(gs._google_auth, "build_service", return_value=svc2):
        created = gs.run({"action": "contacts_create", "name": "Rahim Uddin",
                          "phone": "+8801712345678"})
    if "Contact added" not in created:
        fail("run dispatch create", [created])
    svc3 = FakeListSvc({"connections": [P2], "totalPeople": 9})
    with patch.object(gs._google_auth, "build_service", return_value=svc3):
        listed = gs.run({"action": "contacts_list", "letter": "a"})
    if "start with 'A' (of 9 total)" not in listed:
        fail("run dispatch list letter", [listed])
    unknown = gs.run({"action": "contacts_nonsense"})
    if "contacts_list" not in unknown:
        fail("run unknown action", [unknown])
    ok("run dispatch", [
        f"search via run: {out.splitlines()[0]}",
        f"create via run: {created}",
        f"letter list via run: {listed.splitlines()[0]}",
        "unknown-action message lists contacts actions",
    ])


def test_run_not_connected():
    def boom(name, version):
        raise gs._google_auth.GoogleNotConnected("Google is not connected yet.")

    with patch.object(gs._google_auth, "build_service", side_effect=boom):
        out = gs.run({"action": "contacts_list"})
    if not out.startswith("Sir, Google is not connected yet"):
        fail("run not-connected", [out])
    ok("run not-connected", [out])


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    test_compile_and_scope()
    test_plugin_dict()
    test_fmt_person()
    test_contacts_list()
    test_contacts_list_total_hint()
    test_contacts_list_empty()
    test_contacts_list_letter_pages()
    test_contacts_list_letter_miss()
    test_contacts_search()
    test_contacts_search_miss_and_blank()
    test_contacts_create()
    test_contacts_create_errors()
    test_run_dispatch()
    test_run_not_connected()
    print(f"\n=== ALL {PASSED}/14 TESTS PASSED ===")


if __name__ == "__main__":
    main()
