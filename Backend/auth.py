from datetime import datetime, timedelta, timezone
import os
import secrets
from jose import jwt
import bcrypt

# ---------- Password hashing ----------
def hash_password(password: str) -> str:
    pwd_bytes = password.encode("utf-8")[:72]
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        plain_bytes = plain.encode("utf-8")[:72]
        hashed_bytes = hashed.encode("utf-8")
        return bcrypt.checkpw(plain_bytes, hashed_bytes)
    except Exception:
        return False


# ---------- JWT ----------
APP_ENV = os.getenv("APP_ENV", "development").lower()
configured_secret = os.getenv("SECRET_KEY")
if configured_secret:
    SECRET_KEY = configured_secret
elif APP_ENV in {"production", "prod"}:
    raise RuntimeError("SECRET_KEY must be configured in production")
else:
    # Development-only fallback. It intentionally changes on every restart so a
    # missing production secret can never silently become a permanent key.
    SECRET_KEY = secrets.token_urlsafe(32)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
