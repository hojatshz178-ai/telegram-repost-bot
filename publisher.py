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
        self.settings, self.db, self.tg, self.gemini, self.media = settings, db, tg, gemini, media

    @staticmethod
    def _parse_ids(row) -> list[int]:
        try: return [int(x) for x in json.loads(row["message_ids_json"] or "[]")]
        except Exception: return []

    async def _source_bundle(self, row) -> tuple[str, list[Path]]:
        ids = self._parse_ids(row)
        if not ids:
            return "", []
        placeholders = ",".join("?" for _ in ids)
        rows = await self.db.fetchall(f"SELECT * FROM messages WHERE id IN ({placeholders}) ORDER BY published_at ASC", tuple(ids))
        source_parts, media_paths = [], []
        for m in rows:
            source_parts.append(f"SOURCE={m['source_name']}\nPUBLISHED={m['published_at']}\nURL={m['source_url'] or ''}\nTEXT:\n{m['raw_text']}")
            try:
                for item in json.loads(m["media_json"] or "[]"):
                    url = item.get("url") if isinstance(item, dict) else None
                    if url:
                        path = await self.media.download_url(url, suffix=self.media.suffix_from_url(url))
                        if path and path not in media_paths:
                            media_paths.append(path)
            except Exception as exc:
                log.warning("Could not retrieve public Telegram media: %s", exc)
        return "\n\n==========\n\n".join(source_parts), media_paths[: self.settings.max_media_per_post]

    async def prepare_and_publish(self, row, overnight: bool = False) -> list[int]:
        bundle, media_paths = await self._source_bundle(row)
        if not bundle.strip():
            raise RuntimeError("Candidate has no source text")
        text = await self.gemini.generate(editorial_prompt(bundle, overnight=overnight), temperature=0.25, max_output_tokens=1300)
        text = self._normalize_final(text)
        if not media_paths and self.settings.image_search_enabled:
            fallback = await self.media.fallback_image(self._image_query_from_candidate(row))
            if fallback:
                media_paths = [fallback]
        ids = await self.tg.send(text, media_paths)
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute("UPDATE candidates SET status='published', ready_at=?, published_message_ids_json=? WHERE id=?", (now, json.dumps(ids), row["id"]))
        await self.db.execute("UPDATE events SET published_count=published_count+1 WHERE id=?", (row["event_id"],))
        await self.db.state_set("last_news_publish_at", now)
        return ids

    @staticmethod
    def _normalize_final(text: str) -> str:
        text = (text or "").strip().replace("```text", "").replace("```", "").strip()
        idx = text.find("#raptor")
        if idx >= 0: text = text[:idx].rstrip()
        return text + "\n\n#raptor\n————————\n@khaatshekaan"

    @staticmethod
    def _image_query_from_candidate(row) -> str:
        return f"{row['country_region'] or 'military'} military defense {(row['body'] or '')[:350]}"

    async def spacing_minutes(self) -> int:
        n = await self.db.queue_count()
        if n <= self.settings.q_t1: return self.settings.default_spacing
        if n <= self.settings.q_t2: return max(self.settings.min_spacing, 55)
        if n <= self.settings.q_t3: return max(self.settings.min_spacing, 45)
        return self.settings.min_spacing

    async def due(self) -> bool:
        last = await self.db.state_get("last_news_publish_at")
        if not last: return True
        try: dt = datetime.fromisoformat(last)
        except ValueError: return True
        return datetime.now(timezone.utc) >= dt + timedelta(minutes=await self.spacing_minutes())
