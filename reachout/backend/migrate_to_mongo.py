"""One-time copy of an existing SQLite database into MongoDB.

    MONGODB_URI=mongodb+srv://... python -m backend.migrate_to_mongo [path/to/reachout.db]

Campaigns already in MongoDB are skipped, so it is safe to run twice. Nothing is deleted from SQLite.
Afterwards keep MONGODB_URI set and the app uses MongoDB.
"""
from __future__ import annotations

import os
import sqlite3
import sys

from .store_mongo import MongoStore
from .store_sqlite import SqliteStore


def main(path: str) -> None:
    if not os.getenv("MONGODB_URI"):
        sys.exit("Set MONGODB_URI first.")
    if not os.path.exists(path):
        sys.exit(f"No SQLite database at {path}")
    src, dst = SqliteStore(path), MongoStore()
    src.init()  # adds tables/columns newer than the file (e.g. saved recordings); changes nothing else
    dst.init()
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    copied = skipped = 0
    for c in src.campaigns():
        if dst.campaign(c["id"]):
            skipped += 1
            continue
        calls = [dict(r) for r in raw.execute(
            "SELECT campaign_id, recipient_id, call_sid, at, answered, outcome FROM calls WHERE campaign_id = ?", (c["id"],))]
        dst.insert_campaign(c, src.recipients(c["id"]), calls)
        for a in raw.execute("SELECT a.* FROM recording_audio a JOIN recipients r ON r.id = a.recipient_id "
                             "WHERE r.campaign_id = ?", (c["id"],)):
            dst.save_recording(a["recipient_id"], bytes(a["data"]), a["content_type"], bool(a["encrypted"]))
        copied += 1
        print(f"copied {c['id']}: {len(src.recipients(c['id']))} recipients, {len(calls)} calls")
    log = [dict(r) for r in raw.execute("SELECT at, action, target, client FROM access_log ORDER BY id")]
    if log and dst.access_c.count_documents({}) == 0:
        dst.access_c.insert_many(log)
    print(f"done: {copied} copied, {skipped} already in MongoDB, {len(log)} access-log rows checked")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.getenv("REACHOUT_DB", "reachout.db"))
