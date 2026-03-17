# OAuth Integration Plan

## Goal
Add Google OAuth + JWT authentication to the backend, backed by PostgreSQL, so real users can sign in and own their sessions.

## Tech Decisions
- **Provider**: Google OAuth 2.0
- **Database**: PostgreSQL (hosted on Railway)
- **Token strategy**: JWT in `Authorization: Bearer <token>` header
- **Frontend**: React/Vite app at idea-sharpen.vercel.app

## Phases

### Phase 1 — PostgreSQL + User Model ✅
**Status: Complete**

**What was built:**
- `app/database.py` — Lazy async SQLAlchemy engine + session factory. Lazy init means the app imports cleanly without `DATABASE_URL` set. Handles `postgres://` → `postgresql+asyncpg://` URL normalization (Railway quirk).
- `app/db_models.py` — `User` SQLAlchemy ORM model with columns: `id` (uuid), `google_id`, `email`, `name`, `avatar_url`, `created_at`. Indexed on `google_id` and `email`.
- `alembic/` — Async-aware Alembic setup. `env.py` loads `.env` via dotenv and runs migrations using the async engine.
- `alembic/versions/637fb842a3b2_create_users_table.py` — First migration: creates `users` table with unique indexes.
- `requirements.txt` — Added `sqlalchemy[asyncio]`, `asyncpg`, `alembic`.

**Verified:** Migration ran against Railway PostgreSQL. `users` and `alembic_version` tables confirmed in DB.

**DB connection:**
- Local: uses `DATABASE_URL` from `.env` pointing to Railway TCP proxy (`mainline.proxy.rlwy.net:56109`)
- Production (Railway): uses private domain URL injected automatically by Railway

---

### Phase 2 — Google OAuth + JWT [ ]
**Plan:**
- Add dependencies: `authlib`, `python-jose[cryptography]`, `httpx`
- New file `app/auth.py` with routes:
  - `GET /auth/google/login` → redirect to Google consent screen
  - `GET /auth/google/callback` → exchange code, upsert user in DB, issue JWT, redirect to frontend
  - `GET /auth/me` → return current user from JWT
- New env vars: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `JWT_SECRET_KEY`, `FRONTEND_URL`

---

### Phase 3 — Protect Existing Routes [ ]
**Plan:**
- `get_current_user` FastAPI dependency — validates JWT from `Authorization` header
- Apply to all `/v1/*` routes
- Tie session `user_id` to authenticated user's DB id
- Add session ownership checks

---

### Phase 4 — Frontend: Auth Context + Token Storage [ ]
**Plan:**
- `AuthContext` — holds `user`, `token`, `login()`, `logout()`
- `/auth/callback` route — reads `?token=` from URL, stores in localStorage, fetches `/auth/me`
- All `fetch` calls in `chatApi.ts` get `Authorization: Bearer <token>` header
- 401 → redirect to login

---

### Phase 5 — Frontend: Login UI [ ]
**Plan:**
- Login page with "Sign in with Google" button
- Protect chat interface — redirect to login if not authenticated
- Navbar shows user avatar + logout when signed in

---

### Phase 6 — Config & Deployment [ ]
**Plan:**
- Add `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `JWT_SECRET_KEY`, `FRONTEND_URL` to Railway backend service variables
- Add `VITE_API_BASE_URL` to Vercel env vars (already set for local)
- Set up Google Cloud Console OAuth credentials:
  - Authorized redirect URI: `https://interrogationclaude-production.up.railway.app/auth/google/callback`
  - Authorized JS origin: `https://idea-sharpen.vercel.app`
