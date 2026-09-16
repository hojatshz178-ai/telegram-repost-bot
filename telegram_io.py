from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Iterable

import aiohttp
from telethon import TelegramClient
from telethon.sessions import StringSession

from .config import Settings
from .media import MediaManager, media_kind

log = logging.getLogger(__name__)


class TelegramIO:
    def __init__(self, settings: Settings, media: MediaManager):
        self.settings = settings
        self.media = media
        self.user: TelegramClient | None = None
        self.bot: aiohttp.ClientSession | None = None
        self.bot_base = f"https://api.telegram.org/bot{settings.bot_token}"

    async def start(self):
        self.user = TelegramClient(
            StringSession(self.settings.telethon_session),
            self.settings.telegram_api_id,
            self.settings.telegram_api_hash,
            connection_retries=8,
            retry_delay=2,
        )
        await self.user.connect()
        if not await self.user.is_user_authorized():
            raise RuntimeError("TELETHON_SESSION is not authorized. Generate it locally with tools/create_telethon_session.py")
        self.bot = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        await self._bot_call("getMe", {})

    async def close(self):
        if self.user:
            await self.user.disconnect()
        if self.bot:
            await self.bot.close()

    async def _bot_call(self, method: str, data: dict, files: dict | None = None) -> dict:
        assert self.bot
        url = f"{self.bot_base}/{method}"
        last = None
        for attempt in range(5):
            try:
                if files:
                    form = aiohttp.FormData()
                    for k, v in data.items():
                        form.add_field(k, str(v))
                    for name, fp in files.items():
                        form.add_field(name, fp[1], filename=fp[0].name, content_type=fp[2])
                    async with self.bot.post(url, data=form) as resp:
                        out = await resp.json(content_type=None)
                else:
                    async with self.bot.post(url, json=data) as resp:
                        out = await resp.json(content_type=None)
                if out.get("ok"):
                    return out
                code = int(out.get("error_code", 0))
                if code == 429:
                    retry_after = int(out.get("parameters", {}).get("retry_after", 3))
                    await asyncio.sleep(min(60, retry_after + 1))
                    continue
                last = RuntimeError(str(out))
                if code and code < 500:
                    raise last
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last = exc
                await asyncio.sleep(min(20, 2**attempt))
        raise last or RuntimeError(f"Telegram API call failed: {method}")

    @staticmethod
    def normalize_channel(ref: str) -> str:
        ref = ref.strip()
        ref = re.sub(r"^https?://t\.me/", "", ref)
        ref = ref.split("?")[0].strip("/")
        return ref if ref.startswith("@") else f"@{ref}"

    async def iter_new_messages(self, channel: str, min_id: int = 0, limit: int = 100):
        assert self.user
        entity = await self.user.get_entity(self.normalize_channel(channel))
        async for msg in self.user.iter_messages(entity, min_id=min_id, limit=limit, reverse=True):
            yield entity, msg

    async def download_message_media(self, msg, max_items: int) -> list[Path]:
        assert self.user
        paths: list[Path] = []
        if not msg.media:
            return paths
        try:
            # Albums are represented as individual Telegram messages; the caller clusters them.
            p = await self.user.download_media(msg, file=str(self.settings.media_path))
            if p:
                paths.append(Path(p))
        except Exception as exc:
            log.warning("Media download failed for message %s: %s", getattr(msg, "id", "?"), exc)
        return paths[:max_items]

    async def send_text(self, text: str, disable_notification: bool = False) -> int:
        payload = {
            "chat_id": self.settings.target_channel,
            "text": text,
            "disable_notification": disable_notification,
            "disable_web_page_preview": True,
        }
        out = await self._bot_call("sendMessage", payload)
        return int(out["result"]["message_id"])

    async def send_single_media(self, path: Path, caption: str) -> int:
        kind = media_kind(path)
        method = {"photo": "sendPhoto", "video": "sendVideo", "document": "sendDocument"}[kind]
        field = {"photo": "photo", "video": "video", "document": "document"}[kind]
        with path.open("rb") as f:
            data = {
                "chat_id": self.settings.target_channel,
                "caption": caption[:1024],
            }
            out = await self._bot_call(method, data, {field: (path, f, "application/octet-stream")})
        return int(out["result"]["message_id"])

    async def send_media_group(self, paths: list[Path], caption: str) -> list[int]:
        if len(paths) == 1:
            return [await self.send_single_media(paths[0], caption)]

        # Telegram's media group supports up to 10 items. To keep the post a single album,
        # split only when needed; the caption is attached to the first item.
        paths = paths[: self.settings.max_media_per_post]
        photo_video = all(media_kind(p) in {"photo", "video"} for p in paths)
        if not photo_video:
            return [await self.send_single_media(paths[0], caption)]

        media = []
        opened = []
        form = aiohttp.FormData()
        form.add_field("chat_id", self.settings.target_channel)
        for i, p in enumerate(paths):
            handle = f"media{i}"
            f = p.open("rb")
            opened.append(f)
            kind = media_kind(p)
            item = {
                "type": kind,
                "media": f"attach://{handle}",
            }
            if i == 0:
                item["caption"] = caption[:1024]
            media.append(item)
            form.add_field(handle, f, filename=p.name, content_type="application/octet-stream")
        form.add_field("media", json.dumps(media, ensure_ascii=False))
        try:
            async with self.bot.post(f"{self.bot_base}/sendMediaGroup", data=form) as resp:
                out = await resp.json(content_type=None)
                if not out.get("ok"):
                    raise RuntimeError(str(out))
                return [int(x["message_id"]) for x in out["result"]]
        finally:
            for f in opened:
                f.close()

    async def send(self, text: str, media_paths: Iterable[Path] = ()) -> list[int]:
        paths = [p for p in media_paths if p.exists()][: self.settings.max_media_per_post]
        if not paths:
            return [await self.send_text(text)]
        return await self.send_media_group(paths, text)
