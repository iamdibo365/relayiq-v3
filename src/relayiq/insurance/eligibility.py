"""Real-time eligibility (X12 270/271 as JSON) through the Stedi clearinghouse API.

Request/response shapes follow Stedi's eligibility-check API. The parser is defensive:
it looks for plan status codes and copay benefits anywhere in the response, so it keeps
working across API versions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings

log = logging.getLogger("relayiq.eligibility")

# X12 EB01 eligibility codes
ACTIVE_CODES = {"1", "2", "3", "4", "5"}  # active coverage variants
INACTIVE_CODES = {"6", "7", "8"}  # inactive / pending inactive


@dataclass
class EligibilityResult:
    status: str  # active | inactive | unknown | error
    summary: str
    plan_name: str = ""
    copay: float | None = None
    notes: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


OFFICE_SERVICES = {"30", "98", "BZ"}  # plan coverage, professional office visit, physician visit
_NOTE_HINTS = ("OUT NETWORK", "OUT-OF-NETWORK", "NOT COVERED", "PRIOR AUTH", "REFERRAL")


def _svc(item: dict) -> str:
    svc = item.get("service") or {}
    return str(svc.get("value", "")) if isinstance(svc, dict) else ""


def _parse_plans(resp: dict[str, Any]) -> EligibilityResult | None:
    """Current Stedi format: plans[].benefits.{statuses, coPayment, deductible, ...}."""
    plans = resp.get("plans")
    if not isinstance(plans, list) or not plans:
        return None
    statuses = [st for p in plans for st in ((p.get("benefits") or {}).get("statuses") or [])]
    if not statuses:
        return None
    primary = [st for st in statuses if _svc(st) == "30"] or statuses
    labels = [str(st.get("status", "")).upper() for st in primary]
    if any(lbl.startswith("ACTIVE") for lbl in labels):
        status = "active"
    elif any("INACTIVE" in lbl for lbl in labels):
        status = "inactive"
    else:
        status = "unknown"
    plan_name = next((st.get("planCoverageDescription") for st in primary if st.get("planCoverageDescription")), "")
    notes = []
    for st in primary:
        for m in st.get("messages") or []:
            if any(h in m.upper() for h in _NOTE_HINTS) and m not in notes:
                notes.append(m)
    copay = None
    copays = [c for p in plans for c in ((p.get("benefits") or {}).get("coPayment") or [])
              if _svc(c) in OFFICE_SERVICES and c.get("amount") not in (None, "")]
    copays.sort(key=lambda c: (c.get("network") or {}).get("indicator") != "IN_NETWORK")
    if copays:
        try:
            copay = float(copays[0]["amount"])
        except (TypeError, ValueError):
            copay = None
    return EligibilityResult(status, "", plan_name, copay, notes, resp)


def _parse_legacy(resp: dict[str, Any]) -> EligibilityResult:
    """Older formats: planStatus[] / benefitsInformation[] with X12 EB01 codes."""
    status, plan_name, copay = "unknown", "", None
    for node in _walk(resp):
        code = str(node.get("statusCode") or node.get("code") or "")
        text = str(node.get("status") or node.get("name") or "").lower()
        if "planStatus" in node or "statusCode" in node or "coverage" in text:
            if code in ACTIVE_CODES or "active coverage" in text:
                status = "active"
            elif status != "active" and (code in INACTIVE_CODES or "inactive" in text):
                status = "inactive"
        if not plan_name:
            plan_name = str(node.get("planDescription") or node.get("groupDescription")
                            or node.get("planName") or "")
        is_copay = str(node.get("code")) == "B" or "co-payment" in text or "copay" in text
        amount = node.get("benefitAmount") or node.get("amount")
        if is_copay and amount not in (None, "") and copay is None:
            try:
                svc = node.get("serviceTypeCodes") or []
                if not svc or "30" in svc or "98" in svc:
                    copay = float(amount)
            except (TypeError, ValueError):
                pass
    return EligibilityResult(status, "", plan_name, copay, [], resp)


def parse_eligibility(resp: dict[str, Any]) -> EligibilityResult:
    errors = resp.get("errors") or []
    if errors:
        msg = "; ".join(str(e.get("description") or e.get("message") or e) for e in errors)[:300]
        return EligibilityResult("error", f"Payer returned an error: {msg}", raw=resp)

    r = _parse_plans(resp) or _parse_legacy(resp)
    if r.status == "active":
        r.summary = "Coverage is active" + (f" under {r.plan_name.title() if r.plan_name.isupper() else r.plan_name}" if r.plan_name else "")
        if r.copay is not None:
            r.summary += f"; office visit copay about ${r.copay:.0f}"
        if r.notes:
            r.summary += ". Payer notes: " + "; ".join(n.capitalize() for n in r.notes)
    elif r.status == "inactive":
        r.summary = "Payer reports coverage is not active"
    else:
        r.summary = "Payer response did not include a clear coverage status"
    return r


class StediEligibilityClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.s = settings
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0))

    @property
    def configured(self) -> bool:
        return bool(self.s.stedi_api_key)

    def build_request(self, patient: dict[str, Any], service_code: str = "30") -> dict[str, Any]:
        """Subscriber-only request, or subscriber + dependent when the patient is covered under
        someone else's plan (spouse, child): the member ID belongs to the subscriber, the date of
        birth and name in `dependent` belong to the patient."""
        body: dict[str, Any] = {
            "payerId": patient["payer_id"],
            "provider": {
                "name": {"organization": self.s.clinic_name},
                "npi": self.s.clinic_npi,
            },
            "encounter": {"services": [{"value": service_code, "system": "STC"}]},
        }
        patient_name = {"person": {"firstName": patient["first_name"], "lastName": patient["last_name"]}}
        if patient.get("subscriber_first_name"):
            subscriber: dict[str, Any] = {
                "memberId": patient["member_id"],
                "name": {"person": {"firstName": patient["subscriber_first_name"],
                                    "lastName": patient.get("subscriber_last_name") or patient["last_name"]}},
            }
            if patient.get("subscriber_dob"):
                subscriber["dateOfBirth"] = patient["subscriber_dob"]
            body["subscriber"] = subscriber
            body["dependent"] = {"name": patient_name, "dateOfBirth": patient["dob"]}
        else:
            body["subscriber"] = {"memberId": patient["member_id"], "dateOfBirth": patient["dob"],
                                  "name": patient_name}
        return body

    async def check(self, patient: dict[str, Any]) -> EligibilityResult:
        if not self.configured:
            return EligibilityResult("error", "Clearinghouse API key not configured")
        body = self.build_request(patient)
        try:
            r = await self.http.post(
                self.s.stedi_eligibility_url, json=body,
                headers={"Authorization": self.s.stedi_api_key, "Content-Type": "application/json"})
        except httpx.HTTPError as e:
            log.warning("eligibility transport error: %s", e)
            return EligibilityResult("error", f"Clearinghouse unreachable: {type(e).__name__}")
        try:
            data = r.json()
        except ValueError:
            data = {"text": r.text[:500]}
        if r.status_code >= 400:
            return EligibilityResult("error", f"Clearinghouse HTTP {r.status_code}", raw=data)
        return parse_eligibility(data)
