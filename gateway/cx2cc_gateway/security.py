"""API key and password primitives."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

KEY_PREFIX = "sk-cx2cc-"

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1


def generate_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(secret: str) -> str:
    """API keys are high-entropy random strings, so a plain SHA-256 is enough:
    the hash only has to make a leaked database useless for authentication."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def key_display_prefix(secret: str) -> str:
    """What the console shows to identify a key without revealing it."""
    if secret.startswith(KEY_PREFIX):
        return secret[: len(KEY_PREFIX) + 6]
    return secret[:6]


def key_fingerprint(secret: str) -> str:
    """Correlates repeated failed attempts with one presented value."""
    return hash_api_key(secret)[:12]


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, n, r, p, salt_b64, digest_b64 = encoded.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def password_problem(password: str) -> str | None:
    if len(password) < 10:
        return "密码至少 10 位"
    return None
