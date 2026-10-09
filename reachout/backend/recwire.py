"""Records web-phone calls and keeps a history record of each, by watching the phone's WebSocket from outside.

webphone.py (/ws/phone) carries everything the person hears and says, for both the live agent
(ElevenLabs or Gemini) and the keypad IVR, so one tap on that socket covers every kind of call
without touching those modules. A pure ASGI middleware sees each message in both directions:

  server -> page  {"type": "incoming", call_id, campaign, recipient, ...}   a call starts ringing
                  binary <u32 rate><PCM16>        what the person hears, placed where it will actually play
                  {"type": "clear"}               queued audio flushed: drop what had not played yet
                  {"type": "ended"}               the call is over
  page -> server  {"type": "answer"}              picked up (so does any audio, in case that message is missed)
                  {"type": "dtmf" | "hangup" | "decline"}
                  binary PCM16 mono 16 kHz        the microphone, mixed in at 8 kHz

On "incoming" the caller shown on the phone is changed from the recipient's own name to the organisation
calling (the event's org). When the call ends: the audio is saved (recstore), a history record is written
(store.save_call: timings, outcome, answers, recording status) and, for a recorded call, callinsight
pulls out every question asked and every answer given. Whatever goes wrong here is logged and never
reaches the call.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
from array import array
from datetime import datetime, timezone

from . import callinsight, callrec, recstore, store
from .dbcommon import LANGUAGES, mask_phone, public_recipient

log = logging.getLogger("reachout.recwire")
PATH = "/ws/phone"
SETTLE_SECONDS = 1.5  # webphone saves the final outcome just after "ended"
_saving: set[asyncio.Task] = set()  # calls being saved; held here so a closing socket cannot cancel them

# This app's INFO lines (call recorded, answers kept) are otherwise hidden: uvicorn only sets up its own loggers.
_app_log = logging.getLogger("reachout")
if not _app_log.handlers and not logging.getLogger().handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
    _app_log.addHandler(_h)
    _app_log.setLevel(logging.INFO)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _to_8k(pcm: bytes, rate: int) -> bytes:
    """PCM16 mono at `rate` to 8 kHz: pairs averaged for 16 kHz, nearest sample for anything else."""
    a = array("h")
    a.frombytes(pcm[:len(pcm) // 2 * 2])
    if rate == callrec.RATE:
        return pcm[:len(a) * 2]
    if rate == 16000:
        out = array("h", ((a[i] + a[i + 1]) // 2 for i in range(0, len(a) - 1, 2)))
    elif rate > 0:
        out = array("h", (a[min(int(i * rate / callrec.RATE), len(a) - 1)] for i in range(len(a) * callrec.RATE // rate)))
    else:
        return b""
    return out.tobytes()


class _Call:
    """One call on a phone socket, from ringing to its end."""

    def __init__(self, sid: str, info: dict):
        self.sid, self.info = sid, info
        self.notice = info.get("notice")  # a reminder/update call (notices.py): the recipient is named, not "in flight"
        if self.notice:
            self.r = store.recipient(self.notice["recipient_id"])
            self.c = store.campaign(self.notice["campaign_id"])
        else:
            self.r = (store.recipient_by_call(sid) or store.recipient_for_call(sid)) if sid else None
            self.c = store.campaign(self.r["campaign_id"]) if self.r else None
        self.recording = recstore.wanted(self.c) if self.c else recstore.saving()
        self.rec = callrec.CallRecorder() if self.recording else None
        self.rang_at, self.answered_at, self.until = _now_iso(), None, 0.0
        self.keys: list[str] = []
        why = "" if self.r else " (no matching recipient: kept in History only)"
        log.info("call %s ringing: %s%s", sid, "recording" if self.recording else "not recording (recording is off)", why)

    def org(self) -> str:
        e = (self.c or {}).get("event") or {}
        return e.get("org") or (self.c or {}).get("name") or self.info.get("campaign") or "Reachout"

    def answered(self) -> None:
        if not self.answered_at:
            self.answered_at = _now_iso()


class _Tap:
    """State for one phone socket: at most one call is on it at a time."""

    def __init__(self) -> None:
        self.call: _Call | None = None

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    def from_page(self, msg: dict) -> None:
        call = self.call
        if msg.get("type") != "websocket.receive" or call is None:
            return
        if msg.get("bytes") is not None:
            call.answered()  # the page only sends its microphone once the call is answered
            if call.rec is not None:
                call.rec.inbound(_to_8k(msg["bytes"], 16000), self._now())
        elif msg.get("text"):
            event = json.loads(msg["text"])
            kind = event.get("type")
            if kind == "answer":
                call.answered()
            elif kind == "dtmf":
                call.keys.append(str(event.get("digit", ""))[:1])

    def to_page(self, msg: dict) -> dict:
        """Returns the message to send (the "incoming" one is rewritten to show the organisation as the caller)."""
        if msg.get("type") != "websocket.send":
            return msg
        call = self.call
        if msg.get("bytes") is not None:
            data = msg["bytes"]
            if call is not None and call.rec is not None and len(data) > 4:
                pcm = _to_8k(data[4:], struct.unpack("<I", data[:4])[0])
                start = max(call.until, self._now())
                call.rec.outbound(pcm, start)
                call.until = start + len(pcm) / callrec.BYTES_PER_SEC
            return msg
        if not msg.get("text"):
            return msg
        event = json.loads(msg["text"])
        kind = event.get("type")
        if kind == "incoming":
            self.finish()  # a call that never got its "ended"
            self.call = _Call(str(event.get("call_id") or ""), event)
            event = event | {"recipient": self.call.org(), "recipient_name": event.get("recipient")}
            return msg | {"text": json.dumps(event)}
        if kind == "clear" and call is not None and call.rec is not None:
            call.until = self._now()
            call.rec.cut(call.until)
        elif kind == "ended":
            self.finish()
        return msg

    def finish(self) -> None:
        """Save the call (audio, history, answers) in the background, so the phone is never held up."""
        call, self.call = self.call, None
        if call is not None and call.sid:
            task = asyncio.get_running_loop().create_task(_finish(call, _now_iso()))
            _saving.add(task)
            task.add_done_callback(_saving.discard)


async def _finish(call: _Call, ended_at: str) -> None:
    try:
        await asyncio.sleep(SETTLE_SECONDS)
        r = call.r if call.notice else (store.recipient_for_call(call.sid) or (store.recipient(call.r["id"]) if call.r else None))
        if call.c and not store.campaign(call.c["id"]):
            return  # the campaign was deleted meanwhile: keep nothing of this call
        c = call.c or (store.campaign(r["campaign_id"]) if r else None)
        rec_note, stereo, seconds = "Recording is off", None, 0.0
        if call.rec is not None:
            ours, theirs = call.rec.seconds()
            seconds = max(ours, theirs)
            wav = await asyncio.to_thread(call.rec.to_wav) if not call.rec.empty else None
            if not call.answered_at:
                rec_note = "Not answered: nothing to record"
            elif not wav:
                rec_note = "Answered, but no audio passed through the call"
            else:
                why = recstore.save(call.sid, wav, "audio/wav", r["campaign_id"] if r else None, r["id"] if r else None)
                rec_note = why or ""
                if not why:
                    if r and not call.notice:  # a reminder's audio lives in History only; the recipient's own link stays theirs
                        store.set_recording_url(r["id"], recstore.url_for(call.sid))  # only if it has none yet
                    stereo = await asyncio.to_thread(call.rec.to_wav, True)
                    log.info("call %s recorded: %.0f s played to the person, %.0f s heard from them", call.sid, ours, theirs)
        secs = lambda a, b: round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds(), 1) if a and b else None
        e = (c or {}).get("event") or {}
        record = {
            "id": call.sid, "campaign_id": (r or {}).get("campaign_id") or (c or {}).get("id"),
            "campaign_name": (c or {}).get("name") or call.info.get("campaign"), "org": call.org(), "event_title": e.get("title"),
            "recipient_id": (r or {}).get("id"), "recipient_name": (r or {}).get("name") or call.info.get("recipient"),
            "phone": mask_phone(r["phone"]) if r else None,
            "language": LANGUAGES.get((r or {}).get("language"), (r or {}).get("language")) or call.info.get("language"),
            "provider": call.info.get("provider"), "mode": "notice" if call.notice else (call.info.get("mode") or (c or {}).get("mode")),
            "kind": "notice" if call.notice else "call", "notice_id": (call.notice or {}).get("id"), "notice_kind": (call.notice or {}).get("kind"),
            "rang_at": call.rang_at, "answered_at": call.answered_at, "ended_at": ended_at,
            "ring_seconds": secs(call.rang_at, call.answered_at or ended_at), "talk_seconds": secs(call.answered_at, ended_at),
            "outcome": None if call.notice else (r or {}).get("outcome"), "channel": None if call.notice else (r or {}).get("channel"),
            "attempt": None if call.notice else (r or {}).get("attempts"),
            "keys_pressed": [k for k in call.keys if k],
            "answers": {} if call.notice else (public_recipient(r, (c or {}).get("questions") or [])["answers"] if r else {}),
            "recording": {"saved": not rec_note, "note": rec_note, "seconds": round(seconds, 1)},
            "analysis": {"status": "pending" if stereo and callinsight.enabled() else "none"},
            "summary": "", "qa": [], "transcript": [], "final_heard": None,
        }
        store.save_call(record)
        if stereo:
            await callinsight.analyse(call.sid, stereo)
    except Exception:
        log.exception("could not save call %s", call.sid)


class CallRecordingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "websocket" or not str(scope.get("path", "")).rstrip("/").endswith(PATH):  # also behind a path prefix
            return await self.app(scope, receive, send)
        tap = _Tap()

        async def rx():
            msg = await receive()
            try:
                tap.from_page(msg)
            except Exception:
                log.exception("call tap (page -> server) failed")
            return msg

        async def tx(msg):
            try:
                msg = tap.to_page(msg)
            except Exception:
                log.exception("call tap (server -> page) failed")
            await send(msg)

        try:
            await self.app(scope, rx, tx)
        finally:
            tap.finish()  # the page vanished mid-call: keep what was said so far
