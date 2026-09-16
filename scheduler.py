from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time, timedelta, timezone

from .config import Settings
from .db import DB
from .publisher import Publisher
from .telegram_io import TelegramIO
from .media import MediaManager

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, settings: Settings, db: DB, tg: TelegramIO, publisher: Publisher, media: MediaManager):
        self.settings = settings
        self.db = db
        self.tg = tg
        self.publisher = publisher
        self.media = media

    def local_now(self):
        return datetime.now(timezone.utc).astimezone(self.settings.tz)

    def is_quiet(self, dt=None) -> bool:
        dt = dt or self.local_now()
        return dt.time() >= time.fromisoformat(self.settings.quiet_start) or dt.time() < time.fromisoformat(self.settings.quiet_end)

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Scheduler tick crashed; continuing")
            await asyncio.sleep(self.settings.queue_poll_seconds)

    async def tick(self):
        now = self.local_now()
        # Midnight boundary: send good-night and discard low-priority leftovers.
        await self._handle_night_boundary(now)
        await self._handle_morning_boundary(now)

        paused = (await self.db.state_get('paused', '0')) == '1'
        if paused:
            return
        if self.is_quiet(now):
            breaking = await self.db.fetchone(
                "SELECT 1 FROM candidates WHERE status='queued' AND urgency='breaking' LIMIT 1"
            )
            if not breaking:
                return
        top = await self.db.fetchone(
            "SELECT urgency FROM candidates WHERE status='queued' ORDER BY CASE WHEN urgency='breaking' THEN 1 ELSE 0 END DESC, priority DESC, created_at ASC LIMIT 1"
        )
        if not top:
            return
        if top['urgency'] != 'breaking' and not await self.publisher.due():
            return
        row = await self.db.fetchone(
            """SELECT * FROM candidates WHERE status='queued'
               ORDER BY CASE WHEN urgency='breaking' THEN 1 ELSE 0 END DESC,
                        priority DESC, created_at ASC LIMIT 1"""
        )
        if not row:
            return
        overnight = False
        try:
            created = datetime.fromisoformat(row["created_at"]).astimezone(self.settings.tz)
            overnight = created.time() >= time(0, 0) and created.time() < time(8, 0)
        except Exception:
            pass
        try:
            await self.publisher.prepare_and_publish(row, overnight=overnight)
        except Exception as exc:
            log.exception("Publishing candidate %s failed: %s", row["id"], exc)
            # Don't burn the whole queue because of one bad item. Move it to failed after repeated attempts.
            attempts_key = f"candidate_fail:{row['id']}"
            attempts = int(await self.db.state_get(attempts_key, "0") or "0") + 1
            await self.db.state_set(attempts_key, str(attempts))
            if attempts >= 3:
                await self.db.execute("UPDATE candidates SET status='failed' WHERE id=?", (row["id"],))

    async def _handle_night_boundary(self, now):
        if now.hour == 0 and now.minute == 0:
            guard = await self.db.state_get("last_midnight_date")
            day = now.date().isoformat()
            if guard == day:
                return
            await self.db.state_set("last_midnight_date", day)
            # Keep only candidates that are already urgent/high-priority; routine leftovers expire.
            await self.db.execute(
                "UPDATE candidates SET status='expired' WHERE status='queued' AND priority < ?",
                (self.settings.low_priority_drop,),
            )
            if self.settings.good_night_enabled:
                try:
                    img = await self.media.fallback_image("military night fighter jet defense")
                    await self.tg.send(self.settings.good_night_text, [img] if img else [])
                except Exception:
                    log.exception("Good-night message failed")

    async def _handle_morning_boundary(self, now):
        if now.hour == 8 and now.minute == 0:
            guard = await self.db.state_get("last_morning_date")
            day = now.date().isoformat()
            if guard == day:
                return
            await self.db.state_set("last_morning_date", day)
            if self.settings.good_morning_enabled:
                try:
                    img = await self.media.fallback_image("military sunrise aircraft carrier fighter")
                    await self.tg.send(self.settings.good_morning_text, [img] if img else [])
                except Exception:
                    log.exception("Good-morning message failed")
