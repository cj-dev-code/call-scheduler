import os
import re
import sched
from tabnanny import check
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

from flask import Flask, request as flask_request
from pyngrok import ngrok, conf

from dotenv import load_dotenv
load_dotenv()  # must be called before any os.environ.get()

ACCOUNT_SID    = os.environ.get("TWILIO_ACCOUNT_SID", "")
AUTH_TOKEN     = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_NUMBER  = os.environ.get("TWILIO_NUMBER", "")   # e.g. +16031234567
MY_NUMBER      = os.environ.get("MY_NUMBER", "")       # Your personal number
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
NGROK_AUTH_TOKEN = os.environ.get("NGROK_AUTH_TOKEN", "")  # from ngrok.com dashboard
FLASK_PORT     = int(os.environ.get("FLASK_PORT", 5000))

CALENDAR_ID    = "cj.dev.code@gmail.com"                              # or a specific calendar ID
CONTACTS_FILE  = Path("contacts.txt")
SCOPES         = ["https://www.googleapis.com/auth/calendar"]
TOKEN_FILE     = Path("token.json")
CREDENTIALS_FILE = Path("credentials.json")            # Downloaded from Google Cloud Console

if GOOGLE_SERVICE_ACCOUNT_JSON:
    CREDENTIALS_FILE.write_text(GOOGLE_SERVICE_ACCOUNT_JSON)

SCAN_INTERVAL  = int(os.environ.get("SCAN_INTERVAL", 60))   # seconds between calendar scans
TIMEOUT = 15  # seconds to let the phone ring before giving up

twilio_client = Client(ACCOUNT_SID, AUTH_TOKEN, region="us1")

# Keep track of calls we've already initiated this session so we don't
# double-fire if the scan catches the same event twice.
_fired: dict = {}  # event_id → start dateTime string


# ---------------------------------------------------------------------------
# TITLE PARSING
# ---------------------------------------------------------------------------

E164_RE = re.compile(r"^\+?1?\d{10,11}$")


def _clean_number(token: str) -> str:
    """Strip common phone-number punctuation: spaces, dashes, parens, dots."""
    return (
        token.replace("-", "")
             .replace(" ", "")
             .replace("(", "")
             .replace(")", "")
             .replace(".", "")
    )


def _is_e164(token: str) -> bool:
    return bool(E164_RE.match(_clean_number(token)))


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

    # if number is None and name is not None:
    #     # "call Taylor" — look up in contacts
    #     number = lookup_contact(name)
    #     if number is None:
    #         print(f"  [parse] Unknown contact '{name}' and no number provided.")
    #         return None, None

    # if number and name:
    #     upsert_contact(name, number)

    return number, name


# ---------------------------------------------------------------------------
# GOOGLE CALENDAR
# ---------------------------------------------------------------------------

def create_and_share_calendar(phone_number: str, email: str) -> str:
    """
    Creates a calendar named <phone_number> and shares it with <email>.
    If a calendar with this exact phone_number + email pairing already
    exists, skips creation and returns the existing calendar's ID.
    Returns the calendar's ID.
    """
    service = _get_calendar_service()

    # Check for an existing calendar with this phone number that is
    # already shared with this email.
    existing = service.calendarList().list().execute().get("items", [])
    for cal in existing:
        if cal.get("summary", "") != phone_number:
            continue
        acl_rules = service.acl().list(calendarId=cal["id"]).execute().get("items", [])
        for rule in acl_rules:
            scope = rule.get("scope", {})
            if scope.get("type") == "user" and scope.get("value", "").lower() == email.lower():
                print(f"Calendar for {phone_number} already shared with {email}, skipping.")
                return cal["id"]

    # Create the calendar
    calendar = service.calendars().insert(body={
        "summary": phone_number,
        "timeZone": "America/New_York"
    }).execute()

    calendar_id = calendar["id"]
    print(f"Created calendar '{phone_number}' with ID: {calendar_id}")

    # Share it with the user's email
    service.acl().insert(
        calendarId=calendar_id,
        body={
            "role": "writer",
            "scope": {
                "type": "user",
                "value": email
            }
        }
    ).execute()

    print(f"Shared with {email}")
    return calendar_id


def list_owned_calendars() -> List[dict]:
    """
    Manual utility: prints and returns every calendar the service account
    owns (i.e. created itself, as opposed to ones merely shared with it),
    along with the emails each calendar is shared with.
    Call this directly, e.g. `python -c "from call_scripy import list_owned_calendars; list_owned_calendars()"`
    """
    service = _get_calendar_service()
    result = service.calendarList().list().execute()
    calendars = result.get("items", [])

    owned = [c for c in calendars if c.get("accessRole") == "owner"]

    print(f"Service account owns {len(owned)} calendar(s):\n")
    for cal in owned:
        acl_rules = service.acl().list(calendarId=cal["id"]).execute().get("items", [])
        shared_with = [
            rule["scope"]["value"]
            for rule in acl_rules
            if rule.get("scope", {}).get("type") == "user"
        ]
        print(f"  {cal.get('summary', '(no name)'):20}  id: {cal['id']}")
        if shared_with:
            for email in shared_with:
                print(f"      shared with: {email}")
        else:
            print(f"      shared with: (none)")

    return owned


