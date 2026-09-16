from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp

from .config import Settings


class MediaManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.media_path.mkdir(parents=True, exist_ok=True)
        self.http: aiohttp.ClientSession | None = None

    async def start(self):
        self.http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=40),
            headers={"User-Agent": self.settings.wikimedia_user_agent},
        )

    async def close(self):
        if self.http:
            await self.http.close()

    def file_for(self, url_or_id: str, ext: str) -> Path:
        digest = hashlib.sha256(url_or_id.encode()).hexdigest()[:24]
        return self.settings.media_path / f"{digest}{ext}"

    async def download_url(self, url: str, suffix: str = ".bin") -> Path | None:
        if not self.http:
            raise RuntimeError("MediaManager not started")
        path = self.file_for(url, suffix)
        if path.exists() and path.stat().st_size > 0:
            return path
        try:
            async with self.http.get(url) as resp:
                if resp.status != 200:
                    return None
                ct = (resp.headers.get("Content-Type") or "").lower()
                if "text/html" in ct:
                    return None
                data = await resp.read()
                if len(data) < 512:
                    return None
                path.write_bytes(data)
                return path
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return None

    async def wikimedia_search(self, query: str) -> str | None:
        if not self.http or not self.settings.image_search_enabled or not query:
            return None
        params = {
            "action": "query",
            "generator": "search",
            "gsrsearch": f"{query} filetype:bitmap",
            "gsrnamespace": "6",
            "gsrlimit": "8",
            "prop": "imageinfo",
            "iiprop": "url|mime|size",
            "iiurlwidth": "1600",
            "format": "json",
        }
        try:
            async with self.http.get("https://commons.wikimedia.org/w/api.php", params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                pages = data.get("query", {}).get("pages", {})
                candidates = []
                for p in pages.values():
                    info = (p.get("imageinfo") or [{}])[0]
                    mime = info.get("mime", "")
                    url = info.get("thumburl") or info.get("url")
                    width = int(info.get("thumbwidth") or info.get("width") or 0)
                    height = int(info.get("thumbheight") or info.get("height") or 0)
                    if url and mime.startswith("image/") and width >= 600 and height >= 300:
                        candidates.append((width * height, url))
                candidates.sort(reverse=True)
                return candidates[0][1] if candidates else None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

    async def fallback_image(self, query: str) -> Path | None:
        url = await self.wikimedia_search(query)
        if not url:
            return None
        return await self.download_url(url, suffix=Path(url).suffix or ".jpg")

    async def cleanup(self):
        cutoff = time.time() - (self.settings.media_retention_hours * 3600)
        # mtime is a practical cleanup signal; DB records retain source metadata.
        for p in self.settings.media_path.iterdir():
            if not p.is_file() or p.name == ".gitkeep":
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except OSError:
                pass


def media_kind(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if mime and mime.startswith("video/"):
        return "video"
    if mime and mime.startswith("image/"):
        return "photo"
    return "document"
