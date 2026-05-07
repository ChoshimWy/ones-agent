"""JWT 认证"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

_SECRET = os.getenv("JWT_SECRET", "ones-agent-dev-secret-change-in-prod")
_ALGORITHM = "HS256"
_EXPIRY_SEC = 86400

_bearer = HTTPBearer(auto_error=False)

USERS: dict[str, dict] = {
    "admin": {"id": "u1", "name": "admin@aputure.com", "role": "admin", "password": "admin"},
    "dev": {"id": "u2", "name": "Developer", "role": "dev", "password": "dev"},
    "viewer": {"id": "u3", "name": "Viewer", "role": "viewer", "password": "viewer"},
}


def create_token(user_id: str, role: str) -> str:
    now = int(time.time())
    payload = {"sub": user_id, "role": role, "iat": now, "exp": now + _EXPIRY_SEC}
    return jwt.encode(payload, _SECRET, algorithm=_ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, _SECRET, algorithms=[_ALGORITHM])


async def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    if not creds:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = decode_token(creds.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    return {"id": payload["sub"], "role": payload.get("role", "viewer")}


async def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] not in ("admin",):
        raise HTTPException(status_code=403, detail="Admin required")
    return user
