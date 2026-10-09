"""The phone: a browser page (/phone) that registers over a WebSocket (/ws/phone) and rings when a campaign
places a call (it works from a real phone over a tunnel). It is the only telephony channel. Answering starts:
  live mode    the campaign's provider (ElevenLabs or Gemini) takes over the whole call, told everything the
               campaign knows (callctx.py)
  hybrid mode  the pre-synthesised IVR (ivr.py) with the page's keypad, then the provider for anyone who
               has a question at the end

Wire format (one WebSocket per phone page):
  server -> page   text   {"type": "registered"} | {"type": "incoming", call_id, campaign, recipient, provider, language, mode}
                          | {"type": "clear"} | {"type": "ended"} | {"type": "error", text}
                   binary <u32 sample rate, little endian><PCM16 mono audio>
  page -> server   text   {"type": "answer"} | {"type": "decline"} | {"type": "hangup"} | {"type": "dtmf", "digit": "1"}
                   binary PCM16 mono 16 kHz microphone frames (20 ms)

The conversation engines speak a telephone-style bridge contract (8 kHz), so the audio is telephone quality. Set PHONE_TOKEN to require ?token= on /ws/phone
(do this whenever the backend is reachable from the internet).
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import array
import secrets
import struct
import time
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from . import audio, callctx, catalog, ivr, store
from .gemini_live import Resampler, audioop
from .store import LANGUAGES

log = logging.getLogger("reachout.webphone")
router = APIRouter()

PAGE = Path(__file__).with_name("static") / "phone.html"
RING_SECONDS = int(os.getenv("WEBPHONE_RING_SECONDS", "30"))
MAX_SECONDS = int(os.getenv("WEBPHONE_MAX_SECONDS", "300"))  # cost cap per call
OUTCOMES = ("confirmed", "declined", "rescheduled")
GATE_RMS = int(os.getenv("WEBPHONE_GATE_RMS", "450"))      # quietest level (16-bit RMS) that counts as speech
GATE_HANG = float(os.getenv("WEBPHONE_GATE_HANG", "0.35"))  # keep the gate open this long after speech


class Phone:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.state = "idle"  # idle | ringing | in_call
        self.inbox: asyncio.Queue = asyncio.Queue()  # ("audio", bytes) | ("event", dict) | None when the page is gone

    async def send(self, msg: dict) -> None:
        await self.ws.send_text(json.dumps(msg))


_phones: set[Phone] = set()


def connected() -> int:
    return len(_phones)


def idle_phone() -> Phone | None:
    return next((p for p in _phones if p.state == "idle"), None)


@router.get("/phone")
def page():
    return FileResponse(PAGE, media_type="text/html")


@router.websocket("/ws/phone")
async def phone_socket(ws: WebSocket):
    token = os.getenv("PHONE_TOKEN", "")
    if token and not hmac.compare_digest(ws.query_params.get("token", ""), token):
        await ws.close(code=1008)
        return
    await ws.accept()
    phone = Phone(ws)
    _phones.add(phone)
    log.info("web phone connected (%d)", len(_phones))
    try:
        await phone.send({"type": "registered"})
        while True:
            m = await ws.receive()
            if m.get("bytes") is not None:
                if phone.state == "in_call":
                    phone.inbox.put_nowait(("audio", m["bytes"]))
            elif m.get("text"):
                try:
                    phone.inbox.put_nowait(("event", json.loads(m["text"])))
                except ValueError:
                    pass
            elif m.get("type") == "websocket.disconnect":
                break
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        _phones.discard(phone)
        phone.inbox.put_nowait(None)
        log.info("web phone disconnected (%d)", len(_phones))


async def _wait_answer(phone: Phone) -> bool:
    """Ring until the page answers, declines, goes away or RING_SECONDS pass."""
    end = asyncio.get_running_loop().time() + RING_SECONDS
    while (left := end - asyncio.get_running_loop().time()) > 0:
        try:
            item = await asyncio.wait_for(phone.inbox.get(), left)
        except asyncio.TimeoutError:
            return False
        if item is None:
            return False
        if item[0] == "event" and item[1].get("type") in ("answer", "decline"):
            return item[1]["type"] == "answer"
    return False


def _rms(pcm: bytes) -> float:
    if audioop:
        return audioop.rms(pcm, 2)
    a = array.array("h", pcm[:len(pcm) - len(pcm) % 2])
    return (sum(x * x for x in a) / max(len(a), 1)) ** 0.5


class Gate:
    """Noise gate on the caller's microphone. Background noise and the phone's own speaker leaking into the mic
    are replaced by silence, so the provider does not hear a "caller" and cut the agent off. The bar follows the
    room's noise level and is higher while the agent is talking (echo), so only clear speech interrupts it."""

    def __init__(self):
        self.floor, self.open_until = 100.0, 0.0

    def __call__(self, pcm: bytes, agent_talking: bool) -> bytes:
        now, level = time.monotonic(), _rms(pcm)
        bar = max(GATE_RMS, self.floor * 3) * (2.2 if agent_talking else 1.0)
        if level >= bar:
            self.open_until = now + GATE_HANG
        elif now > self.open_until:
            self.floor = 0.97 * self.floor + 0.03 * min(level, 1500)
        return pcm if now <= self.open_until else bytes(len(pcm))


