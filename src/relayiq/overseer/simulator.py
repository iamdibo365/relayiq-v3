"""AI-to-AI simulated calls: an OpenAI model plays the caller, the real agent stack answers.

Runs against a sandbox copy of the database, in text mode (same orchestrator, gateway, tools,
prompts and models as production voice - only the audio layer is skipped).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ..agents.context import CallContext
from ..agents.orchestrator import Orchestrator
from ..agents.registry import AgentSpec
from ..db import Database, now_iso
from .watchdog import Watchdog

if TYPE_CHECKING:
    from ..platform import Platform

CALLER_SYSTEM = """You are role-playing a caller phoning a medical clinic's automated phone line.
Persona: {persona}
Your goal: {goal}
Rules: speak like a real person on the phone (one or two short sentences). Only say what the caller
would say. Answer the agent's questions using your persona details. When your goal is achieved or
clearly impossible, or the agent says they're transferring you or says goodbye, reply with exactly [END]."""


@dataclass
class Scenario:
    id: str
    caller_phone: str
    persona: str
    goal: str
    must_call: list[str] = field(default_factory=list)
    must_not_call: list[str] = field(default_factory=list)
    must_escalate: bool = False


@dataclass
class SimResult:
    scenario: Scenario
    transcript: list[dict[str, str]]
    tool_log: list[dict[str, Any]]
    escalated: bool
    error: str = ""


class _RecordingSMS:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    async def send(self, to: str, body: str) -> None:
        self.sent.append((to, body))


async def simulate(platform: "Platform", scenario: Scenario, sandbox: Database,
                   sandbox_agent: AgentSpec | None = None, max_turns: int = 10) -> SimResult:
    s = platform.settings
    sms = _RecordingSMS()
    services = platform.services(db=sandbox, sms=sms, sandbox=True)
    if sandbox_agent:
        services["sandbox_agent"] = sandbox_agent
    ctx = CallContext(call_sid=f"sim_{scenario.id}_{uuid.uuid4().hex[:6]}",
                      caller_phone=scenario.caller_phone, db=sandbox, settings=s, mode="sim",
                      services=services)
    ctx.caller_id_match = sandbox.patient_by_phone(scenario.caller_phone)
    sandbox.execute("INSERT INTO calls(call_sid, from_number, started_at, active_agent) VALUES (?,?,?,?)",
                    (ctx.call_sid, scenario.caller_phone, now_iso(), "front_desk"))
    ctx.journey = await platform.journey_for(sandbox, scenario.caller_phone)
    orch = Orchestrator(ctx, platform.gateway, platform.registry, s, platform.model_factory)
    watchdog = Watchdog()
    caller_llm = platform.model_factory(s.sim_caller_model, s)
    greeting = f"Thanks for calling {s.clinic_name}. This is Relay, an AI assistant. How can I help you today?"
    orch.opening_line = greeting
    transcript = [{"role": "agent", "text": greeting}]
    caller_msgs: list = [SystemMessage(CALLER_SYSTEM.format(persona=scenario.persona, goal=scenario.goal)),
                         HumanMessage(greeting)]
    escalated = False
    try:
        for _ in range(max_turns):
            reply = await caller_llm.ainvoke(caller_msgs)
            said = (reply.text or "").strip()
            if not said or "[END]" in said:
                break
            caller_msgs.append(AIMessage(said))
            transcript.append({"role": "caller", "text": said})
            act = watchdog.on_user(said)
            if act.kind != "none":
                transcript.append({"role": "agent", "text": act.say})
                escalated = True
                break
            text = ""
            async for ev in orch.respond(said):
                if ev.kind == "text":
                    text += ev.value
                elif ev.kind == "break":
                    text += " "
            text = text.strip() or "(no reply)"
            transcript.append({"role": "agent", "text": text, "agent": ctx.active_agent})
            caller_msgs.append(HumanMessage(text))
            if ctx.pending_action:
                escalated = escalated or ctx.pending_action["type"] == "transfer"
                break
        error = ""
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"[:400]
    return SimResult(scenario, transcript, ctx.tool_log, escalated, error)
