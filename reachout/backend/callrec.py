"""Mixes the two sides of a call into one recording.

`recwire` feeds it both directions of a web-phone call: the audio played to the person (placed on
the timeline where it will actually be heard) and the person's microphone as it arrives. This keeps both, mixes them into one mono 16-bit 8 kHz WAV and hands it to
`recstore`. A key press that cuts a prompt short also cuts it in the recording.

Not recorded: anything after the phone page disconnects, and anything before it answers.
"""
from __future__ import annotations

import io
import sys
import wave
from array import array

RATE = 8000
BYTES_PER_SEC = RATE * 2
MAX_SECONDS = 15 * 60  # also keeps the WAV under recstore.MAX_BYTES (15 MiB)


class CallRecorder:
    def __init__(self) -> None:
        self._out: list[list] = []  # [start, pcm] we played, start = when it begins to be heard
        self._in: list[tuple[float, bytes]] = []
        self._in_end = 0.0

    def outbound(self, pcm: bytes, start: float) -> None:
        if pcm:
            self._out.append([start, pcm])

    def inbound(self, pcm: bytes, now: float) -> None:
        """Caller audio that has just arrived (it ends `now`); chunks are kept back to back."""
        if not pcm:
            return
        dur = len(pcm) / BYTES_PER_SEC
        start = max(self._in_end, now - dur)
        self._in.append((start, pcm))
        self._in_end = start + dur

    def cut(self, now: float) -> None:
        """The queued prompts were flushed at `now`: drop whatever had not played yet."""
        kept = []
        for start, pcm in self._out:
            if start >= now:
                continue
            keep = int((now - start) * BYTES_PER_SEC) // 2 * 2
            if keep < len(pcm):
                pcm = pcm[:keep]
            if pcm:
                kept.append([start, pcm])
        self._out = kept

    @property
    def empty(self) -> bool:
        return not self._out and not self._in

    def seconds(self) -> tuple[float, float]:
        """(seconds of our audio, seconds of caller audio) kept, for the log."""
        return (sum(len(p) for _, p in self._out) / BYTES_PER_SEC, sum(len(p) for _, p in self._in) / BYTES_PER_SEC)

    def to_wav(self, stereo: bool = False) -> bytes | None:
        """Both sides on one timeline: mixed to mono (summed, clipped), or stereo with our side on the left and
        the person on the right (for transcription, so it is always clear who said what). None if nothing was captured."""
        tracks = [[(s, p) for s, p in self._out if p], [(s, p) for s, p in self._in if p]]
        parts = tracks[0] + tracks[1]
        if not parts:
            return None
        t0 = min(s for s, _ in parts)
        total = min(int(max((s - t0) * RATE + len(p) // 2 for s, p in parts)), MAX_SECONDS * RATE)
        mixes = [[0] * total for _ in (tracks if stereo else [parts])]
        for mix, track in zip(mixes, tracks if stereo else [parts]):
            for start, pcm in track:
                samples = array("h")
                samples.frombytes(pcm[:len(pcm) // 2 * 2])
                if sys.byteorder == "big":
                    samples.byteswap()
                off = int((start - t0) * RATE)
                for i, v in enumerate(samples[:max(0, total - off)]):
                    mix[off + i] += v
        clip = lambda v: -32768 if v < -32768 else 32767 if v > 32767 else v
        out = array("h", (clip(v) for frame in zip(*mixes) for v in frame))
        if sys.byteorder == "big":
            out.byteswap()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(len(mixes))
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(out.tobytes())
        return buf.getvalue()
