from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.db_models import User

router = APIRouter(prefix="/auth", tags=["auth"])

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 30


def _cfg(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


# ---------------------------------------------------------------------------
# JWT utilities
# ---------------------------------------------------------------------------

def create_jwt(user: User) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS)
    payload = {
        "sub": user.id,
        "email": user.email,
        "name": user.name,
        "avatar_url": user.avatar_url,
        "exp": expire,
    }
    return jwt.encode(payload, _cfg("JWT_SECRET_KEY"), algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, _cfg("JWT_SECRET_KEY"), algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")


# ---------------------------------------------------------------------------
# Current user dependency  (used in Phase 3 to protect routes)
# ---------------------------------------------------------------------------

class CurrentUser(BaseModel):
    id: str
    email: str
    name: str
    avatar_url: str | None = None


_GUEST_USER = CurrentUser(
    id="guest",
    email="guest@local",
    name="Guest",
    avatar_url=None,
)


async def get_current_user(request: Request) -> CurrentUser:
    if os.getenv("SKIP_AUTH", "1") == "1":
        return _GUEST_USER

    # Accept token from Authorization header or ?token= query param (needed for EventSource)
    token = request.query_params.get("token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
        token = auth_header.removeprefix("Bearer ").strip()
    payload = decode_jwt(token)
    return CurrentUser(
        id=payload["sub"],
        email=payload["email"],
        name=payload["name"],
        avatar_url=payload.get("avatar_url"),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/google/login")
def google_login() -> RedirectResponse:
    """Redirect the user to Google's OAuth consent screen."""
    params = {
        "client_id": _cfg("GOOGLE_CLIENT_ID"),
        "redirect_uri": _cfg("GOOGLE_REDIRECT_URI"),
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{query}")


@router.get("/google/callback")
async def google_callback(code: str, db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    """Exchange Google auth code → access token → user info → issue JWT."""
    async with httpx.AsyncClient() as client:
        # 1. Exchange code for tokens
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": _cfg("GOOGLE_CLIENT_ID"),
                "client_secret": _cfg("GOOGLE_CLIENT_SECRET"),
                "redirect_uri": _cfg("GOOGLE_REDIRECT_URI"),
                "grant_type": "authorization_code",
            },
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]

        # 2. Fetch user profile from Google
        userinfo_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        userinfo_resp.raise_for_status()
        info = userinfo_resp.json()

    # 3. Upsert user in DB
    result = await db.execute(select(User).where(User.google_id == info["id"]))
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            id=str(uuid.uuid4()),
            google_id=info["id"],
            email=info["email"],
            name=info.get("name", info["email"]),
            avatar_url=info.get("picture"),
        )
        db.add(user)
    else:
        user.name = info.get("name", user.name)
        user.avatar_url = info.get("picture", user.avatar_url)

    await db.commit()
    await db.refresh(user)

    # 4. Issue JWT and redirect to frontend
    token = create_jwt(user)
    frontend_url = _cfg("FRONTEND_URL")
    return RedirectResponse(f"{frontend_url}/auth/callback?token={token}")


@router.get("/me", response_model=CurrentUser)
async def get_me(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Return the currently authenticated user."""
    return current_user
