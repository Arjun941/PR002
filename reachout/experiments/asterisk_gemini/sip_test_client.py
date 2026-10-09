"""Fake softphone to check sip_gemini.py without a real phone: INVITE, send silence, count audio back."""
import audioop
import socket
import struct
import sys
import time

host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sip.bind(("0.0.0.0", 0)); sip.settimeout(0.05)
rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); rtp.bind(("0.0.0.0", 0)); rtp.settimeout(0.001)
lport, rport = sip.getsockname()[1], rtp.getsockname()[1]
sdp = f"v=0\r\no=t 1 1 IN IP4 127.0.0.1\r\ns=t\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio {rport} RTP/AVP 0 8 101\r\na=rtpmap:0 PCMU/8000\r\n"
msg = lambda m, extra="", body="": (f"{m} sip:gemini@{host} SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:{lport};branch=z9hG4bK1\r\n"
    f"From: <sip:t@127.0.0.1>;tag=abc\r\nTo: <sip:gemini@{host}>{extra}\r\nCall-ID: test-call-1\r\nCSeq: {2 if m == 'BYE' else 1} {m}\r\n"
    f"Contact: <sip:t@127.0.0.1:{lport}>\r\nContent-Length: {len(body)}\r\n" + ("Content-Type: application/sdp\r\n" if body else "") + f"\r\n{body}").encode()

sip.sendto(msg("INVITE", body=sdp), (host, 5060))
resp = []
t0 = time.time()
while time.time() - t0 < 3 and not any(r.startswith("SIP/2.0 200") for r in resp):
    try: resp.append(sip.recv(4096).decode())
    except socket.timeout: pass
ok = next((r for r in resp if r.startswith("SIP/2.0 200")), None)
print("responses:", [r.split("\r\n")[0] for r in resp])
if not ok: sys.exit("no 200 OK")
rtp_port = int(ok.split("m=audio ")[1].split()[0]); tag = ok.split("tag=")[-1].split("\r\n")[0] if "To:" in ok else ""
sip.sendto(msg("ACK", extra=f";tag={tag}").replace(b"CSeq: 1 ACK", b"CSeq: 1 ACK"), (host, 5060))

seq = ts = 0; pkts = loud = 0; silence = bytes([0xFF]) * 160
end = time.time() + 10; nxt = time.time()
while time.time() < end:
    rtp.sendto(struct.pack(">BBHII", 0x80, 0, seq, ts, 1234) + silence, (host, rtp_port)); seq += 1; ts += 160
    for _ in range(3):
        try:
            d = rtp.recv(2048); pkts += 1
            if audioop.rms(audioop.ulaw2lin(d[12:], 2), 2) > 200: loud += 1
        except socket.timeout: break
    nxt += 0.02; time.sleep(max(0, nxt - time.time()))
print(f"received {pkts} RTP packets back, {loud} with audible speech")
sip.sendto(msg("BYE", extra=f";tag={tag}"), (host, 5060))
