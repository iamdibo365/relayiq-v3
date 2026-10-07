"""Settings loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    anthropic_api_key: str = ""
    openai_api_key: str = ""
    frontdesk_model: str = "claude-haiku-4-5"
    specialist_model: str = "claude-haiku-4-5"
    reasoning_model: str = "claude-sonnet-5-5"
    sim_caller_model: str = "gpt-5.4-mini"
    stt_model: str = "gpt-4o-mini-transcribe"
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "marin"
    openai_realtime_url: str = "wss://api.openai.com/v1/realtime?intent=transcription"

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    public_base_url: str = "http://localhost:8000"
    admin_phone: str = ""
    human_transfer_phone: str = ""

    clinic_name: str = "Lakeside Family Medicine"
    clinic_npi: str = ""
    clinic_timezone: str = "America/Chicago"
    billing_portal_url: str = ""

    stedi_api_key: str = ""
    stedi_eligibility_url: str = "https://healthcare.us.stedi.com/2026-06-01/eligibility-check"
    portal_automation_enabled: bool = False
    portal_headless: bool = True
    portal_cigna_url: str = "https://cignaforhcp.cigna.com/"
    portal_cigna_username: str = ""
    portal_cigna_password: str = ""
    portal_cigna_totp_secret: str = ""
    portal_uhc_url: str = "https://www.uhcprovider.com/"
    portal_uhc_username: str = ""
    portal_uhc_password: str = ""
    portal_uhc_totp_secret: str = ""

    db_path: str = "data/relayiq.db"
    mcp_url: str = "http://127.0.0.1:8765/mcp"
    latency_slo_ms: int = 1500
    speculative_turns: bool = True  # start the LLM on the partial transcript (audio/actions wait for final)
    voice_mode: str = "chained"  # chained (STT -> Claude -> TTS) | s2s (OpenAI realtime front door)
    realtime_model: str = "gpt-realtime"
    realtime_url: str = "wss://api.openai.com/v1/realtime"
    max_call_seconds: int = 900

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else ROOT / p


@lru_cache
def get_settings() -> Settings:
    return Settings()
