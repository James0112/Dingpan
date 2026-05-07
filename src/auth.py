from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from src.database import fetch_one


SESSION_COOKIE_NAME = "dingpan_session"


@dataclass(frozen=True)
class AuthUser:
    id: int
    email: str
    preferred_model: str
    points_balance: int
    daily_push_time: str
    push_timezone: str
    last_daily_push_trade_date: str


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_b64, digest_b64 = stored_hash.split("$", 1)
    except ValueError:
        return False
    salt = base64.b64decode(salt_b64.encode())
    expected = base64.b64decode(digest_b64.encode())
    candidate = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
    return hmac.compare_digest(candidate, expected)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode((raw + padding).encode("ascii"))


def create_session_token(user_id: int, secret: str, expires_in: int = 60 * 60 * 24 * 14) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"sub": str(user_id), "exp": int(time.time()) + expires_in}
    signing_input = ".".join(
        (
            _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
            _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
        )
    )
    signature = hmac.new(secret.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url_encode(signature)}"


def decode_session_token(token: str, secret: str) -> int | None:
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        signing_input = f"{header_b64}.{payload_b64}"
        expected_signature = hmac.new(
            secret.encode("utf-8"),
            signing_input.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected_signature, _b64url_decode(signature_b64)):
            return None
        payload = json.loads(_b64url_decode(payload_b64))
        if int(payload["exp"]) < int(time.time()):
            return None
        return int(payload["sub"])
    except Exception:
        return None


async def get_optional_user(request: Request, db_path: str, jwt_secret: str) -> AuthUser | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    user_id = decode_session_token(token, jwt_secret)
    if user_id is None:
        return None
    row = await fetch_one(
        db_path,
        """
        SELECT id, email, preferred_model, points_balance, daily_push_time, push_timezone, last_daily_push_trade_date
        FROM users
        WHERE id = ? AND is_active = 1
        """,
        (user_id,),
    )
    if row is None:
        return None
    return AuthUser(
        id=int(row["id"]),
        email=str(row["email"]),
        preferred_model=str(row["preferred_model"]),
        points_balance=int(row["points_balance"]),
        daily_push_time=str(row["daily_push_time"]),
        push_timezone=str(row["push_timezone"]),
        last_daily_push_trade_date=str(row["last_daily_push_trade_date"]),
    )


async def require_user(request: Request, db_path: str, jwt_secret: str) -> AuthUser:
    user = await get_optional_user(request, db_path, jwt_secret)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user
