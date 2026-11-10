"""Tiny fake SIP registrar/UAS used only by tests: digest auth, answers INVITE, echoes RTP, handles BYE/CANCEL."""
import asyncio, hashlib, re, struct
from app import sip, g711


class FakeSipServer(asyncio.DatagramProtocol):
    REALM, NONCE = "test.realm", "abc123nonce"

    def __init__(self, user="1001", password="secret", ring=True):
        self.user, self.password, self.ring = user, password, ring
        self.transport = None
        self.log: list[str] = []
        self.rtp_transport = None
        self.rtp_peer = None
        self.rtp_rx: list[bytes] = []
        self.dtmf_rx: list[int] = []
        self.rtp_port = 0
        self.bye_seen = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.hangup_after_answer = False

    async def start(self):
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=("127.0.0.1", 0))
        self.port = self.transport.get_extra_info("sockname")[1]
        outer = self

        class Rtp(asyncio.DatagramProtocol):
            def datagram_received(s, data, addr):
                outer.rtp_peer = addr
                pt = data[1] & 0x7F
                if pt == 101:
                    outer.dtmf_rx.append(data[12])
                else:
                    outer.rtp_rx.append(data[12:])
                    outer.rtp_transport.sendto(data, addr)  # echo
        self.rtp_transport, _ = await loop.create_datagram_endpoint(Rtp, local_addr=("127.0.0.1", 0))
        self.rtp_port = self.rtp_transport.get_extra_info("sockname")[1]

    def _resp(self, req, code, reason, extra=(), body=b"", to_tag=True):
        to = req.h("to") + (";tag=srv1" if to_tag and "tag=" not in req.h("to") else "")
        hdrs = [("Via", v) for v in req.hs("via")] + [("From", req.h("from")), ("To", to), ("Call-ID", req.call_id), ("CSeq", req.h("cseq"))] + list(extra)
        self.transport.sendto(sip.build(f"SIP/2.0 {code} {reason}", hdrs, body), self._peer)

    def _authed(self, req, method):
        a = req.h("authorization")
        if not a:
            return False
        ch = sip.parse_challenge(a)
        ch_srv = {"realm": self.REALM, "nonce": self.NONCE, "qop": "auth", "algorithm": "MD5"}
        want = sip.digest_response(self.user, self.password, method, ch["uri"], ch_srv, int(ch["nc"], 16), ch["cnonce"])
        return sip.parse_challenge(want)["response"] == ch["response"]

    def datagram_received(self, data, addr):
        self._peer = addr
        req = sip.parse(data)
        if not req or not req.is_request:
            return
        self.log.append(req.method)
        if req.method in ("REGISTER", "INVITE"):
            if not self._authed(req, req.method):
                if req.method == "INVITE":
                    pass
                self._resp(req, 401, "Unauthorized", [("WWW-Authenticate", f'Digest realm="{self.REALM}", nonce="{self.NONCE}", algorithm=MD5, qop="auth"')], to_tag=False)
                return
        if req.method == "REGISTER":
            self._resp(req, 200, "OK", [("Contact", req.h("contact")), ("Expires", req.h("expires"))], to_tag=False)
        elif req.method == "INVITE":
            self._resp(req, 100, "Trying", to_tag=False)
            if self.ring:
                self._resp(req, 180, "Ringing")
            sdp = sip.build_sdp("127.0.0.1", self.rtp_port)
            self._resp(req, 200, "OK", [("Contact", "<sip:svc@127.0.0.1:%d>" % self.port), ("Content-Type", "application/sdp")], sdp)
            self._last_invite = req
        elif req.method == "BYE":
            self._resp(req, 200, "OK", to_tag=False)
            self.bye_seen.set()
        elif req.method == "CANCEL":
            self._resp(req, 200, "OK", to_tag=False)
            self.cancel_seen.set()
        elif req.method == "ACK":
            pass

    def send_bye_to_client(self):
        r = self._last_invite
        hdrs = [("Via", "SIP/2.0/UDP 127.0.0.1:%d;branch=z9hG4bKsrv" % self.port), ("From", r.h("to")), ("To", r.h("from")), ("Call-ID", r.call_id), ("CSeq", "1 BYE")]
        self.transport.sendto(sip.build("BYE sip:x@x SIP/2.0", hdrs), self._peer)

    def send_rtp(self, pcm8k, seq=1):
        hdr = struct.pack("!BBHII", 0x80, 0, seq, seq * 160, 99)
        self.rtp_transport.sendto(hdr + g711.ulaw_encode(pcm8k), self.rtp_peer)

    def close(self):
        self.transport.close(); self.rtp_transport.close()
