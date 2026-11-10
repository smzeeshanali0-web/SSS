import asyncio
import numpy as np
import pytest
from app import config, g711, providers
from app.service import ServiceError, VoiceService
from app.session import CallSession
from app.store import Store
from .fake_sip import FakeSipServer


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VOICE_PROVIDER", "test")
    monkeypatch.setenv("ALLOW_TEST_PROVIDER", "1")
    store = Store(tmp_path / "t.db")
    return store, VoiceService(store)


def mk(svc, relation="self", owner="Sonya"):
    return svc.create_profile(name="Mine", owner_name=owner, owner_relation=relation, filename="me.mp3", data=b"ID3fakeaudio")


def test_cannot_clone_without_consent(env):
    store, svc = env
    p = mk(svc)
    assert p["status"] == "awaiting_consent" and "consent_text" in p
    with pytest.raises(ServiceError) as e:
        asyncio.run(svc.clone(p["id"]))
    assert e.value.status == 403


def test_decline_deletes_everything(env):
    store, svc = env
    p = mk(svc)
    r = svc.respond_consent(p["id"], accept=False, disclosure_ack=False)
    assert r["status"] == "declined" and store.profile(p["id"]) is None
    assert not list((config.data_dir() / "samples").iterdir())


def test_accept_requires_name_disclosure_and_permission_note(env):
    store, svc = env
    p = mk(svc)
    with pytest.raises(ServiceError):
        svc.respond_consent(p["id"], accept=True, disclosure_ack=False, typed_name="Sonya")
    with pytest.raises(ServiceError):
        svc.respond_consent(p["id"], accept=True, disclosure_ack=True, typed_name="Wrong")
    q = mk(svc, relation="other_with_permission", owner="Ali Singer")
    with pytest.raises(ServiceError):
        svc.respond_consent(q["id"], accept=True, disclosure_ack=True, typed_name="Ali Singer", permission_note="ok")
    svc.respond_consent(q["id"], accept=True, disclosure_ack=True, typed_name="ali singer", permission_note="Signed WhatsApp voice note, 10 Nov 2026")
    assert store.profile(q["id"])["status"] == "consented"


def test_clone_then_revoke_blocks_calls(env):
    store, svc = env
    p = mk(svc)
    svc.respond_consent(p["id"], accept=True, disclosure_ack=True, typed_name="sonya")
    out = asyncio.run(svc.clone(p["id"]))
    assert out["status"] == "ready" and "provider_voice_id" not in out
    assert svc.usable_voice(p["id"])[1].startswith("test-")
    store.revoke_consents(p["id"])
    with pytest.raises(ServiceError):
        svc.usable_voice(p["id"])
    assert [a["event"] for a in store.audit_log(p["id"])][:3] == ["profile_created", "consent_accepted", "clone_created"]


def test_no_provider_is_501(env, monkeypatch):
    store, svc = env
    monkeypatch.delenv("VOICE_PROVIDER")
    p = mk(svc)
    svc.respond_consent(p["id"], accept=True, disclosure_ack=True, typed_name="sonya")
    with pytest.raises(ServiceError) as e:
        asyncio.run(svc.clone(p["id"]))
    assert e.value.status == 501


def test_bad_upload_rejected(env):
    store, svc = env
    with pytest.raises(ServiceError) as e:
        svc.create_profile(name="x", owner_name="y", owner_relation="self", filename="virus.exe", data=b"x")
    assert e.value.status == 415


async def _run_call(store, svc, profile_id, ack=True, sip_kw=None):
    srv = FakeSipServer(**(sip_kw or {}))
    await srv.start()
    sent_json, sent_bytes = [], []

    async def sj(m): sent_json.append(m)
    async def sb(b): sent_bytes.append(b)
    s = CallSession(store, svc, sj, sb)
    await s.start({"destination": "+923001112222", "profile_id": profile_id, "disclosure_ack": ack, "chunk_ms": 200,
                   "account": {"username": "1001", "password": "secret", "server": "127.0.0.1", "port": srv.port}})
    return srv, s, sent_json, sent_bytes


def ready_profile(svc):
    p = mk(svc)
    svc.respond_consent(p["id"], accept=True, disclosure_ack=True, typed_name="sonya")
    asyncio.run(svc.clone(p["id"]))
    return p["id"]


