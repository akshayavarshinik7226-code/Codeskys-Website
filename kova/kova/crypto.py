"""Symmetric encryption for secrets held at rest (OAuth tokens, webhook secrets).

The key comes from ``KOVA_ENCRYPTION_KEY``. In development, if the variable is
unset, a key is generated once into ``instance/encryption_key``; in production a
missing key is a startup error, because silently generating one would make every
stored token unreadable after a redeploy.
"""
import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class EncryptionError(RuntimeError):
    pass


def normalise_key(raw: str) -> bytes:
    """Accept either a real Fernet key or any passphrase (hashed into one)."""
    raw = (raw or "").strip()
    if not raw:
        raise EncryptionError("Empty encryption key.")
    try:
        if len(base64.urlsafe_b64decode(raw.encode())) == 32:
            return raw.encode()
    except (ValueError, TypeError):
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())


def load_key(instance_dir: Path, env: str) -> bytes:
    configured = os.environ.get("KOVA_ENCRYPTION_KEY", "").strip()
    if configured:
        return normalise_key(configured)
    if env == "production":
        raise EncryptionError(
            "KOVA_ENCRYPTION_KEY must be set when KOVA_ENV=production. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    key_file = Path(instance_dir) / "encryption_key"
    if key_file.is_file():
        return key_file.read_text(encoding="utf-8").strip().encode()
    key = Fernet.generate_key()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(key.decode(), encoding="utf-8")
    try:
        key_file.chmod(0o600)
    except OSError:  # pragma: no cover - platform dependent
        pass
    return key


class Secrets:
    """Encrypts and decrypts short strings. Never logs plaintext."""

    def __init__(self, key: bytes):
        self._f = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        if plaintext is None:
            raise EncryptionError("Cannot encrypt None.")
        return self._f.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._f.decrypt(ciphertext.encode()).decode()
        except (InvalidToken, AttributeError, ValueError) as exc:
            raise EncryptionError(
                "Stored secret could not be decrypted. The encryption key has changed "
                "or the value is corrupt; reconnect the account."
            ) from exc


def secrets_for(app) -> Secrets:
    box = app.config.get("_SECRETS")
    if box is None:
        box = Secrets(app.config["ENCRYPTION_KEY"])
        app.config["_SECRETS"] = box
    return box