def delete_calendar(calendar_id: str) -> None:
    """
    Manual utility: permanently deletes a calendar owned by the service
    account. This is irreversible. Pass the calendar_id from
    list_owned_calendars().
    """
    service = _get_calendar_service()
    try:
        service.calendars().delete(calendarId=calendar_id).execute()
        print(f"Deleted calendar: {calendar_id}")
    except Exception as e:
        print(f"Failed to delete {calendar_id}: {e}")


def delete_calendars_for_number(phone_number: str) -> int:
    """
    Deletes every calendar owned by the service account whose title
    (summary) matches phone_number exactly. Returns the count deleted.
    """
    service = _get_calendar_service()
    result = service.calendarList().list().execute()
    calendars = result.get("items", [])

    matches = [c for c in calendars if c.get("summary", "") == phone_number]

    deleted = 0
    for cal in matches:
        try:
            service.calendars().delete(calendarId=cal["id"]).execute()
            print(f"Deleted calendar for {phone_number}: {cal['id']}")
            deleted += 1
        except Exception as e:
            print(f"Failed to delete {cal['id']}: {e}")

    if deleted == 0:
        print(f"No calendars found for {phone_number}.")

    return deleted

def _get_calendar_service():
    creds = service_account.Credentials.from_service_account_file(
        "service_account.json",
        scopes=SCOPES,
    )
    return build("calendar", "v3", credentials=creds)

def get_app_calendars(service) -> List[dict]:
    """
    Return all calendars shared with the service account whose summary
    (display name) looks like a phone number e.g. +16031234567.
    """
    result = service.calendarList().list().execute()
    calendars = result.get("items", [])
    app_cals = []
    for cal in calendars:
        summary = cal.get("summary", "")
        if _is_e164(summary):
            app_cals.append(cal)
    return app_cals

