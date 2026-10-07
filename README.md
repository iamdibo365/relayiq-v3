# RelayIQ v3 — voice-first agentic patient access

**A real phone line for a medical clinic where Claude agents verify the caller, check insurance eligibility with the payer, and book the visit end to end — ~2.2 s median turn latency, 26 of 28 release-gate scenarios passing across 4 runs with zero safety failures, and a real payer eligibility response (Stedi → UnitedHealthcare test system) driving the booking decision.**

## Result

| Metric | Value |
|---|---|
| Turn latency, end of caller speech → first agent audio (p50 / p95) | 2,188 ms / 3,608 ms (27 turns; includes runs before the speak-before-tools fix) |
| Of which: transcript finalized / first LLM token (medians) | 615 ms / 1,366 ms |
| Overseer release gate (7 baseline scenarios, Claude-graded) | 7/7, 7/7, 6/7, 6/7 across 4 runs (26/28), mean 4.1/5; safety scored 4–5 in every scenario |
| Insurance check via clearinghouse API (Stedi test mode) | 1,286 ms; real 271 for a dependent: active, Choice Plus, payer note "provider is out of network" → booked |
| Payer-portal fallback (browser agent) | Not run against a real portal (no provider-portal credentials); passes against a local test portal in real Chromium (6 steps, secrets never reach the model) |
| Gateway decisions | Live demo: 27 executed, 2 failed (tool ran but couldn't act), 0 blocked (none attempted). Gate runs: 1 blocked of 76 tool calls |

![RelayIQ live console: call history with outcomes, agent roster, Forge agent builder](docs/dashboard_pg1.png)
*Live console across 11 real calls (headline tiles include every call, before and after the latency fixes).*

![Automations ledger, insurance verification results and appointments](docs/dashboard_pg2.png)
*Every tool call is audited in the ledger. Jane Doe: Stedi returns active coverage, so the visit is booked. John Doe: the payer rejects the member ID, so the visit is booked as pending_insurance for staff follow-up.*

![Overseer release gate runs](docs/dashboard_pg3.png)
*Overseer release-gate runs: simulated callers graded by a Claude judge; a run fails if any scenario fails.*

Everything runs on real services: Twilio Programmable Voice + Media Streams, OpenAI `gpt-4o-transcribe` (streaming STT) and `gpt-4o-mini-tts`, Claude Haiku 4.5 for the voice agents, Claude Sonnet 5.5 for Forge, grading and the browser agent, and an OpenAI GPT-5.4 mini model as the simulated caller. Test doubles exist only under `tests/`.

## Architecture

```mermaid
flowchart LR
    PSTN[Caller on a phone] --> TW[Twilio number]
    TW -- "webhook (signed)" --> APP
    TW <-- "Media Stream WSS<br/>8 kHz mu-law, 20 ms frames" --> APP
    subgraph APP[RelayIQ app - FastAPI]
      STT[OpenAI realtime STT<br/>server VAD, barge-in] --> ORCH
      ORCH[Orchestrator<br/>one active agent:<br/>front desk / scheduling /<br/>billing / refills / Forge-built] --> TTS[OpenAI TTS<br/>sentence streaming]
      ORCH --> GW{Tool gateway<br/>allow-list · identity ·<br/>confirmation · idempotency · audit}
      WD[Overseer watchdog<br/>emergency / human / loop rules] -.-> ORCH
    end
    GW --> DB[(Clinic system of record<br/>SQLite, synthetic patients)]
    GW --> INS[Insurance service]
    INS -- "X12 270/271 JSON" --> STEDI[Stedi clearinghouse<br/>Cigna, UHC, Aetna...]
    INS -. "fallback" .-> BA[Claude browser agent<br/>Playwright Chromium] --> PORTAL[Payer provider portal]
    APP <-- MCP --> MCPS[Customer-journey MCP server<br/>calls, SMS, portal, chat]
    GW --> LEDGER[(Automations ledger)]
    FORGE[Forge: agent builder] --> GATE[Sentinel gate:<br/>simulated callers + Claude judge] --> REG[Agent registry]
```

A call: Twilio streams caller audio → OpenAI transcribes with server VAD (end of turn, barge-in) → the active Claude agent streams a reply, calling tools only through the gateway → each sentence is synthesized as soon as it completes and streamed back to Twilio with marks, so we know exactly what the caller heard if they interrupt.

## Design decisions

1. **Chained STT → Claude → TTS instead of a speech-to-speech model.** Every stage is observable (per-stage latency in `turns`), the reasoning model is swappable, and tool calls go through one audited gateway. Given up: roughly 300–600 ms versus an end-to-end realtime model (estimate), and some prosody. Sentence-level TTS streaming, cached fillers ("Let me verify that with your insurance plan") and a 550 ms VAD window claw most of it back.
2. **One active agent with handoffs, not a supervisor relaying every turn.** The specialist talks to the caller directly, so a turn costs one LLM round trip; tool scope is enforced per agent. Shared context travels as a "case file" of tool-established facts rather than raw tool transcripts. Given up: a central planner that could coordinate multi-specialist tasks in one turn.
3. **Clearinghouse API first, payer-portal browser agent second.** The 270/271 API answers in seconds and is how production revenue-cycle systems check eligibility; the browser agent covers payers or plans the API can't answer, runs asynchronously (book as "pending insurance", text the patient when confirmed) and is off until you have portal credentials and permission to automate. Given up: an instant answer on every call — the portal path takes about a minute.

## What did not work

- **The first v3 never completed a real voice call.** It was a scaffold tested against an offline fake model. This rebuild started from the audio path: bit-exact G.711 codec, Twilio Media Streams protocol (start / media / mark / clear / stop) and barge-in are covered by protocol tests and an end-to-end smoke caller that behaves exactly like Twilio against real models.
- **Newer Claude models reject parameters older ones accept.** Claude Sonnet 5.5 refuses non-default `temperature` and forced `tool_choice`, so the grader and Forge would have errored on their first real call. The model factory now omits those for adaptive-thinking models and uses JSON-schema structured output.
- **The browser agent leaked a portal password to the model.** A test portal that submits its login form with GET echoed the password back in the URL, and the page snapshot carried it to the LLM. Snapshots and tool results are now scrubbed of every configured secret (raw and URL-encoded, plus live TOTP codes).
- **The test caller interrupted the agent while it was thinking.** The smoke caller treated 1.8 s of silence as "agent finished", so it spoke during the LLM's thinking time and the app (correctly) treated that as barge-in. It now waits for the reply to start, then for it to finish.
- **The agent invented a slot ID** (`slot_chen_thu_0830`) and the booking failed: between turns agents only see text plus the case file, and offered slot IDs weren't in it. Offered slots and appointment IDs are now written to the case file; an unknown ID returns "do not invent ids".
- **The agent's "Let me check…" waited for the tool.** A sentence without trailing whitespace stayed in the TTS buffer until the tool returned and the next model call started (one 6 s turn). Buffered text is now spoken the moment a tool call begins; a regression test fails without the fix.
- **My eligibility client was written for an older Stedi API.** The 2026-06-01 endpoint rejected `controlNumber`, returns coverage under `plans[].benefits.statuses[]`, and Stedi's UHC test member is a *dependent* (spouse on the subscriber's plan). Removed the field, added dependent requests, parse the new format with the old one as fallback; the real 271 is now a test fixture.
- **The release gate caught agents announcing actions they never took** ("I'll connect you", "I'll create that task") with no tool call. Fixed with a platform rule for every agent, including Forge-built ones: act, don't announce. Gate went from 5/7 to 7/7.
- **Caller-ID history could show for the wrong patient.** History is looked up by caller ID; if a different patient verifies on that phone, it's now withheld (test added).

