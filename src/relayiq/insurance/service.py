"""Insurance verification service: clearinghouse API first, payer-portal agent as fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from ..agents.context import CallContext
from ..config import Settings
from ..db import Database, now_iso
from .eligibility import StediEligibilityClient
from .portal_agent import PayerPortalAgent, PortalCredentials

log = logging.getLogger("relayiq.insurance")


class InsuranceService:
    def __init__(self, settings: Settings, db: Database, api: StediEligibilityClient,
                 portal: PayerPortalAgent | None, sms=None):
        self.s, self.db, self.api, self.portal, self.sms = settings, db, api, portal, sms
        self.background: set[asyncio.Task] = set()

    def portal_credentials(self, payer_id: str) -> PortalCredentials | None:
        s = self.s
        table = {
            "62308": (s.portal_cigna_url, s.portal_cigna_username, s.portal_cigna_password,
                      s.portal_cigna_totp_secret),
            "87726": (s.portal_uhc_url, s.portal_uhc_username, s.portal_uhc_password,
                      s.portal_uhc_totp_secret),
        }
        row = table.get(payer_id)
        if not row or not row[1] or not row[2]:
            return None
        return PortalCredentials(*row)

    def _record(self, check_id: str, patient: dict, method: str, status: str, summary: str,
                details: Any, done: bool = True) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO insurance_checks VALUES (?,?,?,?,?,?,?,?,?)",
            (check_id, patient["id"], patient["payer_id"], method, status, summary,
             json.dumps(details, default=str)[:20000], now_iso(), now_iso() if done else None))

    async def verify(self, ctx: CallContext | None, patient: dict, reason: str = "office visit") -> dict:
        check_id = "ins_" + uuid.uuid4().hex[:8]
        api_status = None
        if self.api.configured:
            res = await self.api.check(patient)
            api_status = res.status
            if res.status in ("active", "inactive"):
                self._record(check_id, patient, "clearinghouse_api", res.status, res.summary, res.raw)
                return {"check_id": check_id, "status": res.status, "summary": res.summary,
                        "copay": res.copay, "method": "clearinghouse_api"}
            log.info("API inconclusive (%s): %s", res.status, res.summary)

        creds = self.portal_credentials(patient["payer_id"])
        if self.s.portal_automation_enabled and self.portal and creds:
            self._record(check_id, patient, "payer_portal", "pending",
                         "Checking the payer portal", {"api_status": api_status}, done=False)
            self.db.execute("INSERT INTO portal_jobs VALUES (?,?,?,?,?,?,?,?)",
                            (check_id, patient["id"], patient["payer_id"], "running", 0, "{}",
                             now_iso(), None))
            task = asyncio.create_task(self._run_portal(check_id, patient, creds, ctx))
            self.background.add(task)
            task.add_done_callback(self.background.discard)
            return {"check_id": check_id, "status": "pending", "method": "payer_portal",
                    "summary": "Verifying on the payer's portal; usually takes about a minute. "
                               "You can book now as pending verification; the patient gets a text "
                               "when it's confirmed."}

        why = ("no clearinghouse key configured" if not self.api.configured else
               f"clearinghouse result {api_status}")
        self._record(check_id, patient, "none", "unverified",
                     f"Could not verify automatically ({why}); staff will verify before the visit",
                     {"api_status": api_status})
        return {"check_id": check_id, "status": "unverified", "method": "none",
                "summary": "Couldn't verify automatically right now; staff will confirm coverage "
                           "before the visit. Booking is allowed as pending verification."}

    async def _run_portal(self, check_id: str, patient: dict, creds: PortalCredentials,
                          ctx: CallContext | None) -> None:
        try:
            res = await self.portal.run(check_id, patient["payer_id"], creds, patient)
            summary = {"active": "Coverage is active per payer portal",
                       "inactive": "Payer portal shows coverage is not active",
                       "unknown": "Payer portal check was inconclusive"}[res.status]
            if res.plan_name:
                summary += f" ({res.plan_name})"
            if res.copay is not None:
                summary += f"; copay about ${res.copay:.0f}"
            self._record(check_id, patient, "payer_portal", res.status, summary, res.__dict__)
            self.db.execute("UPDATE portal_jobs SET status=?, steps=?, result=json_patch(COALESCE(result,'{}'), ?), "
                            "completed_at=? WHERE id=?",
                            ("done", res.steps, json.dumps(res.__dict__, default=str), now_iso(), check_id))
        except Exception as e:  # noqa: BLE001
            log.exception("portal job failed")
            summary = f"Payer portal check failed ({type(e).__name__}); staff will verify"
            self._record(check_id, patient, "payer_portal", "error", summary, {"error": str(e)[:500]})
            self.db.execute("UPDATE portal_jobs SET status='failed', completed_at=? WHERE id=?",
                            (now_iso(), check_id))
            res = None
        status = res.status if res else "error"
        self.db.execute("UPDATE appointments SET insurance_status=?, status=CASE WHEN ?='active' "
                        "THEN 'booked' ELSE status END WHERE insurance_check_id=?",
                        (status, status, check_id))
        if ctx is not None:
            ctx.notices.append(f"Insurance verification update ({check_id}): {summary}")
            ctx.emit("insurance", {"check_id": check_id, "status": status})
        if self.sms:
            msg = {"active": "your insurance is verified for your upcoming visit.",
                   "inactive": "we couldn't confirm active coverage. Please call us or bring your "
                               "current insurance card."}.get(status, "our staff will confirm your "
                                                                "insurance before your visit.")
            await self.sms.send(patient["phone"], f"{self.s.clinic_name}: {msg}")
