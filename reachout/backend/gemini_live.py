"""Gemini Live as the key-4 assistant: a drop-in alternative to the ElevenLabs agent.

Same contract as elevenlabs.bridge(): connect the caller to the agent until either side hangs up.
Audio is PCM16 8 kHz mono on the telephony side; Gemini takes 16 kHz and answers at 24 kHz, so we
resample both ways. Gemini's own voice-activity detection decides turns and tells us when the
caller barges in (`interrupted`), at which point the queued telephony audio is cleared. The agent
can save the outcome with a `record_outcome` tool, as the ElevenLabs agent does.

Needs GEMINI_API_KEY. GEMINI_LIVE_MODEL picks the model (default gemini-3.8-live: ~1 s to first
audio in our tests; the 3.1 preview model was closing sessions with a 1011 error, the 2.5 native-audio
model took 7-14 s). The caller's words are never logged at INFO.
"""
from __future__ import annotations

import array
import asyncio
import base64
import logging
import os
import time
from typing import Awaitable, Callable

try:  # removed in Python 3.13; the pure-Python fallback below is slower but fine for 20 ms frames
    import audioop
except ImportError:  # pragma: no cover
    audioop = None

log = logging.getLogger("reachout.gemini_live")

MODEL_DEFAULT = "gemini-3.8-live"
OUTCOMES = ("confirmed", "declined", "rescheduled")


def model() -> str:
    return os.getenv("GEMINI_LIVE_MODEL") or MODEL_DEFAULT


def ready() -> bool:
    return bool(os.getenv("GEMINI_API_KEY"))


class Resampler:
    """Streaming 16-bit mono resampler between whole-number-friendly rates (8k/16k/24k)."""

    def __init__(self, src: int, dst: int):
        self.src, self.dst, self.state = src, dst, None
        self._tail = 0

    def __call__(self, pcm: bytes) -> bytes:
        if audioop:
            out, self.state = audioop.ratecv(pcm, 2, 1, self.src, self.dst, self.state)
            return out
        a = array.array("h", pcm[:len(pcm) - len(pcm) % 2])
        if self.dst > self.src:  # upsample by linear interpolation (8k -> 16k)
            k, out = self.dst // self.src, array.array("h")
            for i, s in enumerate(a):
                nxt = a[i + 1] if i + 1 < len(a) else s
                out.extend(int(s + (nxt - s) * j / k) for j in range(k))
            return out.tobytes()
        k = self.src // self.dst  # downsample by averaging (24k -> 8k)
        return array.array("h", (sum(a[i:i + k]) // k for i in range(0, len(a) - k + 1, k))).tobytes()


def system_prompt(variables: dict[str, str], language: str) -> str:
    when = variables.get("when", "")
    facts = "\n".join(f"- {k}: {v}" for k, v in (
        ("Organisation", variables.get("org")), ("About", variables.get("title")), ("When", when),
        ("Where", variables.get("venue")), ("Details", variables.get("details"))) if v)
    return (
        "You are a phone assistant for an organisation that has just called someone with an automated "
        "message. The person pressed 4 to speak to you. Be warm, brief and natural, like a helpful receptionist: "
        "one or two short sentences at a time, no lists, no markdown. Let the caller talk.\n"
        f"Reply in {variables.get('language') or language}, and switch if the caller does.\n"
        f"What the call was about:\n{facts or '- (no details given)'}\n"
        "Answer questions using only these facts; if you do not know, say someone will follow up. "
        "When the caller clearly confirms they will attend or pay, declines, or wants to reschedule, call "
        "the record_outcome tool with confirmed, declined or rescheduled, then say a short goodbye. "
        "Start by saying hello and asking how you can help."
    )


async def bridge(recv: Callable[[], Awaitable[dict | None]], send_audio: Callable[[bytes], Awaitable[None]],
                 clear: Callable[[], Awaitable[None]], variables: dict[str, str], language: str,
                 on_outcome: Callable[[str], bool]) -> None:
    """recv() yields Exotel events (None when the call ends); audio is PCM16 8 kHz both ways."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=system_prompt(variables, language),
        tools=[types.Tool(function_declarations=[types.FunctionDeclaration(
            name="record_outcome", description="Save the caller's answer to the invitation or reminder.",
            parameters=types.Schema(type="OBJECT", properties={
                "outcome": types.Schema(type="STRING", enum=list(OUTCOMES))}, required=["outcome"]))])],
    )
    up, down = Resampler(8000, 16000), Resampler(24000, 8000)
    async with client.aio.live.connect(model=model(), config=config) as session:
        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text="The caller just pressed 4. Greet them.")]),
            turn_complete=True)
        log.info("gemini live assistant connected (%s)", model())

        async def caller_to_agent() -> None:
            while (msg := await recv()) is not None:
                if msg.get("event") == "stop":
                    return
                if msg.get("event") == "media":
                    pcm = up(base64.b64decode(msg["media"]["payload"]))
                    await session.send_realtime_input(audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000"))

        async def agent_to_caller() -> None:
            while True:  # receive() ends after every turn; keep listening for the next one
                async for m in session.receive():
                    if m.tool_call:
                        responses = []
                        for fc in m.tool_call.function_calls:
                            ok = fc.name == "record_outcome" and on_outcome(str((fc.args or {}).get("outcome", "")))
                            responses.append(types.FunctionResponse(
                                id=fc.id, name=fc.name, response={"result": "saved" if ok else "unknown tool or outcome"}))
                        await session.send_tool_response(function_responses=responses)
                    sc = m.server_content
                    if not sc:
                        continue
                    if sc.interrupted:
                        await clear()
                    if sc.model_turn:
                        for part in sc.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                await send_audio(down(part.inline_data.data))

        tasks = [asyncio.create_task(caller_to_agent()), asyncio.create_task(agent_to_caller())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            if t.exception():
                log.warning("gemini live bridge ended with %s", type(t.exception()).__name__)


async def check() -> dict:
    """Open a session, ask for a one-word reply, report the time to first audio. For the Providers page."""
    from google import genai
    from google.genai import types

    t0 = time.monotonic()
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    try:
        async with client.aio.live.connect(model=model(), config=types.LiveConnectConfig(response_modalities=["AUDIO"])) as s:
            connected = time.monotonic() - t0
            await s.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text="Say hello.")]), turn_complete=True)
            t1 = time.monotonic()

            async def first_audio() -> None:
                async for m in s.receive():
                    if m.server_content and m.server_content.model_turn:
                        return

            await asyncio.wait_for(first_audio(), 20)
            return {"ok": True, "model": model(), "connect_ms": round(connected * 1000),
                    "first_audio_ms": round((time.monotonic() - t1) * 1000)}
    except asyncio.TimeoutError:
        return {"ok": False, "model": model(), "error": "Connected, but no audio came back within 20 s"}
    except Exception as exc:
        return {"ok": False, "model": model(), "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