async def call(c: dict, r: dict, phone: Phone) -> None:
    """One campaign call to the web phone, from ringing to the saved outcome. Always finishes the attempt."""
    sid = f"web-{secrets.token_hex(6)}"
    store.set_call_sid(r["id"], sid)
    provider = c.get("provider") or c.get("agent_provider") or catalog.default()
    mode = c.get("mode") or "live"
    answered = heard = False
    phone.state = "ringing"
    while not phone.inbox.empty():  # stale audio or events from an earlier call
        phone.inbox.get_nowait()
    try:
        await phone.send({"type": "incoming", "call_id": sid, "campaign": c["name"], "recipient": r["name"],
                          "provider": catalog.label(provider), "language": LANGUAGES.get(r["language"], r["language"]),
                          "mode": mode})
        log.info("ringing web phone for %s (%s)", r["id"], provider)
        # The provider connects and prepares its opening line while the phone rings, so the agent speaks the
        # moment the page answers instead of after a 2-4 s connection.
        picked_up = asyncio.Event()
        convo = asyncio.create_task(_converse(c, r, phone, sid, provider, picked_up)) if mode == "live" else None
        try:
            if await _wait_answer(phone):
                answered = True
                phone.state = "in_call"
                picked_up.set()
                if convo:
                    store.mark_outcome(sid, None, "agent")
                    heard = await convo
                else:
                    heard = await _hybrid(c, r, phone, sid)
        finally:
            if convo and not convo.done():
                convo.cancel()
    except Exception:
        log.exception("web phone call failed")
    finally:
        phone.state = "idle"
        with_page = phone in _phones
        if with_page:
            try:
                await phone.send({"type": "ended"})
            except Exception:
                pass
        # Answered and heard the agent: whatever the agent recorded stands (none = counted like voicemail, retried).
        store.finish_call(r["id"], sid, answered and heard, None)


class PhoneLine:
    """The IVR's view of the phone page (the interface ivr.py expects): 8 kHz audio out, keypad and noise-gated
    microphone audio in, and a clock for when queued audio will have finished playing."""

    def __init__(self, phone: Phone, sid: str):
        self.phone, self.call_sid = phone, sid
        self.until = 0.0
        self.spoke = False
        self._down, self._gate = Resampler(16000, 8000), Gate()

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    def left(self) -> float:
        return max(0.0, self.until - self._now())

    async def recv(self, timeout: float | None = None) -> dict | None:
        """Next event: media, dtmf, stop, or {"event": "timeout"} after `timeout` s; None once the page is gone."""
        end = None if timeout is None else self._now() + timeout
        while True:
            left = None if end is None else max(0.0, end - self._now())
            try:
                item = await asyncio.wait_for(self.phone.inbox.get(), left)
            except asyncio.TimeoutError:
                return {"event": "timeout"}
            if item is None:
                return None
            kind, data = item
            if kind == "audio":
                quiet = self._gate(data, self.left() > 0)
                return {"event": "media", "media": {"payload": base64.b64encode(self._down(quiet)).decode()}}
            if data.get("type") == "hangup":
                return {"event": "stop"}
            if data.get("type") == "dtmf":
                return {"event": "dtmf", "dtmf": {"digit": str(data.get("digit", ""))[:1]}}

    async def send(self, pcm: bytes) -> None:
        self.spoke = True
        self.until = max(self.until, self._now()) + len(pcm) / 2 / 8000
        for i in range(0, len(pcm), 16000):  # about a second per message
            await self.phone.ws.send_bytes(struct.pack("<I", 8000) + pcm[i:i + 16000])

    play = send

    async def clear(self) -> None:
        self.until = self._now()
        await self.phone.send({"type": "clear"})

    async def finish(self) -> None:
        """Let queued audio play out before we hang up."""
        await asyncio.sleep(self.left() + 0.5)


