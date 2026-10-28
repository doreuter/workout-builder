"""Password hashing (stdlib scrypt) and session helpers."""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from . import db

_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=_N, r=_R, p=_P)
    return hmac.compare_digest(digest.hex(), digest_hex)


def start_session(request: Request, user) -> None:
    """Sign the browser in. The session version lets a password reset sign out every other session:
    the cookie lives in the browser, so the server can't delete it, but it can stop accepting it."""
    request.session.clear()
    request.session["user_id"] = user["id"]
    request.session["sv"] = user["session_version"]


def current_user(request: Request):
    user_id = request.session.get("user_id")
    user = db.get_user(user_id) if user_id else None
    if user is not None and request.session.get("sv", 0) != user["session_version"]:
        request.session.clear()  # signed out by a password reset since this cookie was issued
        user = None
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%d %H:%M:%S"  # same as SQLite's CURRENT_TIMESTAMP, so the two compare
    db.touch_user(user["id"], now.strftime(fmt), (now - timedelta(hours=1)).strftime(fmt))
    return user
