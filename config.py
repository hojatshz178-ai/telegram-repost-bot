from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()


def _csv(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


def _gemini_keys() -> tuple[str, ...]:
    values = _csv("GEMINI_API_KEYS")
    for i in range(1, 100):
        v = os.getenv(f"GEMINI_API_KEY_{i}", "").strip()
        if v:
            values.append(v)
    return tuple(dict.fromkeys(values))


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    bot_token: str
    target_channel: str
    source_channels: tuple[str, ...]
    gemini_keys: tuple[str, ...]
    gemini_model: str
    gemini_fallback_models: tuple[str, ...]
    gemini_api_version: str
    gemini_max_concurrency: int
    gemini_max_retries: int
    gemini_timeout: int
    gemini_429_cooldown: int
    gemini_503_cooldown: int
    image_search_enabled: bool
    wikimedia_user_agent: str
    timezone_name: str
    quiet_start: str
    quiet_end: str
    default_spacing: int
    min_spacing: int
    queue_poll_seconds: int
    ingest_poll_seconds: int
    source_page_size: int
    source_max_pages: int
    source_request_timeout: int
    burst_window_minutes: int
    event_lookback_hours: int
    low_priority_drop: int
    q_t1: int
    q_t2: int
    q_t3: int
    media_retention_hours: int
    max_media_per_post: int
    good_morning_enabled: bool
    good_night_enabled: bool
    good_morning_text: str
    good_night_text: str
    database_path: Path
    log_level: str
    media_path: Path

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)


settings = Settings(
    bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
    target_channel=os.getenv("TELEGRAM_TARGET_CHANNEL", "@khaatshekaan").strip(),
    source_channels=tuple(_csv("TELEGRAM_SOURCE_CHANNELS")),
    gemini_keys=_gemini_keys(),
    gemini_model=os.getenv("GEMINI_MODEL", "gemini-flash-latest").strip(),
    gemini_fallback_models=tuple(_csv("GEMINI_FALLBACK_MODELS")),
    gemini_api_version=os.getenv("GEMINI_API_VERSION", "v1beta").strip(),
    gemini_max_concurrency=int(os.getenv("GEMINI_MAX_CONCURRENCY", "2")),
    gemini_max_retries=int(os.getenv("GEMINI_MAX_RETRIES", "5")),
    gemini_timeout=int(os.getenv("GEMINI_TIMEOUT_SECONDS", "45")),
    gemini_429_cooldown=int(os.getenv("GEMINI_COOLDOWN_429_SECONDS", "60")),
    gemini_503_cooldown=int(os.getenv("GEMINI_COOLDOWN_503_SECONDS", "30")),
    image_search_enabled=_bool("IMAGE_SEARCH_ENABLED", True),
    wikimedia_user_agent=os.getenv("WIKIMEDIA_USER_AGENT", "RaptorMilitaryNewsBot/2.0").strip(),
    timezone_name=os.getenv("TIMEZONE", "Asia/Tehran").strip(),
    quiet_start=os.getenv("QUIET_START", "00:00").strip(),
    quiet_end=os.getenv("QUIET_END", "08:00").strip(),
    default_spacing=int(os.getenv("DEFAULT_SPACING_MINUTES", "60")),
    min_spacing=int(os.getenv("MIN_SPACING_MINUTES", "30")),
    queue_poll_seconds=int(os.getenv("QUEUE_POLL_SECONDS", "30")),
    ingest_poll_seconds=int(os.getenv("INGEST_POLL_SECONDS", "60")),
    source_page_size=int(os.getenv("SOURCE_PAGE_SIZE", "50")),
    source_max_pages=int(os.getenv("SOURCE_MAX_PAGES", "4")),
    source_request_timeout=int(os.getenv("SOURCE_REQUEST_TIMEOUT_SECONDS", "30")),
    burst_window_minutes=int(os.getenv("BURST_WINDOW_MINUTES", "20")),
    event_lookback_hours=int(os.getenv("EVENT_LOOKBACK_HOURS", "48")),
    low_priority_drop=int(os.getenv("LOW_PRIORITY_DROP_AT_MIDNIGHT", "45")),
    q_t1=int(os.getenv("QUEUE_T1", "5")),
    q_t2=int(os.getenv("QUEUE_T2", "10")),
    q_t3=int(os.getenv("QUEUE_T3", "20")),
    media_retention_hours=int(os.getenv("MEDIA_RETENTION_HOURS", "72")),
    max_media_per_post=int(os.getenv("MAX_MEDIA_PER_POST", "10")),
    good_morning_enabled=_bool("GOOD_MORNING_ENABLED", True),
    good_night_enabled=_bool("GOOD_NIGHT_ENABLED", True),
    good_morning_text=os.getenv("GOOD_MORNING_TEXT", "🌅 صبح بخیر؛ در جریان مهم‌ترین تحولات دفاعی و نظامی امروز باشید.").strip(),
    good_night_text=os.getenv("GOOD_NIGHT_TEXT", "🌙 شب بخیر؛ مهم‌ترین تحولات نظامی و دفاعی امروز را دنبال کردید. خبرهای ضروری در ساعات سکوت نیز رصد می‌شوند.").strip(),
    database_path=Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3")),
    log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    media_path=Path(os.getenv("MEDIA_PATH", "media")),
)


def validate() -> None:
    missing = []
    if not settings.bot_token or settings.bot_token.startswith("REPLACE_"):
        missing.append("TELEGRAM_BOT_TOKEN")
    if not settings.source_channels:
        missing.append("TELEGRAM_SOURCE_CHANNELS")
    if not settings.gemini_keys or any(k.startswith("REPLACE_") for k in settings.gemini_keys):
        missing.append("GEMINI_API_KEY_1..N")
    if settings.default_spacing < settings.min_spacing or settings.min_spacing < 30:
        raise ValueError("Spacing must satisfy DEFAULT_SPACING_MINUTES >= MIN_SPACING_MINUTES >= 30")
    if not 1 <= settings.gemini_max_concurrency <= 10:
        raise ValueError("GEMINI_MAX_CONCURRENCY must be between 1 and 10")
    if settings.source_page_size < 10 or settings.source_page_size > 100:
        raise ValueError("SOURCE_PAGE_SIZE must be 10..100")
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))
