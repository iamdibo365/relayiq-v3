"""End-to-end Twilio Media Streams protocol test: webhook -> WebSocket -> STT -> agents -> TTS -> Twilio."""

import base64
import json
import time

from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from relayiq.app import create_app


def _start(platform, call_sid="CAtest", caller="+15555550101"):
    return {"event": "start", "sequenceNumber": "1", "streamSid": "MZ1",
            "start": {"streamSid": "MZ1", "callSid": call_sid, "accountSid": "AC1", "tracks": ["inbound"],
                      "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                      "customParameters": {"from": caller, "token": platform.stream_token(call_sid)}}}


def _media():
    return {"event": "media", "streamSid": "MZ1",
            "media": {"track": "inbound", "payload": base64.b64encode(b"\xff" * 160).decode()}}


def _collect(ws, until, timeout=8.0):
    out = []
    end = time.time() + timeout
    while time.time() < end:
        msg = json.loads(ws.receive_text())
        out.append(msg)
        if until(out):
            return out
    raise AssertionError("timeout: " + str([m["event"] for m in out]))


def test_webhook_signature_and_twiml(make_platform):
    p = make_platform()
    with TestClient(create_app(p)) as client:
        params = {"CallSid": "CAabc", "From": "+15555550101", "To": "+15550000000"}
        sig = RequestValidator("test-token").compute_signature("https://relay.example.com/twilio/voice", params)
        r = client.post("/twilio/voice", data=params, headers={"X-Twilio-Signature": sig})
        assert r.status_code == 200
        assert 'wss://relay.example.com/twilio/media' in r.text and p.stream_token("CAabc") in r.text
        bad = client.post("/twilio/voice", data=params, headers={"X-Twilio-Signature": "nope"})
        assert bad.status_code == 403


def test_full_call_turn_and_barge_in(make_platform):
    p = make_platform(
        script=[{"text": "Sure, I can check that. What's your full name and date of birth?"},
                {"text": "Thanks John, you're verified. Your balance is forty five dollars. Want a payment link?"}],
        utterances=["Hi, what's my balance?", "John Doe, April 12 1980"])
    with TestClient(create_app(p)) as client, client.websocket_connect("/twilio/media") as ws:
        ws.send_text(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        ws.send_text(json.dumps(_start(p)))
        greet = _collect(ws, lambda m: m[-1]["event"] == "mark")
        assert any(m["event"] == "media" for m in greet)
        ws.send_text(json.dumps({"event": "mark", "streamSid": "MZ1", "mark": greet[-1]["mark"]}))
        for _ in range(25):  # caller speaks -> fake STT emits utterance 1
            ws.send_text(json.dumps(_media()))
        reply = _collect(ws, lambda m: sum(x["event"] == "mark" for x in m) >= 2)
        assert all(m["streamSid"] == "MZ1" for m in reply)
        assert any("date of birth" in s for s in p.tts.spoken)
        # caller talks over the agent before the mark comes back -> barge-in sends "clear"
        for _ in range(25):
            ws.send_text(json.dumps(_media()))
        after = _collect(ws, lambda m: any(x["event"] == "clear" for x in m))
        assert any(x["event"] == "clear" for x in after)
        ws.send_text(json.dumps({"event": "stop", "streamSid": "MZ1"}))
    turns = p.db.query("SELECT role, text FROM turns WHERE call_sid='CAtest' ORDER BY id")
    assert turns[0]["role"] == "caller" and turns[1]["role"] == "agent"
    call = p.db.one("SELECT * FROM calls WHERE call_sid='CAtest'")
    assert call["ended_at"]


def test_stream_with_bad_token_is_rejected(make_platform):
    p = make_platform()
    with TestClient(create_app(p)) as client, client.websocket_connect("/twilio/media") as ws:
        start = _start(p)
        start["start"]["customParameters"]["token"] = "forged"
        ws.send_text(json.dumps(start))
        try:
            ws.receive_text()
            raise AssertionError("expected close")
        except Exception as e:  # noqa: BLE001
            assert "4403" in repr(e) or "Disconnect" in type(e).__name__


def test_emergency_is_handled_deterministically(make_platform):
    p = make_platform(script=[], utterances=["I have chest pain and can't breathe"])
    with TestClient(create_app(p)) as client, client.websocket_connect("/twilio/media") as ws:
        ws.send_text(json.dumps(_start(p, "CAem")))
        greet = _collect(ws, lambda m: m[-1]["event"] == "mark")
        ws.send_text(json.dumps({"event": "mark", "streamSid": "MZ1", "mark": greet[-1]["mark"]}))
        for _ in range(25):
            ws.send_text(json.dumps(_media()))
        msgs = _collect(ws, lambda m: m[-1]["event"] == "mark")
        ws.send_text(json.dumps({"event": "mark", "streamSid": "MZ1", "mark": msgs[-1]["mark"]}))
        deadline = time.time() + 5
        while not p.twilio.transfers and time.time() < deadline:
            time.sleep(0.05)
        ws.send_text(json.dumps({"event": "stop"}))
    assert any("9 1 1" in s for s in p.tts.spoken)
    assert p.twilio.transfers == ["CAem"]
    assert p._test_model.seen == []  # LLM never consulted for the emergency path


def test_text_before_a_tool_call_is_spoken_before_the_tool_runs(make_platform):
    """Regression: 'Let me look...' used to wait in the sentence buffer until the tool returned."""
    from relayiq.gateway import clinic_tools

    order = []
    orig = clinic_tools.list_providers

    async def spy(ctx, a):
        order.append(("tool", list(p.tts.spoken)))
        return await orig(ctx, a)

    p = make_platform(script=[
        {"text": "Let me look at the schedule for you", "tools": [("list_providers", {})]},
        {"text": "I have Monday at nine thirty."}],
        utterances=["Any openings next week?"])
    p.gateway.tools["list_providers"].fn = spy
    with TestClient(create_app(p)) as client, client.websocket_connect("/twilio/media") as ws:
        ws.send_text(json.dumps(_start(p, "CAflush")))
        greet = _collect(ws, lambda m: m[-1]["event"] == "mark")
        ws.send_text(json.dumps({"event": "mark", "streamSid": "MZ1", "mark": greet[-1]["mark"]}))
        for _ in range(25):
            ws.send_text(json.dumps(_media()))
        _collect(ws, lambda m: sum(x["event"] == "mark" for x in m) >= 2)
        ws.send_text(json.dumps({"event": "stop"}))
    # The model's own lead-in is spoken immediately at the tool call, so no canned filler is needed
    # and nothing waits for the tool + second model call.
    assert order, "tool should have run"
    spoken = p.tts.spoken
    assert "Let me look at the schedule for you" in spoken
    assert "One moment." not in spoken
    assert spoken.index("Let me look at the schedule for you") < spoken.index("I have Monday at nine thirty.")


def _human_texts(messages):
    from langchain_core.messages import HumanMessage
    return [m.text for m in messages if isinstance(m, HumanMessage)]


def _one_turn(p, sid):
    import logging
    logging.getLogger("relayiq.session").setLevel(logging.INFO)
    with TestClient(create_app(p)) as client, client.websocket_connect("/twilio/media") as ws:
        ws.send_text(json.dumps(_start(p, sid)))
        greet = _collect(ws, lambda m: m[-1]["event"] == "mark")
        ws.send_text(json.dumps({"event": "mark", "streamSid": "MZ1", "mark": greet[-1]["mark"]}))
        for _ in range(25):
            ws.send_text(json.dumps(_media()))
        reply = _collect(ws, lambda m: m[-1]["event"] == "mark")
        ws.send_text(json.dumps({"event": "mark", "streamSid": "MZ1", "mark": reply[-1]["mark"]}))
        time.sleep(0.3)
        ws.send_text(json.dumps({"event": "stop"}))
    return reply


def test_speculative_turn_hit_reuses_the_early_llm_call(make_platform, caplog):
    """Partial transcript == final: the LLM started early, and its reply is spoken once confirmed."""
    p = make_platform(script=[{"text": "Sure thing. Your balance is forty five dollars."}],
                      utterances=[{"partial": "What's my balance?", "final": "What's my balance?", "delay": 0.4}])
    _one_turn(p, "CAhit")
    assert len(p._test_model.seen) == 1  # one LLM call, started before the final transcript
    agent = p.db.query("SELECT text FROM turns WHERE call_sid='CAhit' AND role='agent' AND text != ''")
    assert agent[-1]["text"].startswith("Sure thing.")
    assert "speculative turn confirmed" in caplog.text


def test_speculative_turn_miss_is_never_heard_and_is_rolled_back(make_platform, caplog):
    """Partial differs from final: the guessed reply is discarded before any audio is sent,
    and the conversation history only ever contains what the caller actually said."""
    p = make_platform(script=[{"text": "GUESSED REPLY."}, {"text": "Sure, Dr. Chen has Monday open."}],
                      utterances=[{"partial": "I want to book.",
                                   "final": "I want to book with Dr. Chen.", "delay": 0.4}])
    _one_turn(p, "CAmiss")
    assert len(p._test_model.seen) == 2
    assert _human_texts(p._test_model.seen[1]) == ["I want to book with Dr. Chen."]
    agent = [r["text"] for r in p.db.query("SELECT text FROM turns WHERE call_sid='CAmiss' AND role='agent'")]
    assert not any("GUESSED" in t for t in agent)
    assert any("Dr. Chen has Monday" in t for t in agent)
    assert "speculative turn discarded" in caplog.text


def test_speculative_turn_never_runs_write_tools_before_confirmation(make_platform):
    """A guessed turn that tries to book is blocked at the gateway and abandoned on a miss."""
    p = make_platform(script=[{"text": "", "tools": [("create_callback_task", {"reason": "x"})]},
                              {"text": "Okay, what day works?"}],
                      utterances=[{"partial": "Book me in.", "final": "Book me in for Tuesday please.", "delay": 0.4}])
    _one_turn(p, "CAwrite")
    rows = p.db.query("SELECT * FROM ledger WHERE call_sid='CAwrite'") if _has_table(p, "ledger") else []
    assert not any(r.get("tool") == "create_callback_task" for r in rows)


def _has_table(p, name):
    return bool(p.db.query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)))
