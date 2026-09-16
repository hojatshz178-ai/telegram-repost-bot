from __future__ import annotations

import logging

from telethon import events

from .config import Settings
from .db import DB
from .publisher import Publisher

log = logging.getLogger(__name__)


class Admin:
    def __init__(self, settings: Settings, db: DB, publisher: Publisher):
        self.settings = settings
        self.db = db
        self.publisher = publisher
        self.paused = False

    def install(self, bot_user_client):
        if not self.settings.admin_user_ids:
            log.warning("ADMIN_USER_IDS is empty; admin commands are disabled")
            return

        @bot_user_client.on(events.NewMessage(pattern=r"^/(status|queue|pause|resume|clearqueue)$"))
        async def handler(event):
            if event.sender_id not in self.settings.admin_user_ids:
                return
            cmd = event.pattern_match.group(1)
            if cmd == "status":
                n = await self.db.queue_count()
                active = await self.db.fetchone("SELECT COUNT(*) n FROM candidates WHERE status='published' AND created_at >= datetime('now','-1 day')")
                await event.respond(
                    f"📊 وضعیت ربات\n\nصف: {n}\nمنتشرشده ۲۴ ساعت اخیر: {int(active['n']) if active else 0}\nفاصله فعلی: {await self.publisher.spacing_minutes()} دقیقه\nانتشار: {'متوقف' if self.paused else 'فعال'}"
                )
            elif cmd == "queue":
                rows = await self.db.fetchall("SELECT id,priority,urgency,title,created_at FROM candidates WHERE status='queued' ORDER BY priority DESC, created_at ASC LIMIT 20")
                if not rows:
                    await event.respond("صف انتشار خالی است.")
                else:
                    lines = [f"{r['id']} | {r['priority']} | {r['urgency']} | {(r['title'] or 'بدون تیتر')[:60]}" for r in rows]
                    await event.respond("🧾 صف فعلی:\n" + "\n".join(lines))
            elif cmd == "pause":
                self.paused = True
                await self.db.state_set("paused", "1")
                await event.respond("⏸ انتشار متوقف شد؛ دریافت و پردازش ادامه دارد.")
            elif cmd == "resume":
                self.paused = False
                await self.db.state_set("paused", "0")
                await event.respond("▶️ انتشار دوباره فعال شد.")
            elif cmd == "clearqueue":
                await self.db.execute("UPDATE candidates SET status='expired' WHERE status='queued'")
                await event.respond("🗑 صف انتشار پاک شد.")
