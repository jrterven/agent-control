from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Admin password must be at least 12 characters")
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def random_token(bytes_count: int = 32) -> str:
    return secrets.token_urlsafe(bytes_count)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_hash_matches(expected: str, token: str) -> bool:
    return hmac.compare_digest(expected, token_hash(token))


@dataclass(frozen=True, slots=True)
class SecretVault:
    key: bytes

    def __post_init__(self) -> None:
        if len(self.key) != 32:
            raise ValueError("AES-GCM vault key must be exactly 32 bytes")

    def encrypt(self, plaintext: str | None, *, aad: str) -> str | None:
        if plaintext is None or plaintext == "":
            return None
        nonce = os.urandom(12)
        ciphertext = AESGCM(self.key).encrypt(
            nonce, plaintext.encode("utf-8"), aad.encode("utf-8")
        )
        return "v1." + ".".join(
            base64.urlsafe_b64encode(part).decode("ascii").rstrip("=")
            for part in (nonce, ciphertext)
        )

    def decrypt(self, envelope: str | None, *, aad: str) -> str | None:
        if envelope is None:
            return None
        try:
            version, nonce_raw, ciphertext_raw = envelope.split(".", 2)
            if version != "v1":
                raise ValueError("Unsupported vault envelope version")
            nonce = base64.urlsafe_b64decode(nonce_raw + "===")
            ciphertext = base64.urlsafe_b64decode(ciphertext_raw + "===")
            plaintext = AESGCM(self.key).decrypt(nonce, ciphertext, aad.encode("utf-8"))
            return plaintext.decode("utf-8")
        except Exception as exc:
            raise ValueError("Unable to decrypt vault value") from exc
