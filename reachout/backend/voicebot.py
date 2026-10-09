"""Exotel Voicebot/Stream WebSocket endpoint (Phase 1 skeleton).

Plays a generated tone prompt on call start, then reads each DTMF digit back as N beeps.
No TTS/STT yet. Audio is assumed 16-bit PCM, 8 kHz mono, base64 in JSON "media" events;
VERIFY against the Exotel docs/first real call (see CLAUDE.md Phase 1) and adjust
SAMPLE_RATE / framing here only.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import struct

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .exotel import mask

log = logging.getLogger("reachout.voicebot")
router = APIRouter()

SAMPLE_RATE = 8000
FRAME_BYTES = 320  # 20 ms of 16-bit mono at 8 kHz; Exotel wants multiples of this


def tone(freq: float, ms: int, gap_ms: int = 0) -> bytes:
    n = SAMPLE_RATE * ms // 1000
    pcm = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))) for i in range(n))
    return pcm + b"\x00\x00" * (SAMPLE_RATE * gap_ms // 1000)


def _pad(pcm: bytes) -> bytes:
    return pcm + b"\x00" * (-len(pcm) % FRAME_BYTES)


async def _play(ws: WebSocket, stream_sid: str | None, pcm: bytes) -> None:
    pcm = _pad(pcm)
    for i in range(0, len(pcm), FRAME_BYTES * 25):  # ~500 ms chunks
        await ws.send_text(json.dumps({
            "event": "media", "stream_sid": stream_sid,
            "media": {"payload": base64.b64encode(pcm[i:i + FRAME_BYTES * 25]).decode()},
        }))


@router.websocket("/ws/exotel")
async def exotel_stream(ws: WebSocket) -> None:
    await ws.accept()
    stream_sid: str | None = None
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            event = msg.get("event")
            if event == "start":
                stream_sid = msg.get("stream_sid") or msg.get("start", {}).get("stream_sid")
                info = msg.get("start", {})
                log.info("call start sid=%s to=%s", info.get("call_sid"), mask(str(info.get("to", ""))))
                log.debug("start payload keys: %s", list(info))  # check audio format fields here
                await _play(ws, stream_sid, tone(440, 400, 200) + tone(660, 400))
            elif event == "dtmf":
                digit = str((msg.get("dtmf") or {}).get("digit", ""))
                log.info("dtmf digit=%s", digit)
                if digit.isdigit():
                    await _play(ws, stream_sid, b"".join(tone(880, 200, 200) for _ in range(int(digit) or 10)))
            elif event == "stop":
                log.info("call stop")
                break
            # "connected", "media" (caller audio) and "mark" are ignored in Phase 1
    except WebSocketDisconnect:
        pass
