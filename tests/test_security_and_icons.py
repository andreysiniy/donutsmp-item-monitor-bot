import logging
from pathlib import Path

from donutsmp_bot.core.logging import SecretRedactionFilter, redact_secrets
from donutsmp_bot.core.security import TokenCipher, token_fingerprint
from donutsmp_bot.infrastructure.icons import IconService


def test_token_encryption_round_trip_and_fingerprint(fernet_key: str) -> None:
    cipher = TokenCipher(fernet_key)
    encrypted = cipher.encrypt("super-secret-token")
    assert "super-secret-token" not in encrypted
    assert cipher.decrypt(encrypted) == "super-secret-token"
    assert token_fingerprint("super-secret-token") == token_fingerprint(" super-secret-token ")
    assert len(token_fingerprint("super-secret-token")) == 12


def test_log_redaction_removes_bearer_and_token_fields() -> None:
    text = redact_secrets("Authorization: Bearer abc.def token=other-secret password=third-secret")
    assert "abc.def" not in text
    assert "other-secret" not in text
    assert "third-secret" not in text

    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "Bearer raw-token %s", ("token=raw",), None
    )
    assert SecretRedactionFilter().filter(record)
    assert "raw-token" not in str(record.msg)
    assert "raw" not in str(record.args)


def test_icon_service_unifies_blocks_items_and_falls_back() -> None:
    root = Path(__file__).parents[1]
    icons = IconService(root / "manifest_detailed.json", root)
    icons.load()
    assert icons.size >= 1600
    assert icons.contains("minecraft:diamond")
    assert icons.contains("minecraft:stone")
    assert icons.icon_path("minecraft:diamond").name == "diamond.png"
    assert icons.icon_path("minecraft:not_real").name == "missingno.png"
    assert any(entry.item_id == "minecraft:diamond" for entry in icons.autocomplete("diamond"))
