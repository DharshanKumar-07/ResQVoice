# ResQVoice

A monorepo for the ResQVoice project containing:
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

### 2. Backend (FastAPI)

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export AGORA_APP_ID="your-agora-app-id"
export AGORA_APP_CERTIFICATE="your-agora-app-certificate"
export GEMINI_API_KEY="your-gemini-api-key"
export AGORA_CUSTOMER_ID="your-rest-customer-id"
export AGORA_CUSTOMER_SECRET="your-rest-customer-secret"
export AGORA_PUBLIC_BASE_URL="https://your-public-fastapi-host"
export AGORA_WEBHOOK_SECRET="a-long-random-bearer-secret"
uvicorn app.main:app --reload
```

Agora credentials remain server-side. The frontend obtains a one-hour RTC token
from `POST /api/agora/token`; it does not need the App Certificate.

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

[`render.yaml`](/Users/dharshankumar/.gemini/antigravity-ide/scratch/ResQVoice/render.yaml)
defines the FastAPI web service and a managed Render Postgres database. It uses
`preDeployCommand: alembic upgrade head`, binds Uvicorn to Render's `$PORT`,
exposes `/health`, and sets `AGORA_PUBLIC_BASE_URL` from Render's stable
`RENDER_EXTERNAL_HOSTNAME`. No tunnel URL is committed or required.

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
npm run dev
```

## Shared Schemas

- **Python**: Available at `/shared/python/schemas.py`. You can configure your backend to import these or copy them into the `app` directory.
- **TypeScript**: Available at `/shared/typescript/types.ts`. You can import these in your React components.
