"""Token encryption scaffold."""

import base64
import hashlib

from cryptography.fernet import Fernet

from app.config import get_settings


def _build_fernet() -> Fernet:
    settings = get_settings()
    configured_key = settings.fernet_key.strip()
    normalized = configured_key.upper()
    placeholder_values = {"TODO", "UPDATE_ME", "CHANGE_ME"}
    if configured_key and normalized not in placeholder_values:
        return Fernet(configured_key.encode("utf-8"))

    # Dev fallback to keep local setup smooth if FERNET_KEY is not set yet.
    derived_key = base64.urlsafe_b64encode(
        hashlib.sha256(settings.session_secret.encode("utf-8")).digest()
    )
    return Fernet(derived_key)


def encrypt_refresh_token(plaintext: str) -> str:
    """Encrypt refresh token with Fernet."""
    token = _build_fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_refresh_token(ciphertext: str) -> str:
    """Decrypt refresh token with Fernet."""
    token = _build_fernet().decrypt(ciphertext.encode("utf-8"))
    return token.decode("utf-8")
