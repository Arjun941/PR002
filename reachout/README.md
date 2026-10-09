# Reachout

Multilingual outbound calling campaigns for institutions: seminar invites, clinic reminders,
school notices and payment reminders. Keypad-first, voice agent only when needed.

## Run

```bash
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload        # API on :8000
cd frontend && npm install && npm run dev  # dashboard on :3000
```

Data is stored in SQLite (`reachout.db`). To try it without Exotel, start the API with `DEMO=1`:
it seeds sample campaigns into an empty database and simulates calls, including for campaigns you
launch from the builder. Copy `.env.example` to `.env` for real calls, script drafting and recording
playback; the backend loads `.env` itself on startup (restart it after editing `.env`).

## Layout

- `backend/main.py`: dashboard API (`/api/overview`, `/api/campaigns`, `/api/campaigns/{id}`, retry, pause/resume).
- `backend/builder.py`, `llm.py`, `costs.py`: campaign builder (one drafting call per campaign, cost estimate, launch).
- `backend/dialer.py`: places calls and records results from Exotel's status callback.
- `backend/recordings.py`: PIN-protected recording playback with an access log.
- `backend/store.py`, `demo.py`: SQLite storage and the opt-in demo data.
- `frontend/`: Next.js (App Router, TypeScript) dashboard; `/api` is proxied to the backend via `next.config.mjs`.

## Build status

- [x] Phase 0: project scaffold, mock API, dashboard (overview, campaigns, campaign detail, retry non-responders)
- [~] Phase 1: Exotel telephony skeleton (code in place, awaiting a real-call test)
- [~] Phase 2: DTMF-first call flow, pre-synthesised ElevenLabs audio, voicemail handling (awaiting a real-call test)
- [~] Phase 3: key 4 bridges to an ElevenLabs Conversational AI agent (awaiting a real-call test)
- [x] Phase 4: agentic campaign builder with cost estimate
- [~] Phase 5: dashboard on real data, recording access control (awaiting a real-call test)
- [ ] Phase 6: encryption at rest, retention policy, access logs
