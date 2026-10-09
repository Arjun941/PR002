"""Minimal SIP endpoint that answers softphone calls and connects them to Gemini Live.

No Asterisk, no SIP server: call this machine directly from a softphone (Linphone, Zoiper...) at
    sip:gemini@<this-pc-LAN-IP>:5060
It answers INVITE, negotiates G.711 (PCMU/PCMA), and relays RTP audio <-> Gemini using the same
pump as bridge.py. UDP only, one call at a time, no registration/auth/NAT traversal.

  set GEMINI_API_KEY=...   (PowerShell: $env:GEMINI_API_KEY="...")
  python sip_gemini.py     # allow Python through Windows Firewall (private networks) when asked
Needs Python <= 3.12 (audioop).
"""
from __future__ import annotations

import asyncio
import audioop
import logging
import os
import random
import re
import socket
import struct
import sys

from google import genai
from google.genai import types

from bridge import MODEL, _config, pump

SIP_PORT = int(os.getenv("SIP_PORT", "5060"))
RTP_PORT = int(os.getenv("RTP_PORT", "40000"))
CODECS = {0: ("PCMU", audioop.ulaw2lin, audioop.lin2ulaw), 8: ("PCMA", audioop.alaw2lin, audioop.lin2alaw)}

log = logging.getLogger("sip")


def parse(data: bytes):
    head, _, body = data.decode(errors="replace").partition("\r\n\r\n")
    lines = head.split("\r\n")
    hdrs: dict[str, list[str]] = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            hdrs.setdefault(k.strip().lower(), []).append(v.strip())
    return lines[0], hdrs, body


def lan_ip(peer: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer, 9))
        return s.getsockname()[0]
    finally:
        s.close()


class Call:
    """RTP session for one call, bridged to Gemini."""

    def __init__(self, loop, remote: tuple[str, int], pt: int, client: genai.Client):
        self.loop, self.remote, self.pt, self.client = loop, remote, pt, client
        self.inq: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.outq: asyncio.Queue[bytes] = asyncio.Queue()
        self.transport = None
        self.seq, self.ts, self.ssrc = random.randrange(65536), random.randrange(2**32), random.randrange(2**32)
        self.tasks: list[asyncio.Task] = []
        self.latched = False

    async def start(self):
        outer = self

        class RTP(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                if len(data) < 12 or data[0] >> 6 != 2:
                    return
                if not outer.latched:  # NAT-friendly: reply to wherever audio actually comes from
                    outer.remote, outer.latched = addr, True
                off = 12 + 4 * (data[0] & 0x0F)
                if data[0] & 0x10 and len(data) >= off + 4:
                    off += 4 + 4 * struct.unpack(">H", data[off + 2:off + 4])[0]
                if (data[1] & 0x7F) != outer.pt:
                    return  # skip DTMF (telephone-event) and anything unexpected
                outer.inq.put_nowait(CODECS[outer.pt][1](data[off:], 2))

        self.transport, _ = await self.loop.create_datagram_endpoint(RTP, local_addr=("0.0.0.0", RTP_PORT))
        self.tasks = [asyncio.create_task(self._run()), asyncio.create_task(self._send_loop())]

    async def _run(self):
        async def read_audio():
            return await self.inq.get()

        async def write_audio(pcm):
            if pcm is None:
                while not self.outq.empty():
                    self.outq.get_nowait()
            else:
                await self.outq.put(pcm)

        try:
            async with self.client.aio.live.connect(model=MODEL, config=_config()) as session:
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text="The call just connected. Greet the caller.")]),
                    turn_complete=True)
                await pump(session, read_audio, write_audio)
        except Exception:
            log.exception("gemini session failed")

    async def _send_loop(self):
        buf, enc = b"", CODECS[self.pt][2]
        nxt = self.loop.time()
        while True:
            try:
                buf += await asyncio.wait_for(self.outq.get(), 0.02)
            except asyncio.TimeoutError:
                pass
            while len(buf) >= 320:  # 20 ms of 8 kHz slin
                frame, buf = buf[:320], buf[320:]
                hdr = struct.pack(">BBHII", 0x80, self.pt, self.seq & 0xFFFF, self.ts & 0xFFFFFFFF, self.ssrc)
                self.transport.sendto(hdr + enc(frame, 2), self.remote)
                self.seq += 1
                self.ts += 160
                nxt = max(nxt + 0.02, self.loop.time())
                await asyncio.sleep(max(0, nxt - self.loop.time()))

    def stop(self):
        for t in self.tasks:
            t.cancel()
        if self.transport:
            self.transport.close()


