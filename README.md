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
uvicorn app.main:app --reload
```

Agora credentials remain server-side. The frontend obtains a one-hour RTC token
from `POST /api/agora/token`; it does not need the App Certificate.

### 3. Frontend (React + Vite)

```bash
cd frontend
npm install
npm run dev
```

## Shared Schemas

- **Python**: Available at `/shared/python/schemas.py`. You can configure your backend to import these or copy them into the `app` directory.
- **TypeScript**: Available at `/shared/typescript/types.ts`. You can import these in your React components.