## Why this matters for the business

Front-desk phones are where clinics lose patients and money: callers on hold hang up, and visits booked without an eligibility check turn into denied claims and surprise bills. RelayIQ answers every call, books the visit only after confirming coverage with the payer, and leaves an audit trail of every action — the ledger turns that into staff minutes saved. The guardrails are what make it deployable in healthcare: no PHI before identity verification, no change without a spoken yes, deterministic emergency handling, and a release gate so a new agent (built in minutes with Forge) can't go live until simulated callers and a grader say it's safe.

---

## Run it

Needs: Python 3.11–3.13, [uv](https://docs.astral.sh/uv/), a Twilio number (upgraded account — trial accounts play a notice and only call verified numbers), Anthropic and OpenAI API keys, and `cloudflared` (or ngrok) for a public HTTPS URL. A Stedi test API key is optional.

```bash
make install                     # deps + Chromium for the portal agent
cp .env.example .env             # fill keys, Twilio number, ADMIN_PHONE (your cell, for Forge)
make test                        # protocol + policy tests, no API spend
make dev                         # MCP journey server :8765 + voice app :8000
make tunnel                      # in another terminal; copy the https URL into PUBLIC_BASE_URL, restart make dev
make twilio DEMO_CALLER=+1214... # point the number here; your cell becomes "John Doe" for caller ID
make smoke                       # real end-to-end call without a phone; saves data/smoke_call.wav
# now call your Twilio number. Dashboard: http://localhost:8000
make evals                       # Overseer release gate with real models
```

Demo patients (synthetic): John Doe, DOB April 12 1980 (UHC) · Jordan Smith, Sept 30 1992 (Cigna) · Ana Lopez, Jan 22 1975 (Aetna). For a real Stedi test-mode check, copy one of Stedi's published mock eligibility requests onto a patient with `scripts/check_eligibility.py` (see its docstring). Until a clearinghouse key is set, insurance comes back "unverified" and the booking is marked pending — never invented.

The demo walkthrough for the interview is in [`docs/DEMO.md`](docs/DEMO.md).

## Repo layout

| Path | What it is |
|---|---|
| `src/relayiq/voice/` | Twilio Media Streams session, OpenAI realtime STT, streaming TTS, G.711 codec, sentence chunker, Twilio REST (SMS, transfer) |
| `src/relayiq/agents/` | Orchestrator (handoffs, streaming), agent registry, prompts and platform guardrails, model factory |
| `src/relayiq/gateway/` | Tool gateway (policy chokepoint + ledger) and clinic tools |
| `src/relayiq/insurance/` | Stedi eligibility client + parser, Claude/Playwright payer-portal agent, verification service |
| `src/relayiq/context/` | Customer-journey MCP server and client |
| `src/relayiq/forge/` | Agent builder (draft → validate → gate → register) |
| `src/relayiq/overseer/` | Watchdog, AI-to-AI simulator, grader, Sentinel release gate, baseline scenarios |
| `src/relayiq/dashboard/` | Live console |
| `scripts/` | Smoke caller, Twilio setup, eval runner, eligibility check |

## Compliance notes

Synthetic patients only. Before real PHI: BAAs with Anthropic, OpenAI, Twilio and the clearinghouse; HIPAA-eligible configurations (e.g. zero data retention); encrypted storage instead of SQLite; and payer portal automation only where the portal's terms allow it for your account (`PORTAL_AUTOMATION_ENABLED` is off by default).

## Latency options

Baseline (chained, before these changes): about 2.2 s from end of speech to first audio, about 2.7 s as the caller hears it (including the 550 ms VAD silence).

| Change | Setting | What it does |
|---|---|---|
| Speculative turns | `SPECULATIVE_TURNS=true` | Claude starts on the streaming transcript once it ends in `.?!`. Audio and any non-read tool wait for the final transcript. If the final transcript differs, the guess is cancelled and rolled back, so the caller never hears it. Logs `speculative turn confirmed (N ms head start)` / `discarded`. |
| Prompt caching | automatic for Claude models | Static system prompt marked `cache_control`. Logs `llm usage: input=… cache_read=… cache_write=…`. Haiku 4.5 only caches prompts ≥ 4,096 tokens, and ours is about 2,100, so it is a no-op on Haiku and takes effect on Sonnet. |
| Short first phrase | always on | The agent opens with 2–4 words ("Got it."), and the chunker sends a first clause of 24+ characters to TTS without waiting for the full sentence. |
| Faster STT | `STT_MODEL=gpt-4o-mini-transcribe` | Smaller transcription model. |
| Speech-to-speech front door | `VOICE_MODE=s2s` | OpenAI `gpt-realtime` takes the call audio directly and speaks. It has one tool, `clinic_agent`, which runs the same Claude agents, gateway and ledger. The deterministic watchdog still overrides on emergencies. `total_ms` is now the realtime acknowledgement; `llm_first_token_ms` is when Claude's answer started. |
