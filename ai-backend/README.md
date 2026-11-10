# ai-backend
SIP gateway + consent-gated voice cloning API. See the root README for setup. API docs at `/docs` once running.

Flow: `POST /v1/voice-profiles` (upload) → `POST /v1/voice-profiles/{id}/consent` → `POST /v1/voice-profiles/{id}/clone` → app opens `WS /v1/calls/ws`.
Modules: `sip.py` (SIP/RTP), `g711.py`, `service.py` (consent), `providers.py`, `bridge.py` (fail-closed audio), `session.py`, `store.py` (SQLite).
