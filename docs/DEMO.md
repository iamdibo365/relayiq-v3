# Demo runbook (about 15 minutes)

**Before the call:** `make dev`, `make tunnel`, update `PUBLIC_BASE_URL`, `make twilio DEMO_CALLER=<your cell>`, `make smoke` once to warm up, open the dashboard and the repo side by side. Run `make evals` earlier in the day so the gate results are on screen.

1. **Live call (4 min).** Put your phone on speaker, call the number from your cell.
   - "Hi, I need a follow-up with Dr. Chen next week." → front desk hands off to scheduling (watch the agent chip change).
   - Give name + DOB → `verify_identity` executes in the ledger.
   - Pick a time → "Let me verify that with your insurance plan" → insurance panel shows the check (API result, or pending → portal job).
   - Confirm → `book_appointment` executes; SMS confirmation arrives on your phone.
   - Interrupt the agent mid-sentence once to show barge-in.
2. **Guardrails (2 min).** Point at the denied rows in the ledger (write without confirmation, PHI before verification). Mention the deterministic emergency path: "chest pain" never reaches the LLM.
3. **Insurance verification (3 min).** `insurance/eligibility.py` (270/271 request shape) and `insurance/portal_agent.py` (allow-listed domains, secrets typed by the tool and scrubbed from snapshots, screenshot audit). Explain why API first, portal second.
4. **Forge + Overseer (4 min).** Call from the ADMIN_PHONE (or use the dashboard box): "Build an agent that answers prior-authorization status questions; it can look up insurance on file and create a callback task." Show the agent appear as `testing`, the simulated calls in the eval report, and `active`/`rejected`.
5. **MCP (1 min).** `context/mcp_server.py`: the same journey any channel or product could read; maps to the intelligence layer with MCP that Ali mentioned.
6. **Close (1 min).** What changes for a real customer: BAAs, their EHR/PM system behind the gateway instead of SQLite, their clearinghouse, the same release gate in their CI.
