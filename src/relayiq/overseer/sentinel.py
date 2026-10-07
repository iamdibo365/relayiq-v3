"""Sentinel release gate: run the suite in a sandbox, grade it, decide pass/fail, keep the report."""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ..agents.registry import AgentSpec
from ..db import now_iso
from .grader import grade
from .simulator import Scenario, simulate

if TYPE_CHECKING:
    from ..platform import Platform

BASELINE = Path(__file__).with_name("scenarios.yaml")


def load_scenarios(path: Path = BASELINE) -> list[Scenario]:
    return [Scenario(**s) for s in yaml.safe_load(path.read_text())]


async def run_gate(platform: "Platform", scenarios: list[Scenario], target: str = "platform",
                   sandbox_agent: AgentSpec | None = None, concurrency: int = 3) -> dict:
    run_id = "eval_" + uuid.uuid4().hex[:8]
    platform.db.execute("INSERT INTO eval_runs VALUES (?,?,?,?,?,?,?)",
                        (run_id, target, now_iso(), None, None, None, None))
    sem = asyncio.Semaphore(concurrency)
    tmp = Path(tempfile.mkdtemp(prefix="relayiq_sandbox_"))

    async def one(sc: Scenario):
        async with sem:
            sandbox = platform.db.backup_to(str(tmp / f"{sc.id}.db"))
            result = await simulate(platform, sc, sandbox, sandbox_agent)
            g = await grade(platform, result)
            return {"scenario": sc.id, "passed": g.passed, "checks": g.checks, "scores": g.scores,
                    "notes": g.notes, "transcript": result.transcript,
                    "tools": [{k: t[k] for k in ("agent", "tool", "decision")} for t in result.tool_log]}

    results = await asyncio.gather(*(one(s) for s in scenarios))
    scored = [r["scores"] for r in results if r["scores"]]
    mean = (sum((s["task_success"] + s["safety"] + s["accuracy"] + s["voice_quality"]) / 4
                for s in scored) / len(scored)) if scored else 0.0
    passed = all(r["passed"] for r in results)
    report = {"run_id": run_id, "target": target, "passed": passed, "mean_score": round(mean, 2),
              "n": len(results), "n_passed": sum(r["passed"] for r in results), "results": results}
    platform.db.execute("UPDATE eval_runs SET completed_at=?, passed=?, score=?, report=? WHERE id=?",
                        (now_iso(), int(passed), round(mean, 2), json.dumps(report), run_id))
    return report


def scenario_from_dict(d: dict) -> Scenario:
    return Scenario(**{k: d[k] for k in asdict(Scenario("", "", "", "")).keys() if k in d})
