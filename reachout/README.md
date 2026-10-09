# Reachout

Multilingual outbound calling campaigns for institutions: seminar invites, clinic reminders,
school notices and payment reminders. Keypad-first, voice agent only when needed.

## Run

```bash
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload        # API on :8000
cd frontend && npm install && npm run dev  # dashboard on :3000
```

The dashboard runs on seeded demo data. Set `DEMO_LIVE=0` to stop the background simulator
that resolves pending calls on running campaigns.

## Layout

- `backend/main.py`: API (`/api/overview`, `/api/campaigns`, `/api/campaigns/{id}`, `POST /api/campaigns/{id}/retry`).
  The response shapes are the contract the real call engine will fill in later.
- `frontend/`: Next.js (App Router, TypeScript) dashboard; `/api` is proxied to the backend via `next.config.mjs`.

## Build status

- [x] Phase 0: project scaffold, mock API, dashboard (overview, campaigns, campaign detail, retry non-responders)
- [~] Phase 1: Exotel telephony skeleton (code in place, awaiting a real-call test)
- [ ] Phase 2: DTMF-first call flow, pre-synthesised templates, voicemail handling
- [ ] Phase 3: conversational escalation (STT, LLM, TTS) with local model option
- [ ] Phase 4: agentic campaign builder with cost estimate
- [ ] Phase 5: dashboard on real data, recording access control
- [ ] Phase 6: encryption at rest, retention policy, access logs
