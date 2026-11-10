# Status

## Tested (pytest, `ai-backend/tests`, 17 tests)
- G.711 codec round-trip, RFC 2617 digest vector.
- SIP REGISTER/INVITE/ACK/BYE/DTMF/RTP against an in-process fake SIP server (digest auth, wrong password, remote BYE).
- Consent state machine: no clone without consent; decline deletes sample+profile; typed-name/disclosure/permission-note checks; revoke blocks calls; audit log.
- Call session: AI call delivers *converted* audio only (cross-correlation proves no raw mic leaks); provider failure ⇒ silence + warning; AI call refused without disclosure ack or after revoke; own-voice call passes audio through.

## Written but NOT executed here
- FastAPI layer `app/main.py` (py_compile OK; FastAPI/httpx not installable in the build sandbox; `tests/test_api.py` runs when they are installed).
- ElevenLabs and self-hosted provider HTTP adapters (no API key/network).
- Entire Android app (no Android SDK in the sandbox). Expect possible small compile errors on first build; send the log.

## Known limits
UDP-only SIP, no incoming calls, hold, SRTP, STUN/ICE, TLS; ACK/BYE are sent via the outbound proxy/registrar address (no Record-Route handling); call audio stops if the app is backgrounded (no foreground service yet); single shared API key (no per-user accounts); SIP password stored in app preferences and sent to your backend per call.
