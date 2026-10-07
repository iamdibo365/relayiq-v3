"""Forge: build a new specialist agent from a plain-language request, then gate it.

Draft (Claude, structured output) -> validate against the gateway catalog -> save as `testing`
-> Overseer runs generated scenarios + baseline safety scenarios with the new agent routable
-> `active` only if Sentinel passes, otherwise `rejected` with the report.
Forge can only compose existing gateway tools; it cannot create new capabilities or bypass policy.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import TYPE_CHECKING, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ..agents.models import structured as to_structured
from ..agents.registry import AgentSpec
from ..db import now_iso
from ..gateway.gateway import ToolDef
from ..overseer.sentinel import load_scenarios, run_gate
from ..overseer.simulator import Scenario

if TYPE_CHECKING:
    from ..agents.context import CallContext
    from ..platform import Platform

FORBIDDEN_TOOLS = {"forge_list", "forge_build_agent", "forge_build_status", "transfer_to_agent",
                   "escalate_to_human", "end_call", "verify_identity", "search_clinic_policy"}
DEMO_CALLERS = {"+15555550101": "John Doe, born April 12 1980",
                "+15555550102": "Jordan Smith, born September 30 1992",
                "+15555550103": "Ana Lopez, born January 22 1975"}


class ScenarioDraft(BaseModel):
    id: str = Field(description="snake_case id")
    caller_phone: Literal["+15555550101", "+15555550102", "+15555550103"]
    persona: str
    goal: str
    must_call: list[str] = Field(default_factory=list)
    must_not_call: list[str] = Field(default_factory=list)


class AgentDraft(BaseModel):
    name: str = Field(description="snake_case agent name, e.g. prior_auth")
    display_name: str
    purpose: str = Field(description="One sentence used by the front desk to decide when to route here")
    instructions: str = Field(description="System instructions for the agent, written for a voice call. "
                              "Use {clinic} where the clinic name goes.")
    tools: list[str]
    write_tools_justification: str = ""
    test_scenarios: list[ScenarioDraft] = Field(min_length=2, max_length=4)


FORGE_SYSTEM = """You design specialist voice agents for a medical clinic's RelayIQ platform.
Compose ONLY from the gateway tools listed. Choose the minimum set of tools (least privilege).
Identity verification, policy lookup, handoffs, escalation and platform safety rules are added automatically.
Demo patients for test scenarios: {callers}.
Write 2-4 realistic test scenarios that exercise the new agent, including one where the caller asks
for something the agent must refuse or hand off."""


class Forge:
    def __init__(self, platform: "Platform"):
        self.p = platform
        self.jobs: dict[str, dict] = {}

    def allowed_tools(self) -> list[dict]:
        return [t for t in self.p.gateway.catalog() if t["name"] not in FORBIDDEN_TOOLS]

    async def draft(self, request: str) -> AgentDraft:
        llm = self.p.model_factory(self.p.settings.reasoning_model, self.p.settings)
        structured = to_structured(llm, AgentDraft)
        catalog = json.dumps(self.allowed_tools(), indent=1)
        return await structured.ainvoke([
            SystemMessage(FORGE_SYSTEM.format(callers=json.dumps(DEMO_CALLERS))),
            HumanMessage(f"Gateway tools:\n{catalog}\n\nExisting agents: "
                         f"{[s.name for s in self.p.registry.all_active()]}\n\nRequest: {request}"),
        ])

    def validate(self, d: AgentDraft) -> tuple[AgentSpec, list[str]]:
        problems = []
        name = re.sub(r"[^a-z0-9_]", "_", d.name.lower()).strip("_")[:40] or "new_agent"
        if self.p.registry.get(name) or name in self.p.registry.builtins:
            name = f"{name}_{uuid.uuid4().hex[:4]}"
        allowed = {t["name"] for t in self.allowed_tools()}
        bad = [t for t in d.tools if t not in allowed]
        if bad:
            problems.append(f"dropped unknown/forbidden tools: {bad}")
        tools = [t for t in d.tools if t in allowed]
        if not tools:
            problems.append("no usable tools")
        instructions = d.instructions.replace("{", "{{").replace("}", "}}").replace("{{clinic}}", "{clinic}")
        spec = AgentSpec(name, d.display_name[:60], d.purpose[:300], instructions[:4000], tools,
                         model="specialist", source="forge", status="testing")
        return spec, problems

    def _save(self, spec: AgentSpec, built_by: str, report: dict | None = None) -> None:
        self.p.db.execute(
            "INSERT OR REPLACE INTO agent_specs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (spec.name, spec.display_name, spec.purpose, spec.instructions, json.dumps(spec.tools),
             spec.model, spec.status, built_by, "forge", json.dumps(report) if report else None,
             now_iso(), now_iso()))

    def _new_job(self, request: str) -> dict:
        job_id = "build_" + uuid.uuid4().hex[:6]
        job = {"id": job_id, "request": request, "status": "drafting", "agent": None,
               "problems": [], "report": None, "created_at": now_iso()}
        self.jobs[job_id] = job
        return job

    async def build(self, request: str, built_by: str = "admin", job: dict | None = None) -> dict:
        job = job or self._new_job(request)
        try:
            draft = await self.draft(request)
            spec, problems = self.validate(draft)
            job.update(agent=spec.name, problems=problems, status="testing")
            if "no usable tools" in problems:
                spec.status = "rejected"
                self._save(spec, built_by)
                job["status"] = "rejected"
                return job
            self._save(spec, built_by)
            scenarios = [Scenario(**s.model_dump()) for s in draft.test_scenarios]
            baseline = {s.id: s for s in load_scenarios()}
            scenarios += [baseline["wrong_dob_cannot_book"], baseline["prompt_injection_other_patient"]]
            report = await run_gate(self.p, scenarios, target=f"agent:{spec.name}", sandbox_agent=spec)
            spec.status = "active" if report["passed"] else "rejected"
            self._save(spec, built_by, report)
            job.update(status=spec.status, report={k: report[k] for k in
                                                   ("run_id", "passed", "mean_score", "n", "n_passed")})
        except Exception as e:  # noqa: BLE001
            job.update(status="failed", problems=job["problems"] + [f"{type(e).__name__}: {e}"[:300]])
        return job

    def start(self, request: str, built_by: str = "admin") -> dict:
        job = self._new_job(request)
        task = asyncio.create_task(self.build(request, built_by, job))
        self.p.background.add(task)
        task.add_done_callback(self.p.background.discard)
        return {"build_id": job["id"], "status": "started",
                "message": "Drafting and testing the agent now; it takes a minute or two."}

    def status(self, name_or_id: str = "") -> list[dict]:
        jobs = list(self.jobs.values())
        if name_or_id:
            jobs = [j for j in jobs if name_or_id in (j["id"], j.get("agent"))]
        return jobs[-5:]


# ------------------------------------------------------------- gateway tools (Forge persona only)
class ForgeBuildArgs(BaseModel):
    description: str = Field(description="What the new agent should handle and which actions it may take")


class ForgeStatusArgs(BaseModel):
    name_or_id: str = ""


class NoArgs(BaseModel):
    pass


def register_forge_tools(platform: "Platform") -> None:
    gw = platform.gateway

    async def forge_list(ctx: "CallContext", a: dict) -> dict:
        return {"ok": True,
                "agents": [{"name": s.name, "purpose": s.purpose, "source": s.source}
                           for s in platform.registry.all_active()],
                "tools": [t["name"] for t in platform.forge.allowed_tools()]}

    async def forge_build_agent(ctx: "CallContext", a: dict) -> dict:
        return {"ok": True, **platform.forge.start(a["description"], built_by=f"phone:{ctx.caller_phone[-4:]}")}

    async def forge_build_status(ctx: "CallContext", a: dict) -> dict:
        jobs = platform.forge.status(a.get("name_or_id", ""))
        return {"ok": True, "builds": jobs or "no builds yet"}

    gw.register(ToolDef("forge_list", "read", "List live agents and the gateway tools Forge can use.",
                        NoArgs, forge_list))
    gw.register(ToolDef("forge_build_agent", "write", "Draft a new specialist agent and send it through "
                        "the Overseer release gate.", ForgeBuildArgs, forge_build_agent, minutes_saved=60))
    gw.register(ToolDef("forge_build_status", "read", "Status and test results of recent builds.",
                        ForgeStatusArgs, forge_build_status))
