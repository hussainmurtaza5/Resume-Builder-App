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
_DEV_SECRET = None


def get_secret_key() -> str:
    global _DEV_SECRET
    configured_secret = os.getenv("SECRET_KEY")
    if configured_secret:
        return configured_secret
    app_env = os.getenv("APP_ENV", "development").lower()
    if app_env in {"production", "prod"}:
        raise RuntimeError("SECRET_KEY must be configured in production")
    if _DEV_SECRET is None:
        _DEV_SECRET = secrets.token_urlsafe(32)
    return _DEV_SECRET


class _SecretKeyProxy:
    def __str__(self):
        return get_secret_key()

    def __repr__(self):
        return repr(get_secret_key())

    def __bytes__(self):
        return get_secret_key().encode("utf-8")

    def __len__(self):
        return len(get_secret_key())

    def encode(self, *args, **kwargs):
        return get_secret_key().encode(*args, **kwargs)


SECRET_KEY = _SecretKeyProxy()
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, str(SECRET_KEY), algorithm=ALGORITHM)