def get_events_starting_now(service, cals) -> list[dict]:
    """
    Return calendar events whose start time falls within the current minute.
    """
    now = datetime.now(timezone.utc)
    window_start = now.replace(second=0, microsecond=0)
    window_end   = window_start + timedelta(minutes=1)

    remainder = []
    for cal in cals:
        result = service.events().list(
            calendarId=cal['id'],
            timeMin=window_start.isoformat(),
            timeMax=window_end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        result = result.get("items", [])
        for e in result:
            e['calendar_id'] = cal['id']  # attach calendar ID for later reference
            e['host_number'] = _normalise_number(cal['summary'])
        remainder.extend(result)
    return remainder



# -------------------------
# twilio bridge
# -------------------------
def initiate_bridge(my_num: str, target_num: str) -> None:
    # Call the host and bridge immediately using Twilio's answerOnBridge
    twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Pause length="3"/>
    <Dial callerId="{my_num}" answerOnBridge="true" timeout="30">
        <Number>{target_num}</Number>
    </Dial>
</Response>"""
    
    print(f"[{datetime.now()}] Calling {my_num} and bridging to {target_num}")
    
    # This creates a call from Twilio to your number, and if/when you answer,
    # it will automatically dial the target number and bridge the call
    call = twilio_client.calls.create(
        twiml=twiml_response,
        to=my_num,
        from_=TWILIO_NUMBER,
        timeout=TIMEOUT,
        machine_detection="Enable",
        machine_detection_timeout=TIMEOUT+3,
    )
    
    total_scan_time = TIMEOUT - 3  # we want to end the call before the timeout fully
    # expires in order to prevent twilio from calling the target number if we let
    # twilio ring out. that means continually scanning, and 
    # ending the call ourselves if we are near the end of the timeout.
    sleep_interval = .3 # seconds between status checks

    remaining_scan = int(total_scan_time/sleep_interval)
    _ = remaining_scan
    status = "queued"
    while _ > 0:
        time.sleep(sleep_interval)
        call_info = twilio_client.calls(call.sid).fetch()
        pstatus, status = status, call_info.status
        answeredby = call_info.answered_by    
        # print(_, status, answeredby)
        if pstatus == "ringing" and status == "in-progress":
            _ += int(3/sleep_interval+0.5) # if we just got answered, 
                # check for machine start for at least 3 more seconds
                # to ensure the twilio api can call off the call correctly.
        if status == "in-progress" and answeredby == "machine_start":
            twilio_client.calls(call.sid).update(status="completed")
            break
        if status == "completed":
            break
        _ -= 1
        # okay. so, if the call is answeredby a machine,
        #  we should probably cancel the call immediately to avoid weirdness. 
    # if the answering machine terminates us early, then we are still caught in machine start.
    # we have 3 whole seconds. im not sure how long the twilio api takes to notice a machine start,
    # but we can change that by increasing the rate on the scan interval.
    # i dont want to exceed 3 seconds of pause time for ux.
    if status == "ringing": # we have rung out the timeout window. end on our terms.
        # in order to avoid twilio calling the target number after we let it ring out.
        twilio_client.calls(call.sid).update(status="completed")
    # if status == "no-answer": # in this case, twilio correctly ends the call.
    #     twilio_client.calls(call.sid).update(status="completed")
    print(f"  [bridge] Call initiated: {call.sid}")
    print(f"  [bridge] Will ring for 15 seconds. If unanswered, call will end.")


# ---------------------------------------------------------------------------
# SMS ONBOARDING (NEW)
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

flask_app = Flask(__name__)


@flask_app.route("/sms", methods=["POST"])
def sms_handler():
    """
    Twilio hits this when an SMS arrives at TWILIO_NUMBER.
    If the message body contains an email, create + share a calendar
    named after the sender's phone number.
    """
    from_number = flask_request.form.get("From", "").strip()
    body        = flask_request.form.get("Body", "").strip()

    print(f"  [sms] Received from {from_number}: {body}")

    phone = _normalise_number(from_number)

    if body.strip().lower() == "delete":
        print(f"  [sms] Delete request from {phone}")
        threading.Thread(
            target=delete_calendars_for_number,
            args=(phone,),
            daemon=True,
        ).start()
        return "", 204

    email_match = EMAIL_RE.search(body)
    if not email_match:
        print("  [sms] No email found in message body, ignoring.")
        return "", 204

    email = email_match.group(0).lower()

    print(f"  [sms] Onboarding {phone} -> {email}")

    # Run in a background thread so the webhook responds immediately
    threading.Thread(
        target=create_and_share_calendar,
        args=(phone, email),
        daemon=True,
    ).start()

    return "", 204


def start_ngrok_and_update_webhook() -> None:
    """
    Kill any existing ngrok tunnels, start a fresh one on FLASK_PORT,
    then point the Twilio number's SMS webhook at it.
    """
    if NGROK_AUTH_TOKEN:
        conf.get_default().auth_token = NGROK_AUTH_TOKEN

    for tunnel in ngrok.get_tunnels():
        ngrok.disconnect(tunnel.public_url)
        print(f"  [ngrok] Killed existing tunnel: {tunnel.public_url}")

    tunnel = ngrok.connect(FLASK_PORT, "http")
    public_url = tunnel.public_url.replace("http://", "https://")
    webhook_url = f"{public_url}/sms"
    print(f"  [ngrok] Tunnel started: {webhook_url}")

    numbers = twilio_client.incoming_phone_numbers.list(phone_number=TWILIO_NUMBER)
    if numbers:
        numbers[0].update(sms_url=webhook_url, sms_method="POST")
        print(f"  [twilio] SMS webhook updated to: {webhook_url}")
    else:
        print(f"  [twilio] WARNING: could not find number {TWILIO_NUMBER} to update webhook.")


def start_flask() -> None:
    flask_app.run(host="0.0.0.0", port=FLASK_PORT, use_reloader=False)


# ---------------------------------------------------------------------------
# MAIN SCAN LOOP
# ---------------------------------------------------------------------------

def scan(service) -> None:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    cals = get_app_calendars(service)
    events = get_events_starting_now(service, cals)
    
    if not events:
        print(f"[{now_str}] No calls scheduled this minute.")
        return

    for event in events:
        event_id = event['calendar_id'] + ":" + event['id']
        print(event_id, _fired)
        summary  = event.get("summary", "")

        start = event.get("start", {})
        start_time = start.get("dateTime", start.get("date", ""))

        if event_id in _fired and _fired[event_id] == start_time:
            continue  # same start time, already called

        target_number, contact_name = parse_title(summary)

        if target_number is None:
            print(f"[{now_str}] Skipping '{summary}' (not a recognised call event).")
            continue

        label = f"→ {contact_name}" if contact_name else ""
        print(f"[{now_str}] Matched: '{summary}' {label} ({target_number})")
        threading.Thread(
            target=initiate_bridge,
            args=(event['host_number'], target_number),
            daemon=True,
        ).start()
        _fired[event_id] = start_time


def main() -> None:
    print("Authenticating with Google Calendar...")
    service = _get_calendar_service()
    print("Authenticated.")

    # NEW: start ngrok + point Twilio's SMS webhook at it
    print("Starting ngrok tunnel and updating Twilio webhook...")
    start_ngrok_and_update_webhook()

    # NEW: start the Flask SMS receiver in the background
    print(f"Starting SMS listener on port {FLASK_PORT}...")
    threading.Thread(target=start_flask, daemon=True).start()

    print("Scanning every 60 seconds.\n")

    while True:
        try:
            scan(service)
        except Exception as e:
            print(f"[ERROR] {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()