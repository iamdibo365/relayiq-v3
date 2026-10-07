import pytest

from relayiq.agents.context import CallContext


@pytest.fixture
def ctx(make_platform):
    p = make_platform()
    c = CallContext("CA1", "+15555550101", p.db, p.settings, services=p.services())
    p.db.execute("INSERT INTO calls(call_sid, from_number) VALUES ('CA1','+15555550101')")
    return p, c


async def test_phi_requires_verification(ctx):
    p, c = ctx
    res = await p.gateway.invoke(c, "billing", "get_balance", {})
    assert res["ok"] is False and "not verified" in res["denied"]
    row = p.db.one("SELECT decision FROM ledger ORDER BY id DESC LIMIT 1")
    assert row["decision"] == "denied"


async def test_verify_then_read(ctx):
    p, c = ctx
    bad = await p.gateway.invoke(c, "front_desk", "verify_identity",
                                 {"first_name": "John", "last_name": "Doe", "date_of_birth": "1985-03-03"})
    assert bad["ok"] is False
    good = await p.gateway.invoke(c, "front_desk", "verify_identity",
                                  {"first_name": "john", "last_name": "DOE", "date_of_birth": "April 12, 1980"})
    assert good["ok"] and c.verified
    bal = await p.gateway.invoke(c, "billing", "get_balance", {})
    assert bal["amount_due"] == 45.0


async def test_write_requires_confirmation_and_insurance_check(ctx):
    p, c = ctx
    await p.gateway.invoke(c, "front_desk", "verify_identity",
                           {"first_name": "John", "last_name": "Doe", "date_of_birth": "1980-04-12"})
    slot = (await p.gateway.invoke(c, "scheduling", "find_open_slots", {}))["slots"][0]
    unconfirmed = await p.gateway.invoke(c, "scheduling", "book_appointment",
                                         {"slot_id": slot["id"], "reason": "follow-up"})
    assert "caller_confirmed" in unconfirmed["denied"]
    no_check = await p.gateway.invoke(c, "scheduling", "book_appointment",
                                      {"slot_id": slot["id"], "reason": "follow-up", "caller_confirmed": True})
    assert "verify_insurance" in no_check["denied"]
    ins = await p.gateway.invoke(c, "scheduling", "verify_insurance", {})
    assert ins["status"] == "unverified"  # no clearinghouse key in tests -> honest, not faked
    booked = await p.gateway.invoke(c, "scheduling", "book_appointment",
                                    {"slot_id": slot["id"], "reason": "follow-up", "caller_confirmed": True})
    assert booked["ok"] and booked["status"] == "pending_insurance"
    again = await p.gateway.invoke(c, "scheduling", "book_appointment",
                                   {"slot_id": slot["id"], "reason": "follow-up", "caller_confirmed": True})
    assert again["note"].startswith("already done")  # idempotent
    assert p.twilio.sms and "booked" in p.twilio.sms[0][1]


async def test_ledger_masks_phi(ctx):
    p, c = ctx
    await p.gateway.invoke(c, "front_desk", "verify_identity",
                           {"first_name": "John", "last_name": "Doe", "date_of_birth": "1980-04-12"})
    row = p.db.one("SELECT args FROM ledger WHERE tool='verify_identity'")
    assert "1980-04-12" not in row["args"]
