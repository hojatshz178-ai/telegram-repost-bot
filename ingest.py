from __future__ import annotations

import hashlib
import html
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse


from .config import Settings
from .db import DB
from .gemini import GeminiPool
from .prompts import classification_prompt
from .telegram_io import TelegramIO

log = logging.getLogger(__name__)


def clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def content_hash(text: str) -> str:
    return hashlib.sha256(clean_text(text).lower().encode("utf-8")).hexdigest()


def parse_json_loose(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("No JSON object in Gemini response")
    import json
    return json.loads(m.group(0))


class Ingestor:
    def __init__(self, settings: Settings, db: DB, tg: TelegramIO, gemini: GeminiPool):
        self.settings = settings
        self.db = db
        self.tg = tg
        self.gemini = gemini

    async def poll_telegram(self):
        for source in self.settings.source_channels:
            key = f"last_id:{self.tg.normalize_channel(source)}"
            last_id = int(await self.db.state_get(key, "0") or "0")
            try:
                async for entity, msg in self.tg.iter_new_messages(source, min_id=last_id, limit=100):
                    await self._store_telegram(entity, msg, source)
                    await self.db.state_set(key, str(msg.id))
            except Exception as exc:
                log.exception("Telegram source polling failed for %s: %s", source, exc)

    async def _store_telegram(self, entity, msg, source: str):
        text = clean_text(msg.message or getattr(msg, "text", "") or "")
        # Service/action messages with no meaningful text/media are ignored.
        media = []
        if msg.media:
            media = [{"source_message_id": str(msg.id), "source": self.tg.normalize_channel(source)}]
        if not text and not media:
            return
        data = {
            "source_type": "telegram",
            "source_name": self.tg.normalize_channel(source),
            "source_message_id": str(msg.id),
            "source_url": f"https://t.me/{self.tg.normalize_channel(source).lstrip('@')}/{msg.id}",
            "published_at": (msg.date or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
            "raw_text": text,
            "media": media,
            "content_hash": content_hash(text),
        }
        mid = await self.db.insert_message(data)
        if mid is not None:
            await self._classify_and_create_candidate(mid, data)

    async def _classify_and_create_candidate(self, message_id: int, data: dict):
        source_text = f"SOURCE: {data['source_name']}\nURL: {data.get('source_url','')}\n\n{data['raw_text']}"
        try:
            raw = await self.gemini.generate(classification_prompt(source_text), temperature=0.0, max_output_tokens=600)
            info = parse_json_loose(raw)
        except Exception as exc:
            log.warning("AI classification failed; holding item safely: %s", exc)
            return

        is_military = bool(info.get("is_military"))
        irrelevant = bool(info.get("is_advertising_or_irrelevant"))
        if not is_military or irrelevant:
            await self.db.execute("UPDATE messages SET processed=1 WHERE id=?", (message_id,))
            return

        priority = int(info.get("importance", 0))
        if info.get("region") == "middle_east":
            priority += 10
        if info.get("region") == "iran":
            priority += 15
        if info.get("region") == "great_power":
            priority += 8
        if info.get("event_type") in {"armed_conflict", "attack", "strike", "intercept"}:
            priority += 15
        priority = max(0, min(100, priority))

        event_key = str(info.get("event_key") or content_hash(data["raw_text"])[:16])
        now = datetime.now(timezone.utc).isoformat()
        event = await self.db.fetchone("SELECT * FROM events WHERE event_key=?", (event_key,))
        if event:
            event_id = int(event["id"])
            # Keep the channel clean: normally cap an event at two published posts per event window.
            # A very high-priority new development is allowed as a third update.
            if int(event["published_count"]) >= 2 and priority < 85:
                await self.db.execute("UPDATE messages SET processed=1 WHERE id=?", (message_id,))
                return
            await self.db.execute("UPDATE events SET last_seen=? WHERE id=?", (now, event_id))
        else:
            cur = await self.db.conn.execute(
                "INSERT INTO events(event_key,first_seen,last_seen,title_hint,summary_hint) VALUES(?,?,?,?,?)",
                (event_key, now, now, "", data["raw_text"][:400]),
            )
            await self.db.conn.commit()
            event_id = cur.lastrowid

        # Add to existing queued event when event_key agrees; otherwise make a new candidate.
        existing = await self.db.fetchone(
            "SELECT * FROM candidates WHERE event_id=? AND status IN ('queued','draft') ORDER BY id DESC LIMIT 1",
            (event_id,),
        )
        import json
        media_json = json.dumps(data.get("media", []), ensure_ascii=False)
        if existing:
            ids = json.loads(existing["message_ids_json"])
            if message_id not in ids:
                ids.append(message_id)
            await self.db.execute(
                "UPDATE candidates SET message_ids_json=?, priority=MAX(priority,?), urgency=?, body=? WHERE id=?",
                (json.dumps(ids), priority, "breaking" if info.get("is_breaking") else existing["urgency"],
                 existing["body"] + "\n\n" + data["raw_text"], existing["id"]),
            )
        else:
            urgency = "breaking" if info.get("is_breaking") else "normal"
            await self.db.execute(
                """INSERT INTO candidates(event_id,message_ids_json,status,priority,urgency,is_military,is_advertising_or_irrelevant,country_region,title,body,media_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""".replace("is_advertising_or_irrelevant", "is_ad"),
                (event_id, json.dumps([message_id]), "queued", priority, urgency, 1, 0,
                 str(info.get("region", "other")), "", data["raw_text"], media_json, now),
            )
        await self.db.execute("UPDATE messages SET processed=1 WHERE id=?", (message_id,))
