"""Demo mode (DEMO=1 only): seeds sample campaigns into an empty database and simulates calls
for campaigns marked `simulated`. Real campaigns are never touched by this module.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone

from . import store
from .store import ANSWERED, NON_RESPONDER, now_iso

log = logging.getLogger("reachout.demo")

DEFAULT_MIX = {"confirmed": .6, "declined": .15, "rescheduled": .25}
FIRST = ["Aarav", "Vihaan", "Ananya", "Diya", "Rohan", "Isha", "Kabir", "Meera", "Arjun", "Saanvi",
         "Neha", "Rahul", "Priya", "Karan", "Sneha", "Aditya", "Pooja", "Vikram", "Lakshmi", "Imran",
         "Farah", "Gurpreet", "Divya", "Suresh", "Anjali", "Manoj", "Kavya", "Harsh", "Nisha", "Tarun"]
LAST = "ABCDEGHJKMNPRSTV"

SPECS = [
    dict(id="pune-ai-summit", name="Pune AI Summit: invitations", kind="seminar", status="running",
         langs={"en": .35, "hi": .40, "mr": .25}, segments={"Students": .5, "Faculty": .2, "Alumni": .3},
         size=420, done=.78, pickup=.58, mix={"confirmed": .52, "declined": .30, "rescheduled": .18}, days_ago=2,
         escalation=True,
         questions=[{"id": "q1", "label": "Lunch preference", "options": ["Vegetarian", "Non-vegetarian", "No lunch"],
                     "only_if_confirmed": True},
                    {"id": "q2", "label": "Joining the hands-on workshop", "options": ["Yes", "No"],
                     "only_if_confirmed": True}],
         handling=dict(audio=dict(provider="Sarvam", note="Processed in India"),
                       text=dict(provider="Ollama (local)", note="Stays on this machine"),
                       recordings=dict(provider="Exotel", note="Stored in India"))),
    dict(id="sunrise-clinic", name="Sunrise Clinic: appointment reminders", kind="clinic", status="completed",
         langs={"hi": .45, "en": .30, "ta": .25}, segments={"Follow-ups": .6, "New patients": .4},
         size=260, done=1.0, pickup=.71, mix={"confirmed": .74, "declined": .06, "rescheduled": .20}, days_ago=5,
         escalation=True,
         handling=dict(audio=dict(provider="Sarvam", note="Processed in India"),
                       text=dict(provider="Ollama (local)", note="Stays on this machine"),
                       recordings=dict(provider="Exotel", note="Stored in India"))),
    dict(id="greenfield-ptm", name="Greenfield School: PTM on 18 Oct", kind="school", status="completed",
         langs={"en": .30, "hi": .45, "kn": .25}, segments={"Classes 1–5": .4, "Classes 6–8": .35, "Classes 9–10": .25},
         size=380, done=1.0, pickup=.64, mix={"confirmed": .61, "declined": .09, "rescheduled": .30}, days_ago=6,
         escalation=True,
         handling=dict(audio=dict(provider="Piper (local)", note="Stays on this machine"),
                       text=dict(provider="Ollama (local)", note="Stays on this machine"),
                       recordings=dict(provider="Exotel", note="Stored in India"))),
    dict(id="fee-reminder-t2", name="Term 2 fee reminders", kind="payment", status="paused",
         langs={"en": .40, "hi": .60}, segments={"Due this week": .55, "Overdue": .45},
         size=300, done=.55, pickup=.49, mix={"confirmed": .46, "declined": .14, "rescheduled": .40}, days_ago=1,
         escalation=False,
         handling=dict(audio=dict(provider="Piper (local)", note="Stays on this machine"),
                       text=dict(provider="Keypad only", note="No speech is processed"),
                       recordings=dict(provider="Exotel", note="Stored in India"))),
]


def _weighted(rng: random.Random, weights: dict) -> str:
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys])[0]


def _roll(rng: random.Random, c: dict, pickup: float | None) -> tuple[str, str | None]:
    if rng.random() < (pickup if pickup is not None else .6):
        channels = {"keypad": .78, "speech": .14, "agent": .08} if c["escalation"] else {"keypad": 1}
        return _weighted(rng, c["sim"].get("mix", DEFAULT_MIX)), _weighted(rng, channels)
    return rng.choices(NON_RESPONDER, [.55, .45])[0], None


def _apply(r: dict, outcome: str, channel: str | None, record: bool, at: str,
           questions: list[dict] = (), rng: random.Random | None = None) -> None:
    r.update(outcome=outcome, channel=channel, attempts=r["attempts"] + 1, retrying=0, last_attempt_at=at,
             recording_url="demo" if record and outcome in ANSWERED else None)
    if rng and outcome in ANSWERED:  # simulated follow-up answers, skewed towards the first options
        r["answers"] = json.dumps({q["id"]: str(rng.choices(range(1, len(q["options"]) + 1),
                                                            [len(q["options"]) - i for i in range(len(q["options"]))])[0])
                                   for q in questions if outcome == "confirmed" or not q.get("only_if_confirmed", True)})


def seed() -> None:
    rng = random.Random(42)
    now = datetime.now(timezone.utc)
    for spec in SPECS:
        seg_adj = {s: rng.uniform(-.08, .08) for s in spec["segments"]}
        lang_adj = {l: rng.uniform(-.07, .07) for l in spec["langs"]}
        c = {
            "id": spec["id"], "name": spec["name"], "kind": spec["kind"], "status": spec["status"],
            "languages": list(spec["langs"]), "segments": list(spec["segments"]),
            "started_at": (now - timedelta(days=spec["days_ago"], hours=3)).isoformat(timespec="seconds"),
            "handling": spec["handling"], "retry": {"max_attempts": 1, "gap_hours": 4},
            "record": 1, "escalation": int(spec["escalation"]), "simulated": 1, "sim": {"mix": spec["mix"]},
            "questions": spec.get("questions", []),
        }
        cc = c | {"escalation": spec["escalation"]}
        recs, calls = [], []
        for i in range(spec["size"]):
            lang, seg = _weighted(rng, spec["langs"]), _weighted(rng, spec["segments"])
            r = {
                "id": f"{spec['id']}-{i}", "campaign_id": spec["id"],
                "name": f"{rng.choice(FIRST)} {rng.choice(LAST)}.",
                "phone": f"+91{rng.choice('6789')}{rng.randint(0, 10**9 - 1):09d}",  # fake numbers
                "language": lang, "segment": seg, "outcome": "pending", "channel": None, "attempts": 0,
                "retrying": 0, "in_flight": 0, "call_sid": None, "last_attempt_at": None, "recording_url": None, "answers": "{}",
                "pickup": max(.2, min(.9, spec["pickup"] + seg_adj[seg] + lang_adj[lang])),
            }
            tries = 0
            if rng.random() < spec["done"]:
                tries = 1 + (spec["done"] == 1.0 and rng.random() < .35)
            for _ in range(tries):
                if r["outcome"] not in ("pending", *NON_RESPONDER):
                    break
                at = (now - timedelta(days=rng.randint(0, spec["days_ago"]), minutes=rng.randint(0, 90)))
                _apply(r, *_roll(rng, cc, r["pickup"]), record=rng.random() < .4, at=at.isoformat(timespec="seconds"),
                       questions=c["questions"], rng=rng)
                calls.append({"campaign_id": spec["id"], "recipient_id": r["id"], "call_sid": None,
                              "at": r["last_attempt_at"], "answered": int(r["outcome"] in ANSWERED), "outcome": r["outcome"]})
            recs.append(r)
        store.insert_campaign(c, recs, calls)
    log.info("seeded %d demo campaigns", len(SPECS))


def _resolve(rng: random.Random, c: dict, r: dict) -> None:
    _apply(r, *_roll(rng, c, r["pickup"]), record=c["record"], at=now_iso(), questions=c["questions"], rng=rng)
    store.save_result(r, r["outcome"])


def _tick(rng: random.Random) -> None:
    for c in store.campaigns():
        if not c["simulated"]:
            continue
        pending = store.pending_recipients(c["id"])
        retrying = [r for r in pending if r["retrying"]]
        picks = rng.sample(retrying, min(len(retrying), 8))
        if c["status"] == "running":
            fresh = [r for r in pending if not r["retrying"]]
            picks += rng.sample(fresh, min(len(fresh), rng.randint(1, 3)))
        for r in picks:
            _resolve(rng, c, r)
        store.refresh_status(c, auto_retry=False)


async def simulate() -> None:
    rng = random.Random()
    while True:
        await asyncio.sleep(1.5)
        try:
            _tick(rng)
        except Exception:
            log.exception("demo tick failed")
