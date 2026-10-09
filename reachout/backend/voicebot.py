"""Exotel Voicebot/Stream WebSocket: the keypad-first call.

Campaign call: greeting + name + message + menu (all pre-synthesised, see audio.py), then wait
for a key. 1/2/3 save the outcome and play the goodbye; 4 hands the caller to the ElevenLabs
agent when the campaign offers it; any other key replays the menu. No key after the menu (asked
twice) plays the voicemail message and ends; the status callback then counts the call as
voicemail and the retry policy applies. Calls without campaign audio (the Phase 1 test call)
get the tone test instead.

Audio is assumed 16-bit PCM, 8 kHz mono, base64 in JSON "media" events, and "clear" flushes
audio Exotel has queued. VERIFY against the Exotel docs/first real call and adjust here only.
The Exotel flow should hang up after the voicebot applet: closing this socket ends our part.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import struct
from contextlib import suppress

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import audio, elevenlabs, store
from .dialer import record_dtmf, record_outcome
from .exotel import mask
from .store import LANGUAGES

log = logging.getLogger("reachout.voicebot")
router = APIRouter()

SAMPLE_RATE = 8000
FRAME_BYTES = 320  # 20 ms of 16-bit mono at 8 kHz; Exotel wants multiples of this
CHUNK = FRAME_BYTES * 25  # ~500 ms per media message
BYTES_PER_SEC = SAMPLE_RATE * 2
MENU_WAIT = float(os.getenv("MENU_WAIT_SECONDS", "8"))  # silence after the menu before asking again


def tone(freq: float, ms: int, gap_ms: int = 0) -> bytes:
    n = SAMPLE_RATE * ms // 1000
    pcm = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))) for i in range(n))
    return pcm + b"\x00\x00" * (SAMPLE_RATE * gap_ms // 1000)


class Line:
    """One Exotel stream: sends audio in whole frames and tracks when queued audio finishes playing."""

    def __init__(self, ws: WebSocket):
        self.ws, self.stream_sid, self.call_sid = ws, None, None
        self.until = 0.0
        self._rest = b""

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    def left(self) -> float:
        return max(0.0, self.until - self._now())

    async def recv(self, timeout: float | None = None) -> dict | None:
        """Next Exotel event; {"event": "timeout"} after `timeout` s; None once the call is gone."""
        try:
            return json.loads(await asyncio.wait_for(self.ws.receive_text(), timeout))
        except asyncio.TimeoutError:
            return {"event": "timeout"}
        except (WebSocketDisconnect, RuntimeError):
            return None

    async def send(self, pcm: bytes) -> None:
        """Streamed audio (the agent): whole frames now, the remainder with the next chunk."""
        data = self._rest + pcm
        cut = len(data) - len(data) % FRAME_BYTES
        self._rest = data[cut:]
        for i in range(0, cut, CHUNK):
            await self.ws.send_text(json.dumps({"event": "media", "stream_sid": self.stream_sid,
                                                "media": {"payload": base64.b64encode(data[i:min(i + CHUNK, cut)]).decode()}}))
        self.until = max(self.until, self._now()) + cut / BYTES_PER_SEC

    async def play(self, pcm: bytes) -> None:
        """A complete prompt, padded with silence to a whole frame."""
        await self.send(pcm + b"\x00" * (-(len(self._rest) + len(pcm)) % FRAME_BYTES))

    async def clear(self) -> None:
        self._rest, self.until = b"", self._now()
        await self.ws.send_text(json.dumps({"event": "clear", "stream_sid": self.stream_sid}))

    async def finish(self) -> None:
        """Let queued audio play out before we hang up."""
        await asyncio.sleep(self.left() + 0.5)


async def _escalate(line: Line, c: dict, r: dict) -> bool:
    """Caller pressed 4: talk to the ElevenLabs agent. False if the agent could not be reached."""
    e = c["event"]
    variables = {"org": e.get("org", ""), "title": e.get("title", ""), "venue": e.get("venue", ""),
                 "when": " at ".join(x for x in (e.get("date", ""), e.get("time", "")) if x),
                 "details": e.get("details", ""), "language": LANGUAGES.get(r["language"], r["language"])}
    record_outcome(line.call_sid, None, "agent")
    log.info("call %s: handing over to the voice agent", line.call_sid)
    try:
        await elevenlabs.bridge(line.recv, line.send, line.clear, variables, r["language"],
                                lambda outcome: record_outcome(line.call_sid, outcome, "agent"))
    except Exception as exc:  # signed URL or connect failed: the caller is still on the keypad call
        log.warning("call %s: voice agent unavailable (%s)", line.call_sid, type(exc).__name__)
        record_outcome(line.call_sid, None, "keypad")
        return False
    return True


async def _campaign_call(line: Line, c: dict, r: dict, clips: dict[str, bytes]) -> None:
    await line.play(clips["intro"] + clips["menu"])
    asked = 1
    while True:
        deadline = line.until + MENU_WAIT
        msg = await line.recv(timeout=max(0.0, deadline - line._now()))
        if msg is None or msg.get("event") == "stop":
            return
        event = msg.get("event")
        if event == "timeout":
            if asked < 2:  # ask once more before treating it as voicemail
                asked += 1
                await line.play(clips["menu"])
                continue
            await line.play(clips["voicemail"])
            await line.finish()
            return
        if event != "dtmf":
            continue  # caller audio ("media") and "mark" events are not used on a keypad call
        digit = str((msg.get("dtmf") or {}).get("digit", ""))
        log.info("call %s: dtmf %s", line.call_sid, digit)
        await line.clear()  # a key press interrupts whatever is playing
        if record_dtmf(line.call_sid, digit):
            await line.play(clips["goodbye"])
            await line.finish()
            return
        if digit == "4" and c["escalation"] and elevenlabs.agent_ready():
            if await _escalate(line, c, r):
                return
        await line.play(clips["menu"])


async def _tone_test(line: Line) -> None:
    """Phase 1 test call: tones, then each digit read back as beeps."""
    await line.play(tone(440, 400, 200) + tone(660, 400))
    while (msg := await line.recv()) is not None and msg.get("event") != "stop":
        if msg.get("event") == "dtmf":
            digit = str((msg.get("dtmf") or {}).get("digit", ""))
            log.info("dtmf digit=%s", digit)
            record_dtmf(line.call_sid, digit)
            if digit.isdigit():
                await line.play(b"".join(tone(880, 200, 200) for _ in range(int(digit) or 10)))


@router.websocket("/ws/exotel")
async def exotel_stream(ws: WebSocket) -> None:
    await ws.accept()
    line = Line(ws)
    while (msg := await line.recv()) is not None and msg.get("event") != "start":
        pass  # "connected" comes first
    if msg is None:
        return
    info = msg.get("start", {})
    line.stream_sid = msg.get("stream_sid") or info.get("stream_sid")
    line.call_sid = info.get("call_sid")
    log.info("call start sid=%s to=%s", line.call_sid, mask(str(info.get("to", ""))))
    log.debug("start payload keys: %s", list(info))  # check audio format fields here
    r = store.recipient_by_call(line.call_sid) if line.call_sid else None
    c = store.campaign(r["campaign_id"]) if r else None
    clips = audio.call_audio(c, r) if c and r else None
    try:
        await (_campaign_call(line, c, r, clips) if clips else _tone_test(line))
    except (WebSocketDisconnect, RuntimeError):
        pass  # caller hung up mid-prompt
    finally:
        with suppress(Exception):
            await ws.close()
        log.info("call stop sid=%s", line.call_sid)
