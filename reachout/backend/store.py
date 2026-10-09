"""Persistence for campaigns, recipients, calls and the access log.

SQLite (backend/store_sqlite.py, stdlib only) is the default. Set MONGODB_URI to use MongoDB
instead (backend/store_mongo.py; optionally MONGODB_DB for the database name). Every other
module talks only to the functions below, so neither backend leaks into the dialer, builder or
dashboard. Both are covered by the same behaviour checks: change one, change the other.

Full phone numbers live only in the recipient records (needed to dial) and never leave this
module unmasked: use `public_recipient` for anything returned by the API.
Phase 6 adds encryption at rest and retention on top of this.
"""
from __future__ import annotations

import os

from .dbcommon import (ANSWERED, LANGUAGES, NON_RESPONDER, OUTCOMES, ago, answers, mask_phone, now_iso,
                       public_recipient)

if os.getenv("MONGODB_URI"):
    from .store_mongo import MongoStore as _Backend
else:
    from .store_sqlite import SqliteStore as _Backend

_s = _Backend()
BACKEND = _s.name

init = _s.init
has_campaigns = _s.has_campaigns
campaigns = _s.campaigns
campaign = _s.campaign
recipients = _s.recipients
recipient = _s.recipient
recipient_by_call = _s.recipient_by_call
pending_recipients = _s.pending_recipients
calls_since = _s.calls_since
insert_campaign = _s.insert_campaign
set_status = _s.set_status
delete_campaign = _s.delete_campaign
pause_running = _s.pause_running
refresh_status = _s.refresh_status
requeue = _s.requeue
pause_preparing = _s.pause_preparing
finish_audio = _s.finish_audio
set_note = _s.set_note
expire_stale = _s.expire_stale
claim_due = _s.claim_due
set_call_sid = _s.set_call_sid
mark_unreachable = _s.mark_unreachable
restore_recipient = _s.restore_recipient
finish_call = _s.finish_call
mark_outcome = _s.mark_outcome
add_answer = _s.add_answer
save_result = _s.save_result
log_access = _s.log_access

