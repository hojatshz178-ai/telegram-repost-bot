from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from aiohttp import web

from config import settings, validate
from db import DB
from gemini import GeminiPool
from .ingest import Ingestor
from media import MediaManager
from publisher import Publisher
from .scheduler import Scheduler
from telegram_io import TelegramIO


async def health_server():
    app = web.Application()
    app.router.add_get("/health", lambda request: web.json_response({"ok": True, "service": "raptor-military-news-bot"}))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(__import__("os").getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner


async def telegram_loop(ingestor: Ingestor):
    while True:
        try:
            await ingestor.poll_telegram()
        except Exception:
            logging.getLogger(__name__).exception("Telegram ingest loop crashed")
        await asyncio.sleep(settings.ingest_poll_seconds)


async def cleanup_loop(media: MediaManager):
    while True:
        try:
            await media.cleanup()
        except Exception:
            logging.getLogger(__name__).exception("Media cleanup failed")
        await asyncio.sleep(1800)


async def main():
    validate()
    settings.media_path.mkdir(exist_ok=True)
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    log = logging.getLogger("raptor")

    db = DB(settings.database_path)
    media = MediaManager(settings)
    gemini = GeminiPool(settings)
    tg = TelegramIO(settings, media)
    await db.connect()
    await media.start()
    await gemini.start()
    await tg.start()

    # Persisted key registry is intentionally not used to store the raw keys in the app DB;
    # environment variables remain the secret source of truth.
    ingestor = Ingestor(settings, db, tg, gemini)
    publisher = Publisher(settings, db, tg, gemini, media)
    scheduler = Scheduler(settings, db, tg, publisher, media)
    runner = await health_server()
    tasks = [
        asyncio.create_task(telegram_loop(ingestor), name="telegram-ingest"),
        asyncio.create_task(scheduler.run(), name="scheduler"),
        asyncio.create_task(cleanup_loop(media), name="media-cleanup"),
    ]

    log.info("Raptor Military News Bot started. Sources=%s", settings.source_channels)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await runner.cleanup()
    await tg.close()
    await gemini.close()
    await media.close()
    await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
