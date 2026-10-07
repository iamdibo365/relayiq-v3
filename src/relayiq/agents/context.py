"""Per-call state shared by the orchestrator, the tool gateway and the watchdog."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Settings
from ..db import Database


@dataclass
class CallContext:
    call_sid: str
    caller_phone: str
    db: Database
    settings: Settings
    mode: str = "voice"  # voice | sim | text
    patient: dict[str, Any] | None = None  # set once identity is verified
    caller_id_match: dict[str, Any] | None = None  # patient whose phone matches ANI (not yet verified)
    verified: bool = False
    verify_attempts: int = 0
    journey: str = ""  # cross-channel context from the MCP server
    case_file: list[str] = field(default_factory=list)  # facts established by tools this call
    notices: list[str] = field(default_factory=list)  # async updates (e.g. portal verification)
    active_agent: str = "front_desk"
    handoff_to: str | None = None
    handoff_note: str = ""
    pending_action: dict[str, Any] | None = None  # {"type": "transfer"|"hangup", ...}
    executed: dict[str, str] = field(default_factory=dict)  # idempotency cache for writes
    insurance_checks: list[str] = field(default_factory=list)
    tool_log: list[dict[str, Any]] = field(default_factory=list)  # for Overseer grading
    services: dict[str, Any] = field(default_factory=dict)  # insurance, sms, forge, kb ...
    on_event: Callable[[str, dict], None] | None = None
    # Set while a turn runs on a PARTIAL transcript; resolves True/False when the final transcript
    # confirms or contradicts it. Nothing the caller can hear or that changes data happens before.
    speculation_gate: Any = None

    def emit(self, kind: str, data: dict) -> None:
        if self.on_event:
            try:
                self.on_event(kind, data)
            except Exception:  # noqa: BLE001
                pass

    def note(self, fact: str) -> None:
        self.case_file.append(fact)
        if len(self.case_file) > 30:
            self.case_file = self.case_file[-30:]
