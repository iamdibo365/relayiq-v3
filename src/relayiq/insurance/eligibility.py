"""Real-time eligibility (X12 270/271 as JSON) through the Stedi clearinghouse API.

Request/response shapes follow Stedi's eligibility-check API. The parser is defensive:
it looks for plan status codes and copay benefits anywhere in the response, so it keeps
working across API versions.
"""

from __future__ import annotations

import logging
import uuid
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
    raw: dict[str, Any] = field(default_factory=dict)


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def parse_eligibility(resp: dict[str, Any]) -> EligibilityResult:
    errors = resp.get("errors") or []
    if errors:
        msg = "; ".join(str(e.get("description") or e.get("message") or e) for e in errors)[:300]
        return EligibilityResult("error", f"Payer returned an error: {msg}", raw=resp)

    status = "unknown"
    plan_name = ""
    copay = None
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

    if status == "active":
        summary = "Coverage is active" + (f" under {plan_name}" if plan_name else "")
        if copay is not None:
            summary += f"; office visit copay about ${copay:.0f}"
    elif status == "inactive":
        summary = "Payer reports coverage is not active"
    else:
        summary = "Payer response did not include a clear coverage status"
    return EligibilityResult(status, summary, plan_name, copay, resp)


class StediEligibilityClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.s = settings
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0))

    @property
    def configured(self) -> bool:
        return bool(self.s.stedi_api_key)

    def build_request(self, patient: dict[str, Any], service_code: str = "30") -> dict[str, Any]:
        return {
            "controlNumber": uuid.uuid4().int.__str__()[:9],
            "payerId": patient["payer_id"],
            "provider": {
                "name": {"organization": self.s.clinic_name},
                "npi": self.s.clinic_npi,
            },
            "subscriber": {
                "memberId": patient["member_id"],
                "dateOfBirth": patient["dob"],
                "name": {"person": {"firstName": patient["first_name"],
                                    "lastName": patient["last_name"]}},
            },
            "encounter": {"services": [{"value": service_code, "system": "STC"}]},
        }

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
