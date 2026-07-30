import hashlib

from cryptography.fernet import Fernet, InvalidToken


class TokenDecryptionError(ValueError):
    """Raised when encrypted token data cannot be decrypted."""


class TokenCipher:
    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("TOKEN_ENCRYPTION_KEY is required")
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("TOKEN_ENCRYPTION_KEY must be a valid Fernet key") from exc

    def encrypt(self, token: str) -> str:
        normalized = token.strip()
        if not normalized:
            raise ValueError("Token must not be empty")
        return self._fernet.encrypt(normalized.encode("utf-8")).decode("ascii")

    def decrypt(self, encrypted_token: str) -> str:
        try:
            return self._fernet.decrypt(encrypted_token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError) as exc:
            raise TokenDecryptionError("Stored token cannot be decrypted") from exc


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()[:12]
