import re
from typing import Dict, Any, Optional

# Simple rule‑based intent parser for voice commands.
# It returns a dict with keys: 'action' (the tool name) and 'parameters' (dict).
# Extend patterns as needed.

def _match_file_list(text: str) -> Optional[Dict[str, Any]]:
    # Example: "list files in C colon backslash Users"
    m = re.search(r"list\s+(files|folders?)\s+in\s+([A-Za-z]:\\[\\\S]*)", text, re.IGNORECASE)
    if m:
        path = m.group(2).replace(' ', '')  # remove spaces in path
        return {"action": "file_controller", "parameters": {"action": "list", "path": path}}
    return None

def _match_query(text: str) -> Optional[Dict[str, Any]]:
    # Example: "search for how to install python"
    m = re.search(r"(search|query|ask)\s+for\s+(.*)", text, re.IGNORECASE)
    if m:
        query = m.group(2).strip()
        return {"action": "document_rag", "parameters": {"action": "query", "query": query}}
    return None

def _match_drive_list(text: str) -> Optional[Dict[str, Any]]:
    if re.search(r"list\s+drives", text, re.IGNORECASE):
        return {"action": "drive_manager", "parameters": {"action": "drives"}}
    return None

def parse_intent(text: str) -> Optional[Dict[str, Any]]:
    """Parse a spoken command into an intent dict.
    Returns None if no pattern matches.
    """
    text = text.strip().lower()
    for matcher in (_match_drive_list, _match_file_list, _match_query):
        result = matcher(text)
        if result:
            return result
    return None
