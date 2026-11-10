"""Пароли, сессии и защита админки."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200

COMMON_PASSWORDS = {
    "password", "password1", "password123", "qwerty12345", "1234567890",
    "administrator", "adminadmin", "lineuplineup", "letmein123",
}


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
        dklen=SCRYPT_DKLEN, maxmem=SCRYPT_MAXMEM,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        expected = _b64d(digest)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=_b64d(salt),
            n=int(n), r=int(r), p=int(p),
            dklen=len(expected), maxmem=SCRYPT_MAXMEM,
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


# Хеш-заглушка: проверяется, когда пользователь не найден, чтобы время ответа
# не выдавало существование логина.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def validate_password(password: str) -> str | None:
    if not isinstance(password, str):
        return "Некорректный пароль."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Пароль должен быть короче {MAX_PASSWORD_LENGTH} символов."
    if password.strip().lower() in COMMON_PASSWORDS:
        return "Пароль слишком простой. Придумайте другой."
    return None


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
