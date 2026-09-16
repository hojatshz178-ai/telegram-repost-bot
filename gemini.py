from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from config import Settings


TRANSIENT = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass
class KeyState:
    key: str
    cooldown_until: datetime | None = None
    fail_count: int = 0
    last_used: datetime | None = None


class GeminiPool:
    """Multi-key, bounded-concurrency Gemini REST client with circuit-breaker style cooldowns."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.keys = [KeyState(k) for k in settings.gemini_keys]
        self.models = [settings.gemini_model, *settings.gemini_fallback_models]
        # Deduplicate while preserving order.
        self.models = list(dict.fromkeys(self.models))
        self._index = 0
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(settings.gemini_max_concurrency)
        self.session: aiohttp.ClientSession | None = None

    async def start(self):
        timeout = aiohttp.ClientTimeout(total=self.settings.gemini_timeout)
        self.session = aiohttp.ClientSession(timeout=timeout)

    async def close(self):
        if self.session:
            await self.session.close()

    async def _pick_key(self) -> KeyState:
        async with self._lock:
            now = datetime.now(timezone.utc)
            for _ in range(len(self.keys)):
                ks = self.keys[self._index % len(self.keys)]
                self._index = (self._index + 1) % len(self.keys)
                if not ks.cooldown_until or ks.cooldown_until <= now:
                    ks.last_used = now
                    return ks
            # All are cooling down; sleep until the earliest cooldown expires.
            ks = min(self.keys, key=lambda x: x.cooldown_until or now)
            delay = max(0.5, (ks.cooldown_until - now).total_seconds()) if ks.cooldown_until else 0.5
        await asyncio.sleep(min(delay, 10.0))
        return await self._pick_key()

    def _cooldown(self, ks: KeyState, seconds: int, severe: bool = False):
        now = datetime.now(timezone.utc)
        ks.fail_count = min(20, ks.fail_count + (2 if severe else 1))
        multiplier = min(3, max(1, ks.fail_count // 3))
        ks.cooldown_until = now + timedelta(seconds=seconds * multiplier)

    async def generate(self, prompt: str, *, temperature: float = 0.2, max_output_tokens: int = 1200) -> str:
        if not self.session:
            raise RuntimeError("GeminiPool.start() was not called")

        last_error: Exception | None = None
        async with self._sem:
            for attempt in range(self.settings.gemini_max_retries + 1):
                ks = await self._pick_key()
                for model in self.models:
                    url = f"https://generativelanguage.googleapis.com/{self.settings.gemini_api_version}/models/{model}:generateContent"
                    payload: dict[str, Any] = {
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "temperature": temperature,
                            "maxOutputTokens": max_output_tokens,
                            "candidateCount": 1,
                        },
                    }
                    try:
                        async with self.session.post(
                            url,
                            json=payload,
                            headers={"Content-Type": "application/json", "x-goog-api-key": ks.key},
                        ) as resp:
                            data = await resp.json(content_type=None)
                            if resp.status == 200:
                                ks.fail_count = 0
                                ks.cooldown_until = None
                                text = self._extract_text(data)
                                if not text:
                                    raise RuntimeError("Gemini returned no text")
                                return text.strip()

                            msg = self._error_message(resp.status, data)
                            if resp.status in {400, 401, 403, 404}:
                                # Try the next model with the same key before giving up on the attempt.
                                last_error = RuntimeError(msg)
                                continue
                            if resp.status in TRANSIENT:
                                self._cooldown(
                                    ks,
                                    self.settings.gemini_429_cooldown if resp.status == 429 else self.settings.gemini_503_cooldown,
                                    severe=resp.status >= 500,
                                )
                                last_error = RuntimeError(msg)
                                break
                            last_error = RuntimeError(msg)
                            break
                    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                        self._cooldown(ks, self.settings.gemini_503_cooldown, severe=True)
                        last_error = exc
                        break

                if attempt < self.settings.gemini_max_retries:
                    # Exponential backoff + jitter, matching Google's guidance for transient failures.
                    base = min(30.0, 1.0 * (2**attempt))
                    await asyncio.sleep(base + random.uniform(0, 0.75))

        raise last_error or RuntimeError("Gemini request failed")

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        try:
            candidates = data.get("candidates") or []
            parts = candidates[0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        except (KeyError, IndexError, TypeError):
            return ""

    @staticmethod
    def _error_message(status: int, data: Any) -> str:
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                return f"Gemini HTTP {status}: {err.get('status', '')} {err.get('message', '')}".strip()
        return f"Gemini HTTP {status}"
