"""Wires settings, database, gateway, agents, insurance, Forge and Overseer together."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from typing import Any, Callable

from .agents.models import chat_model
from .agents.registry import AgentRegistry
from .config import Settings
from .context.client import JourneyClient
from .db import Database, seed
from .forge.forge import Forge, register_forge_tools
from .gateway.clinic_tools import register_clinic_tools
from .gateway.gateway import ToolGateway
from .insurance.eligibility import StediEligibilityClient
from .insurance.portal_agent import PayerPortalAgent
from .insurance.service import InsuranceService
from .kb import PolicyKB
from .voice.stt import OpenAIRealtimeTranscriber
from .voice.tts import OpenAITTS
from .voice.twilio_api import TwilioAPI

log = logging.getLogger("relayiq")


class Platform:
    def __init__(self, settings: Settings, db: Database | None = None,
                 model_factory: Callable | None = None, tts=None,
                 transcriber_factory: Callable | None = None, journey: JourneyClient | None = None,
                 twilio: TwilioAPI | None = None, eligibility: StediEligibilityClient | None = None,
                 portal: PayerPortalAgent | None = None):
        self.settings = settings
        self.db = db or Database(settings.db_file)
        seed(self.db, settings.clinic_timezone)
        self.model_factory = model_factory or (lambda name, s: chat_model(name, s, max_tokens=500))
        self.kb = PolicyKB()
        self.gateway = register_clinic_tools(ToolGateway())
        self.registry = AgentRegistry(self.db)
        self.twilio = twilio or TwilioAPI(settings)
        self.tts = tts or OpenAITTS(settings)
        self.transcriber_factory = transcriber_factory or (lambda: OpenAIRealtimeTranscriber(
            settings, prompt="Medical clinic phone call: appointments, insurance (Cigna, "
                             "UnitedHealthcare, Aetna), refills, billing."))
        self.journey = journey or JourneyClient(settings.mcp_url)
        self.eligibility = eligibility or StediEligibilityClient(settings)
        self.portal = portal or PayerPortalAgent(
            self.model_factory(settings.reasoning_model, settings), headless=settings.portal_headless)
        self.insurance = InsuranceService(settings, self.db, self.eligibility, self.portal, sms=self.twilio)
        self.forge = Forge(self)
        register_forge_tools(self)
        self.live_calls: dict[str, dict[str, Any]] = {}
        self.background: set[asyncio.Task] = set()

    def services(self, db: Database | None = None, sms=None, sandbox: bool = False) -> dict[str, Any]:
        insurance = self.insurance
        if db is not None and db is not self.db:
            # Sandbox: same real clearinghouse API, but no browser automation and no real SMS
            insurance = InsuranceService(self.settings.model_copy(update={"portal_automation_enabled": False}),
                                         db, self.eligibility, None, sms=sms)
        return {"kb": self.kb, "insurance": insurance, "sms": sms if sandbox else self.twilio,
                "registry": self.registry, "billing_portal_url": self.settings.billing_portal_url,
                "forge": self.forge}

    async def journey_for(self, db: Database, phone: str) -> str:
        if db is self.db:
            return await self.journey.journey(phone)
        from .context.mcp_server import build_server
        return await JourneyClient(build_server(db)).journey(phone)

    # ---- Twilio stream auth: HMAC of the CallSid with the account auth token
    def stream_token(self, call_sid: str) -> str:
        key = (self.settings.twilio_auth_token or "dev").encode()
        return hmac.new(key, call_sid.encode(), hashlib.sha256).hexdigest()[:32]

    def verify_stream_token(self, call_sid: str, token: str) -> bool:
        return bool(call_sid) and hmac.compare_digest(self.stream_token(call_sid), token or "")
