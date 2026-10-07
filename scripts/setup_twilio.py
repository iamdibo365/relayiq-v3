"""Point your Twilio number at this app and (optionally) map your cell to a demo patient.

  uv run python scripts/setup_twilio.py                     # set Voice webhook to PUBLIC_BASE_URL
  uv run python scripts/setup_twilio.py --demo-caller +1214XXXXXXX   # calling from your cell = John Doe
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from twilio.rest import Client  # noqa: E402

from relayiq.config import get_settings  # noqa: E402
from relayiq.db import Database, seed  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--demo-caller", help="Your cell number; it becomes John Doe's phone on file")
ap.add_argument("--patient", default="pt_1001")
args = ap.parse_args()
s = get_settings()

if args.demo_caller:
    db = Database(s.db_file)
    seed(db, s.clinic_timezone)
    db.execute("UPDATE patients SET phone=? WHERE id=?", (args.demo_caller, args.patient))
    p = db.one("SELECT first_name, last_name FROM patients WHERE id=?", (args.patient,))
    print(f"Caller ID {args.demo_caller} now maps to {p['first_name']} {p['last_name']} ({args.patient})")

if not (s.twilio_account_sid and s.twilio_auth_token and s.twilio_phone_number):
    sys.exit("Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER in .env")
client = Client(s.twilio_account_sid, s.twilio_auth_token)
numbers = client.incoming_phone_numbers.list(phone_number=s.twilio_phone_number, limit=1)
if not numbers:
    sys.exit(f"{s.twilio_phone_number} is not a number on this Twilio account")
url = s.public_base_url.rstrip("/") + "/twilio/voice"
numbers[0].update(voice_url=url, voice_method="POST")
print(f"{s.twilio_phone_number} -> {url}")
print("Call it now. Dashboard:", s.public_base_url.rstrip("/") + "/")
