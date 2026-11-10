"""One phone call driven over a websocket-like channel (framework independent => unit-testable)."""
from __future__ import annotations

import asyncio
import re
from typing import Awaitable, Callable, Optional

from . import providers
from .bridge import CallBridge
from .service import ServiceError, VoiceService
from .sip import SipAccount, SipCall, SipError, SipUA
from .store import Store

DEST_RE = re.compile(r"^[A-Za-z0-9+*#@._:\-]{1,128}$")


class CallSession:
    def __init__(self, store: Store, service: VoiceService, send_json: Callable[[dict], Awaitable[None]],
                 send_bytes: Callable[[bytes], Awaitable[None]], ua_factory=SipUA):
        self.store, self.service = store, service
        self.send_json, self.send_bytes, self.ua_factory = send_json, send_bytes, ua_factory
        self.ua: Optional[SipUA] = None
        self.call: Optional[SipCall] = None
        self.bridge: Optional[CallBridge] = None
        self.call_row: Optional[str] = None
        self._watch: Optional[asyncio.Task] = None
        self.agent_mode = False
        self._stopping = False
        self._agent_task: Optional[asyncio.Task] = None
        self._provider = None
        self._voice_id = None
        self._profile_id = None
        self._chunk_ms = 800

    async def _state(self, state: str, detail: str = ""):
        await self.send_json({"type": "state", "state": state, "detail": detail})

    async def start(self, msg: dict):
        """Validate everything (fail closed), register, dial, start audio bridge."""
        self.agent_mode = msg.get("mode") == "agent"   # VICIdial/Asterisk agent phone: wait for the server to call us
        dest = str(msg.get("destination", "")).strip()
        if not self.agent_mode and not DEST_RE.match(dest):
            return await self._fail("Invalid destination number / SIP URI.")
        acc = msg.get("account") or {}
        try:
            account = SipAccount(username=acc["username"], password=acc.get("password", ""), server=acc["server"], port=int(acc.get("port", 5060)),
                                 domain=acc.get("domain", ""), auth_id=acc.get("auth_id", ""), display_name=acc.get("display_name", ""),
                                 caller_id=acc.get("caller_id", ""), proxy=acc.get("proxy", ""), transport=acc.get("transport", "UDP"))
        except (KeyError, ValueError, TypeError):
            return await self._fail("SIP account needs at least username and server.")

        provider = voice_id = None
        profile_id = msg.get("profile_id") or None
        if profile_id:  # AI voice ON -> every gate must pass, otherwise the call is refused (no raw-voice fallback)
            if not msg.get("disclosure_ack"):
                return await self._fail("Acknowledge that the other party must be told the voice is AI-generated.")
            try:
                _, voice_id = self.service.usable_voice(profile_id)
                provider = providers.get_provider(str(msg.get("elevenlabs_key", "")))
            except ServiceError as e:
                return await self._fail(e.message)
            except providers.ProviderNotConfigured as e:
                return await self._fail(str(e))

        if self.agent_mode:
            self._provider, self._voice_id, self._profile_id = provider, voice_id, profile_id
            self._chunk_ms = int(msg.get("chunk_ms", 800))
            try:
                await self._state("registering")
                self.ua = self.ua_factory(account)
                await self.ua.start()
                await self.ua.register()
            except (SipError, OSError) as e:
                await self._cleanup("failed", str(e))
                return await self._fail(f"SIP error: {e}")
            self.ua.on_incoming = self._on_incoming
            await self._state("waiting", "Registered. Now log in to the VICIdial agent screen with this phone.")
            return

        self.call_row = self.store.start_call(dest, profile_id, bool(profile_id))
        try:
            await self._state("registering")
            self.ua = self.ua_factory(account)
            await self.ua.start()
            await self.ua.register()
            await self._state("calling")
            loop = asyncio.get_running_loop()
            self.call = await self.ua.call(dest, on_state=lambda s, d: loop.create_task(self._state(s, d)) if s == "ringing" else None)
        except (SipError, OSError) as e:
            await self._cleanup("failed", str(e))
            return await self._fail(f"SIP error: {e}")
        self.bridge = CallBridge(self.call, provider=provider, voice_id=voice_id, send_to_client=self.send_bytes, notify=self.send_json,
                                 chunk_ms=int(msg.get("chunk_ms", 800)))
        self.bridge.start()
        await self._state("connected", "ai_voice" if provider else "own_voice")
        self._watch = asyncio.create_task(self._watch_end())

    def _on_incoming(self, call: SipCall):
        """VICIdial/Asterisk called our extension (agent login). Auto-answered by the SIP layer."""
        self._agent_task = asyncio.get_running_loop().create_task(self._run_incoming(call))

    async def _run_incoming(self, call: SipCall):
        self.call = call
        self.call_row = self.store.start_call(call.caller or "vicidial", self._profile_id, bool(self._profile_id))
        self.bridge = CallBridge(call, provider=self._provider, voice_id=self._voice_id, send_to_client=self.send_bytes,
                                 notify=self.send_json, chunk_ms=self._chunk_ms)
        self.bridge.start()
        await self._state("connected", "ai_voice" if self._provider else "own_voice")
        await call.ended.wait()
        if self.bridge:
            await self.bridge.stop(); self.bridge = None
        if self.call_row:
            self.store.finish_call(self.call_row, "completed", call.end_reason); self.call_row = None
        self.call = None
        if self.ua and not self._stopping:  # session still active -> wait for the next agent login / call
            await self._state("waiting", f"Call ended ({call.end_reason}). Waiting for VICIdial to call again.")

    async def _watch_end(self):
        await self.call.ended.wait()
        await self._cleanup("completed", self.call.end_reason)
        await self._state("ended", self.call.end_reason)

    async def _fail(self, message: str):
        await self.send_json({"type": "error", "message": message})
        await self._state("ended", message)

    async def on_bytes(self, data: bytes):
        if self.bridge:
            self.bridge.feed_mic(data)

    async def on_json(self, msg: dict):
        t = msg.get("type")
        if t == "hangup" and self.agent_mode:
            await self._stop_agent()
        elif t == "hangup" and self.call:
            await self.call.hangup()
        elif t == "dtmf" and self.call:
            try:
                await self.call.dtmf(str(msg.get("digit", ""))[:1])
            except SipError as e:
                await self.send_json({"type": "error", "message": str(e)})
        elif t == "mute" and self.bridge:
            self.bridge.set_muted(bool(msg.get("on")))
            await self.send_json({"type": "muted", "on": self.bridge.muted})

    async def _stop_agent(self):
        """User pressed End session: hang up any active call, unregister, tell the app."""
        self._stopping = True
        if self.call and self.call.state != "ended":
            await self.call.hangup()
        if self._agent_task:
            await asyncio.gather(self._agent_task, return_exceptions=True)
        await self._cleanup("completed", "session ended")
        await self._state("ended", "Session ended")

    async def _cleanup(self, status: str, reason: str):
        if self.bridge:
            await self.bridge.stop(); self.bridge = None
        if self.call_row:
            self.store.finish_call(self.call_row, status, reason); self.call_row = None
        if self.ua:
            await self.ua.stop(); self.ua = None

    async def close(self):
        """Websocket closed: always hang up and release resources."""
        self._stopping = True
        if self._agent_task:
            if self.call and self.call.state != "ended":
                await self.call.hangup()
            await asyncio.gather(self._agent_task, return_exceptions=True)
        if self.call and self.call.state != "ended":
            await self.call.hangup()
        if self._watch:
            await asyncio.gather(self._watch, return_exceptions=True)
        await self._cleanup("completed", "client disconnected")
