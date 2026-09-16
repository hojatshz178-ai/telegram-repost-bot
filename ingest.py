from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from datetime import datetime, timezone

from config import Settings
from db import DB
from gemini import GeminiPool
from prompts import classification_prompt
from telegram_io import TelegramIO

log = logging.getLogger(__name__)


def clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"https?://\S+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(clean_text(text).lower().encode("utf-8")).hexdigest()


def parse_json_loose(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("No JSON object in Gemini response")
    return json.loads(m.group(0))


class Ingestor:
    def __init__(self, settings: Settings, db: DB, tg: TelegramIO, gemini: GeminiPool):
        self.settings = settings
        self.db = db
        self.tg = tg
        self.gemini = gemini

    async def poll_telegram(self):
        for source in self.settings.source_channels:
            normalized = self.tg.normalize_channel(source)
            key = f"last_id:{normalized}"
            last_id = int(await self.db.state_get(key, "0") or "0")
            try:
                newest_seen = last_id
                async for post in self.tg.iter_new_messages(source, min_id=last_id, limit=self.settings.source_page_size):
                    newest_seen = max(newest_seen, int(post["id"]))
                    await self._store_telegram(post, source)
                if newest_seen > last_id:
                    await self.db.state_set(key, str(newest_seen))
            except Exception:
                log.exception("Public Telegram source polling failed for %s", source)

    async def _store_telegram(self, post: dict, source: str):
        text = clean_text(post.get("text", ""))
        media = post.get("media") or []
        if not text:
            # A media-only post cannot be reliably classified from text alone.
            # Keep the channel clean rather than inventing a news story.
            return
        published = post.get("published_at") or datetime.now(timezone.utc).isoformat()
        data = {
            "source_type": "telegram_public_web",
            "source_name": self.tg.normalize_channel(source),
            "source_message_id": str(post["id"]),
            "source_url": post.get("source_url") or f"https://t.me/{self.tg.channel_username(source)}/{post['id']}",
            "published_at": published,
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
            raw = await self.gemini.generate(classification_prompt(source_text), temperature=0.0, max_output_tokens=700)
            info = parse_json_loose(raw)
        except Exception as exc:
            log.warning("AI classification failed; holding source item safely: %s", exc)
            return

        if not bool(info.get("is_military")) or bool(info.get("is_advertising_or_irrelevant")):
            await self.db.execute("UPDATE messages SET processed=1 WHERE id=?", (message_id,))
            return

        priority = int(info.get("importance", 0) or 0)
        if info.get("region") == "middle_east": priority += 10
        if info.get("region") == "iran": priority += 15
        if info.get("region") == "great_power": priority += 8
        if info.get("event_type") in {"armed_conflict", "attack", "strike", "intercept"}: priority += 15
        if info.get("is_breaking"): priority += 20
        priority = max(0, min(100, priority))

        event_key = str(info.get("event_key") or content_hash(data["raw_text"])[:16]).strip().lower()
        now = datetime.now(timezone.utc).isoformat()
        event = await self.db.fetchone("SELECT * FROM events WHERE event_key=?", (event_key,))
        if event:
            event_id = int(event["id"])
            await self.db.execute("UPDATE events SET last_seen=? WHERE id=?", (now, event_id))
            if int(event["published_count"]) >= 2 and priority < 85:
                await self.db.execute("UPDATE messages SET processed=1 WHERE id=?", (message_id,))
                return
        else:
            cur = await self.db.conn.execute(
                "INSERT INTO events(event_key,first_seen,last_seen,title_hint,summary_hint) VALUES(?,?,?,?,?)",
                (event_key, now, now, "", data["raw_text"][:400]),
            )
            await self.db.conn.commit()
            event_id = cur.lastrowid

        existing = await self.db.fetchone(
            "SELECT * FROM candidates WHERE event_id=? AND status IN ('queued','draft') ORDER BY id DESC LIMIT 1",
            (event_id,),
        )
        media_json = json.dumps(data.get("media", []), ensure_ascii=False)
        if existing:
            ids = json.loads(existing["message_ids_json"] or "[]")
            if message_id not in ids:
                ids.append(message_id)
            old_media = json.loads(existing["media_json"] or "[]")
            merged_media = old_media + [m for m in data.get("media", []) if m not in old_media]
            # Keep the strongest urgency and priority. The editorial stage receives all grouped source texts.
            urgency = "breaking" if info.get("is_breaking") else existing["urgency"]
            await self.db.execute(
                "UPDATE candidates SET message_ids_json=?, priority=MAX(priority,?), urgency=?, body=?, media_json=? WHERE id=?",
                (json.dumps(ids), priority, urgency, existing["body"] + "\n\n" + data["raw_text"], json.dumps(merged_media, ensure_ascii=False), existing["id"]),
            )
        else:
            await self.db.execute(
                """INSERT INTO candidates(event_id,message_ids_json,status,priority,urgency,is_military,is_ad,country_region,title,body,media_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, json.dumps([message_id]), "queued", priority,
                 "breaking" if info.get("is_breaking") else "normal", 1, 0,
                 str(info.get("region", "other")), "", data["raw_text"], media_json, now),
            )
        await self.db.execute("UPDATE messages SET processed=1 WHERE id=?", (message_id,))
