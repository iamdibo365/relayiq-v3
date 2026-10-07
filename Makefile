.PHONY: install dev app mcp tunnel twilio smoke evals test

install:            ## deps + Chromium for the payer-portal agent
	uv sync
	uv run playwright install chromium

dev:                ## MCP context server + voice app together
	./scripts/dev.sh

app:
	uv run uvicorn relayiq.app:app --host 0.0.0.0 --port 8000 --app-dir src

mcp:
	uv run python -m relayiq.context.mcp_server

tunnel:             ## public HTTPS URL for Twilio; copy it into PUBLIC_BASE_URL
	cloudflared tunnel --url http://localhost:8000

twilio:             ## point your Twilio number at PUBLIC_BASE_URL (add DEMO_CALLER=+1... to map your cell)
	uv run python scripts/setup_twilio.py $(if $(DEMO_CALLER),--demo-caller $(DEMO_CALLER),)

smoke:              ## real end-to-end call without a phone (needs `make dev` running)
	uv run python scripts/smoke_call.py

evals:              ## Overseer release gate with real models (exit 1 on failure)
	uv run python scripts/run_evals.py

test:               ## unit + protocol tests (test doubles, no API spend)
	uv run pytest -q
