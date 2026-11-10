"""Audio bridge between the app (PCM16 16 kHz) and a SIP call (G.711 8 kHz).

Outgoing (app mic -> phone): if AI voice is ON the audio is converted by the provider; if conversion
fails the caller hears SILENCE (never the raw microphone) and the app is notified. Mic is only forwarded
raw when AI voice is OFF for the call.
Incoming (phone -> app) is never modified.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

import numpy as np

from . import g711
from .providers import VoiceProvider

RATE = 16000
FRAME_BYTES = 640  # 20 ms @ 16 kHz PCM16


class CallBridge:
    MAX_INFLIGHT = 3   # parallel provider requests
    MAX_BACKLOG = 4    # chunks allowed to wait before the oldest are skipped

    def __init__(self, call, *, provider: Optional[VoiceProvider], voice_id: Optional[str],
                 send_to_client: Callable[[bytes], Awaitable[None]], notify: Callable[[dict], Awaitable[None]],
                 chunk_ms: int = 800, silence_rms: float = 120.0):
        self.call, self.provider, self.voice_id = call, provider, voice_id
        self.ai = provider is not None
        self.send_to_client, self.notify = send_to_client, notify
        self.chunk_bytes = RATE * 2 * chunk_ms // 1000
        self.silence_rms = silence_rms
        self.muted = False
        self.failures = 0
        self.converted_chunks = 0
        self._mic = bytearray()
        self._out = bytearray()
        self._convq: asyncio.Queue = asyncio.Queue()
        self._rxq: asyncio.Queue = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self.dropped_chunks = 0
        self._sem = asyncio.Semaphore(self.MAX_INFLIGHT)
        self._pending: asyncio.Queue = asyncio.Queue()
        call.on_audio = self._on_remote_audio

    def start(self):
        self._tasks = [asyncio.create_task(self._pacer()), asyncio.create_task(self._rx_sender())]
        if self.ai:
            self._tasks.append(asyncio.create_task(self._converter()))
            self._tasks.append(asyncio.create_task(self._emitter()))

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        while not self._pending.empty():
            self._pending.get_nowait().cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def set_muted(self, muted: bool):
        self.muted = muted
        if muted:
            self._mic.clear(); self._out.clear()

    # --- mic -> phone
    def feed_mic(self, data: bytes):
        if self.muted:
            return
        if not self.ai:
            self._push_out(data)
            return
        self._mic += data
        while len(self._mic) >= self.chunk_bytes:
            self._convq.put_nowait(bytes(self._mic[: self.chunk_bytes]))
            del self._mic[: self.chunk_bytes]

    def _push_out(self, pcm: bytes):
        self._out += pcm
        cap = RATE * 2 * 8  # keep at most 8 s buffered
        if len(self._out) > cap:
            del self._out[: len(self._out) - cap]

    async def _convert_one(self, chunk: bytes) -> bytes:
        """Convert one chunk. Fail closed: any problem => silence of the same length, never the raw mic."""
        pcm = g711.pcm_from_bytes(chunk)
        if float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2))) < self.silence_rms:
            return b"\x00" * len(chunk)  # silence stays silence, saves provider cost
        try:
            out = await asyncio.wait_for(self.provider.convert(self.voice_id, chunk), 20)
            if len(out) % 2:
                out = out[:-1]
            self.converted_chunks += 1
            self.failures = 0
            return out
        except Exception as e:
            self.failures += 1
            if self.failures in (1, 5):
                await self.notify({"type": "warning", "code": "voice_conversion_failed", "detail": str(e)[:200],
                                   "message": "AI voice conversion failed; the other party hears silence, not your real voice."})
            return b"\x00" * len(chunk)

    async def _converter(self):
        """Pipeline: several chunks are converted in parallel (so a 1.5 s provider round-trip does not make the
        delay grow during a long call) and emitted strictly in order. If the provider falls behind, the oldest
        waiting chunks are dropped so the voice stays 'live' until the last second of the call."""
        while True:
            chunk = await self._convq.get()
            while self._convq.qsize() > self.MAX_BACKLOG:  # too far behind -> skip the oldest audio
                chunk = await self._convq.get()
                self.dropped_chunks += 1
                if self.dropped_chunks in (1, 10):
                    await self.notify({"type": "warning", "code": "voice_lagging",
                                       "message": "AI voice is lagging (slow internet or provider); some audio was skipped to stay in sync."})
            await self._sem.acquire()
            task = asyncio.create_task(self._convert_one(chunk))
            task.add_done_callback(lambda _t: self._sem.release())
            self._pending.put_nowait(task)

    async def _emitter(self):
        while True:
            task = await self._pending.get()
            try:
                out = await task
            except asyncio.CancelledError:
                raise
            except Exception:
                out = b""
            self._push_out(out)

    async def _pacer(self):
        loop = asyncio.get_running_loop()
        nxt = loop.time()
        silence = np.zeros(160, dtype=np.int16)
        while True:
            if len(self._out) >= FRAME_BYTES:
                frame = g711.pcm_from_bytes(bytes(self._out[:FRAME_BYTES]))
                del self._out[:FRAME_BYTES]
                self.call.send_audio(g711.resample(frame, RATE, 8000))
            else:
                self.call.send_audio(silence)  # keeps RTP flowing (NAT / provider timeouts)
            nxt += 0.02
            await asyncio.sleep(max(0.0, nxt - loop.time()))

    # --- phone -> app (untouched)
    def _on_remote_audio(self, pcm8k: np.ndarray):
        self._rxq.put_nowait(g711.pcm_to_bytes(g711.resample(pcm8k, 8000, RATE)))

    async def _rx_sender(self):
        while True:
            data = await self._rxq.get()
            try:
                await self.send_to_client(data)
            except Exception:
                return
