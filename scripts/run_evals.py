"""Run the Overseer release gate with real models. Exit code 1 if the gate fails (use in CI)."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from relayiq.config import get_settings  # noqa: E402
from relayiq.overseer.sentinel import load_scenarios, run_gate  # noqa: E402
from relayiq.platform import Platform  # noqa: E402


async def main():
    p = Platform(get_settings())
    only = set(sys.argv[1:])
    scenarios = [s for s in load_scenarios() if not only or s.id in only]
    report = await run_gate(p, scenarios, target="platform")
    for r in report["results"]:
        sc = r["scores"] or {}
        failed = [k for k, v in r["checks"].items() if not v]
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['scenario']:<32} "
              f"task={sc.get('task_success')} safety={sc.get('safety')} acc={sc.get('accuracy')} "
              f"voice={sc.get('voice_quality')}  {('failed: ' + ', '.join(failed)) if failed else ''} {r['notes']}")
    print(f"\nGate {'PASSED' if report['passed'] else 'FAILED'}: {report['n_passed']}/{report['n']} "
          f"scenarios, mean score {report['mean_score']} (run {report['run_id']})")
    Path("data").mkdir(exist_ok=True)
    Path(f"data/{report['run_id']}.json").write_text(json.dumps(report, indent=2))
    sys.exit(0 if report["passed"] else 1)


asyncio.run(main())
