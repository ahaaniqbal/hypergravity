"""Originate an outbound call over the a1mobile SIP trunk.

The sponsor gives every team a number plus SIP credentials and says, of outbound:
"originate through the same SIP credential connection from your framework." There
is no REST endpoint for it — /api/sms answers 422 on an empty body while every
outbound-shaped route 404s — so placing a call means being a SIP user agent
ourselves.

That sounds heavier than it is. A UAC that only ever places one outgoing call
needs REGISTER, INVITE, ACK and BYE, and none of the server-side machinery: no
dialog forking, no re-INVITE, no transfer. Digest auth is MD5 and Telnyx doesn't
even ask for qop. The whole thing is stdlib.

The part that could have killed it — NAT — costs nothing. Telnyx reports our
public address back in the Via ``received=`` parameter, so we advertise that with
our local RTP port and send first; their media server latches symmetrically and
streams back to wherever our packets actually came from. No STUN, no TURN, no
port forwarding.

Signalling is blocking sockets on purpose. It is a handful of round trips over
about a second, it runs in a thread, and the proven-correct straight-line version
is worth more today than an elegant asyncio one.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import re
import socket
import string
import time
from dataclasses import dataclass

from loguru import logger

# The trunk credentials POST /api/numbers/claim already handed us — same three
# values the inbound number is built on, so there is nothing new to configure.
DOMAIN = os.getenv("A1_SIP_HOST", "sip.telnyx.com")
PORT = int(os.getenv("A1_SIP_PORT", "5060"))
USER = os.getenv("A1_SIP_USERNAME", "")
PASSWORD = os.getenv("A1_SIP_PASSWORD", "")
CALLER_ID = os.getenv("A1_PHONE_NUMBER", "")

RING_TIMEOUT = 45.0


class SipError(RuntimeError):
    pass


def _rnd(n: int = 10) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _parse_auth(header: str) -> dict[str, str]:
    body = header.split(None, 1)[1] if " " in header else header
    return {
        m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3)
        for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', body)
    }


def _digest(method: str, uri: str, chal: dict[str, str]) -> str:
    """RFC 2617 MD5. Telnyx omits qop, so handle both shapes."""
    realm = chal.get("realm", DOMAIN)
    nonce = chal["nonce"]
    ha1 = _md5(f"{USER}:{realm}:{PASSWORD}")
    ha2 = _md5(f"{method}:{uri}")
    parts = [
        f'username="{USER}"', f'realm="{realm}"', f'nonce="{nonce}"',
        f'uri="{uri}"', f"algorithm={chal.get('algorithm', 'MD5')}",
    ]
    if qop := chal.get("qop"):
        qop = qop.split(",")[0].strip()
        cnonce, nc = _rnd(16), "00000001"
        parts += [
            f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"',
            f'response="{_md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")}"',
        ]
    else:
        parts.append(f'response="{_md5(f"{ha1}:{nonce}:{ha2}")}"')
    if "opaque" in chal:
        parts.append(f'opaque="{chal["opaque"]}"')
    return "Digest " + ", ".join(parts)


def _status(msg: str | None) -> str:
    return msg.split("\r\n", 1)[0] if msg else ""


def _code(msg: str | None) -> str:
    bits = _status(msg).split()
    return bits[1] if len(bits) > 1 else ""


def _header(msg: str, name: str) -> str | None:
    for line in msg.split("\r\n"):
        if line.lower().startswith(name.lower() + ":"):
            return line.split(":", 1)[1].strip()
    return None


class _Socket:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.dst = (socket.gethostbyname(DOMAIN), PORT)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(self.dst)
        self.local_ip = probe.getsockname()[0]
        probe.close()
        self.local_port = self.sock.getsockname()[1]
        self.cseq = 0

    def send(self, msg: str) -> None:
        self.sock.sendto(msg.encode(), self.dst)

    def recv(self, timeout: float = 5.0) -> str | None:
        self.sock.settimeout(timeout)
        try:
            data, _ = self.sock.recvfrom(65535)
        except (socket.timeout, OSError):
            return None
        return data.decode(errors="replace")


@dataclass
class SipCall:
    """An answered call. Owns the RTP socket and enough dialog state to hang up."""

    rtp: socket.socket
    remote_media: tuple[str, int]
    _sig: _Socket
    _target: str
    _call_id: str
    _from_tag: str
    _to_header: str
    answered_at: float

    def hangup(self) -> None:
        """BYE, best effort. A failed teardown must never raise into the pipeline."""
        try:
            self._sig.cseq += 1
            self._sig.send("\r\n".join([
                f"BYE {self._target} SIP/2.0",
                f"Via: SIP/2.0/UDP {self._sig.local_ip}:{self._sig.local_port}"
                f";branch=z9hG4bK{_rnd(12)};rport",
                "Max-Forwards: 70",
                f"From: <sip:{CALLER_ID}@{DOMAIN}>;tag={self._from_tag}",
                f"To: {self._to_header}",
                f"Call-ID: {self._call_id}",
                f"CSeq: {self._sig.cseq} BYE",
                "Content-Length: 0",
            ]) + "\r\n\r\n")
            self._sig.recv(timeout=3)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"sip: BYE failed: {e}")
        finally:
            try:
                self.rtp.close()
                self._sig.sock.close()
            except Exception:  # noqa: BLE001
                pass


def _sdp(ip: str, port: int) -> str:
    return "\r\n".join([
        "v=0", f"o=- {int(time.time())} 1 IN IP4 {ip}", "s=hypergravity",
        f"c=IN IP4 {ip}", "t=0 0", f"m=audio {port} RTP/AVP 0 101",
        "a=rtpmap:0 PCMU/8000", "a=rtpmap:101 telephone-event/8000",
        "a=fmtp:101 0-16", "a=sendrecv", "a=ptime:20",
    ]) + "\r\n"


def _remote_media(body: str) -> tuple[str | None, int | None]:
    ip = port = None
    for line in body.split("\r\n"):
        if line.startswith("c=IN IP4 "):
            ip = line[9:].strip()
        elif line.startswith("m=audio "):
            port = int(line.split()[1])
    return ip, port


def _register(sig: _Socket) -> str:
    """REGISTER, and return our public IP as Telnyx reports it."""
    call_id, from_tag = _rnd(20) + "@" + sig.local_ip, _rnd(8)
    auth, resp = None, None
    for attempt in range(2):
        sig.cseq += 1
        lines = [
            f"REGISTER sip:{DOMAIN} SIP/2.0",
            f"Via: SIP/2.0/UDP {sig.local_ip}:{sig.local_port}"
            f";branch=z9hG4bK{_rnd(12)}{attempt};rport",
            "Max-Forwards: 70",
            f"From: <sip:{USER}@{DOMAIN}>;tag={from_tag}",
            f"To: <sip:{USER}@{DOMAIN}>",
            f"Call-ID: {call_id}",
            f"CSeq: {sig.cseq} REGISTER",
            f"Contact: <sip:{USER}@{sig.local_ip}:{sig.local_port};transport=udp>",
            "Expires: 300", "User-Agent: hypergravity/1.0",
            "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS", "Content-Length: 0",
        ]
        if auth:
            lines.insert(-1, f"Authorization: {auth}")
        sig.send("\r\n".join(lines) + "\r\n\r\n")

        resp = sig.recv()
        while resp and _code(resp) == "100":
            resp = sig.recv()
        if resp and _code(resp) in ("401", "407"):
            hdr = _header(resp, "WWW-Authenticate") or _header(resp, "Proxy-Authenticate")
            auth = _digest("REGISTER", f"sip:{DOMAIN}", _parse_auth(hdr or ""))
            continue
        break

    if not resp or _code(resp) != "200":
        raise SipError(f"REGISTER refused: {_status(resp) or 'no response'}")

    public = sig.local_ip
    for token in (_header(resp, "Via") or "").split(";"):
        if token.startswith("received="):
            public = token.split("=", 1)[1]
    return public


def _place(to: str) -> SipCall:
    """Blocking: register, invite, ack. Returns once the far end answers."""
    if not USER or not PASSWORD:
        raise SipError("A1_SIP_USERNAME / A1_SIP_PASSWORD are not set")

    sig = _Socket()
    public = _register(sig)

    rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp.bind(("0.0.0.0", 0))
    rtp_port = rtp.getsockname()[1]

    ruri = f"sip:{to}@{DOMAIN}"
    call_id, from_tag = _rnd(20) + "@" + public, _rnd(8)
    body = _sdp(public, rtp_port)
    auth: tuple[str, str] | None = None
    resp = None

    for attempt in range(2):
        sig.cseq += 1
        branch = f"z9hG4bK{_rnd(12)}{attempt}"
        lines = [
            f"INVITE {ruri} SIP/2.0",
            f"Via: SIP/2.0/UDP {sig.local_ip}:{sig.local_port};branch={branch};rport",
            "Max-Forwards: 70",
            f"From: <sip:{CALLER_ID}@{DOMAIN}>;tag={from_tag}",
            f"To: <{ruri}>",
            f"Call-ID: {call_id}",
            f"CSeq: {sig.cseq} INVITE",
            f"Contact: <sip:{USER}@{sig.local_ip}:{sig.local_port};transport=udp>",
            "User-Agent: hypergravity/1.0",
            "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS",
            "Content-Type: application/sdp",
            f"Content-Length: {len(body)}",
        ]
        if auth:
            lines.insert(-2, f"{auth[0]}: {auth[1]}")
        sig.send("\r\n".join(lines) + "\r\n\r\n" + body)

        # 180/183 mean it is ringing; keep waiting for the final answer.
        final, deadline = None, time.time() + RING_TIMEOUT
        while time.time() < deadline:
            r = sig.recv(timeout=min(8.0, max(1.0, deadline - time.time())))
            if r is None:
                continue
            if _code(r) in ("100", "180", "183"):
                continue
            final = r
            break
        resp = final
        if resp is None:
            rtp.close()
            raise SipError("no answer — the call rang out")

        if _code(resp) in ("401", "407") and attempt == 0:
            # A challenged INVITE must still be ACKed inside its transaction.
            sig.send("\r\n".join([
                f"ACK {ruri} SIP/2.0",
                f"Via: SIP/2.0/UDP {sig.local_ip}:{sig.local_port};branch={branch};rport",
                "Max-Forwards: 70",
                f"From: <sip:{CALLER_ID}@{DOMAIN}>;tag={from_tag}",
                f"To: {_header(resp, 'To')}",
                f"Call-ID: {call_id}",
                f"CSeq: {sig.cseq} ACK",
                "Content-Length: 0",
            ]) + "\r\n\r\n")
            if proxy := _header(resp, "Proxy-Authenticate"):
                auth = ("Proxy-Authorization", _digest("INVITE", ruri, _parse_auth(proxy)))
            else:
                www = _header(resp, "WWW-Authenticate") or ""
                auth = ("Authorization", _digest("INVITE", ruri, _parse_auth(www)))
            continue
        break

    if _code(resp) != "200":
        rtp.close()
        raise SipError(f"call refused: {_status(resp)}")

    to_header = _header(resp, "To") or f"<{ruri}>"
    contact = (_header(resp, "Contact") or f"<{ruri}>").strip()
    target = contact[contact.find("<") + 1: contact.find(">")] if "<" in contact else ruri
    rbody = resp.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in resp else ""
    rip, rport = _remote_media(rbody)
    if not rip or not rport:
        rtp.close()
        raise SipError("answered without usable media")

    sig.send("\r\n".join([
        f"ACK {target} SIP/2.0",
        f"Via: SIP/2.0/UDP {sig.local_ip}:{sig.local_port};branch=z9hG4bK{_rnd(12)};rport",
        "Max-Forwards: 70",
        f"From: <sip:{CALLER_ID}@{DOMAIN}>;tag={from_tag}",
        f"To: {to_header}",
        f"Call-ID: {call_id}",
        f"CSeq: {sig.cseq} ACK",
        f"Contact: <sip:{USER}@{sig.local_ip}:{sig.local_port};transport=udp>",
        "Content-Length: 0",
    ]) + "\r\n\r\n")

    rtp.setblocking(False)
    logger.info(f"sip: {to} answered — media {rip}:{rport}")
    return SipCall(
        rtp=rtp, remote_media=(rip, rport), _sig=sig, _target=target,
        _call_id=call_id, _from_tag=from_tag, _to_header=to_header,
        answered_at=time.time(),
    )


async def place_call(to: str) -> SipCall:
    """Ring ``to`` and return once they pick up. Raises SipError if they don't."""
    return await asyncio.to_thread(_place, to)


def configured() -> bool:
    return bool(USER and PASSWORD and CALLER_ID)
