# ResQVoice Final Audit Report

**Audit date:** 2026-09-04  
**Result:** Development-ready with production hardening items documented below

## Executive summary

ResQVoice now has a coherent real-time incident-assistance pipeline:

```text
Human microphone
  -> Agora RTC + Conversational AI (VAD, turn detection, ASR)
  -> ResQVoice FastAPI orchestration middleware
  -> batched Gemini Incident State extraction
  -> claim lifecycle / contradiction / silence / SOP safety engines
  -> policy-gated intervention
  -> Agora Speak API + TTS audio in the RTC channel
```

The active browser path no longer records fixed five-second chunks or uploads
them to a custom STT endpoint. Agora owns the live VAD/ASR loop. Groq Whisper
remains only as a compatibility endpoint and is not used by the Agora room.

The final backend suite passes **97 tests**. The frontend TypeScript and Vite
production build passes. Live verification established that:

- the browser publishes its microphone to Agora;
- a real Agora agent starts in `RUNNING` state and subscribes to the current UID;
- finalized turns reach FastAPI and appear immediately in the UI;
- registered users retain their name and incident role;
- Gemini extraction remains batched rather than running once per utterance;
- explicit summary requests produce fresh Gemini text;
- Agora accepts Speak requests and publishes the agent audio track;
- the trigger-specific speaking indicator follows actual audio activity; and
- the Command Center timeline scrolls without overlapping rows.

No credentials are stored in this report or tracked example files. Credentials
disclosed during development should be rotated before deployment.

## Final architecture

### Browser and RTC

`frontend/src/hooks/useAgoraIncidentRoom.ts`:

1. Requests a server-generated RTC token.
2. Reuses one stable RTC UID for the browser tab.
3. Joins the incident channel and publishes the microphone directly.
4. Registers UID, display name, role, and participant type with FastAPI.
5. Starts an agent subscribed to that concrete human UID.
6. Subscribes to and plays the agent's remote audio independently.
7. Keeps SSE open for transcripts and WebSocket open for state/speaking events.
8. Uses microphone volume only for `Speech detected` / `Transcribing` indicators;
   it does not perform or upload STT.

### Agora agent session

`backend/app/integrations/agora_conversational_ai.py` configures:

- channel, agent UID, publish-capable token, and subscribed remote UID;
- Agora ARES ASR with explicit `params`;
- current `turn_detection` with configurable threshold and silence gap;
- start-of-speech interruption;
- Agora-managed MiniMax TTS and voice/audio overrides;
- `vendor: custom` LLM routing to the ResQVoice callback;
- the ResQVoice Incident Co-pilot system prompt; and
- join, leave, and Speak REST operations.

Before agent creation, FastAPI verifies the public HTTPS callback health endpoint.
An expired tunnel now produces an actionable join error instead of a misleading
online agent with no transcription.

Agora treats the deterministic agent name as an idempotency boundary. On a 409
`TaskConflict`, ResQVoice adopts the session only when it is bound to the same
speaker UID. Otherwise it stops the stale agent and creates one for the current
UID. This fixes agents that appeared online while listening to a departed client.

### Turn ingestion and extraction

`backend/app/api/agora_agent_webhook.py` exposes an authenticated,
OpenAI-compatible custom-LLM endpoint. Each finalized human turn is:

1. resolved through the participant registry;
2. rejected if it originates from the AI agent;
3. normalized and deduplicated;
4. persisted with speaker, role, UID, timestamp, sequence, and utterance metadata;
5. broadcast immediately to the transcript feed; and
6. queued into the shared extraction batcher.

The batcher flushes on word count, accumulated speech duration, or maximum wait.
Every segment retains its original speaker and timestamp in the Gemini prompt.

### Incident intelligence pipeline

The intelligence services share canonical models re-exported from
`shared/python/schemas.py`. Evidence uses `target_id` / `target_type`; silence
signals use `source_id`. PostgreSQL models, initialization DDL, migrations, Python
schemas, and TypeScript types use compatible names and types.

New evidence follows one orchestrated transaction:

```text
persist evidence
  -> claim_lifecycle transition
  -> contradiction_radar re-check
  -> evidence_graph/provenance update
  -> silence_signal resolution/reset
  -> commit
```

The payment outage fixture proves this path end to end. Its deterministic final
state is:

```text
claim-db-cpu:   CONFIRMED
claim-db-fine:  CORROBORATED
hypo-pricing:   UNCONFIRMED
conflict:       claim-db-cpu <-> claim-db-fine = UNRESOLVED
silence signal: claim-db-cpu = RESOLVED
```

### SOP safety and approval

The repository contains numbered runbooks for production rollback, server
shutdown, database compromise, and external communication. The safety layer:

