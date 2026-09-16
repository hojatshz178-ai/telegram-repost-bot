from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import AsyncIterator, Iterable
from urllib.parse import quote, urljoin

import aiohttp
from bs4 import BeautifulSoup

from .config import Settings
from .media import MediaManager, media_kind

log = logging.getLogger(__name__)


class TelegramIO:
    """Telegram I/O without Telegram API ID/HASH or a user session.

    Source channels are read through Telegram's public web preview (t.me/s/<channel>).
    Publishing still uses the official Bot API.
    """

    def __init__(self, settings: Settings, media: MediaManager):
        self.settings = settings
        self.media = media
        self.http: aiohttp.ClientSession | None = None
        self.bot_base = f"https://api.telegram.org/bot{settings.bot_token}"

    async def start(self):
        timeout = aiohttp.ClientTimeout(total=max(20, self.settings.source_request_timeout))
        self.http = aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "Mozilla/5.0 RaptorMilitaryNewsBot/2.0"})
        await self._bot_call("getMe", {})

    async def close(self):
        if self.http:
            await self.http.close()
            self.http = None

    @staticmethod
    def normalize_channel(ref: str) -> str:
        ref = ref.strip()
        ref = re.sub(r"^https?://t\.me/", "", ref)
        ref = ref.split("?")[0].strip("/")
        return ref if ref.startswith("@") else f"@{ref}"

    @staticmethod
    def channel_username(ref: str) -> str:
        return TelegramIO.normalize_channel(ref).lstrip("@")

    async def _get_text(self, url: str) -> str:
        assert self.http
        for attempt in range(4):
            try:
                async with self.http.get(url) as resp:
                    text = await resp.text(errors="ignore")
                    if resp.status == 200:
                        return text
                    if resp.status in {429, 500, 502, 503, 504}:
                        await asyncio.sleep(min(10, 1.5 ** attempt))
                        continue
                    raise RuntimeError(f"Telegram public page HTTP {resp.status}: {url}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt == 3:
                    raise
                await asyncio.sleep(min(10, 1.5 ** attempt))
        return ""

    def _parse_page(self, html_text: str, username: str) -> list[dict]:
        soup = BeautifulSoup(html_text, "html.parser")
        result: list[dict] = []
        for wrap in soup.select(".tgme_widget_message_wrap"):
            msg = wrap.select_one(".tgme_widget_message")
            if not msg:
                continue
            post_ref = msg.get("data-post", "")
            if "/" not in post_ref:
                continue
            post_id = post_ref.rsplit("/", 1)[-1]
            if not post_id.isdigit():
                continue

            text_node = msg.select_one(".tgme_widget_message_text")
            text = text_node.get_text("\n", strip=True) if text_node else ""
            date_node = msg.select_one(".tgme_widget_message_date")
            source_url = ""
            published_at = ""
            if date_node:
                anchor = date_node if date_node.name == "a" else date_node.find("a")
                if anchor:
                    source_url = urljoin("https://t.me/", anchor.get("href", ""))
                time_node = date_node.find("time") if hasattr(date_node, "find") else None
                if not time_node and date_node.name == "time":
                    time_node = date_node
                published_at = (time_node.get("datetime") if time_node else "") or ""
            if not source_url:
                source_url = f"https://t.me/{username}/{post_id}"

            media: list[dict] = []
            for photo in msg.select(".tgme_widget_message_photo_wrap"):
                style = photo.get("style", "")
                m = re.search(r"url\(['\"]?(.*?)['\"]?\)", style)
                if m:
                    media.append({"type": "photo", "url": urljoin("https://t.me/", m.group(1))})
            for video in msg.select(".tgme_widget_message_video_player"):
                thumb = video.get("style", "")
                m = re.search(r"url\(['\"]?(.*?)['\"]?\)", thumb)
                if m:
                    media.append({"type": "photo", "url": urljoin("https://t.me/", m.group(1)), "video_thumbnail": True})
                direct = video.get("data-video") or video.get("data-src") or video.get("src")
                if direct and direct.startswith("http"):
                    media.append({"type": "video", "url": direct})
            for video in msg.select("video"):
                direct = video.get("src") or video.get("data-src")
                if direct and direct.startswith("http"):
                    media.append({"type": "video", "url": direct})
            for doc in msg.select(".tgme_widget_message_document_wrap a"):
                href = doc.get("href")
                if href and href.startswith("http"):
                    media.append({"type": "document", "url": href})

            if text or media:
                result.append({
                    "id": int(post_id),
                    "text": text,
                    "published_at": published_at,
                    "source_url": source_url,
                    "media": media,
                })
        result.sort(key=lambda x: x["id"])
        return result

    async def iter_new_messages(self, channel: str, min_id: int = 0, limit: int = 100) -> AsyncIterator[dict]:
        """Yield public posts newer than min_id, using t.me/s pagination."""
        username = self.channel_username(channel)
        before: int | None = None
        seen: set[int] = set()
        pages = max(1, self.settings.source_max_pages)
        per_page = max(10, min(100, self.settings.source_page_size))

        for _ in range(pages):
            url = f"https://t.me/s/{quote(username)}"
            if before:
                url += f"?before={before}"
            html_text = await self._get_text(url)
            posts = self._parse_page(html_text, username)
            if not posts:
                break
            oldest = min(p["id"] for p in posts)
            for post in posts:
                if post["id"] > min_id and post["id"] not in seen:
                    seen.add(post["id"])
                    yield post
            if oldest <= min_id or len(posts) < per_page:
                break
            before = oldest

    async def _bot_call(self, method: str, data: dict, files: dict | None = None) -> dict:
        assert self.http
        url = f"{self.bot_base}/{method}"
        last: Exception | None = None
        for attempt in range(6):
            try:
                if files:
                    form = aiohttp.FormData()
                    for k, v in data.items():
                        form.add_field(k, str(v))
                    for name, fp in files.items():
                        form.add_field(name, fp[1], filename=fp[0].name, content_type=fp[2])
                    async with self.http.post(url, data=form) as resp:
                        out = await resp.json(content_type=None)
                else:
                    async with self.http.post(url, json=data) as resp:
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
                await asyncio.sleep(min(20, 2 ** attempt))
        raise last or RuntimeError(f"Telegram API call failed: {method}")

    async def send_text(self, text: str, disable_notification: bool = False) -> int:
        out = await self._bot_call("sendMessage", {
            "chat_id": self.settings.target_channel,
            "text": text,
            "disable_notification": disable_notification,
            "disable_web_page_preview": True,
        })
        return int(out["result"]["message_id"])

    async def send_single_media(self, path: Path, caption: str) -> int:
        kind = media_kind(path)
        method = {"photo": "sendPhoto", "video": "sendVideo", "document": "sendDocument"}[kind]
        field = {"photo": "photo", "video": "video", "document": "document"}[kind]
        with path.open("rb") as f:
            out = await self._bot_call(method, {
                "chat_id": self.settings.target_channel,
                "caption": caption[:1024],
            }, {field: (path, f, "application/octet-stream")})
        return int(out["result"]["message_id"])

    async def send_media_group(self, paths: list[Path], caption: str) -> list[int]:
        paths = paths[: self.settings.max_media_per_post]
        if len(paths) == 1:
            return [await self.send_single_media(paths[0], caption)]
        if not all(media_kind(p) in {"photo", "video"} for p in paths):
            return [await self.send_single_media(paths[0], caption)]

        assert self.http
        media = []
        opened = []
        form = aiohttp.FormData()
        form.add_field("chat_id", self.settings.target_channel)
        for i, p in enumerate(paths):
            handle = f"media{i}"
            f = p.open("rb")
            opened.append(f)
            kind = media_kind(p)
            item = {"type": kind, "media": f"attach://{handle}"}
            if i == 0:
                item["caption"] = caption[:1024]
            media.append(item)
            form.add_field(handle, f, filename=p.name, content_type="application/octet-stream")
        form.add_field("media", json.dumps(media, ensure_ascii=False))
        try:
            async with self.http.post(f"{self.bot_base}/sendMediaGroup", data=form) as resp:
                out = await resp.json(content_type=None)
                if not out.get("ok"):
                    raise RuntimeError(str(out))
                return [int(x["message_id"]) for x in out["result"]]
        finally:
            for f in opened:
                f.close()

    async def send(self, text: str, media_paths: Iterable[Path] = ()) -> list[int]:
        paths = [p for p in media_paths if p.exists()][: self.settings.max_media_per_post]
        return await self.send_media_group(paths, text) if paths else [await self.send_text(text)]
