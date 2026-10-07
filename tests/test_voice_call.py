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
