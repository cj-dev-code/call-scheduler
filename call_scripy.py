import os
import re
import sched
import time
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Tuple


from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google.oauth2 import service_account

from twilio.rest import Client

from dotenv import load_dotenv
load_dotenv()  # must be called before any os.environ.get()

ACCOUNT_SID    = os.environ.get("TWILIO_ACCOUNT_SID", "")
AUTH_TOKEN     = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_NUMBER  = os.environ.get("TWILIO_NUMBER", "")   # e.g. +16031234567
MY_NUMBER      = os.environ.get("MY_NUMBER", "")       # Your personal number
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

CALENDAR_ID    = "cj.dev.code@gmail.com"                              # or a specific calendar ID
CONTACTS_FILE  = Path("contacts.txt")
SCOPES         = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_FILE     = Path("token.json")
CREDENTIALS_FILE = Path("credentials.json")            # Downloaded from Google Cloud Console

if GOOGLE_SERVICE_ACCOUNT_JSON:
    CREDENTIALS_FILE.write_text(GOOGLE_SERVICE_ACCOUNT_JSON)

SCAN_INTERVAL  = int(os.environ.get("SCAN_INTERVAL", 60))   # seconds between calendar scans


twilio_client = Client(ACCOUNT_SID, AUTH_TOKEN, region="us1")

# Keep track of calls we've already initiated this session so we don't
# double-fire if the scan catches the same event twice.
_fired: set[str] = set()


# ---------------------------------------------------------------------------
# TITLE PARSING
# ---------------------------------------------------------------------------

E164_RE = re.compile(r"^\+?1?\d{10,11}$")


def _is_e164(token: str) -> bool:
    return bool(E164_RE.match(token.replace("-", "").replace(" ", "")))


def _normalise_number(raw: str) -> str:
    """Strip non-digits except leading +, ensure +1 prefix for US numbers."""
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits


def parse_title(title: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Parse a calendar event title into (target_number, contact_name_or_None).

    Returns (None, None) if the title isn't a recognised call instruction.
    """
    lower = title.strip().lower()
    if not lower.startswith("call "):
        return None, None

    body = title.strip()[5:].strip()   # everything after "call "
    parts = [p.strip() for p in body.split(",")]

    number: str | None = None
    name: str | None = None

    for part in parts:
        if _is_e164(part):
            number = _normalise_number(part)
        else:
            name = part

    if number is None and name is not None:
        # "call Taylor" — look up in contacts
        number = lookup_contact(name)
        if number is None:
            print(f"  [parse] Unknown contact '{name}' and no number provided.")
            return None, None

    if number and name:
        upsert_contact(name, number)

    return number, name


# ---------------------------------------------------------------------------
# GOOGLE CALENDAR
# ---------------------------------------------------------------------------

def _get_calendar_service():
    creds = service_account.Credentials.from_service_account_file(
        "service_account.json",
        scopes=SCOPES,
    )
    return build("calendar", "v3", credentials=creds)

def get_events_starting_now(service) -> list[dict]:
    """
    Return calendar events whose start time falls within the current minute.
    """
    now = datetime.now(timezone.utc)
    window_start = now.replace(second=0, microsecond=0)
    window_end   = window_start + timedelta(minutes=1)

    result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=window_start.isoformat(),
        timeMax=window_end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    return result.get("items", [])



# -------------------------
# twilio bridge
# -------------------------
def initiate_bridge(my_num: str, target_num: str) -> None:
    laml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial callerId="{my_num}" timeout="30" answerOnBridge="true">
        <Number codec="PCMU">{target_num}</Number>
    </Dial>
</Response>"""

    print(f"[{datetime.now()}] Calling {my_num} and bridging to {target_num}...")

    call = twilio_client.calls.create(
        twiml=laml,
        to=my_num,
        from_=TWILIO_NUMBER,
        record=False,
    )

    print(f"Call SID: {call.sid}")

# ---------------------------------------------------------------------------
# MAIN SCAN LOOP
# ---------------------------------------------------------------------------

def scan(service) -> None:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    events = get_events_starting_now(service)

    if not events:
        print(f"[{now_str}] No calls scheduled this minute.")
        return

    for event in events:
        event_id = event["id"]
        summary  = event.get("summary", "")

        if event_id in _fired:
            continue  # already initiated this call

        target_number, contact_name = parse_title(summary)

        if target_number is None:
            print(f"[{now_str}] Skipping '{summary}' (not a recognised call event).")
            continue

        label = f"→ {contact_name}" if contact_name else ""
        print(f"[{now_str}] Matched: '{summary}' {label} ({target_number})")
        initiate_bridge(MY_NUMBER, target_number)
        _fired.add(event_id)


def main() -> None:
    print("Authenticating with Google Calendar...")
    service = _get_calendar_service()
    print("Authenticated. Scanning every 60 seconds.\n")

    while True:
        try:
            scan(service)
        except Exception as e:
            print(f"[ERROR] {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
