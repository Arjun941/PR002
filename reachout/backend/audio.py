"""Pre-synthesised call audio. Before a live campaign starts ("preparing"), every script phrase
is synthesised once per language and every recipient name once, then cached on disk as PCM16
8 kHz. Calls only read from the cache, so no voice is generated during a keypad call.

The cache is content-addressed (provider, voice, model, text), so identical phrases and names
are synthesised once across campaigns, and resuming after a failure only does what is missing.
Name audio is personal data; Phase 6 adds retention for this folder.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path

import httpx

from . import elevenlabs, store

log = logging.getLogger("reachout.audio")

AUDIO_DIR = Path(os.getenv("AUDIO_DIR", "audio"))
SYNTH_CONCURRENCY = int(os.getenv("ELEVENLABS_CONCURRENCY", "2"))
PAUSE = b"\x00\x00" * (elevenlabs.RATE * 250 // 1000)  # 250 ms between parts of a prompt
SYNTH = {"elevenlabs": elevenlabs.tts}  # voice providers that can produce call audio today
_active: set[str] = set()


def ready(voice: str) -> bool:
    return voice == "elevenlabs" and elevenlabs.tts_ready()


def _path(voice: str, lang: str, text: str) -> Path:
    k = hashlib.sha256(f"{voice}|{elevenlabs.voice_for(lang)}|{elevenlabs.model_for(lang)}|{text}".encode()).hexdigest()
    return AUDIO_DIR / "cache" / k[:2] / f"{k}.pcm"


def load(voice: str, lang: str, text: str) -> bytes:
    p = _path(voice, lang, text)
    return p.read_bytes() if text.strip() and p.exists() else b""


def phrases(script: dict) -> dict[str, str]:
    """The pieces a call is assembled from; the greeting is split around {name}."""
    pre, _, post = script["greeting"].partition("{name}")
    return {"greeting_pre": pre.strip(), "greeting_post": post.strip(), "message": script["message"],
            "menu": script["menu"], "voicemail": script["voicemail"], "goodbye": script["goodbye"]}


def call_audio(c: dict, r: dict) -> dict[str, bytes] | None:
    """Everything one call plays, from the cache. None if the campaign has no audio yet."""
    if not c.get("audio_ready") or r["language"] not in c["scripts"]:
        return None
    lang, voice = r["language"], c["voice"]
    p = {k: load(voice, lang, t) for k, t in phrases(c["scripts"][lang]).items()}
    greeting = b"".join(x for x in (p["greeting_pre"], load(voice, lang, r["name"]), p["greeting_post"]) if x)
    return {"intro": greeting + PAUSE + p["message"] + PAUSE, "menu": p["menu"],
            "voicemail": p["voicemail"], "goodbye": p["goodbye"]}


async def prepare(cid: str) -> None:
    c = store.campaign(cid)
    if not c:
        return
    voice = c["voice"]
    if not ready(voice):
        with store.tx() as db:
            db.execute("UPDATE campaigns SET status = 'paused', note = ? WHERE id = ? AND status = 'preparing'",
                       ("ElevenLabs is not configured: set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID, then resume."
                        if voice in SYNTH else "This campaign's voice provider cannot produce call audio. "
                        "Create the campaign again with ElevenLabs.", cid))
        return
    todo = {(l, t) for l, s in c["scripts"].items() for t in phrases(s).values() if t.strip()}
    todo |= {(r["language"], r["name"]) for r in store.recipients(cid)}
    todo = sorted(x for x in todo if not _path(voice, *x).exists())
    total, done = len(todo), 0
    sem = asyncio.Semaphore(SYNTH_CONCURRENCY)
    log.info("campaign %s: synthesising %d phrases with %s", cid, total, voice)

    async def one(client: httpx.AsyncClient, lang: str, text: str) -> None:
        nonlocal done
        async with sem:
            pcm = await SYNTH[voice](client, text, lang)
        p = _path(voice, lang, text)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(pcm)
        tmp.replace(p)
        done += 1
        if done % 20 == 0:
            _note(cid, f"Preparing voice: {done} of {total} phrases")

    try:
        async with httpx.AsyncClient() as client, asyncio.TaskGroup() as tg:  # first failure cancels the rest
            for l, t in todo:
                tg.create_task(one(client, l, t))
    except Exception as group:
        exc = group.exceptions[0] if isinstance(group, ExceptionGroup) else group
        why = f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
        log.error("campaign %s: voice synthesis failed (%s)", cid, why)
        with store.tx() as db:
            db.execute("UPDATE campaigns SET status = 'paused', note = ? WHERE id = ? AND status = 'preparing'",
                       (f"Voice synthesis failed ({why}). Check the ElevenLabs key, voice and quota, then resume.", cid))
        return
    with store.tx() as db:
        db.execute("UPDATE campaigns SET audio_ready = 1, note = NULL WHERE id = ?", (cid,))
        db.execute("UPDATE campaigns SET status = 'running' WHERE id = ? AND status = 'preparing'", (cid,))
    log.info("campaign %s: voice ready", cid)


def _note(cid: str, note: str) -> None:
    with store.tx() as db:
        db.execute("UPDATE campaigns SET note = ? WHERE id = ?", (note, cid))


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
