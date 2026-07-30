import asyncio

from sqlalchemy import text

from .core.config import get_settings
from .persistence.database import Database


async def check() -> None:
    settings = get_settings()
    if not settings.manifest_path.is_file():
        raise RuntimeError("Minecraft manifest is unavailable")
    if not (settings.assets_dir / "icons" / "missingno.png").is_file():
        raise RuntimeError("Minecraft fallback icon is unavailable")
    database = Database(settings.database_url)
    try:
        async with database.session_factory() as session:
            await session.execute(text("SELECT 1"))
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(check())
