"""MongoDB backend, used when MONGODB_URI is set (see `store`). Same operations as the SQLite
backend, so the dialer, builder and dashboard do not know which one they run on.

Collections: campaigns (event, scripts, languages, retry policy and questions are native
documents), recipients (one per phone number; `seq` keeps insertion order), calls (one per
finished attempt, for the daily chart) and access_log. `_id` is the campaign/recipient id.

No multi-document transactions (they need a replica set): a new campaign's recipients are written
first and the campaign document last, so a half-written campaign is never listed or dialled.
Each recipient is claimed with one atomic find-and-update, so two dialer ticks cannot take the same
number. Phone numbers are stored in clear text here: use your provider's encryption at rest
(Atlas has it on by default) until the app encrypts them itself (Phase 6).
"""
from __future__ import annotations

import json
import os
import re
import time

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument

from .dbcommon import ANSWERED, BOOL_FIELDS, ago, missing_questions, now_iso

_CAMPAIGN_DEFAULTS = {"voice": "", "audio_ready": False, "note": None, "questions": [], "sim": {},
                      "event": {}, "scripts": {}, "retry": {}, "segments": [], "languages": [],
                      "agent_provider": "elevenlabs", "provider": "", "telephony": "webphone", "system_prompt": "", "mode": "live", "chat": [], "chat_summary": ""}
_QID = re.compile(r"[\w-]+")  # becomes part of a field path, so no dots or operators
_DUE_RETRY = ("voicemail", "no_answer")


def _campaign(doc: dict) -> dict:
    c = _CAMPAIGN_DEFAULTS | doc
    c["id"] = c.pop("_id")
    for k in BOOL_FIELDS:
        c[k] = bool(c.get(k))
    return c


def _recipient(doc: dict) -> dict:
    r = dict(doc)
    r["id"] = r.pop("_id")
    r.pop("seq", None)
    r["answers"] = json.dumps(r.get("answers") or {})  # callers read it as JSON, as with SQLite
    return r


