"""ElevenLabs: text-to-speech for pre-synthesis, and the Conversational AI agent that answers a
caller who asks a question when the call ends with "any other questions?".

All audio we exchange with Exotel is 16-bit PCM, 8 kHz mono; everything here converts to and
from that. Endpoint paths, the agent WebSocket events and the output formats were written from
memory and are UNVERIFIED: check them on the first real call.

Agent setup (ElevenLabs dashboard), so the bridge below can work:
- Prompt may use the dynamic variables {{org}}, {{title}}, {{when}}, {{venue}}, {{details}}, {{language}} and
  {{answer}} (what the caller chose on the keypad). Leave the first message empty: the caller has already
  started asking, and their first words are passed on, so the agent should answer rather than greet.
- A client tool `record_outcome` with one string parameter `outcome` (confirmed | declined | rescheduled).
  Tell the agent to call it once the caller decides, then to end the call (enable the end_call tool).
- Audio formats: μ-law 8000 Hz in and out is the closest to the phone line; others are converted.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from array import array
from typing import Awaitable, Callable

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedOK

log = logging.getLogger("reachout.elevenlabs")

API = "https://api.elevenlabs.io"
RATE = 8000  # the phone line
AGENT_MAX_SECONDS = int(os.getenv("AGENT_MAX_SECONDS", "300"))  # cost cap per escalated call
# multilingual_v2 covers English, Hindi and Tamil; v3 is used for the other Indian languages.
V2_LANGS = {"en", "hi", "ta"}


def _key() -> str:
    return os.environ["ELEVENLABS_API_KEY"]


def voice_for(lang: str) -> str:
    return os.getenv(f"ELEVENLABS_VOICE_ID_{lang.upper()}") or os.getenv("ELEVENLABS_VOICE_ID", "")


def model_for(lang: str) -> str:
    return (os.getenv(f"ELEVENLABS_MODEL_{lang.upper()}") or os.getenv("ELEVENLABS_MODEL")
            or ("eleven_multilingual_v2" if lang in V2_LANGS else "eleven_v3"))


def tts_ready() -> bool:
    return bool(os.getenv("ELEVENLABS_API_KEY") and os.getenv("ELEVENLABS_VOICE_ID"))


def agent_ready() -> bool:
    return bool(os.getenv("ELEVENLABS_API_KEY") and os.getenv("ELEVENLABS_AGENT_ID"))


# ---------- audio conversion (stdlib only; audioop is gone in Python 3.13) ----------

def _ulaw_to_lin(b: int) -> int:
    b = ~b & 0xFF
    s = (((b & 0x0F) << 3) + 0x84) << ((b >> 4) & 7)
    return 0x84 - s if b & 0x80 else s - 0x84


def _lin_to_ulaw(s: int) -> int:
    sign = 0x80 if s < 0 else 0
    s = min(abs(s), 32635) + 0x84
    exp, mask = 7, 0x4000
    while exp and not s & mask:
        exp, mask = exp - 1, mask >> 1
    return ~(sign | (exp << 4) | ((s >> (exp + 3)) & 0x0F)) & 0xFF


_ULAW_DEC = array("h", (_ulaw_to_lin(i) for i in range(256)))
_ULAW_ENC = bytes(_lin_to_ulaw(i - 32768) for i in range(65536))


def _resample(a: array, src: int, dst: int) -> array:
    if src == dst or not a:
        return a
    if src % dst == 0:  # integer downsample: average each group (a crude low-pass)
        k = src // dst
        return array("h", (sum(a[i:i + k]) // k for i in range(0, len(a) - k + 1, k)))
    n, step = int(len(a) * dst / src), src / dst  # otherwise linear interpolation
    out = array("h", bytes(2 * n))
    for i in range(n):
        x = i * step
        j = int(x)
        nxt = a[j + 1] if j + 1 < len(a) else a[j]
        out[i] = int(a[j] + (nxt - a[j]) * (x - j))
    return out


def _fmt(name: str) -> tuple[str, int]:
    kind, _, rate = name.partition("_")
    return kind, int(rate or RATE)


def to_line(data: bytes, fmt: str) -> bytes:
    """Provider audio (e.g. 'ulaw_8000', 'pcm_16000') -> PCM16 8 kHz."""
    kind, rate = _fmt(fmt)
    if kind == "ulaw":
        a = array("h", (_ULAW_DEC[b] for b in data))
    else:
        a = array("h")
        a.frombytes(data[: len(data) - len(data) % 2])
    return _resample(a, rate, RATE).tobytes()


def from_line(pcm: bytes, fmt: str) -> bytes:
    """PCM16 8 kHz -> provider audio."""
    kind, rate = _fmt(fmt)
    a = array("h")
    a.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    a = _resample(a, RATE, rate)
    return bytes(_ULAW_ENC[s + 32768] for s in a) if kind == "ulaw" else a.tobytes()


# ---------- text-to-speech (batch, before the campaign starts) ----------

async def tts(client: httpx.AsyncClient, text: str, lang: str) -> bytes:
    """One phrase -> PCM16 8 kHz. Retries briefly on rate limits."""
    for attempt in range(4):
        resp = await client.post(
            f"{API}/v1/text-to-speech/{voice_for(lang)}", params={"output_format": "ulaw_8000"},
            headers={"xi-api-key": _key()}, json={"text": text, "model_id": model_for(lang)}, timeout=90)
        if resp.status_code != 429 or attempt == 3:
            resp.raise_for_status()
            return to_line(resp.content, "ulaw_8000")
        await asyncio.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


# ---------- conversational agent (live, only after the caller presses 4) ----------

async def _signed_url() -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{API}/v1/convai/conversation/get-signed-url",
                                params={"agent_id": os.environ["ELEVENLABS_AGENT_ID"]}, headers={"xi-api-key": _key()})
    resp.raise_for_status()
    return resp.json()["signed_url"]


async def bridge(recv: Callable[[], Awaitable[dict | None]], send_audio: Callable[[bytes], Awaitable[None]],
                 clear: Callable[[], Awaitable[None]], variables: dict[str, str], language: str,
                 on_outcome: Callable[[str], bool], preroll: bytes = b"") -> None:
    """Connect the caller to the agent until either side hangs up or AGENT_MAX_SECONDS pass.
    recv() yields Exotel events (None when the call ends); audio is PCM16 8 kHz both ways.
    preroll: what the caller already said before the agent was connected; it is sent first."""
    init: dict = {"type": "conversation_initiation_client_data", "dynamic_variables": variables}
    if os.getenv("ELEVENLABS_AGENT_LANGUAGE_OVERRIDE") == "1":  # needs overrides enabled on the agent
        init["conversation_config_override"] = {"agent": {"language": language}}
    async with connect(await _signed_url(), max_size=None) as agent:
        await agent.send(json.dumps(init))
        fmt = {"in": "pcm_16000", "out": "pcm_16000"}
        ready = asyncio.Event()

        async def caller_to_agent() -> None:
            while (msg := await recv()) is not None:
                if msg.get("event") == "stop":
                    return
                if msg.get("event") == "media" and ready.is_set():
                    pcm = base64.b64decode(msg["media"]["payload"])
                    await agent.send(json.dumps({"user_audio_chunk": base64.b64encode(from_line(pcm, fmt["in"])).decode()}))

        async def agent_to_caller() -> None:
            async for raw in agent:
                m = json.loads(raw)
                t = m.get("type")
                if t == "conversation_initiation_metadata":
                    meta = m.get("conversation_initiation_metadata_event", {})
                    fmt["in"] = meta.get("user_input_audio_format", fmt["in"])
                    fmt["out"] = meta.get("agent_output_audio_format", fmt["out"])
                    log.info("agent conversation started (in=%s out=%s)", fmt["in"], fmt["out"])
                    if preroll:
                        await agent.send(json.dumps({"user_audio_chunk": base64.b64encode(from_line(preroll, fmt["in"])).decode()}))
                    ready.set()
                elif t == "audio":
                    await send_audio(to_line(base64.b64decode(m["audio_event"]["audio_base_64"]), fmt["out"]))
                elif t == "interruption":
                    await clear()
                elif t == "ping":
                    await agent.send(json.dumps({"type": "pong", "event_id": m["ping_event"]["event_id"]}))
                elif t == "client_tool_call":
                    call = m["client_tool_call"]
                    ok = call.get("tool_name") == "record_outcome" and on_outcome(str(call.get("parameters", {}).get("outcome", "")))
                    await agent.send(json.dumps({"type": "client_tool_result", "tool_call_id": call.get("tool_call_id"),
                                                 "result": "saved" if ok else "unknown tool or outcome", "is_error": not ok}))
                # user_transcript / agent_response carry what was said: never logged at INFO.

        tasks = [asyncio.create_task(caller_to_agent()), asyncio.create_task(agent_to_caller())]
        done, pending = await asyncio.wait(tasks, timeout=AGENT_MAX_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            log.info("agent conversation reached the %d s cap", AGENT_MAX_SECONDS)
        for t in pending:
            t.cancel()
        for t in done:
            if t.exception() and not isinstance(t.exception(), ConnectionClosedOK):  # OK = the agent hung up
                log.warning("agent bridge ended with %s", type(t.exception()).__name__)
