"""Overseer watchdog: deterministic live-call safety rules that run outside the LLM."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

EMERGENCY = re.compile(
    r"\b(chest pain|heart attack|can'?t breathe|cannot breathe|trouble breathing|short of breath|"
    r"stroke|face (is )?drooping|slurred speech|severe bleeding|bleeding a lot|unconscious|"
    r"passed out|overdos\w*|seizure)\b", re.I)
SELF_HARM = re.compile(r"\b(kill myself|suicid\w*|end my life|hurt myself|self[- ]harm)\b", re.I)
HUMAN = re.compile(r"\b(human|real person|representative|operator|agent please|talk to (a|some)one|"
                   r"speak (to|with) (a|some)one|front desk staff)\b", re.I)

EMERGENCY_SCRIPT = ("If this is a medical emergency, please hang up and call 9 1 1 right now. "
                    "I'm also connecting you to our staff.")
SELF_HARM_SCRIPT = ("I'm really glad you told me. If you're thinking about harming yourself, please call "
                    "or text 9 8 8 to reach the Suicide and Crisis Lifeline right now, or call 9 1 1 if "
                    "you're in danger. I'm connecting you to our staff as well.")


@dataclass
class WatchdogAction:
    kind: str  # emergency | self_harm | escalate | none
    say: str = ""
    reason: str = ""


@dataclass
class Watchdog:
    latency_slo_ms: int = 1500
    human_requests: int = 0
    denied_streak: int = 0
    slo_breaches: int = 0
    turns: int = 0
    alerts: list[str] = field(default_factory=list)

    def on_user(self, text: str) -> WatchdogAction:
        self.turns += 1
        if SELF_HARM.search(text):
            self.alerts.append("self-harm language")
            return WatchdogAction("self_harm", SELF_HARM_SCRIPT, "self-harm language detected")
        if EMERGENCY.search(text):
            self.alerts.append("emergency symptoms")
            return WatchdogAction("emergency", EMERGENCY_SCRIPT, "possible emergency symptoms")
        if HUMAN.search(text):
            self.human_requests += 1
            if self.human_requests >= 2:
                self.alerts.append("caller asked for a human twice")
                return WatchdogAction("escalate", "Of course. Connecting you to a team member now.",
                                      "caller asked for a human twice")
        return WatchdogAction("none")

    def on_tool(self, decision: str) -> WatchdogAction:
        self.denied_streak = self.denied_streak + 1 if decision == "denied" else 0
        if self.denied_streak >= 4:
            self.alerts.append("agent stuck: 4 denied tool calls in a row")
            return WatchdogAction("escalate", "Let me get a team member to help with this.",
                                  "agent stuck in denied tool loop")
        return WatchdogAction("none")

    def on_latency(self, ms: int) -> None:
        if ms > self.latency_slo_ms:
            self.slo_breaches += 1
            self.alerts.append(f"turn latency {ms} ms > SLO {self.latency_slo_ms} ms")
