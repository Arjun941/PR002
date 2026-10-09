"""Experiment: Asterisk (AudioSocket) <-> Gemini Live API bridge.

Asterisk places/answers the call (e.g. over a Bluetooth phone via chan_mobile) and streams
the call audio to this script over AudioSocket (TCP). We relay it to Gemini Live and play
Gemini's voice back into the call. This is a throwaway test of the voice loop, not Reachout code.

  python bridge.py            # AudioSocket server on :9092 for Asterisk
  python bridge.py --local    # no Asterisk: talk to Gemini with this machine's mic/speaker

Env: GEMINI_API_KEY (required), GEMINI_LIVE_MODEL (default below), PORT (default 9092).
Needs Python <= 3.12 (uses audioop). Local mode also needs: pip install sounddevice numpy
"""
from __future__ import annotations

import argparse
import asyncio
import audioop
import logging
import os
import struct
import sys

from google import genai
from google.genai import types

MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live")
PORT = int(os.getenv("PORT", "9092"))
PROMPT = ("You are Reachout's test voice assistant on a phone call. Greet the caller briefly, "
          "then answer in one or two short sentences. Match the caller's language.")

# AudioSocket frame: 1 byte type, 2 byte big-endian length, payload.
T_HANGUP, T_UUID, T_DTMF, T_AUDIO, T_ERROR = 0x00, 0x01, 0x03, 0x10, 0xFF
CALL_RATE, GEMINI_IN, GEMINI_OUT = 8000, 16000, 24000  # Asterisk slin, Gemini in, Gemini out

log = logging.getLogger("bridge")


def _config() -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=PROMPT,
    )


class Resampler:
    """Stateful audioop.ratecv wrapper (16-bit mono)."""
    def __init__(self, src: int, dst: int):
        self.src, self.dst, self.state = src, dst, None

    def __call__(self, pcm: bytes) -> bytes:
        out, self.state = audioop.ratecv(pcm, 2, 1, self.src, self.dst, self.state)
        return out


async def pump(session, read_audio, write_audio) -> None:
    """Shared loop: caller audio (8 kHz) -> Gemini, Gemini audio (24 kHz) -> caller."""
    up, down = Resampler(CALL_RATE, GEMINI_IN), Resampler(GEMINI_OUT, CALL_RATE)

    async def send():
        while (pcm := await read_audio()) is not None:
            await session.send_realtime_input(
                audio=types.Blob(data=up(pcm), mime_type=f"audio/pcm;rate={GEMINI_IN}"))

    async def recv():
        while True:  # receive() ends after every turn; keep listening for the next one
            async for msg in session.receive():
                sc = msg.server_content
                if not sc:
                    continue
                if sc.interrupted:
                    await write_audio(None)  # caller barged in: drop queued speech
                if sc.model_turn:
                    for part in sc.model_turn.parts:
                        if part.inline_data and part.inline_data.data:
                            await write_audio(down(part.inline_data.data))

    tasks = [asyncio.create_task(send()), asyncio.create_task(recv())]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    for t in done:
        t.result()


# ---------------------------------------------------------------- Asterisk side
async def handle_call(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, client: genai.Client):
    out_q: asyncio.Queue[bytes | None] = asyncio.Queue()
    call_id = "?"

    async def read_frame():
        hdr = await reader.readexactly(3)
        return hdr[0], await reader.readexactly(struct.unpack(">H", hdr[1:])[0])

    async def read_audio():
        while True:
            try:
                kind, payload = await read_frame()
            except asyncio.IncompleteReadError:
                return None
            if kind == T_AUDIO:
                return payload
            if kind == T_DTMF:
                log.info("dtmf %s", payload.decode(errors="replace"))
            elif kind in (T_HANGUP, T_ERROR):
                return None

    async def write_audio(pcm: bytes | None):
        if pcm is None:
            while not out_q.empty():
                out_q.get_nowait()
        else:
            await out_q.put(pcm)

    async def writer_loop():
        # Pace to real time (20 ms frames) so interruption can flush what is still queued.
        buf = b""
        while True:
            buf += await out_q.get() or b""
            while len(buf) >= 320:
                frame, buf = buf[:320], buf[320:]
                writer.write(bytes([T_AUDIO]) + struct.pack(">H", len(frame)) + frame)
                await writer.drain()
                await asyncio.sleep(0.02)

    try:
        kind, payload = await read_frame()
        call_id = payload.hex() if kind == T_UUID else "?"
        log.info("call %s connected", call_id)
        wl = asyncio.create_task(writer_loop())
        try:
            async with client.aio.live.connect(model=MODEL, config=_config()) as session:
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text="The call just connected. Greet the caller.")]),
                    turn_complete=True)
                await pump(session, read_audio, write_audio)
        finally:
            wl.cancel()
    except Exception:
        log.exception("call %s failed", call_id)
    finally:
        writer.close()
        log.info("call %s ended", call_id)


async def serve(client: genai.Client):
    server = await asyncio.start_server(lambda r, w: handle_call(r, w, client), "0.0.0.0", PORT)
    log.info("AudioSocket listening on :%d, model %s", PORT, MODEL)
    async with server:
        await server.serve_forever()


# ------------------------------------------------------------------ local mode
async def local(client: genai.Client):
    import numpy as np
    import sounddevice as sd

    loop = asyncio.get_running_loop()
    inq: asyncio.Queue[bytes] = asyncio.Queue()
    stream_in = sd.InputStream(samplerate=CALL_RATE, channels=1, dtype="int16", blocksize=320,
                               callback=lambda d, *_: loop.call_soon_threadsafe(inq.put_nowait, d.tobytes()))
    stream_out = sd.OutputStream(samplerate=CALL_RATE, channels=1, dtype="int16")

    async def write_audio(pcm: bytes | None):
        if pcm:
            await asyncio.to_thread(stream_out.write, np.frombuffer(pcm, dtype="int16"))

    with stream_in, stream_out:
        async with client.aio.live.connect(model=MODEL, config=_config()) as session:
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text="Greet the user.")]), turn_complete=True)
            log.info("local mode: speak to Gemini (use headphones); Ctrl+C to stop")
            await pump(session, inq.get, write_audio)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true", help="use mic/speaker instead of Asterisk")
    args = ap.parse_args()
    if not os.getenv("GEMINI_API_KEY"):
        sys.exit("Set GEMINI_API_KEY")
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    try:
        asyncio.run(local(client) if args.local else serve(client))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
