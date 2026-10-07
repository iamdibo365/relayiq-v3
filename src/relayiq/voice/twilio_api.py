"""Twilio REST helpers: SMS, live call transfer and hangup (sync SDK run in a thread)."""

from __future__ import annotations

import asyncio
import logging
from xml.sax.saxutils import escape

from ..config import Settings

log = logging.getLogger("relayiq.twilio")


class TwilioAPI:
    def __init__(self, settings: Settings):
        self.s = settings
        self._client = None
        if settings.twilio_account_sid and settings.twilio_auth_token:
            from twilio.rest import Client
            self._client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def send(self, to: str, body: str) -> None:
        """SMS (also used as the `sms` service by tools)."""
        if not self._client or not self.s.twilio_phone_number:
            log.info("SMS not configured; would send to ***%s: %s", to[-4:], body)
            return
        try:
            await asyncio.to_thread(self._client.messages.create, to=to,
                                    from_=self.s.twilio_phone_number, body=body)
        except Exception as e:  # noqa: BLE001
            log.warning("SMS failed: %s", e)

    async def transfer(self, call_sid: str, say: str = "") -> bool:
        if not self._client or not self.s.human_transfer_phone:
            return False
        twiml = "<Response>"
        if say:
            twiml += f"<Say>{escape(say)}</Say>"
        twiml += f"<Dial>{escape(self.s.human_transfer_phone)}</Dial></Response>"
        await asyncio.to_thread(self._client.calls(call_sid).update, twiml=twiml)
        return True

    async def hangup(self, call_sid: str) -> None:
        if self._client:
            try:
                await asyncio.to_thread(self._client.calls(call_sid).update, status="completed")
            except Exception as e:  # noqa: BLE001
                log.warning("hangup failed: %s", e)