class MongoStore:
    name = "MongoDB"

    def __init__(self, uri: str | None = None, db_name: str | None = None, client=None):
        self.client = client or MongoClient(uri or os.environ["MONGODB_URI"], serverSelectionTimeoutMS=5000)
        if db_name or os.getenv("MONGODB_DB"):
            self.db = self.client[db_name or os.environ["MONGODB_DB"]]
        else:
            self.db = self.client.get_default_database(default="reachout")
        self.campaigns_c, self.recipients_c = self.db["campaigns"], self.db["recipients"]
        self.calls_c, self.access_c = self.db["calls"], self.db["access_log"]
        self.audio_c = self.db["recording_audio"]  # one document per call, keyed by call id
        self.notices_c = self.db["notices"]  # reminders and updates, with their targets and progress
        self.history_c = self.db["call_history"]  # one document per call: timings, outcome, answers, transcript

    def init(self) -> None:
        self.client.admin.command("ping")  # fail at start-up, with a clear error, if MongoDB is unreachable
        self.campaigns_c.create_index([("started_at", DESCENDING)])
        self.recipients_c.create_index([("campaign_id", ASCENDING), ("seq", ASCENDING)])
        self.recipients_c.create_index("call_sid")
        self.recipients_c.create_index("in_flight")
        self.calls_c.create_index("at")
        self.audio_c.create_index("saved_at")  # also creates the recording_audio collection up front
        self.audio_c.create_index("campaign_id")
        self.history_c.create_index([("rang_at", DESCENDING)])
        self.history_c.create_index("campaign_id")
        self.notices_c.create_index("campaign_id")
        self.notices_c.create_index("status")

    def _log(self, r: dict, outcome: str, at: str | None = None) -> None:
        self.calls_c.insert_one({"campaign_id": r["campaign_id"], "recipient_id": r["id"], "call_sid": r["call_sid"],
                                 "at": at or now_iso(), "answered": int(outcome in ANSWERED), "outcome": outcome})

    # ---------- reads ----------

    def has_campaigns(self) -> bool:
        return self.campaigns_c.find_one({}, {"_id": 1}) is not None

    def campaigns(self) -> list[dict]:
        return [_campaign(d) for d in self.campaigns_c.find().sort("started_at", DESCENDING)]

    def campaign(self, cid: str) -> dict | None:
        d = self.campaigns_c.find_one({"_id": cid})
        return _campaign(d) if d else None

    def recipients(self, cid: str | None = None) -> list[dict]:
        return [_recipient(d) for d in self.recipients_c.find({"campaign_id": cid} if cid else {}).sort("seq", ASCENDING)]

    def recipient(self, rid: str) -> dict | None:
        d = self.recipients_c.find_one({"_id": rid})
        return _recipient(d) if d else None

    def recipient_by_call(self, call_sid: str) -> dict | None:
        d = self.recipients_c.find_one({"call_sid": call_sid, "in_flight": 1})
        return _recipient(d) if d else None

    def pending_recipients(self, cid: str) -> list[dict]:
        return [_recipient(d) for d in self.recipients_c.find({"campaign_id": cid, "outcome": "pending"}).sort("seq", ASCENDING)]

    def calls_since(self, iso: str) -> list[dict]:
        return list(self.calls_c.find({"at": {"$gte": iso}}, {"_id": 0, "campaign_id": 1, "at": 1, "answered": 1}))

    # ---------- campaigns ----------

    def insert_campaign(self, c: dict, recs: list[dict], calls: list[dict] = ()) -> None:
        base = time.time_ns()
        if recs:
            self.recipients_c.insert_many([
                {k: v for k, v in r.items() if k not in ("id", "answers")} | {
                    "_id": r["id"], "seq": base + i,
                    "answers": json.loads(r["answers"]) if isinstance(r.get("answers"), str) else (r.get("answers") or {})}
                for i, r in enumerate(recs)])
        if calls:
            self.calls_c.insert_many([dict(x) for x in calls])
        doc = {k: v for k, v in c.items() if k != "id"} | {"_id": c["id"]}
        for k in BOOL_FIELDS:
            if k in doc:
                doc[k] = bool(doc[k])
        self.campaigns_c.insert_one(doc)

    def set_status(self, cid: str, status: str) -> None:
        self.campaigns_c.update_one({"_id": cid}, {"$set": {"status": status}})

    def delete_campaign(self, cid: str) -> bool:
        """The campaign, its recipients and its call log. False if there was no such campaign."""
        self.calls_c.delete_many({"campaign_id": cid})
        ids = [d["_id"] for d in self.recipients_c.find({"campaign_id": cid}, {"_id": 1})]
        self.audio_c.delete_many({"$or": [{"campaign_id": cid}, {"_id": {"$in": ids}}]})  # the second: recordings from before calls had ids
        self.history_c.delete_many({"campaign_id": cid})
        self.notices_c.delete_many({"campaign_id": cid})
        self.recipients_c.delete_many({"campaign_id": cid})
        return self.campaigns_c.delete_one({"_id": cid}).deleted_count > 0

    def reset_campaign(self, cid: str) -> int:
        """Development: forget every call made. Recipients go back to queued with no attempts, answers or recordings; the
        call log, history and saved audio of the campaign are deleted; the campaign is left paused. Returns the recipients reset."""
        ids = [d["_id"] for d in self.recipients_c.find({"campaign_id": cid}, {"_id": 1})]
        n = self.recipients_c.update_many({"campaign_id": cid}, {"$set": {
            "outcome": "pending", "channel": None, "attempts": 0, "retrying": 0, "in_flight": 0, "call_sid": None,
            "last_attempt_at": None, "recording_url": None, "answers": {}}}).matched_count
        self.calls_c.delete_many({"campaign_id": cid})
        self.audio_c.delete_many({"$or": [{"campaign_id": cid}, {"_id": {"$in": ids}}]})
        self.history_c.delete_many({"campaign_id": cid})
        self.campaigns_c.update_one({"_id": cid}, {"$set": {"status": "paused", "note": None}})
        return n

    def pause_running(self, cid: str) -> None:
        self.campaigns_c.update_one({"_id": cid, "status": "running"}, {"$set": {"status": "paused"}})

    def refresh_status(self, c: dict, auto_retry: bool = True) -> None:
        """Running -> completed once nobody is queued, on a call, or due an automatic retry."""
        if c["status"] != "running":
            return
        max_attempts = c["retry"].get("max_attempts", 1) if auto_retry else 0
        left = self.recipients_c.find_one({"campaign_id": c["id"], "$or": [
            {"in_flight": 1}, {"outcome": "pending"}, {"retrying": 1},
            {"outcome": {"$in": list(_DUE_RETRY)}, "attempts": {"$lt": max_attempts}}]}, {"_id": 1})
        if not left:
            self.campaigns_c.update_one({"_id": c["id"]}, {"$set": {"status": "completed"}})

    def requeue(self, cid: str) -> int:
        """Non-responders back to pending for another try, and people who answered but did not finish the questions queued to
        be called back (their decision stays). Reopens a finished campaign. Returns how many were queued."""
        c = self.campaign(cid)
        qs = (c or {}).get("questions") or []
        n = self.recipients_c.update_many(
            {"campaign_id": cid, "in_flight": 0, "outcome": {"$in": list(_DUE_RETRY)}},
            {"$set": {"outcome": "pending", "channel": None, "retrying": 1}}).modified_count
        unfinished = [r["id"] for r in self.recipients(cid)
                      if not r["in_flight"] and not r["retrying"] and missing_questions(qs, r)]
        if unfinished:
            n += self.recipients_c.update_many({"_id": {"$in": unfinished}}, {"$set": {"retrying": 1}}).modified_count
        if n:
            self.campaigns_c.update_one({"_id": cid, "status": "completed"}, {"$set": {"status": "running"}})
        return n

    # ---------- contacts of an existing campaign ----------

    def add_recipients(self, recs: list[dict]) -> None:
        if recs:
            base = time.time_ns()
            self.recipients_c.insert_many([
                {k: v for k, v in r.items() if k not in ("id", "answers")} | {"_id": r["id"], "seq": base + i, "answers": {}}
                for i, r in enumerate(recs)])

    def update_recipient(self, rid: str, fields: dict) -> None:
        """name, language and segment only (the phone number is the identity: remove and add instead)."""
        fields = {k: v for k, v in fields.items() if k in ("name", "language", "segment")}
        if fields:
            self.recipients_c.update_one({"_id": rid}, {"$set": fields})

    def remove_recipients(self, cid: str, ids: list[str]) -> int:
        """Deletes these contacts of the campaign with their call log, history and recordings (a person asked to be
        removed must not leave audio behind). Contacts on a call right now are skipped. Returns how many went."""
        ok = [d["_id"] for d in self.recipients_c.find({"_id": {"$in": ids}, "campaign_id": cid, "in_flight": 0}, {"_id": 1})]
        if not ok:
            return 0
        self.calls_c.delete_many({"recipient_id": {"$in": ok}})
        self.audio_c.delete_many({"$or": [{"recipient_id": {"$in": ok}}, {"_id": {"$in": ok}}]})
        self.history_c.delete_many({"recipient_id": {"$in": ok}})
        return self.recipients_c.delete_many({"_id": {"$in": ok}}).deleted_count

    # ---------- voice preparation ----------

    def pause_preparing(self, cid: str, note: str) -> None:
        self.campaigns_c.update_one({"_id": cid, "status": "preparing"}, {"$set": {"status": "paused", "note": note}})

    def finish_audio(self, cid: str) -> None:
        self.campaigns_c.update_one({"_id": cid}, {"$set": {"audio_ready": True, "note": None}})
        self.campaigns_c.update_one({"_id": cid, "status": "preparing"}, {"$set": {"status": "running"}})

    def update_campaign(self, cid: str, fields: dict) -> None:
        self.campaigns_c.update_one({"_id": cid}, {"$set": fields})

    def set_note(self, cid: str, note: str | None) -> None:
        self.campaigns_c.update_one({"_id": cid}, {"$set": {"note": note}})

    # ---------- dialing ----------

    def expire_stale(self, before: str) -> None:
        """Attempts with no status callback since `before` are given up on."""
        for d in list(self.recipients_c.find({"in_flight": 1, "last_attempt_at": {"$lt": before}})):
            outcome = d["outcome"] if d["outcome"] != "pending" else "no_answer"
            done = self.recipients_c.update_one({"_id": d["_id"], "in_flight": 1}, {"$set": {"in_flight": 0, "outcome": outcome}})
            if done.modified_count:
                self._log(_recipient(d), outcome)

    def claim_due(self, running: list[dict], concurrency: int) -> list[tuple[dict, dict]]:
        """Marks up to `concurrency` (minus calls already in flight) due recipients as in flight and
        returns (campaign, recipient as it was before the claim) pairs. Each claim is one atomic
        find-and-update, so a recipient can only be taken once."""
        batch: list[tuple[dict, dict]] = []
        free = concurrency - self.recipients_c.count_documents({"in_flight": 1})
        for c in running:
            due = {"campaign_id": c["id"], "in_flight": 0, "$or": [
                {"outcome": "pending"}, {"retrying": 1},
                {"outcome": {"$in": list(_DUE_RETRY)}, "attempts": {"$lt": c["retry"].get("max_attempts", 1)},
                 "last_attempt_at": {"$lte": ago(hours=c["retry"].get("gap_hours", 4))}}]}
            while free > 0:
                nxt = self.recipients_c.find_one(due, sort=[("attempts", ASCENDING), ("seq", ASCENDING)])
                if nxt is None:
                    break
                # A recipient who already gave a decision and is called back to finish the questions keeps it.
                claim = {"in_flight": 1, "retrying": 0, "call_sid": None, "recording_url": None, "last_attempt_at": now_iso()}
                if nxt["outcome"] not in ANSWERED:
                    claim |= {"outcome": "pending", "channel": None}
                d = self.recipients_c.find_one_and_update({"_id": nxt["_id"], "in_flight": 0}, {"$set": claim, "$inc": {"attempts": 1}},
                                                          return_document=ReturnDocument.BEFORE)
                if d is None:  # taken by another tick between the two steps
                    continue
                batch.append((c, _recipient(d)))
                free -= 1
        return batch

    def set_call_sid(self, rid: str, sid: str | None) -> None:
        if sid:
            self.recipients_c.update_one({"_id": rid, "call_sid": None}, {"$set": {"call_sid": sid}})

    def mark_unreachable(self, r: dict) -> None:
        """This number is the problem: count the attempt as unreachable."""
        self.recipients_c.update_one({"_id": r["id"]}, {"$set": {"in_flight": 0, "outcome": "no_answer"}})
        self._log(r, "no_answer")

    def restore_recipient(self, r: dict) -> None:
        """Put a recipient back exactly as it was before it was claimed."""
        self.recipients_c.update_one({"_id": r["id"]}, {"$set": {
            "in_flight": 0, **{k: r[k] for k in ("attempts", "outcome", "retrying", "channel", "call_sid",
                                                 "recording_url", "last_attempt_at")}}})

    def finish_call(self, rid: str | None, sid: str | None, completed: bool, recording: str | None) -> tuple[dict, str] | None:
        """Exotel's final status for a call in flight: (recipient before, outcome), None if unknown or already done.
        Answered with no digit pressed counts as voicemail."""
        match = ([{"_id": rid}] if rid else []) + ([{"call_sid": sid}] if sid else [])
        d = self.recipients_c.find_one({"$or": match}) if match else None
        if not d or not d.get("in_flight"):
            return None
        outcome = d["outcome"]
        if outcome == "pending":
            outcome = "voicemail" if completed else "no_answer"
        set_ = {"in_flight": 0, "outcome": outcome}
        if recording and completed:  # otherwise keep what we recorded ourselves during the call
            set_["recording_url"] = recording
        if not d.get("call_sid") and sid:
            set_["call_sid"] = sid
        if not self.recipients_c.update_one({"_id": d["_id"], "in_flight": 1}, {"$set": set_}).modified_count:
            return None  # a duplicate callback got there first
        r = _recipient(d)
        self._log(r | {"call_sid": r["call_sid"] or sid}, outcome)
        return r, outcome

    def mark_outcome(self, call_sid: str, outcome: str | None, channel: str) -> bool:
        """outcome=None only marks the channel, e.g. when the caller is handed to the agent."""
        set_ = {"channel": channel} | ({"outcome": outcome} if outcome else {})
        return self.recipients_c.update_one({"call_sid": call_sid, "in_flight": 1}, {"$set": set_}).matched_count > 0

    def add_answer(self, call_sid: str, question_id: str, digit: str) -> bool:
        if not _QID.fullmatch(question_id):
            return False
        return self.recipients_c.update_one({"call_sid": call_sid, "in_flight": 1},
                                            {"$set": {f"answers.{question_id}": digit}}).matched_count > 0

    def set_answer(self, rid: str, question_id: str, digit: str) -> None:
        """Sets one recipient's saved answer to a question (used to correct what the agent saved)."""
        if _QID.fullmatch(question_id):
            self.recipients_c.update_one({"_id": rid}, {"$set": {f"answers.{question_id}": digit}})

    def save_result(self, r: dict, outcome: str) -> None:
        """Simulated call result: store the recipient's new state and log the call."""
        got = json.loads(r["answers"]) if isinstance(r.get("answers"), str) else (r.get("answers") or {})
        self.recipients_c.update_one({"_id": r["id"]}, {"$set": {
            "outcome": r["outcome"], "channel": r["channel"], "attempts": r["attempts"], "retrying": 0,
            "last_attempt_at": r["last_attempt_at"], "recording_url": r["recording_url"], "answers": got}})
        self._log(r, outcome)

    # ---------- saved recordings ----------

    def set_recording_url(self, rid: str, url: str) -> None:
        """Marks a recipient as having a recording, unless it already has one."""
        self.recipients_c.update_one({"_id": rid, "recording_url": None}, {"$set": {"recording_url": url}})

    def save_recording(self, key: str, data: bytes, content_type: str, encrypted: bool,
                       campaign_id: str | None = None, recipient_id: str | None = None) -> None:
        """One call's audio, keyed by its call id."""
        self.audio_c.replace_one({"_id": key}, {"_id": key, "data": data, "content_type": content_type, "encrypted": encrypted,
                                                "saved_at": now_iso(), "campaign_id": campaign_id, "recipient_id": recipient_id},
                                 upsert=True)

    def load_recording(self, key: str) -> tuple[bytes, str, bool] | None:
        """(audio as stored, content type, encrypted) for a call id, or None."""
        d = self.audio_c.find_one({"_id": key})
        return (bytes(d["data"]), d["content_type"], bool(d["encrypted"])) if d else None

    def recordings_present(self, keys: list[str]) -> set[str]:
        return {d["_id"] for d in self.audio_c.find({"_id": {"$in": list(keys)}}, {"_id": 1})} if keys else set()

    # ---------- call history ----------

    def recipient_for_call(self, call_sid: str) -> dict | None:
        """The recipient a call belongs to, in flight or not."""
        d = self.recipients_c.find_one({"call_sid": call_sid})
        return _recipient(d) if d else None

    def save_call(self, call: dict) -> None:
        """Creates or replaces one call's history record (`id` is the call id)."""
        doc = {k: v for k, v in call.items() if k != "id"} | {"_id": call["id"], "rang_at": call.get("rang_at") or now_iso()}
        self.history_c.replace_one({"_id": call["id"]}, doc, upsert=True)

    def get_call(self, call_id: str) -> dict | None:
        d = self.history_c.find_one({"_id": call_id})
        return ({k: v for k, v in d.items() if k != "_id"} | {"id": d["_id"]}) if d else None

    def list_calls(self, campaign_id: str | None = None, limit: int = 200) -> list[dict]:
        """Newest first."""
        cur = self.history_c.find({"campaign_id": campaign_id} if campaign_id else {}).sort("rang_at", DESCENDING).limit(limit)
        return [{k: v for k, v in d.items() if k != "_id"} | {"id": d["_id"]} for d in cur]

    # ---------- reminders and updates ----------

    def save_notice(self, n: dict) -> None:
        """Creates or replaces one reminder/update (`id`; its targets and progress live inside the record)."""
        self.notices_c.replace_one({"_id": n["id"]}, {k: v for k, v in n.items() if k != "id"} | {"_id": n["id"]}, upsert=True)

    def get_notice(self, nid: str) -> dict | None:
        d = self.notices_c.find_one({"_id": nid})
        return ({k: v for k, v in d.items() if k != "_id"} | {"id": d["_id"]}) if d else None

    def list_notices(self, campaign_id: str | None = None, statuses: list[str] | None = None) -> list[dict]:
        """Newest first (by when they were created)."""
        f: dict = {}
        if campaign_id:
            f["campaign_id"] = campaign_id
        if statuses:
            f["status"] = {"$in": list(statuses)}
        rows = [{k: v for k, v in d.items() if k != "_id"} | {"id": d["_id"]} for d in self.notices_c.find(f)]
        return sorted(rows, key=lambda n: n.get("created_at", ""), reverse=True)

    # ---------- access log ----------

    def log_access(self, action: str, target: str | None, client: str | None) -> None:
        self.access_c.insert_one({"at": now_iso(), "action": action, "target": target, "client": client})
