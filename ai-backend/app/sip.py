"""Minimal asyncio SIP user agent (UDP) with RTP (G.711) -- no native dependencies.

Supports: REGISTER (+digest auth, re-register), outgoing INVITE (+digest auth), ACK, BYE,
RFC 2833 DTMF, remote BYE, OPTIONS keep-alive replies, symmetric RTP.
Incoming calls: auto-answered when `ua.on_incoming` is set (VICIdial / Asterisk agent-phone mode).
Not supported (raise a clear error): TCP/TLS transport, SRTP, STUN/ICE, hold/transfer.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import random
import re
import secrets
import socket
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import quote

import numpy as np

from . import g711

CRLF = "\r\n"
_COMPACT = {"i": "call-id", "f": "from", "t": "to", "v": "via", "m": "contact", "l": "content-length", "c": "content-type"}


class SipError(Exception):
    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


@dataclass
class SipAccount:
    username: str
    password: str
    server: str
    port: int = 5060
    domain: str = ""
    auth_id: str = ""
    display_name: str = ""
    caller_id: str = ""
    proxy: str = ""
    transport: str = "UDP"

    @property
    def realm_host(self) -> str:
        return self.domain or self.server


# --------------------------------------------------------------------------- parsing
class Msg:
    def __init__(self, start: str, headers: list[tuple[str, str]], body: bytes):
        self.start, self.headers, self.body = start, headers, body
        self.is_request = not start.startswith("SIP/2.0")
        parts = start.split(" ", 2)
        if self.is_request:
            self.method, self.uri, self.code, self.reason = parts[0], parts[1] if len(parts) > 1 else "", 0, ""
        else:
            self.method, self.uri = "", ""
            self.code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            self.reason = parts[2] if len(parts) > 2 else ""

    def h(self, name: str, default: str = "") -> str:
        name = name.lower()
        for k, v in self.headers:
            if k == name:
                return v
        return default

    def hs(self, name: str) -> list[str]:
        name = name.lower()
        return [v for k, v in self.headers if k == name]

    @property
    def call_id(self) -> str:
        return self.h("call-id")

    @property
    def cseq(self) -> tuple[int, str]:
        parts = self.h("cseq").split()
        return (int(parts[0]), parts[1].upper()) if len(parts) == 2 and parts[0].isdigit() else (0, "")


def parse(data: bytes) -> Optional[Msg]:
    try:
        head, _, body = data.partition(b"\r\n\r\n")
        lines = head.decode("utf-8", "replace").replace("\r\n", "\n").split("\n")
        start, headers = lines[0].strip(), []
        for line in lines[1:]:
            if ":" not in line or line[0] in " \t":
                continue
            k, v = line.split(":", 1)
            k = k.strip().lower()
            headers.append((_COMPACT.get(k, k), v.strip()))
        msg = Msg(start, headers, body)
        cl = msg.h("content-length")
        if cl.isdigit():
            msg.body = body[: int(cl)]
        return msg
    except Exception:
        return None


def build(start: str, headers: list[tuple[str, str]], body: bytes = b"") -> bytes:
    hdrs = list(headers) + [("Content-Length", str(len(body)))]
    return (start + CRLF + CRLF.join(f"{k}: {v}" for k, v in hdrs) + CRLF + CRLF).encode() + body


def _param(value: str, key: str) -> str:
    m = re.search(rf'[;\s,]{key}\s*=\s*"?([^";,>\s]+)"?', ";" + value, re.I)
    return m.group(1) if m else ""


def parse_challenge(value: str) -> dict:
    value = re.sub(r"^\s*Digest\s+", "", value, flags=re.I)
    out = {}
    for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', value):
        out[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def digest_response(user: str, password: str, method: str, uri: str, ch: dict, nc: int, cnonce: str) -> str:
    algo = (ch.get("algorithm") or "MD5").upper().replace("-", "")
    hfn = {"MD5": hashlib.md5, "SHA256": hashlib.sha256}.get(algo)
    if not hfn:
        raise SipError(f"Unsupported digest algorithm {algo}")
    H = lambda s: hfn(s.encode()).hexdigest()
    ha1 = H(f"{user}:{ch.get('realm', '')}:{password}")
    ha2 = H(f"{method}:{uri}")
    qop = ""
    if ch.get("qop"):
        qop = "auth" if "auth" in [q.strip() for q in ch["qop"].split(",")] else ""
    if qop:
        resp = H(f"{ha1}:{ch['nonce']}:{nc:08x}:{cnonce}:{qop}:{ha2}")
    else:
        resp = H(f"{ha1}:{ch['nonce']}:{ha2}")
    parts = [f'username="{user}"', f'realm="{ch.get("realm", "")}"', f'nonce="{ch["nonce"]}"', f'uri="{uri}"',
             f'response="{resp}"', f"algorithm={ch.get('algorithm') or 'MD5'}"]
    if qop:
        parts += [f"qop={qop}", f"nc={nc:08x}", f'cnonce="{cnonce}"']
    if ch.get("opaque"):
        parts.append(f'opaque="{ch["opaque"]}"')
    return "Digest " + ", ".join(parts)


# --------------------------------------------------------------------------- RTP
class RtpStream(asyncio.DatagramProtocol):
    FRAME = 160  # 20 ms @ 8 kHz

    def __init__(self, on_pcm: Callable[[np.ndarray], None]):
        self.on_pcm = on_pcm
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.remote: Optional[tuple[str, int]] = None
        self.pt, self.dtmf_pt = 0, 101
        self.ssrc = random.getrandbits(32)
        self.seq = random.getrandbits(16)
        self.ts = random.getrandbits(32)
        self._dtmf_active = False
        self._latched = False
        self.rx_packets = 0
        self.tx_packets = 0

    async def open(self) -> int:
        lo, hi = (int(x) for x in os.environ.get("RTP_PORT_RANGE", "10000-20000").split("-"))
        loop = asyncio.get_running_loop()
        for _ in range(200):
            port = random.randrange(lo, hi, 2)
            try:
                self.transport, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=("0.0.0.0", port), family=socket.AF_INET)
                return port
            except OSError:
                continue
        raise SipError("No free RTP port")

    def set_remote(self, ip: str, port: int, pt: int, dtmf_pt: int):
        self.remote, self.pt, self.dtmf_pt = (ip, port), pt, dtmf_pt

    def _send(self, payload: bytes, pt: int, marker: bool = False, ts: Optional[int] = None):
        if not self.transport or not self.remote:
            return
        hdr = struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | pt, self.seq, (self.ts if ts is None else ts) & 0xFFFFFFFF, self.ssrc)
        self.seq = (self.seq + 1) & 0xFFFF
        self.transport.sendto(hdr + payload, self.remote)
        self.tx_packets += 1

    def send_pcm(self, pcm8k: np.ndarray):
        """Send exactly one 20 ms frame (160 samples @ 8 kHz)."""
        if self._dtmf_active:
            return
        enc = g711.alaw_encode(pcm8k) if self.pt == 8 else g711.ulaw_encode(pcm8k)
        self._send(enc, self.pt, marker=self.tx_packets == 0)
        self.ts = (self.ts + self.FRAME) & 0xFFFFFFFF

    async def send_dtmf(self, digit: str, duration_ms: int = 160):
        events = "0123456789*#ABCD"
        if digit.upper() not in events:
            raise SipError(f"Invalid DTMF digit {digit!r}")
        ev = events.index(digit.upper())
        self._dtmf_active = True
        try:
            start_ts, n = self.ts, max(3, duration_ms // 20)
            for i in range(n):
                self._send(struct.pack("!BBH", ev, 10, (i + 1) * self.FRAME), self.dtmf_pt, marker=i == 0, ts=start_ts)
                await asyncio.sleep(0.02)
            for _ in range(3):
                self._send(struct.pack("!BBH", ev, 0x80 | 10, (n + 1) * self.FRAME), self.dtmf_pt, ts=start_ts)
            self.ts = (start_ts + (n + 1) * self.FRAME) & 0xFFFFFFFF
        finally:
            self._dtmf_active = False

    def datagram_received(self, data: bytes, addr):
        if len(data) < 12 or data[0] >> 6 != 2:
            return
        cc = data[0] & 0x0F
        off = 12 + 4 * cc
        if data[0] & 0x10 and len(data) >= off + 4:  # header extension
            off += 4 + 4 * struct.unpack("!H", data[off + 2: off + 4])[0]
        pt = data[1] & 0x7F
        payload = data[off:]
        if not self._latched:  # symmetric RTP / NAT latching
            self.remote, self._latched = (addr[0], addr[1]), True
        if pt == self.dtmf_pt or pt == 13 or not payload:
            return
        self.rx_packets += 1
        if pt == 0:
            self.on_pcm(g711.ulaw_decode(payload))
        elif pt == 8:
            self.on_pcm(g711.alaw_decode(payload))

    def close(self):
        if self.transport:
            self.transport.close()
            self.transport = None


# --------------------------------------------------------------------------- SDP
def build_sdp(ip: str, port: int) -> bytes:
    sid = int(time.time())
    lines = ["v=0", f"o=aiclone {sid} {sid} IN IP4 {ip}", "s=AIClone", f"c=IN IP4 {ip}", "t=0 0",
             f"m=audio {port} RTP/AVP 0 8 101", "a=rtpmap:0 PCMU/8000", "a=rtpmap:8 PCMA/8000",
             "a=rtpmap:101 telephone-event/8000", "a=fmtp:101 0-16", "a=ptime:20", "a=sendrecv"]
    return (CRLF.join(lines) + CRLF).encode()


def parse_sdp(body: bytes) -> tuple[str, int, int, int]:
    text = body.decode("utf-8", "replace")
    ip = re.search(r"^c=IN IP4 (\S+)", text, re.M)
    m = re.search(r"^m=audio (\d+) \S+ ([\d ]+)", text, re.M)
    if not ip or not m:
        raise SipError("Remote SDP has no usable audio stream")
    pts = [int(p) for p in m.group(2).split()]
    pt = next((p for p in pts if p in (0, 8)), None)
    if pt is None:
        raise SipError("Remote does not offer PCMU/PCMA (G.711)")
    dt = re.search(r"^a=rtpmap:(\d+) telephone-event/8000", text, re.M)
    return ip.group(1), int(m.group(1)), pt, int(dt.group(1)) if dt else 101


# --------------------------------------------------------------------------- UA
class SipUA(asyncio.DatagramProtocol):
    def __init__(self, account: SipAccount):
        if account.transport.upper() != "UDP":
            raise SipError(f"Transport {account.transport} is not supported by the gateway yet (UDP only).")
        self.account = account
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.remote: tuple[str, int] = ("", 0)
        self.local_ip, self.local_port = "", 0
        self.registered = False
        self.calls: dict[str, "SipCall"] = {}
        self.on_incoming: Optional[Callable[["SipCall"], None]] = None  # set => incoming INVITEs are auto-answered
        self._pending: dict[tuple[str, int], asyncio.Queue] = {}
        self._reg_call_id = f"{uuid.uuid4().hex}@aiclone"
        self._reg_tag = secrets.token_hex(4)
        self._reg_cseq = 0
        self._reg_task: Optional[asyncio.Task] = None
        self._auth: dict[str, dict] = {}  # method -> (challenge, header name) cache
        self._nc = 0

    # --- lifecycle
    async def start(self):
        loop = asyncio.get_running_loop()
        host = self.account.proxy or self.account.server
        host, _, port = host.partition(":") if ":" in host else (host, "", "")
        infos = await loop.getaddrinfo(host, int(port or self.account.port), family=socket.AF_INET, type=socket.SOCK_DGRAM)
        if not infos:
            raise SipError(f"Cannot resolve SIP server {host}")
        self.remote = infos[0][4][:2]
        lo, hi = (int(x) for x in os.environ.get("RTP_PORT_RANGE", "10000-20000").split("-"))
        for _ in range(50):  # odd port inside the range that is already open for RTP (RTP uses even ports)
            try:
                self.transport, _ = await loop.create_datagram_endpoint(
                    lambda: self, local_addr=("0.0.0.0", random.randrange(lo + 1, hi, 2)), family=socket.AF_INET)
                break
            except OSError:
                continue
        else:
            self.transport, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=("0.0.0.0", 0), family=socket.AF_INET)
        self.local_port = self.transport.get_extra_info("sockname")[1]
        self.local_ip = os.environ.get("SIP_PUBLIC_IP") or self._local_ip_for(self.remote[0])

    @staticmethod
    def _local_ip_for(remote_ip: str) -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((remote_ip, 9))
            return s.getsockname()[0]
        finally:
            s.close()

    async def stop(self):
        if self._reg_task:
            self._reg_task.cancel()
        for call in list(self.calls.values()):
            await call.hangup()
        if self.registered:
            try:
                await self.register(expires=0)
            except Exception:
                pass
        if self.transport:
            self.transport.close()

    # --- helpers
    @property
    def _aor(self) -> str:
        return f"sip:{self.account.username}@{self.account.realm_host}"

    def _from(self, tag: str) -> str:
        name = self.account.display_name or self.account.caller_id
        disp = f'"{name}" ' if name else ""
        return f"{disp}<{self._aor}>;tag={tag}"

    def _via(self, branch: str) -> str:
        return f"SIP/2.0/UDP {self.local_ip}:{self.local_port};branch={branch};rport"

    def _contact(self) -> str:
        return f"<sip:{self.account.username}@{self.local_ip}:{self.local_port}>"

    @staticmethod
    def _branch() -> str:
        return "z9hG4bK" + secrets.token_hex(6)

    def _authorization(self, challenge_hdr: str, method: str, uri: str) -> str:
        ch = parse_challenge(challenge_hdr)
        if "nonce" not in ch:
            raise SipError("Malformed digest challenge")
        self._nc += 1
        user = self.account.auth_id or self.account.username
        return digest_response(user, self.account.password, method, uri, ch, self._nc, secrets.token_hex(8))

    def _send(self, data: bytes):
        if self.transport:
            self.transport.sendto(data, self.remote)

    async def _transact(self, data: bytes, key: tuple[str, int], *, timeout: float = 32.0, ring_timeout: float = 90.0, on_provisional=None) -> Msg:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        self._pending[key] = q
        try:
            self._send(data)
            interval, deadline, provisional = 0.5, loop.time() + timeout, False
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise SipError("SIP timeout (no response from server)", 408)
                try:
                    resp = await asyncio.wait_for(q.get(), remaining if provisional else min(interval, remaining))
                except asyncio.TimeoutError:
                    if not provisional:
                        interval = min(interval * 2, 4.0)
                        self._send(data)
                    continue
                if 100 <= resp.code < 200:
                    if not provisional:
                        provisional = True
                        deadline = max(deadline, loop.time() + ring_timeout)
                    if on_provisional:
                        on_provisional(resp)
                    continue
                return resp
        finally:
            self._pending.pop(key, None)

    # --- inbound datagrams
    def datagram_received(self, data: bytes, addr):
        msg = parse(data)
        if not msg:
            return
        if msg.is_request:
            self._on_request(msg)
            return
        q = self._pending.get((msg.call_id, msg.cseq[0]))
        if q:
            q.put_nowait(msg)
        elif msg.code == 200 and msg.cseq[1] == "INVITE" and msg.call_id in self.calls:
            self.calls[msg.call_id]._send_ack()  # retransmitted 200 -> our ACK was lost

    def _reply(self, req: Msg, code: int, reason: str):
        hdrs = [("Via", v) for v in req.hs("via")] + [("From", req.h("from")), ("To", req.h("to")), ("Call-ID", req.call_id), ("CSeq", req.h("cseq"))]
        self._send(build(f"SIP/2.0 {code} {reason}", hdrs))

    def _on_request(self, req: Msg):
        if req.method == "BYE":
            self._reply(req, 200, "OK")
            call = self.calls.get(req.call_id)
            if call:
                call._ended("remote hangup")
        elif req.method in ("OPTIONS", "NOTIFY", "INFO", "UPDATE", "MESSAGE"):
            self._reply(req, 200, "OK")
        elif req.method == "ACK":
            call = self.calls.get(req.call_id)
            if call:
                call._acked.set()
        elif req.method == "CANCEL":
            self._reply(req, 200, "OK")
            call = self.calls.get(req.call_id)
            if call and call.incoming:
                call._ended("cancelled")
        elif req.method == "INVITE":
            existing = self.calls.get(req.call_id)
            if existing is not None and existing.incoming:
                existing._respond(req, 200, "OK", existing._sdp)  # retransmit / re-INVITE: confirm same media
                return
            busy = any(c.state != "ended" for c in self.calls.values())
            if self.on_incoming is None or busy:
                self._reply(req, 486, "Busy Here")
                return
            try:
                call = SipCall.from_invite(self, req)
            except SipError:
                self._reply(req, 488, "Not Acceptable Here")
                return
            self.calls[call.call_id] = call
            asyncio.get_running_loop().create_task(call._answer())
            self.on_incoming(call)
        else:
            self._reply(req, 405, "Method Not Allowed")

    # --- registration
    async def register(self, expires: int = 300):
        uri = f"sip:{self.account.realm_host}"
        auth = None
        for attempt in range(2):
            self._reg_cseq += 1
            hdrs = [("Via", self._via(self._branch())), ("Max-Forwards", "70"), ("From", self._from(self._reg_tag)),
                    ("To", f"<{self._aor}>"), ("Call-ID", self._reg_call_id), ("CSeq", f"{self._reg_cseq} REGISTER"),
                    ("Contact", f"{self._contact()};expires={expires}"), ("Expires", str(expires)), ("User-Agent", "AICloneDialer/1.0")]
            if auth:
                hdrs.append(("Authorization" if auth[0] == "www-authenticate" else "Proxy-Authorization", auth[1]))
            resp = await self._transact(build(f"REGISTER {uri} SIP/2.0", hdrs), (self._reg_call_id, self._reg_cseq), timeout=15)
            if resp.code in (401, 407) and attempt == 0:
                name = "www-authenticate" if resp.code == 401 else "proxy-authenticate"
                auth = (name, self._authorization(resp.h(name), "REGISTER", uri))
                continue
            if resp.code == 200:
                self.registered = expires > 0
                if expires > 0:
                    granted = int(_param(resp.h("contact"), "expires") or resp.h("expires") or expires)
                    if self._reg_task:
                        self._reg_task.cancel()
                    self._reg_task = asyncio.create_task(self._reregister(max(30, int(granted * 0.8)), expires))
                return
            raise SipError(f"Registration failed: {resp.code} {resp.reason}", resp.code)

    async def _reregister(self, delay: int, expires: int):
        while True:
            await asyncio.sleep(delay)
            try:
                await self.register(expires)
                return  # register() installs a fresh task
            except Exception:
                self.registered = False

    # --- calls
    def dest_uri(self, dest: str) -> str:
        dest = dest.strip()
        if dest.lower().startswith("sip:"):
            return dest
        user, _, host = dest.partition("@")
        return f"sip:{quote(user, safe='+*')}@{host or self.account.realm_host}"

    async def call(self, dest: str, on_state: Optional[Callable[[str, str], None]] = None) -> "SipCall":
        call = SipCall(self, self.dest_uri(dest), on_state)
        self.calls[call.call_id] = call
        try:
            await call._invite()
        except BaseException:
            call._ended("failed")
            raise
        return call


class SipCall:
    def __init__(self, ua: SipUA, target: str, on_state=None):
        self.ua, self.target, self._on_state = ua, target, on_state
        self.call_id = f"{uuid.uuid4().hex}@aiclone"
        self.local_tag = secrets.token_hex(4)
        self.remote_tag = ""
        self.remote_target = target
        self.cseq = 0
        self.state = "calling"
        self.end_reason = ""
        self.on_audio: Callable[[np.ndarray], None] = lambda pcm: None
        self.rtp = RtpStream(lambda pcm: self.on_audio(pcm))
        self.ended = asyncio.Event()
        self._invite_branch = ""
        self._invite_cseq = 0
        self._ack_to_header = ""
        # incoming-call (UAS) state
        self.incoming = False
        self.caller = ""
        self._acked = asyncio.Event()
        self._req: Optional[Msg] = None
        self._from_hdr = ""
        self._to_base = ""
        self._remote_media: tuple = ()
        self._sdp = b""
        self._retx: Optional[asyncio.Task] = None

    @classmethod
    def from_invite(cls, ua: "SipUA", req: Msg) -> "SipCall":
        media = parse_sdp(req.body)  # raises SipError if no G.711
        from_hdr = req.h("from")
        uri = re.search(r"<([^>]+)>", from_hdr)
        call = cls(ua, uri.group(1) if uri else from_hdr.split(";")[0].strip())
        call.call_id = req.call_id
        call.incoming = True
        call.state = "ringing"
        call.remote_tag = _param(from_hdr, "tag")
        contact = re.search(r"<([^>]+)>", req.h("contact"))
        call.remote_target = contact.group(1) if contact else call.target
        call._req, call._remote_media, call._from_hdr = req, media, from_hdr
        call._to_base = re.sub(r";\s*tag=[^;>\s]+", "", req.h("to"))
        m = re.search(r"sip:([^@;>]+)", call.target)
        call.caller = m.group(1) if m else call.target
        return call

    def _respond(self, req: Msg, code: int, reason: str, body: bytes = b""):
        to = req.h("to")
        if "tag=" not in to:
            to += f";tag={self.local_tag}"
        hdrs = [("Via", v) for v in req.hs("via")] + [("From", req.h("from")), ("To", to), ("Call-ID", req.call_id), ("CSeq", req.h("cseq"))]
        if code >= 200 and body:
            hdrs += [("Contact", self.ua._contact()), ("Content-Type", "application/sdp"), ("Allow", "INVITE, ACK, CANCEL, BYE, OPTIONS")]
        self.ua._send(build(f"SIP/2.0 {code} {reason}", hdrs, body))

    async def _answer(self):
        """Auto-answer: 180 -> 200 (+SDP), retransmit 200 until ACK."""
        try:
            port = await self.rtp.open()
            self.rtp.set_remote(*self._remote_media)
            self._sdp = build_sdp(self.ua.local_ip, port)
            self._respond(self._req, 180, "Ringing")
            self._respond(self._req, 200, "OK", self._sdp)
        except Exception as e:
            self._ended(f"answer failed: {e}")
            return
        self._set("connected")
        self._retx = asyncio.create_task(self._retransmit_200())

    async def _retransmit_200(self):
        for delay in (0.5, 1, 2, 4, 4, 4, 4):
            try:
                await asyncio.wait_for(self._acked.wait(), delay)
                return
            except asyncio.TimeoutError:
                self._respond(self._req, 200, "OK", self._sdp)
        if not self._acked.is_set():
            self._ended("no ACK from server")

    def _set(self, state: str, detail: str = ""):
        self.state = state
        if self._on_state:
            self._on_state(state, detail)

    def _ended(self, reason: str):
        if self.state == "ended":
            return
        self.end_reason = reason
        if self._retx:
            self._retx.cancel()
        self.rtp.close()
        self.ua.calls.pop(self.call_id, None)
        self._set("ended", reason)
        self.ended.set()

    def _headers(self, method: str, cseq: int, branch: str, to_tag: bool) -> list[tuple[str, str]]:
        if self.incoming:  # we are the callee: From = original To (our tag), To = original From (their tag)
            return [("Via", self.ua._via(branch)), ("Max-Forwards", "70"), ("From", f"{self._to_base};tag={self.local_tag}"),
                    ("To", self._from_hdr), ("Call-ID", self.call_id), ("CSeq", f"{cseq} {method}"), ("User-Agent", "AICloneDialer/1.0")]
        to = f"<{self.target}>" + (f";tag={self.remote_tag}" if to_tag and self.remote_tag else "")
        h = [("Via", self.ua._via(branch)), ("Max-Forwards", "70"), ("From", self.ua._from(self.local_tag)), ("To", to),
             ("Call-ID", self.call_id), ("CSeq", f"{cseq} {method}"), ("User-Agent", "AICloneDialer/1.0")]
        if self.ua.account.caller_id:
            h.append(("P-Preferred-Identity", f"<sip:{self.ua.account.caller_id}@{self.ua.account.realm_host}>"))
        return h

    async def _invite(self):
        port = await self.rtp.open()
        sdp = build_sdp(self.ua.local_ip, port)
        auth = None
        for attempt in range(2):
            self.cseq += 1
            branch = self.ua._branch()
            self._invite_branch, self._invite_cseq = branch, self.cseq
            hdrs = self._headers("INVITE", self.cseq, branch, False) + [("Contact", self.ua._contact()), ("Content-Type", "application/sdp"), ("Allow", "INVITE, ACK, CANCEL, BYE, OPTIONS")]
            if auth:
                hdrs.append(("Authorization" if auth[0] == "www-authenticate" else "Proxy-Authorization", auth[1]))
            data = build(f"INVITE {self.target} SIP/2.0", hdrs, sdp)

            def prov(r: Msg):
                if r.code == 180 or r.code == 183:
                    self._set("ringing", r.reason)

            resp = await self.ua._transact(data, (self.call_id, self.cseq), on_provisional=prov)
            if resp.code in (401, 407) and attempt == 0:
                self.remote_tag = _param(resp.h("to"), "tag")
                self._send_ack(non2xx=True, resp=resp)
                self.remote_tag = ""
                name = "www-authenticate" if resp.code == 401 else "proxy-authenticate"
                auth = (name, self.ua._authorization(resp.h(name), "INVITE", self.target))
                continue
            if resp.code == 200:
                self.remote_tag = _param(resp.h("to"), "tag")
                contact = re.search(r"<([^>]+)>", resp.h("contact"))
                self.remote_target = contact.group(1) if contact else self.target
                ip, rport, pt, dpt = parse_sdp(resp.body)
                self.rtp.set_remote(ip, rport, pt, dpt)
                self._send_ack()
                self._set("connected")
                return
            self._send_ack(non2xx=True, resp=resp)
            raise SipError(f"Call failed: {resp.code} {resp.reason}", resp.code)

    def _send_ack(self, non2xx: bool = False, resp: Optional[Msg] = None):
        if non2xx:
            hdrs = self._headers("ACK", self._invite_cseq, self._invite_branch, False)
            hdrs[3] = ("To", resp.h("to") if resp else hdrs[3][1])
            self.ua._send(build(f"ACK {self.target} SIP/2.0", hdrs))
        else:
            hdrs = self._headers("ACK", self._invite_cseq, self.ua._branch(), True)
            self.ua._send(build(f"ACK {self.remote_target} SIP/2.0", hdrs))

    def send_audio(self, pcm8k: np.ndarray):
        if self.state == "connected":
            self.rtp.send_pcm(pcm8k)

    async def dtmf(self, digit: str):
        if self.state == "connected":
            await self.rtp.send_dtmf(digit)

    async def hangup(self):
        if self.state == "ended":
            return
        if self.state in ("calling", "ringing") and self._invite_branch:
            hdrs = self._headers("CANCEL", self._invite_cseq, self._invite_branch, False)
            for _ in range(2):  # best-effort; the INVITE then ends with 487
                self.ua._send(build(f"CANCEL {self.target} SIP/2.0", hdrs))
            return
        was_connected = self.state == "connected"
        if was_connected:
            self.cseq += 1
            data = build(f"BYE {self.remote_target} SIP/2.0", self._headers("BYE", self.cseq, self.ua._branch(), True))
            try:
                await self.ua._transact(data, (self.call_id, self.cseq), timeout=6)
            except SipError:
                pass
        self._ended("local hangup")
