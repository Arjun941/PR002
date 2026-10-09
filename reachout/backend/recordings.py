"""Recording playback behind access control.

Off unless RECORDINGS_PIN is set. The PIN unlocks a short signed session (httpOnly cookie,
15 minutes). Audio is proxied through this server so the browser never sees the provider
URL, and every unlock, failed PIN and play is written to access_log.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import os
import secrets
import time
import wave
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from . import exotel, store
from .voicebot import SAMPLE_RATE, tone

router = APIRouter()

COOKIE = "ro_rec"
TTL = 15 * 60
MAX_FAILS, LOCKOUT = 5, 300
_SECRET = os.getenv("SESSION_SECRET", "").encode() or secrets.token_bytes(32)  # random: sessions end on restart
_fails: list[float] = []


def enabled() -> bool:
    return bool(os.getenv("RECORDINGS_PIN"))


def _sign(exp: int) -> str:
    return f"{exp}.{hmac.new(_SECRET, str(exp).encode(), hashlib.sha256).hexdigest()}"


def _valid(token: str | None) -> bool:
    exp = (token or "").split(".", 1)[0]
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(token, _sign(int(exp)))


def _client(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/api/auth/recordings")
def access_state(request: Request):
    return {"enabled": enabled(), "unlocked": enabled() and _valid(request.cookies.get(COOKIE))}


class Unlock(BaseModel):
    pin: str


@router.post("/api/auth/recordings")
def unlock(body: Unlock, request: Request, response: Response):
    if not enabled():
        raise HTTPException(403, "Recording playback is off. Set RECORDINGS_PIN to turn it on.")
    now = time.time()
    _fails[:] = [t for t in _fails if now - t < LOCKOUT]
    if len(_fails) >= MAX_FAILS:
        raise HTTPException(429, "Too many wrong PINs. Try again in a few minutes.")
    if not hmac.compare_digest(body.pin.encode(), os.environ["RECORDINGS_PIN"].encode()):
        _fails.append(now)
        store.log_access("recordings.unlock_failed", None, _client(request))
        raise HTTPException(401, "Wrong PIN")
    store.log_access("recordings.unlock", None, _client(request))
    response.set_cookie(COOKIE, _sign(int(now) + TTL), max_age=TTL, httponly=True, samesite="strict", path="/api")
    return {"unlocked": True, "expires_in": TTL}


@router.delete("/api/auth/recordings")
def lock(response: Response):
    response.delete_cookie(COOKIE, path="/api")
    return {"unlocked": False}


def _demo_audio() -> bytes:
    """Seeded demo recipients have no real audio: a short tone stands in."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(tone(440, 300, 100) + tone(550, 300, 100) + tone(660, 600))
    return buf.getvalue()


@router.get("/api/recordings/{rid}")
async def play(rid: str, request: Request):
    if not enabled() or not _valid(request.cookies.get(COOKIE)):
        raise HTTPException(401, "Unlock recordings first")
    r = store.recipient(rid)
    if not r or not r["recording_url"]:
        raise HTTPException(404, "No recording")
    store.log_access("recordings.play", rid, _client(request))
    url = r["recording_url"]
    if url == "demo":
        return Response(_demo_audio(), media_type="audio/wav", headers={"Cache-Control": "no-store"})
    host = urlparse(url).hostname or ""
    if urlparse(url).scheme != "https":
        raise HTTPException(502, "Recording URL is not https")
    # Only Exotel's own hosts get our API credentials; recordings on S3 links are fetched without them.
    auth = exotel.auth() if exotel.configured() and (host == "exotel.com" or host.endswith((".exotel.com", ".exotel.in"))) else None
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url, auth=auth)
    if resp.status_code != 200:
        raise HTTPException(502, "Could not fetch the recording from Exotel")
    return Response(resp.content, media_type=resp.headers.get("content-type", "audio/mpeg"),
                    headers={"Cache-Control": "no-store"})
