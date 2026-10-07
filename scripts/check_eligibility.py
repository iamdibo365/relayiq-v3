"""One real eligibility check through the clearinghouse API, for a patient in the demo DB.

  uv run python scripts/check_eligibility.py pt_1001
  # Stedi test mode: first copy a mock request's member values onto the patient:
  uv run python scripts/check_eligibility.py pt_1001 --payer 87726 --member-id <from Stedi docs> \
      --first <first> --last <last> --dob YYYY-MM-DD
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from relayiq.config import get_settings  # noqa: E402
from relayiq.db import PAYERS, Database, seed  # noqa: E402
from relayiq.insurance.eligibility import StediEligibilityClient  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("patient_id")
ap.add_argument("--payer")
ap.add_argument("--member-id")
ap.add_argument("--first")
ap.add_argument("--last")
ap.add_argument("--dob")
a = ap.parse_args()
s = get_settings()
db = Database(s.db_file)
seed(db, s.clinic_timezone)
updates = {"payer_id": a.payer, "member_id": a.member_id, "first_name": a.first, "last_name": a.last, "dob": a.dob}
for col, val in updates.items():
    if val:
        db.execute(f"UPDATE patients SET {col}=? WHERE id=?", (val, a.patient_id))
if a.payer:
    db.execute("UPDATE patients SET payer_name=? WHERE id=?", (PAYERS.get(a.payer, a.payer), a.patient_id))
patient = db.one("SELECT * FROM patients WHERE id=?", (a.patient_id,))
if not patient:
    sys.exit("no such patient")
if not s.stedi_api_key:
    sys.exit("Set STEDI_API_KEY in .env (a test key works against Stedi's mock requests)")
res = asyncio.run(StediEligibilityClient(s).check(patient))
print(f"{res.status.upper()}: {res.summary}")
print(json.dumps(res.raw, indent=2)[:4000])
