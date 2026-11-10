import asyncio
import numpy as np
import pytest
from app import g711, sip
from .fake_sip import FakeSipServer


def test_ulaw_known_values_and_roundtrip():
    assert g711.ulaw_encode(np.array([0], dtype=np.int16)) == b"\xff"
    assert g711.alaw_encode(np.array([0], dtype=np.int16)) == b"\xd5"
    t = np.arange(8000)
    x = (12000 * np.sin(2 * np.pi * 440 * t / 8000)).astype(np.int16)
    for enc, dec in ((g711.ulaw_encode, g711.ulaw_decode), (g711.alaw_encode, g711.alaw_decode)):
        y = dec(enc(x)).astype(np.float64)
        snr = 10 * np.log10((x.astype(np.float64) ** 2).sum() / ((y - x) ** 2).sum())
        assert snr > 30  # G.711 gives ~38 dB on a loud tone


def test_resample_lengths():
    x = np.ones(320, dtype=np.int16) * 1000
    assert g711.resample(x, 16000, 8000).size == 160
    assert g711.resample(x[:160], 8000, 16000).size == 320


def test_digest_matches_rfc2617_example():
    ch = {"realm": "testrealm@host.com", "nonce": "dcd98b7102dd2f0e8b11d0f600bfb0c093", "qop": "auth", "opaque": "5ccc069c403ebaf9f0171e9517f40e41"}
    r = sip.digest_response("Mufasa", "Circle Of Life", "GET", "/dir/index.html", ch, 1, "0a4f113b")
    assert 'response="6629fae49393a05397450978507c4ef1"' in r


async def _setup(**kw):
    srv = FakeSipServer(**kw)
    await srv.start()
    return srv


def test_register_call_audio_dtmf_hangup():
    async def run():
        srv = await _setup()
        ua = sip.SipUA(sip.SipAccount("1001", "secret", "127.0.0.1", srv.port))
        await ua.start()
        await ua.register()
        assert ua.registered and srv.log.count("REGISTER") == 2  # 401 then 200
        states = []
        call = await ua.call("+923001234567", on_state=lambda s, d: states.append(s))
        assert "ringing" in states and call.state == "connected"
        got = []
        call.on_audio = lambda pcm: got.append(pcm)
        tone = (8000 * np.sin(np.arange(160) * 0.3)).astype(np.int16)
        for _ in range(5):
            call.send_audio(tone)
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)
        assert len(srv.rtp_rx) == 5 and len(got) == 5  # echoed back & decoded
        await call.dtmf("5")
        await asyncio.sleep(0.1)
        assert set(srv.dtmf_rx) == {5}
        await call.hangup()
        await asyncio.wait_for(srv.bye_seen.wait(), 2)
        assert call.state == "ended"
        await ua.stop(); srv.close()
    asyncio.run(run())


def test_wrong_password_fails_register():
    async def run():
        srv = await _setup()
        ua = sip.SipUA(sip.SipAccount("1001", "WRONG", "127.0.0.1", srv.port))
        await ua.start()
        with pytest.raises(sip.SipError) as e:
            await ua.register()
        assert e.value.code == 401
        await ua.stop(); srv.close()
    asyncio.run(run())


def test_remote_bye_ends_call():
    async def run():
        srv = await _setup()
        ua = sip.SipUA(sip.SipAccount("1001", "secret", "127.0.0.1", srv.port))
        await ua.start(); await ua.register()
        call = await ua.call("100")
        srv.send_bye_to_client()
        await asyncio.wait_for(call.ended.wait(), 2)
        assert call.end_reason == "remote hangup"
        await ua.stop(); srv.close()
    asyncio.run(run())


def test_udp_only_error():
    with pytest.raises(sip.SipError):
        sip.SipUA(sip.SipAccount("a", "b", "c", transport="TLS"))