- retrieves a relevant runbook with local embedding-style vector search;
- detects missing and out-of-order steps;
- persists `POTENTIAL SOP CONFLICT` and the corrected sequence;
- prevents protected actions from executing without complete approval;
- records approver and approval time;
- executes only the mocked action adapter;
- requires a linked Fact/metric verification before completion; and
- retains recommendation, evidence, SOP reference, approval, status, and result.

Protected actions include rollback, production restart/config modification,
infrastructure scaling, shutdown, escalation, and external communication.

### Voice intervention policy

Fresh spoken text is generated by Gemini over current Incident State for:

1. `CONFLICT_DETECTED`;
2. unresolved/stale critical claims;
3. `DECISION_APPROVED`;
4. explicit summary intent; and
5. the configurable periodic summary interval.

Potential SOP conflicts and unassigned critical actions enter the same queue.
Defaults are: minimum severity `HIGH`, 120-second duplicate cooldown, 300-second
expiration, 3 interventions per 300 seconds, 2-second post-human guard, 30-second
monitor scan, and 300-second periodic summary.

Conflict and stale-critical triggers default to Agora `INTERRUPT`; confirmations
and summaries use `APPEND`. This mapping is configurable.

Summary replies previously waited for the next 30-second scan after being deferred
during speech. They now retry immediately after the speech guard. Natural forms
such as “Give me a brief live summary” are recognized.

`speak_to_channel` logs trigger, text, priority, raw Agora response, and result.
Failure always emits `speaking_stopped`. The frontend combines WebSocket intent
with remote audio volume, so its badge represents playback rather than only a
backend trigger.

## Defects found and corrected

| Area | Root cause | Correction | Final evidence |
|---|---|---|---|
| Missing transcription | Expired callback URL | Callback preflight and visible error | Health returned HTTP 200 before start |
| Agent online but deaf | 409 reused old subscribed UID | Stable tab UID; replace stale conflict | Fresh agent `RUNNING` on current UID |
| Inconsistent turns | Obsolete Agora VAD shape | Current turn detection/ASR/interruption | Join accepted; payload unit tested |
| Repeated transcript | Cumulative ASR snapshots persisted | Final/ID/sequence/cumulative guard | Exact three-segment replay |
| Unknown user/role | No deterministic UID mapping | Persistent participant registry/fallback | `Priya / Incident Commander` live |
| No apparent response | Summary intent narrow; deferred up to 30s | Expanded intent; post-pause dispatch | Summary Speak succeeded after guard |
| Silent TTS failure | Speak response not inspected | Raw response/failure logging | Real response included agent/channel/time |
| Fixed browser STT | MediaRecorder slicing | Removed active upload loop | RTC microphone publish only |
| Timeline overlap | Flex rows shrank in capped panel | Non-shrinking rows and safe wrapping | Visual check and frontend build |

## Verification matrix

Legend: ✅ verified; 🟡 needs broader live/production testing; 🟠 deliberately mocked.

| Capability | Status | Evidence / boundary |
|---|:---:|---|
| RTC join and microphone publish | ✅ | Live browser joined with server token |
| Agent start/stop/conflict recovery | ✅ | Live `RUNNING`; 409 paths tested |
| Callback preflight | ✅ | Dead URL blocked; active URL returned 200 |
| Agora VAD/turn configuration | ✅ | Current payload asserted and accepted |
| Live finalized transcription | ✅ | Human turns appeared in both UIs |
| Speech/transcribing indicators | ✅ | Local volume state wired and visible |
| Registered identity attribution | ✅ | UID mapped to selected name and role |
| Multi-human attribution by one agent | 🟡 | Current agent subscribes to one concrete UID |
| Duplicate/cumulative prevention | ✅ | Replay and guard tests |
| Transcript/state streaming | ✅ | SSE transcript plus WebSocket state |
| Gemini extraction batching | ✅ | Word/duration/timer tests and counters |
| Per-segment provenance | ✅ | Integration assertions |
| Claim lifecycle orchestration | ✅ | Fixture integration test |
| Contradiction Radar | ✅ | Unit and integration tests |
| Silence-as-Signal reset | ✅ | Evidence-resolution integration path |
| Evidence graph | ✅ | Node/edge/provenance tests |
| SOP retrieval/order validation | ✅ | Safety tests and sample runbooks |
| Explicit approval gate | ✅ | Unapproved execution rejected |
| Execute/verify/audit lifecycle | 🟠 | Complete with mocked executor |
| Gemini intervention generation | ✅ | Live summary generated from state |
| Agora voice response | ✅ | Speak succeeded; audio track published |
| High-priority barge-in | 🟡 | Native interrupt configured; needs audio test |
| Speaking indicator | ✅ | Trigger label plus real audio activity |
| Provider request counters | ✅ | Groq/Gemini RPM and totals exposed |
| Mock-data toggle | ✅ | Isolated fixture view |
| Workspace reset | ✅ | API test and guarded UI action |
| Timeline layout | ✅ | Non-overlapping scrolling rows verified |
| Frontend production build | ✅ | `tsc -b && vite build` passed |
| Backend suite | ✅ | **97 passed** |

