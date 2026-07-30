from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from donutsmp_bot.persistence.database import Database


@pytest.fixture
def fernet_key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest_asyncio.fixture
async def database() -> AsyncIterator[Database]:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema_for_tests()
    try:
        yield database
    finally:
        await database.close()
