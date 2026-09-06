# ResQVoice

ResQVoice is a real-time AI incident co-pilot. It joins an Agora RTC channel,
turns responder speech into structured incident state, detects contradictions
and unresolved critical signals, checks proposed actions against SOPs, and can
speak evidence-grounded interventions back into the channel.

## What it can do

- Stream live human audio through Agora Conversational AI for VAD, turn
  detection, ASR, TTS, interruption, and RTC audio I/O.
- Preserve participant name, role, timestamp, UID, and transcript provenance.
- Batch irregular transcript turns before Gemini extraction to reduce API calls.
- Extract and maintain Facts, Hypotheses, Claims, Actions, Decisions, Unknowns,
  conflicts, and evidence relationships in PostgreSQL.
- Re-check claim lifecycle, contradictions, the evidence graph, and
  Silence-as-Signal alerts whenever new evidence arrives.
- Retrieve numbered incident runbooks and flag missing or out-of-order SOP steps.
- Require an explicit human approval record before protected actions can execute,
  followed by evidence-based verification before a Decision is completed.
- Generate live spoken interventions for conflicts, unresolved critical claims,
  approved decisions, explicit summary requests, and periodic summaries.
- Display live transcripts, incident health, agent activity, approvals, evidence,
  actions, decisions, and a non-overlapping scrollable Incident Timeline.
- Support mock data, workspace reset, provider request counters, and a deterministic
  demo agent cycle with read-only/mock operational tools.

The demo execution adapter is intentionally mocked: it does not modify real
production infrastructure.

## Repository layout

- `/frontend`: React + Vite (TypeScript)
- `/backend`: FastAPI (Python)
- `/shared`: Shared schema definitions (Pydantic models and TypeScript interfaces)
- `/docs`: Product specifications and documentation

## Prerequisites

- Docker and Docker Compose
- Node.js (for frontend)
- Python 3.10+ (for backend)

## Running Locally

### 1. Database & Cache

Start the PostgreSQL and Redis containers using Docker Compose. On the very first run, an initialization script will automatically create all the necessary empty tables matching the core schema.

```bash
docker-compose up -d
```

The Docker Compose PostgreSQL service uses host port `5432` by default. If that
port is occupied, start it with `POSTGRES_HOST_PORT=5433 docker-compose up -d`
and update the port in `backend/.env` to match.

### 2. Backend (FastAPI)

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Populate `backend/.env` with the required provider credentials and use the local
database URL shown in `.env.example`. Agora credentials remain server-side. The
frontend obtains a one-hour RTC token from `POST /api/agora/token`; it does not
need the App Certificate.

Copy `backend/.env.example` and `frontend/.env.example` when configuring local
development. The browser publishes its microphone directly to the Agora RTC
channel. Agora Conversational AI joins as UID `1000` by default and owns VAD,
turn detection, ASR, TTS, interruption, and real-time audio I/O. There is no
`MediaRecorder`, chunk upload, or browser STT polling in the active room hook.

Agora calls the OpenAI-compatible ResQVoice endpoint at
`/api/agora-agent/llm/chat/completions`. This persists each finalized utterance,
queues it into the shared Gemini extraction batcher, then updates Incident State
and runs the Contradiction Radar, Silence-as-Signal, SOP safety checks, and
intervention policy. Approved interventions are dispatched with Agora's agent
Speak API. `AGORA_PUBLIC_BASE_URL` must therefore be a public HTTPS
origin (use a tunnel in local development). Live state mutations reach browsers
over `/api/agora-agent/state/ws`; transcripts remain available over the existing
SSE/read endpoints for dashboard compatibility.

Set `MOCK_AGORA_AGENT=true` to test the local room UI without creating an Agora
cloud agent. The legacy Groq upload endpoint remains temporarily available for
older clients, but the incident-room hook no longer calls it.

## Render deployment

[`render.yaml`](render.yaml)
defines the FastAPI web service and a managed Render Postgres database. It runs
`alembic upgrade head` before Uvicorn (compatible with Render's free plan),
binds Uvicorn to Render's `$PORT`,
exposes `/health`, and sets `AGORA_PUBLIC_BASE_URL` from Render's stable
`RENDER_EXTERNAL_HOSTNAME`. No tunnel URL is committed or required.

The currently configured public backend is:

```text
https://resqvoice-api-p1cj.onrender.com
```

Its deployment/readiness probe is `GET /health`. The frontend derives HTTP,
SSE, and WebSocket endpoints from `VITE_BACKEND_URL`.

During the initial Blueprint import, Render prompts for these values:

- `GEMINI_API_KEY`, `GROQ_API_KEY`
- `AGORA_APP_ID`, `AGORA_APP_CERTIFICATE`
- `AGORA_CUSTOMER_ID`, `AGORA_CUSTOMER_SECRET`, `AGORA_WEBHOOK_SECRET`
- `CORS_ALLOW_ORIGINS` — for example
  `http://localhost:5173,http://localhost:3000,https://your-frontend.example`

The Render-managed `DATABASE_URL` is injected automatically; do not set it to
a local URL. For a hosted frontend, set its build-time
`VITE_BACKEND_URL=https://<your-render-service>.onrender.com` and include that
frontend's origin in `CORS_ALLOW_ORIGINS`. The incident-state WebSocket is
`wss://<your-render-service>.onrender.com/api/agora-agent/state/ws` and is
derived automatically by the frontend hook from `VITE_BACKEND_URL`.

The checked-in Blueprint intentionally uses Render's free development plans.
They retain an assigned `onrender.com` hostname, but a free web service spins
down after 15 minutes without inbound HTTP or WebSocket traffic and can take
about a minute to wake. A free Render Postgres database expires after 30 days.
Use paid Render compute and Postgres only if always-on availability and durable
database retention are required.

## Callback health

Agent startup performs an application-level check against
`/api/agora-agent/health` and requires the exact ResQVoice response signature
before returning a ready session. `/health` and `/healthz` additionally check
database readiness for deployment platforms.

### 3. Frontend (React + Vite)

```bash
cd frontend
npm install
cp .env.example .env
npm run dev
```

Open `http://127.0.0.1:5173`. Set `VITE_BACKEND_URL` in `frontend/.env` to
either the local FastAPI origin or the deployed Render origin before starting
Vite.

## Security and environment variables

- Never commit `backend/.env` or `frontend/.env`; both are ignored by Git.
- Keep `GEMINI_API_KEY`, `GROQ_API_KEY`, `AGORA_APP_CERTIFICATE`,
  `AGORA_CUSTOMER_SECRET`, `AGORA_WEBHOOK_SECRET`, and `DATABASE_URL` on the
  backend only.
- Every `VITE_*` variable is public browser configuration. Do not place secrets
  in a `VITE_*` variable.
- Use Render secret environment variables for hosted credentials and rotate any
  credential that has been pasted into chat, logs, screenshots, or source code.
- Restrict local secret-file permissions with
  `chmod 600 backend/.env frontend/.env`.
- The current deployment is development/demo oriented. Before handling real
  incident data, add application authentication, role-based authorization for
  approvals and agent controls, authenticated SSE/WebSockets, and API rate limits.

See [`docs/AUDIT_REPORT.md`](docs/AUDIT_REPORT.md) for the implementation and
verification audit.

## Shared Schemas

- **Python:** `shared/python/schemas.py`
- **TypeScript:** `shared/typescript/types.ts`

These canonical definitions are used to keep the backend services, database
models, and frontend representations aligned.
