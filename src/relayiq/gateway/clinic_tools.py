"""Clinic tools exposed through the gateway (scheduling, insurance, billing, refills, control)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from ..agents.context import CallContext
from ..db import now_iso
from .gateway import GatewayDenied, ToolDef, ToolGateway


def spoken_time(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    return dt.strftime("%A, %B %-d at %-I:%M %p").replace(":00 ", " ")


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _parse_dob(text: str) -> str | None:
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
                "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- arg schemas
class NoArgs(BaseModel):
    pass


class VerifyIdentityArgs(BaseModel):
    first_name: str
    last_name: str
    date_of_birth: str = Field(description="Date of birth as the caller said it, e.g. 'April 12, 1980' or '1980-04-12'")


class FindSlotsArgs(BaseModel):
    visit_type: Literal["new_patient", "follow_up", "any"] = "any"
    provider_name: Optional[str] = Field(None, description="Part of a provider's name, if the caller asked for one")
    day: Optional[str] = Field(None, description="Weekday name or YYYY-MM-DD, if the caller has a preference")
    part_of_day: Literal["morning", "afternoon", "any"] = "any"


class PolicyArgs(BaseModel):
    question: str


class VerifyInsuranceArgs(BaseModel):
    reason: str = Field("office visit", description="Why the visit is needed, short")


class CheckStatusArgs(BaseModel):
    check_id: str


class UpdateInsuranceArgs(BaseModel):
    payer_name: str = Field(description="Cigna, UnitedHealthcare, Aetna, ...")
    member_id: str


class BookArgs(BaseModel):
    slot_id: str
    reason: str
    self_pay: bool = Field(False, description="Caller agreed to self-pay if coverage is not active")


class CancelArgs(BaseModel):
    appointment_id: str


class RescheduleArgs(BaseModel):
    appointment_id: str
    new_slot_id: str


class RefillArgs(BaseModel):
    medication: str
    pharmacy: str


class CallbackArgs(BaseModel):
    reason: str


class TransferArgs(BaseModel):
    agent_name: str = Field(description="Name of the specialist agent to hand the call to")
    reason: str


class EscalateArgs(BaseModel):
    reason: str


class EndCallArgs(BaseModel):
    reason: str = "caller is done"


# ---------------------------------------------------------------- implementations
async def verify_identity(ctx: CallContext, a: dict) -> dict:
    if ctx.verify_attempts >= 3:
        raise GatewayDenied("Too many failed attempts. Offer to transfer to staff.")
    ctx.verify_attempts += 1
    dob = _parse_dob(a["date_of_birth"])
    if not dob:
        return {"ok": False, "message": "Could not understand the date of birth. Ask again (month, day, year)."}
    candidates = ctx.db.query("SELECT * FROM patients WHERE dob = ?", (dob,))
    match = next((p for p in candidates if _norm(p["first_name"]) == _norm(a["first_name"])
                  and _norm(p["last_name"]) == _norm(a["last_name"])), None)
    if not match:
        return {"ok": False, "attempts_left": 3 - ctx.verify_attempts,
                "message": "No patient matches that name and date of birth."}
    ctx.patient, ctx.verified = match, True
    ani = bool(ctx.caller_id_match and ctx.caller_id_match["id"] == match["id"])
    ctx.note(f"Identity verified: {match['first_name']} {match['last_name']} (patient {match['id']})"
             + ("; caller ID also matches" if ani else ""))
    ctx.db.execute("UPDATE calls SET patient_id=? WHERE call_sid=?", (match["id"], ctx.call_sid))
    ctx.emit("verified", {"patient_id": match["id"]})
    return {"ok": True, "patient_id": match["id"], "first_name": match["first_name"],
            "caller_id_match": ani}


async def get_patient_summary(ctx: CallContext, a: dict) -> dict:
    p = ctx.patient
    appts = ctx.db.query(
        "SELECT a.id, a.start, a.visit_type, a.status, a.insurance_status, pr.name AS provider "
        "FROM appointments a JOIN providers pr ON pr.id = a.provider_id "
        "WHERE a.patient_id=? AND a.status IN ('booked','pending_insurance') ORDER BY a.start",
        (p["id"],))
    if appts:
        ctx.note("Upcoming appointments (appointment_id values): " +
                 "; ".join(f"{spoken_time(x['start'])} with {x['provider']} = {x['id']}" for x in appts))
    pcp = ctx.db.one("SELECT name FROM providers WHERE id=?", (p["pcp_provider_id"],))
    bal = ctx.db.one("SELECT amount_due FROM balances WHERE patient_id=?", (p["id"],))
    return {"ok": True, "name": f"{p['first_name']} {p['last_name']}",
            "primary_care_provider": pcp["name"] if pcp else None,
            "insurance": p["payer_name"], "balance_due": bal["amount_due"] if bal else 0,
            "upcoming_appointments": [
                {**x, "when": spoken_time(x["start"])} for x in appts]}


async def list_providers(ctx: CallContext, a: dict) -> dict:
    return {"ok": True, "providers": ctx.db.query("SELECT id, name, specialty, location FROM providers")}


async def find_open_slots(ctx: CallContext, a: dict) -> dict:
    rows = ctx.db.query(
        "SELECT s.id, s.start, s.visit_type, s.location, p.name AS provider FROM slots s "
        "JOIN providers p ON p.id = s.provider_id WHERE s.status='open' AND s.start > ? "
        "ORDER BY s.start", (datetime.now().astimezone().isoformat(),))
    out = []
    for r in rows:
        dt = datetime.fromisoformat(r["start"])
        if a.get("visit_type") not in (None, "any") and r["visit_type"] != a["visit_type"]:
            continue
        if a.get("provider_name") and _norm(a["provider_name"]) not in _norm(r["provider"]):
            continue
        day = (a.get("day") or "").strip().lower()
        if day and day not in (dt.strftime("%A").lower(), dt.date().isoformat()):
            continue
        pod = a.get("part_of_day", "any")
        if pod == "morning" and dt.hour >= 12 or pod == "afternoon" and dt.hour < 12:
            continue
        out.append({**r, "when": spoken_time(r["start"])})
        if len(out) >= 4:
            break
    if out:
        # Keep ids across turns: later turns only see text history + the case file
        ctx.note("Slots offered (use these exact slot_id values): " +
                 "; ".join(f"{x['when']} with {x['provider']} = {x['id']}" for x in out))
    return {"ok": True, "slots": out, "note": "Offer at most two or three options out loud."}


async def search_clinic_policy(ctx: CallContext, a: dict) -> dict:
    return {"ok": True, "results": ctx.services["kb"].search(a["question"])}


async def get_insurance_on_file(ctx: CallContext, a: dict) -> dict:
    p = ctx.patient
    last = ctx.db.one("SELECT id, status, summary, method, created_at FROM insurance_checks "
                      "WHERE patient_id=? ORDER BY created_at DESC LIMIT 1", (p["id"],))
    return {"ok": True, "payer": p["payer_name"], "member_id_last4": (p["member_id"] or "")[-4:],
            "last_check": last}


async def verify_insurance(ctx: CallContext, a: dict) -> dict:
    result = await ctx.services["insurance"].verify(ctx, ctx.patient, a.get("reason", "office visit"))
    ctx.insurance_checks.append(result["check_id"])
    ctx.note(f"Insurance check {result['check_id']}: {result['status']} - {result.get('summary', '')}")
    return {"ok": True, **result}


async def check_insurance_status(ctx: CallContext, a: dict) -> dict:
    row = ctx.db.one("SELECT id, status, summary, method FROM insurance_checks WHERE id=? AND patient_id=?",
                     (a["check_id"], ctx.patient["id"]))
    if not row:
        return {"ok": False, "message": "No such check for this patient."}
    return {"ok": True, **row}


async def update_insurance_on_file(ctx: CallContext, a: dict) -> dict:
    from ..db import PAYERS
    payer_id = next((pid for pid, n in PAYERS.items() if _norm(n) in _norm(a["payer_name"])
                     or _norm(a["payer_name"]) in _norm(n)), None)
    if not payer_id:
        return {"ok": False, "message": f"{a['payer_name']} is not a payer we can verify automatically. "
                "Create a callback task for billing staff."}
    ctx.db.execute("UPDATE patients SET payer_id=?, payer_name=?, member_id=? WHERE id=?",
                   (payer_id, [n for k, n in PAYERS.items() if k == payer_id][0], a["member_id"].upper(),
                    ctx.patient["id"]))
    ctx.patient = ctx.db.one("SELECT * FROM patients WHERE id=?", (ctx.patient["id"],))
    ctx.note(f"Insurance on file updated to {ctx.patient['payer_name']}")
    return {"ok": True, "payer": ctx.patient["payer_name"], "next": "Run verify_insurance again."}


def _latest_check(ctx: CallContext) -> dict | None:
    if not ctx.insurance_checks:
        return None
    return ctx.db.one("SELECT * FROM insurance_checks WHERE id=?", (ctx.insurance_checks[-1],))


async def book_appointment(ctx: CallContext, a: dict) -> dict:
    slot = ctx.db.one("SELECT * FROM slots WHERE id=?", (a["slot_id"],))
    if not slot:
        return {"ok": False, "message": f"Unknown slot_id {a['slot_id']!r}. Use an exact slot_id from the "
                "'Slots offered' line in the case file (or call find_open_slots again). Do not invent ids."}
    if slot["status"] != "open":
        return {"ok": False, "message": "That slot was just taken. Find another one."}
    check = _latest_check(ctx)
    if check is None:
        raise GatewayDenied("Insurance has not been verified on this call. Call verify_insurance "
                            "before booking.")
    status = "booked"
    if check["status"] == "inactive" and not a.get("self_pay"):
        raise GatewayDenied("Coverage is not active. Tell the caller, offer to update their "
                            "insurance or book as self-pay (quote the self-pay price) and book "
                            "with self_pay=true only if they agree.")
    if check["status"] in ("pending", "unverified", "error"):
        status = "pending_insurance"
    appt_id = "apt_" + uuid.uuid4().hex[:8]
    ctx.db.execute("UPDATE slots SET status='booked' WHERE id=?", (slot["id"],))
    ctx.db.execute(
        "INSERT INTO appointments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (appt_id, ctx.patient["id"], slot["id"], slot["provider_id"], slot["start"],
         slot["visit_type"], a["reason"], status,
         "self_pay" if a.get("self_pay") else check["status"], check["id"], now_iso(),
         ctx.active_agent))
    if status == "pending_insurance":
        ctx.db.execute("UPDATE portal_jobs SET result=json_set(COALESCE(result,'{}'),'$.appointment_id',?) "
                       "WHERE id=?", (appt_id, check["id"]))
    prov = ctx.db.one("SELECT name FROM providers WHERE id=?", (slot["provider_id"],))
    ctx.db.add_journey(ctx.patient["id"], "call", f"Booked {slot['visit_type']} with {prov['name']} "
                       f"for {spoken_time(slot['start'])} ({status}).")
    ctx.note(f"Booked appointment {appt_id} on {spoken_time(slot['start'])} with {prov['name']}, status {status}")
    sms = ctx.services.get("sms")
    if sms:
        await sms.send(ctx.patient["phone"], f"{ctx.settings.clinic_name}: you're booked with "
                       f"{prov['name']} on {spoken_time(slot['start'])} at {slot['location']}. "
                       "Reply C to cancel.")
    return {"ok": True, "appointment_id": appt_id, "status": status,
            "when": spoken_time(slot["start"]), "provider": prov["name"],
            "location": slot["location"],
            "say": ("We'll confirm your coverage before the visit and text you." if
                    status == "pending_insurance" else "")}


async def cancel_appointment(ctx: CallContext, a: dict) -> dict:
    appt = ctx.db.one("SELECT * FROM appointments WHERE id=? AND patient_id=?",
                      (a["appointment_id"], ctx.patient["id"]))
    if not appt or appt["status"] == "cancelled":
        return {"ok": False, "message": "No active appointment with that id for this patient."}
    ctx.db.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (appt["id"],))
    ctx.db.execute("UPDATE slots SET status='open' WHERE id=?", (appt["slot_id"],))
    ctx.note(f"Cancelled appointment {appt['id']}")
    return {"ok": True, "cancelled": appt["id"], "when": spoken_time(appt["start"])}


async def reschedule_appointment(ctx: CallContext, a: dict) -> dict:
    appt = ctx.db.one("SELECT * FROM appointments WHERE id=? AND patient_id=? AND status!='cancelled'",
                      (a["appointment_id"], ctx.patient["id"]))
    slot = ctx.db.one("SELECT * FROM slots WHERE id=? AND status='open'", (a["new_slot_id"],))
    if not appt or not slot:
        return {"ok": False, "message": "Appointment or new slot not available."}
    ctx.db.execute("UPDATE slots SET status='open' WHERE id=?", (appt["slot_id"],))
    ctx.db.execute("UPDATE slots SET status='booked' WHERE id=?", (slot["id"],))
    ctx.db.execute("UPDATE appointments SET slot_id=?, start=?, provider_id=? WHERE id=?",
                   (slot["id"], slot["start"], slot["provider_id"], appt["id"]))
    ctx.note(f"Rescheduled {appt['id']} to {spoken_time(slot['start'])}")
    return {"ok": True, "appointment_id": appt["id"], "when": spoken_time(slot["start"])}


async def request_refill(ctx: CallContext, a: dict) -> dict:
    rid = "rx_" + uuid.uuid4().hex[:8]
    ctx.db.execute("INSERT INTO refills VALUES (?,?,?,?,?,?)",
                   (rid, ctx.patient["id"], a["medication"], a["pharmacy"], "pending_review", now_iso()))
    ctx.note(f"Refill request {rid} for {a['medication']} sent to clinician review")
    return {"ok": True, "request_id": rid, "status": "pending_review",
            "say": "Clinician reviews within 2 business days; approval is not guaranteed."}


async def get_balance(ctx: CallContext, a: dict) -> dict:
    row = ctx.db.one("SELECT * FROM balances WHERE patient_id=?", (ctx.patient["id"],))
    if not row:
        return {"ok": True, "amount_due": 0}
    return {"ok": True, "amount_due": row["amount_due"], "statement_date": row["last_statement"],
            "line_items": json.loads(row["line_items"] or "[]")}


async def send_payment_link(ctx: CallContext, a: dict) -> dict:
    sms = ctx.services.get("sms")
    portal = ctx.services.get("billing_portal_url", "")
    if not sms or not portal:
        return {"ok": False, "message": "Payment links are not configured. Create a callback task for billing."}
    await sms.send(ctx.patient["phone"], f"{ctx.settings.clinic_name}: pay your balance securely at {portal}")
    ctx.note("Payment link texted")
    return {"ok": True, "sent_to_last4": ctx.patient["phone"][-4:]}


async def create_callback_task(ctx: CallContext, a: dict) -> dict:
    pid = ctx.patient["id"] if ctx.patient else None
    tid = "task_" + uuid.uuid4().hex[:6]
    ctx.db.execute(
        "INSERT INTO journey_events(patient_id, channel, ts, summary, sentiment) VALUES (?,?,?,?,?)",
        (pid, "staff_task", now_iso(), f"[{tid}] Callback requested: {a['reason']}", "neutral"))
    ctx.note(f"Staff callback task {tid}: {a['reason']}")
    return {"ok": True, "task_id": tid, "say": "A staff member will call back within one business day."}


async def transfer_to_agent(ctx: CallContext, a: dict) -> dict:
    registry = ctx.services["registry"]
    target = a["agent_name"]
    if target == ctx.active_agent:
        return {"ok": False, "message": "You are already that agent. Help the caller directly."}
    if not registry.is_routable(target, ctx):
        return {"ok": False, "message": f"Unknown agent {target}. Options: {registry.routable_names(ctx)}"}
    ctx.handoff_to = target
    ctx.handoff_note = a["reason"]
    return {"ok": True, "message": f"Handed off to {target}. Do not say anything more."}


async def escalate_to_human(ctx: CallContext, a: dict) -> dict:
    ctx.pending_action = {"type": "transfer", "reason": a["reason"]}
    ctx.db.execute("UPDATE calls SET escalated=1 WHERE call_sid=?", (ctx.call_sid,))
    return {"ok": True, "message": "Tell the caller you're connecting them to a team member now."}


async def end_call(ctx: CallContext, a: dict) -> dict:
    ctx.pending_action = {"type": "hangup", "reason": a.get("reason", "")}
    return {"ok": True, "message": "Say a brief goodbye."}


def register_clinic_tools(gw: ToolGateway) -> ToolGateway:
    T = ToolDef
    for t in [
        T("verify_identity", "auth", "Verify the caller is the patient using full name and date of birth. "
          "Required before discussing any health or account details.", VerifyIdentityArgs, verify_identity),
        T("get_patient_summary", "read", "Verified patient's upcoming appointments, PCP, insurance, balance.",
          NoArgs, get_patient_summary, requires_verified=True),
        T("list_providers", "read", "Clinic providers and locations.", NoArgs, list_providers),
        T("find_open_slots", "read", "Find open appointment slots.", FindSlotsArgs, find_open_slots),
        T("search_clinic_policy", "read", "Look up clinic policies: hours, locations, insurance accepted, "
          "self-pay prices, cancellations, refills, billing, emergencies.", PolicyArgs, search_clinic_policy),
        T("get_insurance_on_file", "read", "Payer and last eligibility check on file.", NoArgs,
          get_insurance_on_file, requires_verified=True),
        T("verify_insurance", "external", "Check the patient's insurance eligibility with the payer in real "
          "time (clearinghouse API, falls back to the payer portal). Required before booking.",
          VerifyInsuranceArgs, verify_insurance, requires_verified=True, minutes_saved=7),
        T("check_insurance_status", "read", "Status of a pending insurance verification.", CheckStatusArgs,
          check_insurance_status, requires_verified=True),
        T("update_insurance_on_file", "write", "Update the payer and member ID on file.",
          UpdateInsuranceArgs, update_insurance_on_file, requires_verified=True,
          requires_confirmation=True, minutes_saved=2),
        T("book_appointment", "write", "Book an open slot for the verified patient. Requires an insurance "
          "check on this call.", BookArgs, book_appointment, requires_verified=True,
          requires_confirmation=True, minutes_saved=4),
        T("cancel_appointment", "write", "Cancel one of the patient's appointments.", CancelArgs,
          cancel_appointment, requires_verified=True, requires_confirmation=True, minutes_saved=2),
        T("reschedule_appointment", "write", "Move an appointment to another open slot.", RescheduleArgs,
          reschedule_appointment, requires_verified=True, requires_confirmation=True, minutes_saved=3),
        T("request_refill", "write", "Send a prescription refill request to the clinician for review "
          "(not for controlled substances).", RefillArgs, request_refill, requires_verified=True,
          requires_confirmation=True, minutes_saved=3),
        T("get_balance", "read", "Patient's balance and statement line items.", NoArgs, get_balance,
          requires_verified=True),
        T("send_payment_link", "write", "Text a secure payment link to the phone number on file. Never take "
          "card numbers by voice.", NoArgs, send_payment_link, requires_verified=True,
          requires_confirmation=True, minutes_saved=3),
        T("create_callback_task", "write", "Create a task for clinic staff to call the patient back.",
          CallbackArgs, create_callback_task, minutes_saved=1),
        T("transfer_to_agent", "control", "Hand the conversation to a specialist agent.", TransferArgs,
          transfer_to_agent),
        T("escalate_to_human", "control", "Transfer the call to a human staff member.", EscalateArgs,
          escalate_to_human),
        T("end_call", "control", "End the call after the caller is done.", EndCallArgs, end_call),
    ]:
        gw.register(t)
    return gw
