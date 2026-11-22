from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from datetime import timedelta
from pathlib import Path
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import Admin, LoginSession, Setting, utcnow

DATA_DIR = Path(os.getenv("DATA_DIR", "/tmp/newsroom-data"))
SENSITIVE_SETTINGS = {"telegram_bot_token", "ai_api_key"}
PUBLIC_SETTINGS = {"ai_provider", "ai_model", "channel_id", "brand_name", "brand_tone", "scan_minutes"}
SETTINGS_DEFAULTS = {
    "ai_provider": "groq",
    "ai_model": "openai/gpt-oss-20b",
    "channel_id": "",
    "brand_name": "Мой VPN",
    "brand_tone": "Дружелюбный, понятный язык, без кликбейта и выдуманных фактов.",
    "scan_minutes": "15",
}


def safe_file(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fp:
            fp.write(content)
    except FileExistsError:
        pass


def fernet() -> Fernet:
    path = DATA_DIR / "encryption.key"
    safe_file(path, Fernet.generate_key())
    return Fernet(path.read_bytes().strip())


def encrypt(value: str) -> str:
    return "enc:" + fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if value.startswith("enc:"):
        return fernet().decrypt(value[4:].encode()).decode()
    return value


def get_setting(db: Session, key: str) -> str:
    row = db.get(Setting, key)
    raw = row.value if row else SETTINGS_DEFAULTS.get(key, "")
    return decrypt(raw) if key in SENSITIVE_SETTINGS and raw else raw


def put_setting(db: Session, key: str, value: str):
    if key not in PUBLIC_SETTINGS | SENSITIVE_SETTINGS:
        raise ValueError("Неизвестная настройка")
    if key in SENSITIVE_SETTINGS and value:
        value = encrypt(value)
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt:{base64.urlsafe_b64encode(salt).decode()}:{base64.urlsafe_b64encode(digest).decode()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, salt, expected = stored.split(":", 2)
        if algo != "scrypt":
            return False
        value = hashlib.scrypt(password.encode(), salt=base64.urlsafe_b64decode(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(value, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False


def session_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, admin_id: int) -> tuple[str, str]:
    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    db.add(LoginSession(token_hash=session_hash(token), csrf_token=csrf, admin_id=admin_id,
                        expires_at=utcnow() + timedelta(days=7)))
    db.commit()
    return token, csrf


def get_session(db: Session, cookie: str | None) -> LoginSession | None:
    if not cookie:
        return None
    return db.scalar(select(LoginSession).where(LoginSession.token_hash == session_hash(cookie),
                                                 LoginSession.expires_at > utcnow()))


def bootstrap_code() -> str:
    code_file = DATA_DIR / "first-run-code"
    safe_file(code_file, (secrets.token_urlsafe(24) + "\n").encode())
    return code_file.read_text().strip()


def delete_bootstrap_code():
    (DATA_DIR / "first-run-code").unlink(missing_ok=True)


def is_bootstrapped(db: Session) -> bool:
    return db.scalar(select(Admin.id).limit(1)) is not None


def public_setting_summary(db: Session):
    summary = {key: get_setting(db, key) for key in PUBLIC_SETTINGS}
    summary["ai_has_key"] = bool(get_setting(db, "ai_api_key"))
    summary["telegram_has_token"] = bool(get_setting(db, "telegram_bot_token"))
    return summary


def normalize_channel(value: str) -> str:
    s = value.strip().lower()
    s = re.sub(r"^https?://(?:www\.)?t\.me/", "@", s)
    return s.rstrip("/")


def valid_channel_id(value: str) -> bool:
    return bool(re.fullmatch(r"@\w{5,32}|-?\d{5,25}", normalize_channel(value)))
