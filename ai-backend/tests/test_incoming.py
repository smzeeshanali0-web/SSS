"""VICIdial/Asterisk agent-phone mode: server REGISTERs us, later INVITEs our extension; we auto-answer, audio flows, BYE ends it."""
import asyncio
import struct

import numpy as np

from app import g711, sip


class FakeAsterisk(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None
        self.ua_addr = None
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.rtp_rx: list[bytes] = []

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        msg = sip.parse(data)
        if msg is None:
            return
        if msg.is_request and msg.method == "REGISTER":
            self.ua_addr = addr
            hdrs = [("Via", v) for v in msg.hs("via")] + [("From", msg.h("from")), ("To", msg.h("to") + ";tag=ast"),
                                                          ("Call-ID", msg.call_id), ("CSeq", msg.h("cseq")),
                                                          ("Contact", msg.h("contact")), ("Expires", "300")]
            self.transport.sendto(sip.build("SIP/2.0 200 OK", hdrs), addr)
        else:
            self.inbox.put_nowait(msg)


class RtpPeer(asyncio.DatagramProtocol):
    def __init__(self):
        self.rx = []

    def connection_made(self, t):
        self.t = t

    def datagram_received(self, data, addr):
        self.rx.append(data[12:])


def test_incoming_call_is_auto_answered_and_audio_flows():
    async def run():
        loop = asyncio.get_running_loop()
        ast = FakeAsterisk()
        ast_t, _ = await loop.create_datagram_endpoint(lambda: ast, local_addr=("127.0.0.1", 0))
        ast_port = ast_t.get_extra_info("sockname")[1]
        rtp = RtpPeer()
        rtp_t, _ = await loop.create_datagram_endpoint(lambda: rtp, local_addr=("127.0.0.1", 0))
        rtp_port = rtp_t.get_extra_info("sockname")[1]

        ua = sip.SipUA(sip.SipAccount(username="8001", password="x", server="127.0.0.1", port=ast_port))
        got = []
        await ua.start()
        await ua.register()
        ua.on_incoming = lambda call: got.append(call)

        # Asterisk dials the agent phone
        sdp = sip.build_sdp("127.0.0.1", rtp_port)
        hdrs = [("Via", "SIP/2.0/UDP 127.0.0.1:%d;branch=z9hG4bKabc" % ast_port), ("From", "<sip:vicidial@127.0.0.1>;tag=astA"),
                ("To", "<sip:8001@127.0.0.1>"), ("Call-ID", "conf-1@ast"), ("CSeq", "1 INVITE"),
                ("Contact", "<sip:asterisk@127.0.0.1:%d>" % ast_port), ("Content-Type", "application/sdp")]
        ast_t.sendto(sip.build("INVITE sip:8001@127.0.0.1 SIP/2.0", hdrs, sdp), ast.ua_addr)

        codes = []
        while 200 not in codes:
            m = await asyncio.wait_for(ast.inbox.get(), 3)
            if not m.is_request:
                codes.append(m.code)
                if m.code == 200:
                    ok = m
        assert 180 in codes
        assert ok.h("to").endswith(tuple("0123456789abcdef")) and "tag=" in ok.h("to")
        ip, port, pt, _ = sip.parse_sdp(ok.body)
        assert pt in (0, 8)

        # ACK, then the call must be connected
        ack_h = [("Via", "SIP/2.0/UDP 127.0.0.1:%d;branch=z9hG4bKack" % ast_port), ("From", "<sip:vicidial@127.0.0.1>;tag=astA"),
                 ("To", ok.h("to")), ("Call-ID", "conf-1@ast"), ("CSeq", "1 ACK")]
        ast_t.sendto(sip.build("ACK sip:8001@127.0.0.1 SIP/2.0", ack_h), ast.ua_addr)
        await asyncio.sleep(0.1)
        call = got[0]
        assert call.state == "connected" and call.incoming and call.caller == "vicidial"

        # customer audio (Asterisk -> us) arrives, our audio goes out
        heard = []
        call.on_audio = heard.append
        tone = (6000 * np.sin(2 * np.pi * 400 * np.arange(160) / 8000)).astype("<i2")
        pkt = struct.pack("!BBHII", 0x80, 0, 1, 160, 7) + g711.ulaw_encode(tone)
        rtp_t.sendto(pkt, (ip, port))
        call.send_audio(tone)
        await asyncio.sleep(0.2)
        assert heard and rtp.rx

        # Asterisk hangs up
        bye_h = [("Via", "SIP/2.0/UDP 127.0.0.1:%d;branch=z9hG4bKbye" % ast_port), ("From", "<sip:vicidial@127.0.0.1>;tag=astA"),
                 ("To", ok.h("to")), ("Call-ID", "conf-1@ast"), ("CSeq", "2 BYE")]
        ast_t.sendto(sip.build("BYE sip:8001@127.0.0.1 SIP/2.0", bye_h), ast.ua_addr)
        await asyncio.wait_for(call.ended.wait(), 2)
        assert call.end_reason == "remote hangup"
        await ua.stop()

    asyncio.run(run())


def test_incoming_rejected_when_not_in_agent_mode():
    async def run():
        loop = asyncio.get_running_loop()
        ast = FakeAsterisk()
        ast_t, _ = await loop.create_datagram_endpoint(lambda: ast, local_addr=("127.0.0.1", 0))
        ua = sip.SipUA(sip.SipAccount(username="8001", password="x", server="127.0.0.1", port=ast_t.get_extra_info("sockname")[1]))
        await ua.start(); await ua.register()
        hdrs = [("Via", "SIP/2.0/UDP 127.0.0.1:1;branch=z9hG4bKq"), ("From", "<sip:a@x>;tag=1"), ("To", "<sip:8001@x>"),
                ("Call-ID", "c2"), ("CSeq", "1 INVITE"), ("Content-Type", "application/sdp")]
        ast_t.sendto(sip.build("INVITE sip:8001@x SIP/2.0", hdrs, sip.build_sdp("127.0.0.1", 4000)), ast.ua_addr)
        m = await asyncio.wait_for(ast.inbox.get(), 2)
        assert m.code == 486
        await ua.stop()
    asyncio.run(run())


if __name__ == "__main__":
    test_incoming_call_is_auto_answered_and_audio_flows(); test_incoming_rejected_when_not_in_agent_mode(); print("incoming tests OK")