async def _hybrid(c: dict, r: dict, phone: Phone, sid: str) -> bool:
    """The IVR call from the cache, then the agent for anyone with a question. True once the person heard audio."""
    line = PhoneLine(phone, sid)
    clips = audio.call_audio(c, r)
    if not clips:
        log.warning("hybrid campaign %s has no synthesised audio yet", c["id"])
        await phone.send({"type": "error", "text": "This campaign's IVR audio has not been synthesised yet."})
        return False
    try:
        await asyncio.wait_for(ivr.campaign_call(line, c, r, clips), MAX_SECONDS)
    except (ivr.Hangup, asyncio.TimeoutError):
        pass
    except Exception as exc:
        log.warning("hybrid call failed: %s", type(exc).__name__)
    return line.spoke


async def _converse(c: dict, r: dict, phone: Phone, sid: str, provider: str, picked_up: asyncio.Event) -> bool:
    """Run the provider's conversation until someone hangs up. True once the agent has spoken to the person.
    Starts while the phone rings: audio the agent produces before the answer is held back and delivered on answer."""
    down, gate = Resampler(16000, 8000), Gate()
    spoke = False
    held: list[bytes] = []
    t_ring = time.monotonic()
    talk_until = 0.0  # when the agent's audio sent so far finishes playing (monotonic seconds)
    qmap = {q["id"]: q for q in c.get("questions") or []}

    async def deliver(pcm: bytes) -> None:
        nonlocal spoke, talk_until
        spoke = True
        talk_until = max(time.monotonic(), talk_until) + len(pcm) / 2 / 8000
        await phone.ws.send_bytes(struct.pack("<I", 8000) + pcm)

    async def recv() -> dict | None:
        await picked_up.wait()  # nothing from the caller until they answer
        log.info("answered %.1f s after ringing; agent opening was %s", time.monotonic() - t_ring,
                 f"ready ({len(held)} chunks held)" if held else "not ready yet")
        while held:
            await deliver(held.pop(0))
        while True:
            item = await phone.inbox.get()
            if item is None:
                return None
            kind, data = item
            if kind == "audio":
                quiet = gate(data, time.monotonic() < talk_until)
                return {"event": "media", "media": {"payload": base64.b64encode(down(quiet)).decode()}}
            if data.get("type") == "hangup":
                return {"event": "stop"}

    async def send_audio(pcm: bytes) -> None:
        if picked_up.is_set() and not held:
            await deliver(pcm)
        else:
            held.append(pcm)

    async def clear() -> None:
        nonlocal talk_until
        if picked_up.is_set():
            talk_until = 0.0
            await phone.send({"type": "clear"})
        else:
            held.clear()

    def on_outcome(o: str) -> bool:
        return o in OUTCOMES and store.mark_outcome(sid, o, "agent")

    def on_answer(qid: str, option: str) -> bool:
        q = qmap.get(qid)
        return bool(q) and option.isdigit() and 0 < int(option) <= len(q["options"]) and store.add_answer(sid, qid, option)

    try:
        await asyncio.wait_for(catalog.engine(provider)(
            recv, send_audio, clear, {"language": LANGUAGES.get(r["language"], r["language"])}, r["language"], on_outcome,
            instructions=callctx.instructions(c, r), opening=callctx.opening(c, r), questions=list(qmap.values()),
            on_answer=on_answer), MAX_SECONDS + RING_SECONDS)
    except asyncio.TimeoutError:
        log.info("web phone call reached the %d s cap", MAX_SECONDS)
    except Exception as exc:
        log.warning("%s conversation failed: %s", provider, type(exc).__name__)
        try:
            await phone.send({"type": "error", "text": f"{catalog.label(provider)} could not run this call ({type(exc).__name__})."})
        except Exception:
            pass
    return spoke
