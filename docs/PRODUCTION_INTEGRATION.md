# Production checklist
- Run the backend behind HTTPS; rotate `API_KEY`; restrict firewall: TCP 443, UDP RTP range.
- Choose provider: ElevenLabs (easy, ~1.5–3 s latency) or self-hosted GPU voice conversion (Seed-VC/RVC wrapper implementing the 3-endpoint contract in `providers.py`) for <0.5 s.
- Verify your SIP provider allows the server's IP, G.711 (PCMU/PCMA) and outbound caller ID rules.
- Move SIP passwords to Android Keystore (EncryptedSharedPreferences) and add per-user auth instead of one API key.
- Add a foreground service (microphone type) so calls survive backgrounding.
- Keep consent records and audit log; honour deletion requests; follow local call-recording / AI-disclosure / telemarketing laws.
- Acceptance tests on two real phones: register, outbound call, DTMF, mute, AI voice on/off, conversion outage (caller must hear silence), consent revoke mid-session.