## Configuration inventory

### Providers and batching

- `GROQ_API_KEY`, `GROQ_TRANSCRIPTION_MODEL`, `GROQ_REQUESTS_PER_MINUTE`
- `GEMINI_API_KEY`, `GEMINI_EXTRACTION_MODEL`, `GEMINI_INTERVENTION_MODEL`
- `EXTRACTION_BATCH_MAX_WORDS`
- `EXTRACTION_BATCH_MAX_DURATION_SECONDS`
- `EXTRACTION_BATCH_MAX_WAIT_SECONDS`

### Agora

- `AGORA_APP_ID`, `AGORA_APP_CERTIFICATE`
- `AGORA_CUSTOMER_ID`, `AGORA_CUSTOMER_SECRET`
- `AGORA_PUBLIC_BASE_URL`, `AGORA_WEBHOOK_SECRET`
- `AGORA_CONVERSATIONAL_AI_BASE_URL`
- `AGORA_AGENT_IDLE_TIMEOUT_SECONDS`, `AGORA_VALIDATE_PUBLIC_URL`
- `AGORA_ASR_VENDOR`, `AGORA_ASR_LANGUAGE`
- `AGORA_VAD_SPEECH_THRESHOLD`, `AGORA_VAD_SILENCE_MS`
- `AGORA_AGENT_PRESET`, `AGORA_TTS_CONFIG_JSON`, `AGORA_LLM_MODEL`
- `MOCK_AGORA_AGENT`

### Intervention policy

- `INTERVENTION_MIN_SEVERITY`, `INTERVENTION_COOLDOWN_SECONDS`
- `INTERVENTION_EXPIRATION_SECONDS`
- `INTERVENTION_MAX_PER_WINDOW`, `INTERVENTION_WINDOW_SECONDS`
- `INTERVENTION_SPEECH_GUARD_SECONDS`, `INTERVENTION_SCAN_INTERVAL_SECONDS`
- `INTERVENTION_PERIODIC_SUMMARY_SECONDS`
- `INTERVENTION_HIGH_PRIORITY_MODE`, `INTERVENTION_HIGH_PRIORITY_TRIGGERS`

### Frontend

- `VITE_BACKEND_URL`, `VITE_AGORA_AGENT_UID`
- `VITE_SPEECH_ACTIVITY_THRESHOLD`, `VITE_SPEECH_SILENCE_GAP_MS`

The Vite speech values affect indicators only. Server-side Agora VAD controls
actual turn segmentation.

## Observability

Structured logs cover RTC/participant joins; partial/final/deduplicated ASR;
extraction and contradictions; assigned actions and SOP conflicts; intervention
creation/defer/speech; Speak start/success/failure; TTS completion; AI self-audio;
and authentication, extraction, monitor, and pause-dispatch errors. Sensitive
token/key/secret fields are removed from structured payloads.

Provider counters are available from:

```bash
curl http://127.0.0.1:8000/api/provider-metrics
```

## Reproduction commands

```bash
docker compose up -d

cd backend
source venv/bin/activate
alembic upgrade head
pytest -q
uvicorn app.main:app --host 127.0.0.1 --port 8000

# Separate terminal
cd frontend
npm run build
npm run dev -- --host 0.0.0.0
```

For live Agora operation, `AGORA_PUBLIC_BASE_URL` must be a stable public HTTPS
origin forwarding to FastAPI. Restart the backend and recreate the agent after
changing it; existing cloud sessions retain their original callback URL.

## Production readiness gaps

1. Configure a free named Cloudflare Tunnel and stable domain; until domain/account
   access is supplied, local development still uses a temporary tunnel.
2. Rotate disclosed Agora and Groq credentials into managed secret storage.
3. Authenticate approvers; recorded names do not prove organizational authority.
4. Add room leases/reference counts so one browser cannot stop a shared agent.
5. Establish authoritative per-turn UID metadata for multi-human rooms.
6. Run a multi-client headphone soak test for seams, barge-in, echo, reconnect,
   long-session stability, and provider usage.
7. Wire authoritative Agora playback-completion events; the UI currently combines
   audio level with a timeout guard.
8. Keep real production execution disabled until least-privilege, idempotent,
   auditable tool adapters are implemented.
9. Replace the placeholder `docs/PRODUCT_SPEC.md` with complete acceptance criteria.

## Final assessment

The requested intelligence, safety, approval, live transcription, and voice
intervention components are integrated and automated-test clean. ResQVoice is
suitable for local demonstrations and controlled development testing. It is not
yet production-authorized because callback hosting, user authorization,
multi-participant identity, and execution remain development-grade.
