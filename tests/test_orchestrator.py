from relayiq.agents.context import CallContext
from relayiq.agents.orchestrator import Orchestrator


async def test_handoff_runs_specialist_in_same_turn_with_scoped_tools(make_platform):
    p = make_platform(script=[
        {"text": "Let me get scheduling for you.",
         "tools": [("transfer_to_agent", {"agent_name": "scheduling", "reason": "wants an appointment"})]},
        {"text": ""},  # front desk after tool result
        {"text": "Happy to help you book. Can I get your full name and date of birth?"},
    ])
    ctx = CallContext("CA2", "+15555550101", p.db, p.settings, services=p.services())
    orch = Orchestrator(ctx, p.gateway, p.registry, p.settings, p.model_factory)
    events = [e async for e in orch.respond("I need an appointment")]
    kinds = [e.kind for e in events]
    assert "agent" in kinds and ctx.active_agent == "scheduling"
    text = "".join(e.value for e in events if e.kind == "text")
    assert "scheduling" in text and "date of birth" in text
    # specialist prompt carries handoff reason and its own tool scope
    sys_prompt = p._test_model.seen[-1][0].content
    assert "wants an appointment" in sys_prompt and "Active agent: scheduling" in sys_prompt
    assert orch.history[-1].content.startswith("Let me get scheduling")


async def test_unverified_specialist_cannot_reach_phi(make_platform):
    p = make_platform(script=[
        {"text": "", "tools": [("get_balance", {})]},
        {"text": "I need to verify your identity first."},
    ])
    ctx = CallContext("CA3", "+15555550101", p.db, p.settings, services=p.services(),
                      active_agent="billing")
    orch = Orchestrator(ctx, p.gateway, p.registry, p.settings, p.model_factory)
    _ = [e async for e in orch.respond("what's my balance")]
    assert ctx.tool_log[0]["decision"] == "denied"
