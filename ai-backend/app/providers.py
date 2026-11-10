"""Voice-conversion providers. All conversion happens through a provider; there is no raw-voice fallback.

Providers:
  elevenlabs  - ELEVENLABS_API_KEY. Instant voice clone + speech-to-speech (chunked, ~1-3 s latency).
  selfhosted  - SELF_HOSTED_VC_URL. Your own Seed-VC / RVC / OpenVoice wrapper implementing the tiny HTTP contract below.
  test        - ALLOW_TEST_PROVIDER=1. Phase-inverts audio so tests can prove conversion ran. NOT a voice clone.

Self-hosted contract:
  POST {url}/voices      multipart(name, sample)            -> {"voice_id": "..."}
  POST {url}/convert?voice_id=..  body=WAV(16 kHz mono)     -> raw PCM16LE 16 kHz mono
  DELETE {url}/voices/{voice_id}
"""
from __future__ import annotations

import asyncio
import io
import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
import wave
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np


class ProviderError(Exception):
    pass


class ProviderNotConfigured(ProviderError):
    pass


def pcm_to_wav(pcm: bytes, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)
    return buf.getvalue()


def multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, (fname, data, ctype) in files.items():
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\nContent-Type: {ctype}\r\n\r\n'.encode())
        out.write(data + b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def _http(method: str, url: str, headers: dict, body: bytes | None = None, timeout: float = 60) -> bytes:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise ProviderError(f"Provider HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}") from None
    except urllib.error.URLError as e:
        raise ProviderError(f"Provider unreachable: {e.reason}") from None


class VoiceProvider(ABC):
    name = "base"

    @abstractmethod
    async def create_voice(self, name: str, sample_path: Path) -> str: ...

    @abstractmethod
    async def delete_voice(self, voice_id: str) -> None: ...

    @abstractmethod
    async def convert(self, voice_id: str, pcm16_16k: bytes) -> bytes:
        """Convert speech to the cloned voice. Input/output: PCM16LE mono 16 kHz."""


class ElevenLabsProvider(VoiceProvider):
    name = "elevenlabs"
    BASE = "https://api.elevenlabs.io"

    def __init__(self, key: str):
        self.key = key

    async def create_voice(self, name, sample_path):
        data = Path(sample_path).read_bytes()
        ctype = mimetypes.guess_type(str(sample_path))[0] or "audio/mpeg"
        body, ct = multipart({"name": name, "remove_background_noise": "true", "description": "Consent-verified voice profile (AI Clone Dialer)"},
                             {"files": (Path(sample_path).name, data, ctype)})
        raw = await asyncio.to_thread(_http, "POST", f"{self.BASE}/v1/voices/add", {"xi-api-key": self.key, "Content-Type": ct}, body, 180)
        vid = json.loads(raw).get("voice_id")
        if not vid:
            raise ProviderError("ElevenLabs returned no voice_id")
        return vid

    async def delete_voice(self, voice_id):
        await asyncio.to_thread(_http, "DELETE", f"{self.BASE}/v1/voices/{voice_id}", {"xi-api-key": self.key})

    async def convert(self, voice_id, pcm16_16k):
        body, ct = multipart({"model_id": "eleven_multilingual_sts_v2", "remove_background_noise": "true"},
                             {"audio": ("chunk.wav", pcm_to_wav(pcm16_16k), "audio/wav")})
        url = f"{self.BASE}/v1/speech-to-speech/{voice_id}?output_format=pcm_16000"
        return await asyncio.to_thread(_http, "POST", url, {"xi-api-key": self.key, "Content-Type": ct}, body, 60)


class SelfHostedProvider(VoiceProvider):
    name = "selfhosted"

    def __init__(self, url: str, token: str = ""):
        self.url = url.rstrip("/")
        self.h = {"Authorization": f"Bearer {token}"} if token else {}

    async def create_voice(self, name, sample_path):
        data = Path(sample_path).read_bytes()
        body, ct = multipart({"name": name}, {"sample": (Path(sample_path).name, data, mimetypes.guess_type(str(sample_path))[0] or "audio/mpeg")})
        raw = await asyncio.to_thread(_http, "POST", f"{self.url}/voices", {**self.h, "Content-Type": ct}, body, 300)
        return json.loads(raw)["voice_id"]

    async def delete_voice(self, voice_id):
        await asyncio.to_thread(_http, "DELETE", f"{self.url}/voices/{voice_id}", self.h)

    async def convert(self, voice_id, pcm16_16k):
        return await asyncio.to_thread(_http, "POST", f"{self.url}/convert?voice_id={voice_id}", {**self.h, "Content-Type": "audio/wav"}, pcm_to_wav(pcm16_16k), 30)


class TestProvider(VoiceProvider):
    """Pipeline test double. Inverts phase; clearly NOT a voice clone."""
    name = "test"

    async def create_voice(self, name, sample_path):
        return "test-" + uuid.uuid4().hex[:8]

    async def delete_voice(self, voice_id):
        return None

    async def convert(self, voice_id, pcm16_16k):
        x = np.frombuffer(pcm16_16k, dtype="<i2").astype(np.int32)
        return np.clip(-x, -32768, 32767).astype("<i2").tobytes()


def get_provider(elevenlabs_key: str = "") -> VoiceProvider:
    """elevenlabs_key: key typed into the app (per request). Falls back to the ELEVENLABS_API_KEY env var."""
    elevenlabs_key = (elevenlabs_key or "").strip()
    which = os.getenv("VOICE_PROVIDER", "").lower()
    if elevenlabs_key and which in ("", "elevenlabs"):
        return ElevenLabsProvider(elevenlabs_key)
    if which == "elevenlabs":
        if not os.getenv("ELEVENLABS_API_KEY"):
            raise ProviderNotConfigured("ElevenLabs API key missing. Enter it in the app (Settings) or set ELEVENLABS_API_KEY on the server.")
        return ElevenLabsProvider(os.environ["ELEVENLABS_API_KEY"])
    if which == "selfhosted":
        if not os.getenv("SELF_HOSTED_VC_URL"):
            raise ProviderNotConfigured("VOICE_PROVIDER=selfhosted but SELF_HOSTED_VC_URL is not set.")
        return SelfHostedProvider(os.environ["SELF_HOSTED_VC_URL"], os.getenv("SELF_HOSTED_VC_TOKEN", ""))
    if which == "test" and os.getenv("ALLOW_TEST_PROVIDER") == "1":
        return TestProvider()
    raise ProviderNotConfigured("No voice provider configured. Set VOICE_PROVIDER=elevenlabs|selfhosted and its credentials (see .env.example).")
