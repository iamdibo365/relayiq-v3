"""Agent registry: built-in specialists plus Forge-built agents that passed the release gate."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..db import Database
from . import prompts
from .context import CallContext

ALWAYS = ["verify_identity", "search_clinic_policy", "transfer_to_agent", "escalate_to_human",
          "end_call"]


@dataclass
class AgentSpec:
    name: str
    display_name: str
    purpose: str
    instructions: str
    tools: list[str]
    model: str = "specialist"  # frontdesk | specialist | reasoning | explicit model id
    source: str = "builtin"
    status: str = "active"
    always_tools: list[str] = field(default_factory=lambda: list(ALWAYS))

    def all_tools(self) -> list[str]:
        return list(dict.fromkeys(self.always_tools + self.tools))


BUILTINS = [
    AgentSpec("front_desk", "Front desk", "Greets callers, verifies identity, answers general "
              "questions and routes to specialists.", prompts.FRONT_DESK,
              ["get_patient_summary", "list_providers", "create_callback_task"], model="frontdesk"),
    AgentSpec("scheduling", "Scheduling", "Books, reschedules and cancels appointments, verifying "
              "insurance before booking.", prompts.SCHEDULING,
              ["get_patient_summary", "list_providers", "find_open_slots", "get_insurance_on_file",
               "verify_insurance", "check_insurance_status", "update_insurance_on_file",
               "book_appointment", "reschedule_appointment", "cancel_appointment",
               "create_callback_task"]),
    AgentSpec("billing", "Billing", "Explains balances and statements, texts a secure payment link.",
              prompts.BILLING, ["get_balance", "get_insurance_on_file", "send_payment_link",
                                "create_callback_task"]),
    AgentSpec("refills", "Refills", "Takes prescription refill requests for clinician review.",
              prompts.REFILLS, ["get_patient_summary", "request_refill", "create_callback_task"]),
    AgentSpec("forge", "Forge", "Builds and gates new agents (admin only).", prompts.FORGE,
              ["forge_list", "forge_build_agent", "forge_build_status"], model="reasoning",
              always_tools=["end_call", "escalate_to_human"]),
]


class AgentRegistry:
    def __init__(self, db: Database):
        self.db = db
        self.builtins = {s.name: s for s in BUILTINS}

    def forge_specs(self, statuses: tuple[str, ...] = ("active",)) -> list[AgentSpec]:
        rows = self.db.query(
            f"SELECT * FROM agent_specs WHERE status IN ({','.join('?' * len(statuses))})", statuses)
        return [AgentSpec(r["name"], r["display_name"], r["purpose"], r["instructions"],
                          json.loads(r["tools"]), r["model"] or "specialist", "forge", r["status"])
                for r in rows]

    def get(self, name: str, include_testing: bool = False) -> AgentSpec | None:
        if name in self.builtins:
            return self.builtins[name]
        statuses = ("active", "testing") if include_testing else ("active",)
        return next((s for s in self.forge_specs(statuses) if s.name == name), None)

    def all_active(self) -> list[AgentSpec]:
        return list(self.builtins.values()) + self.forge_specs()

    def routable(self, ctx: CallContext) -> list[AgentSpec]:
        extra = ctx.services.get("sandbox_agent")  # agent under test in Overseer simulations
        specs = [s for s in self.all_active() if s.name != "forge"]
        if extra and extra.name not in {s.name for s in specs}:
            specs.append(extra)
        return [s for s in specs if s.name != ctx.active_agent]

    def routable_names(self, ctx: CallContext) -> list[str]:
        return [s.name for s in self.routable(ctx)]

    def is_routable(self, name: str, ctx: CallContext) -> bool:
        return name in self.routable_names(ctx)

    def resolve(self, name: str, ctx: CallContext) -> AgentSpec | None:
        extra = ctx.services.get("sandbox_agent")
        if extra and extra.name == name:
            return extra
        return self.get(name)
