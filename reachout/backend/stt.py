"""Voice input for the dashboard assistant: speech -> text, so people can describe an event by
speaking in their own language. The text then goes to the assistant like a typed message.

When a service is configured this is the preferred path: it is more accurate for Indian
languages than the browsers' free recognition (Chrome/Edge), which remains the fallback. The browser sends 16 kHz mono WAV; the
configured providers are tried in order: Sarvam (India), then ElevenLabs (USA). Request and
response formats were written from memory and are UNVERIFIED: check them on the first use.
Transcripts are never logged.
"""
from __future__ import annotations

import logging
import os

import httpx
from fastapi import APIRouter, HTTPException, Request

from . import elevenlabs

log = logging.getLogger("reachout.stt")
router = APIRouter(prefix="/api")

MAX_BYTES = 4_000_000  # about two minutes of 16 kHz mono WAV
SPEECH_LANGS = {"auto", "en-IN", "hi-IN", "mr-IN", "ta-IN", "kn-IN", "ml-IN"}  # auto: the service detects it
PROVIDERS = {
    "sarvam": dict(label="Sarvam", region="India", sends="Your voice goes to Sarvam, India"),
    "elevenlabs": dict(label="ElevenLabs", region="United States", sends="Your voice goes to ElevenLabs, USA"),
}


def available() -> list[str]:
    return [k for k, env in (("sarvam", "SARVAM_API_KEY"), ("elevenlabs", "ELEVENLABS_API_KEY")) if os.getenv(env)]


async def _sarvam(client: httpx.AsyncClient, wav: bytes, lang: str) -> str:
    resp = await client.post("https://api.sarvam.ai/speech-to-text",
                             headers={"api-subscription-key": os.environ["SARVAM_API_KEY"]},
                             files={"file": ("speech.wav", wav, "audio/wav")},
                             data={"model": os.getenv("SARVAM_STT_MODEL", "saarika:v2"),
                                   "language_code": "unknown" if lang == "auto" else lang})
    resp.raise_for_status()
    return resp.json()["transcript"]


async def _elevenlabs(client: httpx.AsyncClient, wav: bytes, lang: str) -> str:
    resp = await client.post(f"{elevenlabs.API}/v1/speech-to-text",
                             headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
                             files={"file": ("speech.wav", wav, "audio/wav")},
                             # No sound-event tags like "(laughter)" in what the assistant reads.
                             data={"model_id": os.getenv("ELEVENLABS_STT_MODEL", "scribe_v1"), "tag_audio_events": "false"}
                             | ({} if lang == "auto" else {"language_code": lang.split("-")[0]}))
    resp.raise_for_status()
    return resp.json()["text"]


@router.get("/assistant/voice")
def voice_options():
    """Server-side recognisers the dashboard can fall back to, with where the audio goes."""
    return {"server": [{"key": k, **PROVIDERS[k]} for k in available()]}


@router.post("/assistant/transcribe")
async def transcribe(request: Request, lang: str = "en-IN"):
    if lang not in SPEECH_LANGS:
        raise HTTPException(400, "Unsupported speech language")
    keys = available()
    if not keys:
        raise HTTPException(503, "Voice input in this browser needs SARVAM_API_KEY or ELEVENLABS_API_KEY on the server. "
                                 "Chrome and Edge work without them.")
    if int(request.headers.get("content-length") or 0) > MAX_BYTES:
        raise HTTPException(413, "That recording is too long; keep it under two minutes")
    wav = await request.body()
    if len(wav) > MAX_BYTES:
        raise HTTPException(413, "That recording is too long; keep it under two minutes")
    if not wav.startswith(b"RIFF"):
        raise HTTPException(400, "Expected WAV audio")
    failed = []
    async with httpx.AsyncClient(timeout=60) as client:
        for k in keys:
            try:
                text = await {"sarvam": _sarvam, "elevenlabs": _elevenlabs}[k](client, wav, lang)
            except Exception as exc:  # network, HTTP or an unexpected reply: try the next provider
                why = f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
                log.warning("speech-to-text via %s failed: %s", k, why)
                failed.append(f"{PROVIDERS[k]['label']} ({why})")
                continue
            return {"text": str(text).strip(), "provider": PROVIDERS[k]["label"], "sends": PROVIDERS[k]["sends"]}
    raise HTTPException(502, f"Could not turn the recording into text: {', '.join(failed)}")
