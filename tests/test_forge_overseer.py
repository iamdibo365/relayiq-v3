from relayiq.forge.forge import AgentDraft, ScenarioDraft
from relayiq.overseer.grader import JudgeScores, deterministic_checks
from relayiq.overseer.sentinel import load_scenarios, run_gate
from relayiq.overseer.simulator import Scenario, SimResult


def _draft(tools):
    return AgentDraft(
        name="Prior Auth!", display_name="Prior authorization", purpose="Prior auth status questions",
        instructions="You handle prior auth for {clinic}. Use {braces} carefully.", tools=tools,
        test_scenarios=[ScenarioDraft(id="a", caller_phone="+15555550101", persona="p", goal="g"),
                        ScenarioDraft(id="b", caller_phone="+15555550102", persona="p", goal="g")])


def test_forge_least_privilege_validation(make_platform):
    p = make_platform()
    spec, problems = p.forge.validate(_draft(["get_insurance_on_file", "forge_build_agent", "rm_rf"]))
    assert spec.name == "prior_auth" and spec.tools == ["get_insurance_on_file"]
    assert any("forge_build_agent" in x and "rm_rf" in x for x in problems)
    assert spec.instructions.format(clinic="Lakeside") .startswith("You handle prior auth for Lakeside")
    assert spec.status == "testing"


def test_deterministic_checks_flag_write_before_verification():
    sc = Scenario("x", "+1", "p", "g", must_call=["book_appointment"], must_escalate=True)
    log = [{"agent": "scheduling", "tool": "book_appointment", "decision": "executed", "args": {}, "result": {"ok": True}}]
    checks = deterministic_checks(SimResult(sc, [], log, escalated=False))
    assert checks["called:book_appointment"] and not checks["no_write_before_verification"]
    assert not checks["escalated"]


async def test_release_gate_end_to_end_with_scripted_models(make_platform):
    """Simulator (caller model) + orchestrator (agent model) + judge, in a sandbox DB."""
    judge = JudgeScores(task_success=5, safety=5, accuracy=5, voice_quality=4, rationale="ok")

    class Router:
        def __init__(self):
            self.caller_turns = 0

        def __call__(self, messages):
            first = messages[0].content
            if first.startswith("You are role-playing a caller"):
                self.caller_turns += 1
                return {"text": ["Can I talk to a real person please?", "I said a human, now."][self.caller_turns - 1]
                        if self.caller_turns <= 2 else "[END]"}
            return {"text": "I can help with most things here. What do you need?"}

    router = Router()
    p = make_platform(script=[router] * 20)
    p._test_model.__class__.with_structured_output = lambda self, schema, **kw: _Fixed(judge)
    scen = [s for s in load_scenarios() if s.id == "asks_for_human"]
    report = await run_gate(p, scen, target="test")
    r = report["results"][0]
    assert r["checks"]["escalated"] is True  # watchdog caught the second human request
    assert report["passed"] is True
    # production DB untouched by the simulated call
    assert p.db.one("SELECT COUNT(*) AS n FROM calls WHERE call_sid LIKE 'sim_%'")["n"] == 0
    assert p.db.one("SELECT passed FROM eval_runs WHERE id=?", (report["run_id"],))["passed"] == 1


class _Fixed:
    def __init__(self, value):
        self.value = value

    async def ainvoke(self, *_a, **_k):
        return self.value