def test_ai_call_sends_converted_audio_only(env):
    store, svc = env
    pid = ready_profile(svc)

    async def run():
        srv, s, js, bs = await _run_call(store, svc, pid)
        assert any(m.get("state") == "connected" and m.get("detail") == "ai_voice" for m in js)
        tone = (np.random.default_rng(1).normal(0, 4000, 3200)).astype(np.int16)  # 200 ms @16k
        for i in range(0, 3200, 320):
            await s.on_bytes(g711.pcm_to_bytes(tone[i:i + 320]))
        await asyncio.sleep(0.6)
        stream = np.concatenate([g711.ulaw_decode(p) for p in srv.rtp_rx]).astype(float)
        ref = g711.resample(tone, 16000, 8000).astype(float)
        corr = np.correlate(stream, ref, "valid")
        norm = np.linalg.norm(ref) ** 2
        assert corr.min() < -0.8 * norm, "converted (phase-inverted) audio did not reach SIP"
        assert corr.max() < 0.2 * norm, "raw microphone audio leaked to SIP"
        await s.on_json({"type": "dtmf", "digit": "1"})
        await s.on_json({"type": "hangup"})
        await asyncio.wait_for(srv.bye_seen.wait(), 2)
        await s.close(); srv.close()
        assert store.calls()[0]["status"] == "completed" and store.calls()[0]["ai_voice"] == 1
    asyncio.run(run())


def test_own_voice_call_passthrough(env):
    store, svc = env

    async def run():
        srv, s, js, bs = await _run_call(store, svc, None)
        tone = (np.random.default_rng(1).normal(0, 4000, 1600)).astype(np.int16)
        for i in range(0, 1600, 320):
            await s.on_bytes(g711.pcm_to_bytes(tone[i:i + 320]))
        await asyncio.sleep(0.4)
        stream = np.concatenate([g711.ulaw_decode(p) for p in srv.rtp_rx]).astype(float)
        ref = g711.resample(tone, 16000, 8000).astype(float)
        corr = np.correlate(stream, ref, "valid")
        assert corr.max() > 0.8 * np.linalg.norm(ref) ** 2  # own-voice mode: raw audio passes through
        await s.close(); srv.close()
    asyncio.run(run())


def test_ai_call_refused_without_ack_or_consent(env):
    store, svc = env
    pid = ready_profile(svc)

    async def run():
        srv, s, js, _ = await _run_call(store, svc, pid, ack=False)
        assert any(m["type"] == "error" for m in js) and s.call is None and "REGISTER" not in srv.log
        store.revoke_consents(pid)
        srv2, s2, js2, _ = await _run_call(store, svc, pid, ack=True)
        assert any(m["type"] == "error" and "consent" in m["message"].lower() for m in js2) and s2.call is None
        srv.close(); srv2.close()
    asyncio.run(run())


def test_provider_failure_is_silence_not_raw_mic(env, monkeypatch):
    store, svc = env
    pid = ready_profile(svc)

    async def boom(self, voice_id, pcm):
        raise providers.ProviderError("down")
    monkeypatch.setattr(providers.TestProvider, "convert", boom)

    async def run():
        srv, s, js, _ = await _run_call(store, svc, pid)
        tone = (np.random.default_rng(1).normal(0, 4000, 3200)).astype(np.int16)
        for i in range(0, 3200, 320):
            await s.on_bytes(g711.pcm_to_bytes(tone[i:i + 320]))
        await asyncio.sleep(0.6)
        assert all(np.abs(g711.ulaw_decode(p)).max() < 100 for p in srv.rtp_rx)  # only silence on the wire
        assert any(m.get("code") == "voice_conversion_failed" for m in js)
        await s.close(); srv.close()
    asyncio.run(run())


def test_preview_requires_ready_voice_and_converts(monkeypatch, tmp_path):
    import numpy as np
    monkeypatch.setenv("DATA_DIR", str(tmp_path)); monkeypatch.setenv("VOICE_PROVIDER", "test"); monkeypatch.setenv("ALLOW_TEST_PROVIDER", "1")
    from app.store import Store
    from app.service import VoiceService, ServiceError
    svc = VoiceService(Store(tmp_path / "a.db"))
    p = svc.create_profile(name="v", owner_name="Me", owner_relation="self", filename="a.wav", data=b"RIFFxxxx")
    pcm = (np.ones(16000 * 2, dtype="<i2") * 1000).tobytes()
    with pytest.raises(ServiceError):                      # not cloned / no consent yet
        asyncio.run(svc.preview(p["id"], pcm))
    svc.respond_consent(p["id"], accept=True, disclosure_ack=True, typed_name="Me")
    asyncio.run(svc.clone(p["id"]))
    out = asyncio.run(svc.preview(p["id"], pcm))
    assert len(out) == len(pcm)
    with pytest.raises(ServiceError):                      # too short
        asyncio.run(svc.preview(p["id"], b"\x00" * 100))
