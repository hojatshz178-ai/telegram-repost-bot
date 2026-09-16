from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings
from .db import DB
from .gemini import GeminiPool
from .media import MediaManager
from .prompts import editorial_prompt
from .telegram_io import TelegramIO

log = logging.getLogger(__name__)


class Publisher:
    def __init__(self, settings: Settings, db: DB, tg: TelegramIO, gemini: GeminiPool, media: MediaManager):
        self.settings = settings
        self.db = db
        self.tg = tg
        self.gemini = gemini
        self.media = media

    @staticmethod
    def _parse_ids(row) -> list[int]:
        try:
            return [int(x) for x in json.loads(row["message_ids_json"])]
        except Exception:
            return []

    async def _source_bundle(self, row) -> tuple[str, list[Path]]:
        ids = self._parse_ids(row)
        source_parts = []
        media_paths: list[Path] = []
        message_rows = await self.db.fetchall(
            f"SELECT * FROM messages WHERE id IN ({','.join('?' for _ in ids)}) ORDER BY published_at ASC",
            tuple(ids),
        ) if ids else []
        for m in message_rows:
            source_parts.append(
                f"SOURCE={m['source_name']}\nPUBLISHED={m['published_at']}\nURL={m['source_url'] or ''}\nTEXT:\n{m['raw_text']}"
            )
            if m["source_type"] == "telegram":
                try:
                    source_channel = m["source_name"]
                    source_message_id = int(m["source_message_id"])
                    entity = await self.tg.user.get_entity(source_channel)
                    source_msg = await self.tg.user.get_messages(entity, ids=source_message_id)
                    if source_msg and source_msg.media:
                        paths = await self.tg.download_message_media(source_msg, self.settings.max_media_per_post)
                        media_paths.extend(paths)
                except Exception as exc:
                    log.warning("Could not retrieve source media: %s", exc)
        return "\n\n==========\n\n".join(source_parts), media_paths[: self.settings.max_media_per_post]

    async def prepare_and_publish(self, row, overnight: bool = False) -> list[int]:
        bundle, media_paths = await self._source_bundle(row)
        if not bundle.strip():
            raise RuntimeError("Candidate has no source text")

        # We already cluster at ingestion; editorial generation is the second, isolated AI stage.
        text = await self.gemini.generate(editorial_prompt(bundle, overnight=overnight), temperature=0.25, max_output_tokens=1300)
        text = self._normalize_final(text)

        if not media_paths and self.settings.image_search_enabled:
            query = self._image_query_from_candidate(row)
            fallback = await self.media.fallback_image(query)
            if fallback:
                media_paths = [fallback]

        ids = await self.tg.send(text, media_paths)
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            "UPDATE candidates SET status='published', ready_at=?, published_message_ids_json=? WHERE id=?",
            (now, json.dumps(ids), row["id"]),
        )
        await self.db.execute(
            "UPDATE events SET published_count=published_count+1 WHERE id=?", (row["event_id"],)
        )
        await self.db.state_set("last_news_publish_at", now)
        return ids

    @staticmethod
    def _normalize_final(text: str) -> str:
        text = text.strip()
        # Remove accidental markdown/code fences or meta headings.
        if text.startswith("```"):
            text = text.strip("`").strip()
        marker = "#raptor\n————————\n@khaatshekaan"
        # Avoid duplicate footer variations by removing everything after the first #raptor.
        idx = text.find("#raptor")
        if idx >= 0:
            text = text[:idx].rstrip()
        return f"{text}\n\n{marker}"

    @staticmethod
    def _image_query_from_candidate(row) -> str:
        region = row["country_region"] or "military"
        body = (row["body"] or "")[:350]
        return f"{region} military defense {body}"

    async def spacing_minutes(self) -> int:
        n = await self.db.queue_count()
        if n <= self.settings.q_t1:
            return self.settings.default_spacing
        if n <= self.settings.q_t2:
            return max(self.settings.min_spacing, 55)
        if n <= self.settings.q_t3:
            return max(self.settings.min_spacing, 45)
        return self.settings.min_spacing

    async def due(self) -> bool:
        last = await self.db.state_get("last_news_publish_at")
        if not last:
            return True
        try:
            dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        return datetime.now(timezone.utc) >= dt + timedelta(minutes=await self.spacing_minutes())
