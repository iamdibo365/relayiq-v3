"""Per-call conversation orchestrator.

One agent is "active" at a time and talks to the caller directly (no relay hops, so voice
latency = one LLM turn). Agents hand off with transfer_to_agent; the new agent answers in the
same turn. Every agent sees the same text history plus a shared "case file" of facts that tools
established, so tool scoping stays strict without losing context across handoffs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator, Callable
from zoneinfo import ZoneInfo

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)

from ..config import Settings
from ..gateway.gateway import ToolGateway
from . import prompts
from .context import CallContext
from .models import chat_model
from .registry import AgentRegistry, AgentSpec

log = logging.getLogger("relayiq.orchestrator")


@dataclass
class TurnEvent:
    kind: str  # text | tool | agent | break
    value: str = ""


ModelFactory = Callable[[str, Settings], BaseChatModel]


class Orchestrator:
    def __init__(self, ctx: CallContext, gateway: ToolGateway, registry: AgentRegistry,
                 settings: Settings, model_factory: ModelFactory | None = None):
        self.ctx, self.gw, self.registry, self.s = ctx, gateway, registry, settings
        self.model_factory = model_factory or (lambda name, s: chat_model(name, s, max_tokens=350))
        self.history: list[BaseMessage] = []
        self._graphs: dict[str, object] = {}
        self.opening_line = ""
        self.usage: list[dict] = []

    # ------------------------------------------------------------------ prompts
    def _model_name(self, spec: AgentSpec) -> str:
        return {"frontdesk": self.s.frontdesk_model, "specialist": self.s.specialist_model,
                "reasoning": self.s.reasoning_model}.get(spec.model, spec.model)

    def static_prompt(self, spec: AgentSpec) -> str:
        """Instructions that never change during a call: cacheable (with the tool list before it)."""
        parts = [prompts.VOICE_STYLE if self.ctx.mode != "text" else "",
                 spec.instructions.format(clinic=self.s.clinic_name),
                 prompts.PLATFORM_GUARDRAILS.format(clinic=self.s.clinic_name)]
        return "\n".join(x for x in parts if x)

    def system_prompt(self, spec: AgentSpec, prior_agent_said: str = "") -> str:
        return self.static_prompt(spec) + "\n" + self.context_prompt(spec, prior_agent_said)

    def system_message(self, spec: AgentSpec, prior_agent_said: str = "") -> SystemMessage:
        """Claude prompt caching: tools + static instructions are a stable prefix marked with a
        cache breakpoint; the per-turn call context goes after it so it never busts the cache."""
        static, dynamic = self.static_prompt(spec), self.context_prompt(spec, prior_agent_said)
        if self._model_name(spec).startswith("claude"):
            return SystemMessage(content=[
                {"type": "text", "text": static, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": dynamic},
            ])
        return SystemMessage(static + "\n" + dynamic)

    def context_prompt(self, spec: AgentSpec, prior_agent_said: str = "") -> str:
        c = self.ctx
        now = datetime.now(ZoneInfo(self.s.clinic_timezone))
        lines = ["## Call context",
                 f"Now: {now.strftime('%A, %B %d %Y, %I:%M %p')} ({self.s.clinic_timezone})",
                 f"Active agent: {spec.name}"]
        if self.opening_line and len(self.history) <= 2:
            lines.append(f'You opened the call by saying: "{self.opening_line}"')
        if c.verified and c.patient:
            lines.append(f"Identity VERIFIED: {c.patient['first_name']} {c.patient['last_name']} "
                         f"(patient {c.patient['id']}).")
        else:
            lines.append("Identity NOT verified yet.")
            if c.caller_id_match:
                lines.append(f"Caller ID matches patient {c.caller_id_match['first_name']} on file "
                             "(greeting by first name is OK; still verify).")
        same_person = bool(c.patient and c.caller_id_match and c.patient["id"] == c.caller_id_match["id"])
        if c.journey and c.verified and same_person:
            # journey was looked up by caller ID; never show it for a different verified patient
            lines.append("Recent cross-channel history (from the customer-journey MCP server):\n" + c.journey)
        if c.case_file:
            lines.append("Case file (facts from tools this call):\n- " + "\n- ".join(c.case_file[-15:]))
        if c.notices:
            lines.append("NEW updates to tell the caller now:\n- " + "\n- ".join(c.notices))
        others = self.registry.routable(c)
        if others and "transfer_to_agent" in spec.all_tools():
            lines.append("Agents you can hand off to (transfer_to_agent):\n" +
                         "\n".join(f"- {o.name}: {o.purpose}" for o in others))
        if c.handoff_note:
            lines.append(f"Context: the caller needs: {c.handoff_note}. "
                         + (f'You (the same voice) just said: "{prior_agent_said}". ' if prior_agent_said else "")
                         + "Continue seamlessly as the same assistant. Never mention handoffs, transfers, "
                         "other agents, teams, or that identity was verified by someone else. Don't repeat "
                         "what was just said; go straight to the next useful question or action.")
        return "\n".join(x for x in lines if x)

    def _graph(self, spec: AgentSpec):
        key = f"{spec.name}:{spec.source}:{hash(tuple(spec.all_tools()))}"
        if key not in self._graphs:
            tools = self.gw.langchain_tools(self.ctx, spec.name, spec.all_tools())
            model = self.model_factory(self._model_name(spec), self.s)
            self._graphs[key] = create_agent(model, tools, name=spec.name)
        return self._graphs[key]

    # ------------------------------------------------------------------ turns
    async def respond(self, user_text: str) -> AsyncIterator[TurnEvent]:
        self.history.append(HumanMessage(user_text))
        said: list[str] = []
        prior_said = ""
        try:
            for _hop in range(3):
                spec = self.registry.resolve(self.ctx.active_agent, self.ctx) or self.registry.get("front_desk")
                graph = self._graph(spec)
                messages = [self.system_message(spec, prior_said)] + self.history
                self.ctx.handoff_to = None
                last_msg_id = None
                streamed_ids: set = set()
                hop_text: list[str] = []
                async for chunk, _meta in graph.astream(
                        {"messages": messages}, {"recursion_limit": 16}, stream_mode="messages"):
                    if not isinstance(chunk, AIMessage):
                        continue
                    is_chunk = isinstance(chunk, AIMessageChunk)
                    self._record_usage(chunk)
                    if is_chunk:
                        streamed_ids.add(chunk.id)
                    elif chunk.id in streamed_ids:
                        continue  # full message re-emitted after its chunks
                    if chunk.id != last_msg_id:
                        if last_msg_id is not None and hop_text:
                            yield TurnEvent("break")
                        last_msg_id = chunk.id
                    calls = (chunk.tool_call_chunks if is_chunk else chunk.tool_calls) or []
                    for tc in calls:
                        if tc.get("name"):
                            yield TurnEvent("tool", tc["name"])
                    text = chunk.text
                    if text:
                        hop_text.append(text)
                        said.append(text)
                        yield TurnEvent("text", text)
                self.ctx.notices.clear()
                if self.ctx.handoff_to:
                    prior_said = "".join(hop_text).strip()
                    self.ctx.active_agent = self.ctx.handoff_to
                    self.ctx.emit("handoff", {"to": self.ctx.active_agent, "reason": self.ctx.handoff_note})
                    self.ctx.db.execute("UPDATE calls SET active_agent=? WHERE call_sid=?",
                                        (self.ctx.active_agent, self.ctx.call_sid))
                    yield TurnEvent("agent", self.ctx.active_agent)
                    if said:
                        said.append(" ")
                        yield TurnEvent("break")
                    continue
                break
        finally:
            self.ctx.handoff_note = "" if not self.ctx.handoff_to else self.ctx.handoff_note
            text = "".join(said).strip()
            self.history.append(AIMessage(text or "(no spoken reply)"))

    def _record_usage(self, msg: AIMessage) -> None:
        """Track prompt-cache effectiveness (Claude reports cache reads/writes per request)."""
        um = getattr(msg, "usage_metadata", None) or {}
        details = um.get("input_token_details") or {}
        if not um.get("input_tokens"):
            return
        read, write = details.get("cache_read") or 0, details.get("cache_creation") or 0
        self.usage.append({"input": um["input_tokens"], "cache_read": read, "cache_write": write})
        log.info("llm usage: input=%s cache_read=%s cache_write=%s", um["input_tokens"], read, write)

    def mark_interrupted(self, heard_text: str) -> None:
        """Replace the last assistant message with what the caller actually heard."""
        if self.history and isinstance(self.history[-1], AIMessage):
            heard = heard_text.strip()
            self.history[-1] = AIMessage((heard + " ..." if heard else "") + " [caller interrupted]")
