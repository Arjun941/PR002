"""Pre-synthesised IVR audio (hybrid mode). Every script phrase is synthesised once per language and every
recipient name once, then cached on disk as PCM16 8 kHz. Calls only read from the cache, so no voice is
generated during the keypad part of a call. Only ElevenLabs does it (catalog capability "voice").

The cache is content-addressed (provider, voice, model, text), so identical phrases and names are
synthesised once across campaigns, and resuming after a failure only does what is missing. Builder previews
and the campaign page's Listen buttons read and fill the same cache. Resynthesising overwrites in place.
Name audio is personal data; Phase 6 adds retention for this folder.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import wave
from pathlib import Path

import httpx

from . import catalog, elevenlabs, store

log = logging.getLogger("reachout.audio")

AUDIO_DIR = Path(os.getenv("AUDIO_DIR", "audio"))
SYNTH_CONCURRENCY = int(os.getenv("ELEVENLABS_CONCURRENCY", "2"))
RATE = 8000
PAUSE = b"\x00\x00" * (RATE * 250 // 1000)  # 250 ms between parts of a prompt
SYNTH = {"elevenlabs": elevenlabs.tts}  # IVR audio is always made with ElevenLabs
_active: set[str] = set()


def ready(voice: str) -> bool:
    return voice in SYNTH and catalog.ready(voice, "voice")


def choose_voice(c: dict) -> str | None:
    """IVR audio is always ElevenLabs (None if it is not set up), whatever provider runs the conversation."""
    return "elevenlabs" if ready("elevenlabs") else None


def _ident(voice: str, lang: str) -> str:
    return f"{elevenlabs.voice_for(lang)}|{elevenlabs.model_for(lang)}"


def _path(voice: str, lang: str, text: str) -> Path:
    k = hashlib.sha256(f"{voice}|{_ident(voice, lang)}|{text}".encode()).hexdigest()
    return AUDIO_DIR / "cache" / k[:2] / f"{k}.pcm"


def load(voice: str, lang: str, text: str) -> bytes:
    p = _path(voice, lang, text)
    return p.read_bytes() if text.strip() and p.exists() else b""


def _store(voice: str, lang: str, text: str, pcm: bytes) -> None:
    p = _path(voice, lang, text)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(pcm)
    tmp.replace(p)


def wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


async def synth_text(voice: str, lang: str, text: str, force: bool = False) -> bytes:
    """One phrase from the cache, or synthesised now (and cached). Used for previews."""
    if not force and (pcm := load(voice, lang, text)):
        return pcm
    async with httpx.AsyncClient() as client:
        pcm = await SYNTH[voice](client, text, lang)
    _store(voice, lang, text, pcm)
    return pcm


def phrases(script: dict) -> dict[str, str]:
    """The pieces a call is assembled from; the greeting is split around {name}."""
    pre, _, post = script["greeting"].partition("{name}")
    return {"greeting_pre": pre.strip(), "greeting_post": post.strip(), "message": script["message"],
            "menu": script["menu"], "voicemail": script["voicemail"], "goodbye": script["goodbye"],
            "doubts": script.get("doubts") or ""} | {
        f"q:{qid}": text for qid, text in (script.get("questions") or {}).items()}


def call_audio(c: dict, r: dict) -> dict[str, bytes] | None:
    """Everything one call plays, from the cache. None if the campaign has no audio yet."""
    if not c.get("audio_ready") or r["language"] not in c["scripts"]:
        return None
    lang, voice = r["language"], c["voice"]
    p = {k: load(voice, lang, t) for k, t in phrases(c["scripts"][lang]).items()}
    greeting = b"".join(x for x in (p["greeting_pre"], load(voice, lang, r["name"]), p["greeting_post"]) if x)
    return {"intro": greeting + PAUSE + p["message"] + PAUSE, "menu": p["menu"],
            "voicemail": p["voicemail"], "goodbye": p["goodbye"], "doubts": p["doubts"],
            "questions": {k[2:]: v for k, v in p.items() if k.startswith("q:")}}


def listen_keys(script: dict, questions: list[dict]) -> list[str]:
    """The phrases a person can listen to on the campaign page, in call order."""
    return ["greeting", "message", "menu", "voicemail", "goodbye"] + (["doubts"] if script.get("doubts") else []) + [
        f"q:{q['id']}" for q in questions if (script.get("questions") or {}).get(q["id"])]


def phrase_audio(c: dict, lang: str, key: str, name: str = "") -> bytes:
    """One listenable phrase from the cache (the greeting with a recipient's name if that is cached too)."""
    voice, script = c.get("voice"), (c.get("scripts") or {}).get(lang)
    if not voice or not script:
        return b""
    ph = phrases(script)
    if key == "greeting":
        pre, post = ph["greeting_pre"], ph["greeting_post"]
        parts = [load(voice, lang, pre) if pre else b"", load(voice, lang, name) if name else b"",
                 load(voice, lang, post) if post else b""]
        have = [x for x in parts if x]
        return b"".join(have) if have and (not pre or parts[0]) and (not post or parts[2]) else b""
    text = ph.get(key, "")
    return load(voice, lang, text) if text else b""


def audio_status(c: dict) -> dict[str, dict[str, bool]]:
    """Which phrases of each language have synthesised audio (for the Listen buttons)."""
    out: dict[str, dict[str, bool]] = {}
    for lang, script in (c.get("scripts") or {}).items():
        out[lang] = {k: bool(phrase_audio(c, lang, k)) for k in listen_keys(script, c.get("questions") or [])}
    return out


async def synthesize(cid: str, force: bool = False) -> str | None:
    """Synthesise every phrase and recipient name of a campaign (only what is missing, or everything with
    force). Returns an error message, or None when the audio is complete. Does not touch the campaign status."""
    c = store.campaign(cid)
    if not c:
        return "No such campaign"
    voice = choose_voice(c)
    if not voice:
        return "IVR audio uses ElevenLabs: set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID."
    todo = {(l, t) for l, s in c["scripts"].items() for t in phrases(s).values() if t.strip()}
    todo |= {(r["language"], r["name"]) for r in store.recipients(cid)}
    todo = sorted(x for x in todo if force or not _path(voice, *x).exists())
    total, done = len(todo), 0
    sem = asyncio.Semaphore(SYNTH_CONCURRENCY)
    log.info("campaign %s: synthesising %d phrases with %s", cid, total, voice)

    async def one(client: httpx.AsyncClient, lang: str, text: str) -> None:
        nonlocal done
        async with sem:
            pcm = await SYNTH[voice](client, text, lang)
        _store(voice, lang, text, pcm)
        done += 1
        if done % 10 == 0:
            store.set_note(cid, f"Synthesising IVR audio: {done} of {total} phrases")

    tasks: list[asyncio.Task] = []
    try:
        async with httpx.AsyncClient() as client:
            tasks = [asyncio.create_task(one(client, l, t)) for l, t in todo]
            await asyncio.gather(*tasks)
    except Exception as exc:
        for t in tasks:  # the first failure stops the rest
            t.cancel()
        why = (f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError)
               else "quota exceeded, try again in a minute" if "RESOURCE_EXHAUSTED" in str(exc) or "429" in str(exc)
               else type(exc).__name__)
        log.error("campaign %s: voice synthesis failed (%s)", cid, why)
        return f"Voice synthesis failed ({why}). Check the {catalog.label(voice)} key, voice and quota, then try again."
    store.update_campaign(cid, {"voice": voice, "audio_ready": True, "note": None})
    log.info("campaign %s: IVR audio ready (%s)", cid, voice)
    return None


async def prepare(cid: str) -> None:
    """A hybrid campaign waiting to start: synthesise, then run (or pause with the reason)."""
    err = await synthesize(cid)
    if err:
        store.pause_preparing(cid, err)
    else:
        store.finish_audio(cid)


def start(cid: str, force: bool = False) -> bool:
    """Synthesise in the background (the Resynthesize button). False if one is already running for it."""
    if cid in _active:
        return False
    _active.add(cid)

    async def job() -> None:
        try:
            err = await synthesize(cid, force)
            if err:
                store.set_note(cid, err)
        finally:
            _active.discard(cid)

    asyncio.get_running_loop().create_task(job())
    return True


async def run() -> None:
    """Picks up campaigns waiting for audio, including after a restart."""
    while True:
        await asyncio.sleep(2)
        try:
            for c in store.campaigns():
                if c["status"] == "preparing" and c["id"] not in _active:
                    _active.add(c["id"])
                    task = asyncio.create_task(prepare(c["id"]))
                    task.add_done_callback(lambda _, cid=c["id"]: _active.discard(cid))
        except Exception:
            log.exception("audio loop failed")