class SIP(asyncio.DatagramProtocol):
    def __init__(self, loop, client):
        self.loop, self.client = loop, client
        self.calls: dict[str, Call] = {}
        self.tags: dict[str, str] = {}

    def connection_made(self, transport):
        self.transport = transport

    def reply(self, code, text, hdrs, addr, sdp="", tag=None, extra=()):
        to = hdrs["to"][0]
        if tag and "tag=" not in to:
            to += f";tag={tag}"
        lines = [f"SIP/2.0 {code} {text}", *[f"Via: {v}" for v in hdrs["via"]],
                 f"From: {hdrs['from'][0]}", f"To: {to}", f"Call-ID: {hdrs['call-id'][0]}",
                 f"CSeq: {hdrs['cseq'][0]}", *extra, "User-Agent: reachout-gemini", "Allow: INVITE, ACK, BYE, CANCEL, OPTIONS",
                 f"Content-Length: {len(sdp)}"]
        if sdp:
            lines.insert(-1, "Content-Type: application/sdp")
        self.transport.sendto(("\r\n".join(lines) + "\r\n\r\n" + sdp).encode(), addr)

    def datagram_received(self, data, addr):
        try:
            first, hdrs, body = parse(data)
            log.info("rx %s from %s", first[:60], addr[0])
            if first.startswith("SIP/2.0") or "call-id" not in hdrs:
                return
            method = first.split()[0]
            cid = hdrs["call-id"][0]
            if method == "INVITE":
                self.on_invite(hdrs, body, addr, cid)
            elif method == "BYE":
                call = self.calls.pop(cid, None)
                if call:
                    call.stop()
                    log.info("call ended by caller")
                self.reply(200, "OK", hdrs, addr)
            elif method == "CANCEL":
                self.reply(200, "OK", hdrs, addr)
                if cid in self.calls:
                    self.calls.pop(cid).stop()
            elif method == "OPTIONS":
                self.reply(200, "OK", hdrs, addr)
            # ACK needs no reply
        except Exception:
            log.exception("bad SIP message")

    def on_invite(self, hdrs, body, addr, cid):
        if cid in self.calls:  # retransmitted INVITE
            return
        if self.calls:
            return self.reply(486, "Busy Here", hdrs, addr)
        m = re.search(r"m=audio (\d+) RTP/AVP ([\d ]+)", body)
        c = re.search(r"c=IN IP4 (\S+)", body)
        pts = [int(p) for p in m.group(2).split()] if m else []
        pt = next((p for p in pts if p in CODECS), None)
        if not (m and c and pt is not None):
            return self.reply(488, "Not Acceptable Here", hdrs, addr)
        remote = (c.group(1), int(m.group(1)))
        ip = lan_ip(addr[0])
        tag = f"{random.randrange(16**8):08x}"
        sdp = ("v=0\r\n" f"o=gemini 1 1 IN IP4 {ip}\r\n" "s=Gemini\r\n" f"c=IN IP4 {ip}\r\n" "t=0 0\r\n"
               f"m=audio {RTP_PORT} RTP/AVP {pt}\r\n" f"a=rtpmap:{pt} {CODECS[pt][0]}/8000\r\n" "a=ptime:20\r\n" "a=sendrecv\r\n")
        log.info("incoming call from %s, codec %s", addr[0], CODECS[pt][0])
        self.reply(180, "Ringing", hdrs, addr, tag=tag)
        self.reply(200, "OK", hdrs, addr, sdp=sdp, tag=tag, extra=[f"Contact: <sip:gemini@{ip}:{SIP_PORT}>"])
        call = Call(self.loop, remote, pt, self.client)
        self.calls[cid] = call
        self.loop.create_task(call.start())


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if not os.getenv("GEMINI_API_KEY"):
        sys.exit("Set GEMINI_API_KEY")
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: SIP(loop, client), local_addr=("0.0.0.0", SIP_PORT))
    log.info("Call sip:gemini@%s:%d from a softphone on the same network (model %s)", lan_ip("8.8.8.8"), SIP_PORT, MODEL)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
