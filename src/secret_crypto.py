"""
Encryption for secret columns (integration tokens).

How it works: a custom SQLAlchemy column type, "EncryptedText". Other code (main.py,
automation_engine.py, and so on) needs no changes: values always read back as plain text in
Python, but what is stored in the database is ciphertext prefixed with "enc1:".

Security rules:
  * Write: values are always encrypted (Fernet = AES-128-CBC + HMAC). If no key is available
    the write is REJECTED (fail-closed); plaintext is never stored silently.
  * Read: values starting with "enc1:" are decrypted. Values WITHOUT the prefix are treated as
    legacy plaintext and returned as-is, so old rows keep working before or without a migration.
  * Tokens never appear in error messages.

Key lookup (resolved at use time, not at import time):
  1) ALSA_ENCRYPTION_KEY: a Fernet key (recommended, kept separate from SECRET_KEY)
  2) otherwise: derived from SECRET_KEY with HKDF-SHA256.
WARNING: if the key is lost or changed, existing tokens can no longer be read. The impact is
limited to integrations (Slack/GitHub/Google Calendar), which must be reconnected; task and
project data are unaffected.
"""
import base64
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy.types import String, TypeDecorator

PREFIX = "enc1:"
_HKDF_INFO = b"alsa-integration-token-v1"


class SecretCryptoError(RuntimeError):
    """Key or decryption error. The message deliberately never contains any secret value."""


def _get_fernet() -> Fernet:
    explicit = (os.getenv("ALSA_ENCRYPTION_KEY") or "").strip()
    if explicit:
        try:
            return Fernet(explicit.encode("utf-8"))
        except Exception:
            raise SecretCryptoError(
                "ALSA_ENCRYPTION_KEY tidak valid (harus kunci Fernet: 44 karakter base64)."
            )
    secret = os.getenv("SECRET_KEY")
    if not secret:
        raise SecretCryptoError(
            "Tidak ada kunci enkripsi: set ALSA_ENCRYPTION_KEY atau SECRET_KEY di environment."
        )
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(
        secret.encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_value(plain: str) -> str:
    if plain.startswith(PREFIX):
        return plain  # already encrypted; never encrypt twice
    token = _get_fernet().encrypt(plain.encode("utf-8")).decode("ascii")
    return PREFIX + token


def decrypt_value(stored: str) -> str:
    if not stored.startswith(PREFIX):
        return stored  # legacy plaintext
    try:
        return _get_fernet().decrypt(stored[len(PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise SecretCryptoError(
            "Gagal mendekripsi kolom rahasia: kunci enkripsi berbeda dari saat disimpan, "
            "atau data rusak. Sambungkan ulang integrasinya."
        )


class EncryptedText(TypeDecorator):
    """A text column that is encrypted transparently. In the database it remains a String/VARCHAR."""
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt_value(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        try:
            return decrypt_value(value)
        except SecretCryptoError as e:
            # Safeguard for a changed or lost key: do NOT raise while reading a row. That would
            # turn every request touching this integration row into a 500, including the
            # "disconnect" button, which is the way out. Return None instead; models.py marks
            # the row status="error" (see _tandai_integrasi_tak_terbaca) so the UI shows
            # "reconnect". The secret value is never printed.
            print(f"[secret_crypto] Kolom rahasia tidak bisa dibaca, dianggap perlu sambung ulang: {e}")
            return None