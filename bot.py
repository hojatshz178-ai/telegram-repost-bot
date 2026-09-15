# -*- coding: utf-8 -*-
"""
ربات خبری نظامی — نسخه V2
ویژگی‌های اصلی:
- دریافت پست از کانال‌های عمومی تلگرام و فیدهای RSS
- دریافت og:image مقاله‌های سایت در صورت وجود
- تحلیل و بازنویسی با Gemini
- پشتیبانی از GEMINI_API_KEYS با هر تعداد کلید؛ برای این پروژه 14 کلید در نظر گرفته شده
- چرخش هوشمند کلیدها + cooldown برای 429/5xx
- جلوگیری از از دست رفتن خبر در خطا؛ خبر فقط پس از موفقیت نهایی علامت‌گذاری می‌شود
- تجمیع پست‌های پشت‌سرهم مربوط به یک رویداد در یک یا چند جمع‌بندی
- اولویت‌بندی: رویداد نظامی > خاورمیانه > ایران > قدرت‌های بزرگ > سایر جهان
- تنوع جغرافیایی در صف تا کانال به یک منطقه محدود نشود
- فاصله استاندارد انتشار 60 دقیقه
- در صف شلوغ، فاصله به‌صورت پویا کم می‌شود ولی هرگز کمتر از 30 دقیقه نیست
- urgent واقعی: بلافاصله منتشر می‌شود، حتی در سکوت شبانه
- مهم: خبرهای مهم زودتر از عادی منتشر می‌شوند ولی کانال را بمباران نمی‌کنند
- ذخیره پایدار وضعیت در SQLite
- retry هوشمند برای Telegram و Gemini
- اعتبارسنجی خروجی JSON و facts
- عدم استفاده از عکس استوک نامرتبط؛ در نبود تصویر معتبر، پست متنی ارسال می‌شود
"""

import os
import re
import json
import time
import html
import sqlite3
import logging
import difflib
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urljoin
from typing import Optional, List, Dict, Any, Tuple

import requests

try:
    import feedparser
except ImportError:
    feedparser = None

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


# ============================================================
# تنظیمات
# ============================================================

def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TARGET_CHAT_ID = os.environ.get("TARGET_CHAT_ID", "").strip()

SOURCE_CHANNELS = [
    c.strip().lstrip("@")
    for c in os.environ.get("SOURCE_CHANNELS", "").split(",")
    if c.strip()
]

SOURCE_WEBSITES = [
    u.strip()
    for u in os.environ.get("SOURCE_WEBSITES", "").split(",")
    if u.strip()
]

_raw_keys = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest").strip()
GEMINI_API_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

POLL_INTERVAL_SECONDS = max(30, env_int("POLL_INTERVAL_SECONDS", 300))

# انتشار:
STANDARD_SPACING_MINUTES = max(30, env_int("STANDARD_POST_SPACING_MINUTES", 60))
MIN_SPACING_MINUTES = max(30, env_int("MIN_POST_SPACING_MINUTES", 30))
MAX_SPACING_MINUTES = max(STANDARD_SPACING_MINUTES, env_int("MAX_POST_SPACING_MINUTES", 60))

# اگر صف بسیار شلوغ باشد، فاصله به سمت 30 دقیقه می‌رود، ولی از 30 کمتر نمی‌شود.
QUEUE_TARGET_HOURS = max(1.0, env_float("QUEUE_TARGET_HOURS", 12.0))

# تجمیع رویداد:
GROUP_WINDOW_MINUTES = max(5, env_int("GROUP_WINDOW_MINUTES", 45))
MAX_GROUP_POSTS = max(3, env_int("MAX_GROUP_POSTS", 12))
MAX_SUMMARY_ITEMS_PER_GROUP = max(1, env_int("MAX_SUMMARY_ITEMS_PER_GROUP", 2))

# Duplicate:
DEDUP_WINDOW_MINUTES = max(30, env_int("DEDUP_WINDOW_MINUTES", 720))
DEDUP_SIMILARITY_THRESHOLD = min(
    0.95, max(0.45, env_float("DEDUP_SIMILARITY_THRESHOLD", 0.72))
)

# سقف رسانه:
MAX_PHOTO_BYTES = max(1_000_000, env_int("MAX_PHOTO_BYTES", 10 * 1024 * 1024))
MAX_VIDEO_BYTES = max(1_000_000, env_int("MAX_VIDEO_BYTES", 50 * 1024 * 1024))

# سکوت شبانه تهران
TEHRAN_TZ = ZoneInfo("Asia/Tehran") if ZoneInfo else None
MORNING_HOUR = 8
NIGHT_HOUR = 0
QUIET_START_HOUR = 0
QUIET_END_HOUR = 8

FOOTER = "#raptor\n————————\n@khaatshekaan"

MORNING_MESSAGE = (
    "🌅 صبح بخیر به همراهان کانال\n"
    "روزتون پر از آرامش و اخبار دقیق باشه 🫡\n\n"
    + FOOTER
)
NIGHT_MESSAGE = (
    "🌙 شب بخیر رپتوری‌های عزیز\n"
    "فردا با اخبار تازه در خدمتتون هستیم 🛡️\n\n"
    + FOOTER
)

DB_FILE = os.environ.get("STATE_DB_FILE", "/data/state.db")
if not os.path.isdir(os.path.dirname(DB_FILE) or "."):
    DB_FILE = "state.db"

HTTP_TIMEOUT = (10, 30)
USER_AGENT = (
    "Mozilla/5.0 (compatible; MilitaryNewsBot/2.0; "
    "+https://telegram.org)"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("military-news-bot")


# ============================================================
# SQLite
# ============================================================

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_items (
                uid TEXT PRIMARY KEY,
                source_key TEXT NOT NULL,
                source_name TEXT NOT NULL,
                published_at TEXT,
                discovered_at TEXT NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                photo_url TEXT,
                video_url TEXT,
                source_url TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_source_items_status
            ON source_items(status);

            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                photo_url TEXT,
                video_url TEXT,
                label TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 50,
                urgent INTEGER NOT NULL DEFAULT 0,
                important INTEGER NOT NULL DEFAULT 0,
                region TEXT NOT NULL DEFAULT 'other',
                event_key TEXT,
                source_time TEXT,
                queued_at REAL NOT NULL,
                available_at REAL NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_queue_priority
            ON queue(priority DESC, urgent DESC, important DESC, queued_at ASC);

            CREATE TABLE IF NOT EXISTS posted_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                event_key TEXT,
                posted_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_posted_events_time
            ON posted_events(posted_at);

            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )


def kv_get(key: str, default: str = "") -> str:
    with db_connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO kv(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )


# ============================================================
# ابزارهای عمومی
# ============================================================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_ts() -> float:
    return time.time()


def tehran_now() -> datetime:
    if TEHRAN_TZ:
        return datetime.now(TEHRAN_TZ)
    return datetime.now()


def is_quiet_hour(dt: Optional[datetime] = None) -> bool:
    dt = dt or tehran_now()
    return QUIET_START_HOUR <= dt.hour < QUIET_END_HOUR


def clean_html_text(value: str) -> str:
    if not value:
        return ""
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n\s*\n\s*\n+", "\n\n", value)
    return value.strip()


def safe_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_feed_time(entry: Any) -> Optional[str]:
    # RSS parser معمولا parsed time را در published_parsed یا updated_parsed می‌دهد.
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        tm = entry.get(key)
        if tm:
            try:
                return datetime(*tm[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass

    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if value:
            return str(value)

    return None


def normalize_title(title: str) -> str:
    title = title.lower()
    title = re.sub(r"[\W_]+", " ", title, flags=re.UNICODE)
    return re.sub(r"\s+", " ", title).strip()


def title_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(
        None, normalize_title(a), normalize_title(b)
    ).ratio()


def html_safe_message(title: str, body: str) -> str:
    # Gemini اجازه HTML نمی‌گیرد؛ بنابراین کل متن را escape می‌کنیم.
    parts = []
    if title:
        parts.append(f"<b>{html.escape(title)}</b>")
    if body:
        parts.append(html.escape(body))
    parts.append(html.escape(FOOTER))
    return "\n\n".join(parts)


def trim_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + "…"


# ============================================================
# Telegram source scraping
# ============================================================

def fetch_channel_posts(channel: str) -> List[Dict[str, Any]]:
    """
    کانال عمومی را از t.me/s می‌خواند.
    این روش برای کانال عمومی است؛ کانال خصوصی با آن قابل دریافت نیست.
    """
    url = f"https://t.me/s/{channel}"
    try:
        resp = requests.get(
            url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("خطا در دریافت کانال %s: %s", channel, exc)
        return []

    page = resp.text
    matches = list(
        re.finditer(
            r'data-post="' + re.escape(channel) + r'/(\d+)"',
            page
        )
    )

    posts = []
    for idx, match in enumerate(matches):
        msg_id = match.group(1)
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(page)
        block = page[start:end]

        text = ""
        m = re.search(
            r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            block,
            flags=re.DOTALL | re.I,
        )
        if m:
            text = clean_html_text(m.group(1))

        photo_url = None
        pm = re.search(
            r'tgme_widget_message_photo_wrap[^"]*"\s+style="[^"]*'
            r'background-image:url\(\'([^\']+)\'\)',
            block,
            flags=re.I,
        )
        if pm:
            photo_url = html.unescape(pm.group(1))

        video_url = None
        vm = re.search(
            r'<video[^>]*class="[^"]*tgme_widget_message_video[^"]*"'
            r'[^>]*src="([^"]+)"',
            block,
            flags=re.I,
        )
        if vm:
            video_url = html.unescape(vm.group(1))

        # لینک پست منبع
        source_url = f"https://t.me/{channel}/{msg_id}"

        # زمان پست
        published_at = None
        dm = re.search(
            r'<time[^>]*datetime="([^"]+)"',
            block,
            flags=re.I,
        )
        if dm:
            published_at = dm.group(1)

        if text or photo_url or video_url:
            posts.append(
                {
                    "uid": f"tg:{channel}:{msg_id}",
                    "source_key": f"tg:{channel}",
                    "source_name": f"کانال {channel}",
                    "text": text,
                    "photo": photo_url,
                    "video": video_url,
                    "source_url": source_url,
                    "published_at": published_at,
                }
            )

    return posts


# ============================================================
# Website / RSS
# ============================================================

def fetch_og_image(article_url: str) -> Optional[str]:
    if not article_url:
        return None
    try:
        resp = requests.get(
            article_url,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
        resp.raise_for_status()
        text = resp.text[:2_000_000]

        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
        ]

        for pattern in patterns:
            m = re.search(pattern, text, flags=re.I)
            if m:
                return urljoin(resp.url, html.unescape(m.group(1).strip()))
    except requests.RequestException as exc:
        log.debug("og:image ناموفق برای %s: %s", article_url, exc)
    return None


def fetch_website_posts(feed_url: str) -> List[Dict[str, Any]]:
    if feedparser is None:
        log.error("feedparser نصب نیست.")
        return []

    try:
        parsed = feedparser.parse(feed_url)
    except Exception as exc:
        log.warning("خطا در RSS %s: %s", feed_url, exc)
        return []

    posts = []
    feed_title = parsed.feed.get("title", feed_url)

    for entry in parsed.entries[:50]:
        link = entry.get("link", "")
        uid = f"web:{feed_url}:{entry.get('id') or link or entry.get('title', '')}"
        title = clean_html_text(entry.get("title", ""))
        summary = clean_html_text(
            entry.get("summary", "") or entry.get("description", "")
        )

        # اگر RSS content:encoded داشته باشد، آن را بر summary ترجیح می‌دهیم.
        content_value = ""
        content_list = entry.get("content") or []
        if content_list:
            try:
                content_value = clean_html_text(content_list[0].get("value", ""))
            except Exception:
                content_value = ""

        article_text = content_value or summary
        text = f"{title}\n\n{article_text}".strip()

        photo_url = None
        for key in ("media_content", "media_thumbnail"):
            values = entry.get(key)
            if values:
                try:
                    photo_url = values[0].get("url")
                except Exception:
                    pass
                if photo_url:
                    break

        if not photo_url:
            for link_item in entry.get("links", []) or []:
                if str(link_item.get("type", "")).startswith("image"):
                    photo_url = link_item.get("href")
                    break

        if not photo_url and link:
            photo_url = fetch_og_image(link)

        if text or photo_url:
            posts.append(
                {
                    "uid": uid,
                    "source_key": f"web:{feed_url}",
                    "source_name": feed_title,
                    "text": text,
                    "photo": photo_url,
                    "video": None,
                    "source_url": link,
                    "published_at": parse_feed_time(entry),
                }
            )

    return posts


# ============================================================
# ثبت و دریافت آیتم‌های منبع
# ============================================================

def source_exists(uid: str) -> bool:
    with db_connect() as conn:
        return conn.execute(
            "SELECT 1 FROM source_items WHERE uid=?",
            (uid,),
        ).fetchone() is not None


def save_source_item(post: Dict[str, Any]) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO source_items
            (uid,source_key,source_name,published_at,discovered_at,text,
             photo_url,video_url,source_url,status)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                post["uid"],
                post["source_key"],
                post["source_name"],
                post.get("published_at"),
                now_utc().isoformat(),
                post.get("text", ""),
                post.get("photo"),
                post.get("video"),
                post.get("source_url"),
                "new",
            ),
        )


def mark_source_status(uid: str, status: str, error: str = "") -> None:
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE source_items
            SET status=?, attempts=attempts+1, last_error=?
            WHERE uid=?
            """,
            (status, error[:1000], uid),
        )


def get_new_source_items(limit: int = 100) -> List[Dict[str, Any]]:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM source_items
            WHERE status='new'
            ORDER BY
              CASE WHEN published_at IS NULL THEN discovered_at
                   ELSE published_at END ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ============================================================
# Gemini key manager
# ============================================================

class GeminiKeyManager:
    def __init__(self, keys: List[str]):
        self.keys = keys
        self.cooldowns: Dict[int, float] = {}
        self.cursor = 0

    def available_indices(self) -> List[int]:
        now = time.time()
        return [
            i for i in range(len(self.keys))
            if self.cooldowns.get(i, 0) <= now
        ]

    def next_key(self) -> Tuple[int, str]:
        available = self.available_indices()
        if not available:
            soonest = min(
                self.cooldowns.items(),
                key=lambda item: item[1]
            )
            wait = max(0, soonest[1] - time.time())
            log.warning(
                "همه کلیدهای Gemini در cooldown هستند؛ %.1f ثانیه صبر می‌کنیم.",
                wait,
            )
            time.sleep(min(wait, 120))
            available = self.available_indices() or [soonest[0]]

        # Round-robin فقط میان کلیدهای قابل استفاده
        for _ in range(len(self.keys)):
            idx = self.cursor % len(self.keys)
            self.cursor += 1
            if idx in available:
                return idx, self.keys[idx]

        idx = available[0]
        return idx, self.keys[idx]

    def cooldown(self, index: int, seconds: int) -> None:
        self.cooldowns[index] = time.time() + seconds


gemini_keys = GeminiKeyManager(GEMINI_API_KEYS)


# ============================================================
# Prompt
# ============================================================

BATCH_PROMPT = r"""
تو سردبیر ارشد یک کانال تلگرامی تخصصی حوزه دفاع، تسلیحات، هوافضا و ژئوپلیتیک هستی.

ورودی شامل چند پست پشت سرهم از یک منبع است. هدف این نیست که هر پست را جداگانه بازنویسی کنی.
اول تشخیص بده کدام پست‌ها واقعاً درباره یک رویداد یا زنجیره واحد هستند.

قواعد بسیار مهم:

1. پست‌هایی که درباره یک رویداد واحد هستند باید در یک گروه قرار بگیرند.
   مثال: اعلام حمله، تصاویر همان حمله، گزارش خسارت همان حمله و واکنش رسمی همان رویداد
   نباید چهار پست جداگانه شوند.

2. اگر یک منبع در فاصله کوتاه چندین آپدیت درباره یک واقعه منتشر کرده، همه را تا حد امکان
   در یک جمع‌بندی واحد ادغام کن. اگر حجم اطلاعات واقعاً زیاد است، حداکثر 2 پست مستقل
   برای همان واقعه تولید کن، نه ده‌ها پست کوتاه.

3. خبرهای مستقل را با هم ادغام نکن. مثلاً خبر یک جنگنده جدید و خبر یک ناو جدید دو رویداد
   متفاوت‌اند، حتی اگر در یک روز منتشر شده باشند.

4. محتوای تبلیغاتی، غیرمرتبط، تکراری یا فاقد ارزش خبری را حذف کن.

5. هیچ واقعیت، عدد، نام سامانه، کشور، مکان، تاریخ یا ادعای اصلی را تغییر نده.
   اگر چیزی ادعا یا تأییدنشده است، صریحاً با عباراتی مانند «بر اساس گزارش‌ها»،
   «به گفته...» یا «در صورت تأیید» بنویس.

6. بازنویسی باید فارسی طبیعی، حرفه‌ای، خبری و کوتاه باشد؛ ترجمه تحت‌اللفظی نباشد.

7. تیتر کوتاه و جدی باشد؛ از اغراق و عبارات زرد استفاده نکن.

8. اولویت:
   - رویداد نظامی، حمله، درگیری، جنگ یا تحول عملیاتی مهم: بالاترین اولویت
   - خاورمیانه: اولویت بالا
   - ایران: بالاترین اولویت در داخل پوشش منطقه‌ای
   - قدرت‌های بزرگ نظامی جهان: مهم
   - سایر نقاط جهان: همچنان پوشش داده شوند و برای تنوع وارد صف شوند

9. urgent فقط وقتی true است که خبر واقعاً نیازمند انتشار فوری باشد؛
   مثل حمله نظامی جاری، شروع درگیری مهم، اعلام عملیات، تحول فوری و مشابه آن.
   خبر عادی هرگز urgent نیست.

10. important یعنی خبر نسبت به اخبار روزمره اهمیت بیشتری دارد ولی الزاماً فوری نیست.

11. region را فقط یکی از این مقادیر قرار بده:
   iran, middle_east, great_power, other

12. event_type را فقط یکی از این مقادیر قرار بده:
   conflict, strike, military_operation, weapons, procurement, test,
   defense_industry, aircraft, naval, air_defense, geopolitical, other

13. event_key یک شناسه کوتاه و پایدار برای رویداد باشد؛ مثلاً:
   iran-israel-missile-strike
   us-patriot-europe-deployment
   اگر دو خبر رویداد یکسان دارند، event_key یکسان بده.

14. هر گروه فقط در صورت relevant=true خروجی شود.

15. خروجی فقط JSON معتبر باشد و هیچ Markdown یا توضیح اضافه نداشته باشد.

فرمت:
{
  "groups": [
    {
      "relevant": true,
      "event_key": "short-event-key",
      "region": "iran",
      "event_type": "strike",
      "urgent": false,
      "important": true,
      "source_uids": ["..."],
      "title": "تیتر",
      "body": "جمع‌بندی کامل و کوتاه",
      "facts": [
        "واقعیت مهم اول",
        "واقعیت مهم دوم"
      ]
    }
  ]
}

پست‌های ورودی:
---
{items_json}
---
"""


def gemini_request(prompt: str, max_attempts: int = 4) -> Dict[str, Any]:
    if not gemini_keys.keys:
        raise RuntimeError("هیچ GEMINI_API_KEY یا GEMINI_API_KEYS تنظیم نشده است.")

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
        },
    }

    last_error = None

    for attempt in range(max_attempts):
        index, key = gemini_keys.next_key()

        try:
            resp = requests.post(
                GEMINI_API_URL,
                headers={
                    "Content-Type": "application/json",
                    "X-goog-api-key": key,
                },
                json=payload,
                timeout=90,
            )

            status = resp.status_code

            if status == 429:
                retry_after = 30
                try:
                    body = resp.json()
                    retry_after = int(
                        body.get("error", {})
                        .get("details", [{}])[0]
                        .get("retryDelay", "30s")
                        .rstrip("s")
                    )
                except Exception:
                    pass

                gemini_keys.cooldown(index, max(30, min(retry_after, 3600)))
                last_error = RuntimeError("Gemini 429 rate limit")
                continue

            if status in (500, 502, 503, 504):
                gemini_keys.cooldown(index, min(60 * (attempt + 1), 300))
                last_error = RuntimeError(f"Gemini server error {status}")
                continue

            resp.raise_for_status()
            data = resp.json()

            candidates = data.get("candidates", [])
            if not candidates:
                raise ValueError(f"Gemini پاسخ خالی داد: {data}")

            parts = candidates[0].get("content", {}).get("parts", [])
            raw = "\n".join(
                p.get("text", "") for p in parts if p.get("text")
            ).strip()

            if not raw:
                raise ValueError("Gemini متن خروجی نداشت.")

            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                match = re.search(r"\{.*\}", raw, re.DOTALL)
                if not match:
                    raise ValueError("JSON خروجی Gemini قابل parse نیست.")
                parsed = json.loads(match.group(0))

            return parsed

        except (requests.Timeout, requests.ConnectionError) as exc:
            gemini_keys.cooldown(index, min(30 * (attempt + 1), 180))
            last_error = exc
            continue
        except requests.HTTPError as exc:
            last_error = exc
            continue

    raise RuntimeError(f"Gemini بعد از چند تلاش ناموفق بود: {last_error}")


def build_batch_input(items: List[Dict[str, Any]]) -> str:
    clean = []
    for item in items:
        clean.append(
            {
                "uid": item["uid"],
                "source_name": item["source_name"],
                "published_at": item.get("published_at"),
                "source_url": item.get("source_url"),
                "text": trim_text(item.get("text", ""), 8000),
            }
        )
    return json.dumps(clean, ensure_ascii=False)


def analyze_batch(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not items:
        return []

    prompt = BATCH_PROMPT.format(items_json=build_batch_input(items))
    result = gemini_request(prompt)
    groups = result.get("groups", [])

    if not isinstance(groups, list):
        raise ValueError("ساختار groups در پاسخ Gemini معتبر نیست.")

    return groups


# ============================================================
# اعتبارسنجی خروجی
# ============================================================

VALID_REGIONS = {"iran", "middle_east", "great_power", "other"}
VALID_EVENT_TYPES = {
    "conflict", "strike", "military_operation", "weapons", "procurement",
    "test", "defense_industry", "aircraft", "naval", "air_defense",
    "geopolitical", "other",
}


def validate_group(group: Dict[str, Any], known_uids: set) -> Optional[Dict[str, Any]]:
    if not isinstance(group, dict):
        return None

    if not bool(group.get("relevant")):
        return None

    title = trim_text(str(group.get("title") or "").strip(), 180)
    body = str(group.get("body") or "").strip()

    if not title or not body:
        return None

    source_uids = group.get("source_uids") or []
    if not isinstance(source_uids, list):
        source_uids = []

    source_uids = [str(x) for x in source_uids if str(x) in known_uids]
    if not source_uids:
        return None

    region = str(group.get("region") or "other")
    if region not in VALID_REGIONS:
        region = "other"

    event_type = str(group.get("event_type") or "other")
    if event_type not in VALID_EVENT_TYPES:
        event_type = "other"

    event_key = re.sub(
        r"[^a-zA-Z0-9_-]+", "-", str(group.get("event_key") or "unknown")
    ).strip("-")[:120] or "unknown"

    urgent = bool(group.get("urgent"))
    important = bool(group.get("important"))

    # اگر urgent است، important نیز منطقا باید در اولویت بالا قرار گیرد.
    if urgent:
        important = True

    return {
        "title": title,
        "body": body,
        "source_uids": source_uids,
        "region": region,
        "event_type": event_type,
        "event_key": event_key,
        "urgent": urgent,
        "important": important,
    }


# ============================================================
# اولویت‌بندی
# ============================================================

def calculate_priority(group: Dict[str, Any]) -> int:
    """
    عدد بزرگ‌تر = اولویت بالاتر.
    این امتیاز برای مرتب‌سازی است و نه زمان دقیق انتشار.
    """

    event_type = group.get("event_type", "other")
    region = group.get("region", "other")
    urgent = bool(group.get("urgent"))
    important = bool(group.get("important"))

    score = 50

    # 1) رویدادهای نظامی
    if event_type in {
        "conflict", "strike", "military_operation"
    }:
        score += 40
    elif event_type in {
        "weapons", "air_defense", "aircraft", "naval",
        "defense_industry", "test", "procurement"
    }:
        score += 20

    # 2) ایران
    if region == "iran":
        score += 35

    # 3) خاورمیانه
    elif region == "middle_east":
        score += 25

    # 4) قدرت‌های بزرگ
    elif region == "great_power":
        score += 15

    # 5) important
    if important:
        score += 15

    # urgent جداگانه نگه داشته می‌شود، ولی امتیاز هم می‌گیرد.
    if urgent:
        score += 100

    return score


# ============================================================
# Duplicate / Event merge
# ============================================================

def recent_posted_titles() -> List[Dict[str, Any]]:
    cutoff = now_ts() - DEDUP_WINDOW_MINUTES * 60
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT title,event_key,posted_at
            FROM posted_events
            WHERE posted_at >= ?
            ORDER BY posted_at DESC
            LIMIT 300
            """,
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def is_recent_duplicate(title: str, event_key: str) -> bool:
    for row in recent_posted_titles():
        if event_key and row.get("event_key") and event_key == row["event_key"]:
            return True
        if title_similarity(title, row["title"]) >= DEDUP_SIMILARITY_THRESHOLD:
            return True
    return False


def remember_posted_event(title: str, event_key: str) -> None:
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO posted_events(title,event_key,posted_at) VALUES(?,?,?)",
            (title, event_key, now_ts()),
        )


def merge_same_event_groups(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    اگر Gemini به هر دلیل دو گروه با event_key یکسان برگرداند، آن‌ها را قبل از صف
    تا حد امکان یکی می‌کند.
    """
    result: List[Dict[str, Any]] = []
    index: Dict[str, int] = {}

    for group in groups:
        key = group["event_key"]
        if key not in index:
            index[key] = len(result)
            result.append(group)
            continue

        old = result[index[key]]
        old["source_uids"] = list(
            dict.fromkeys(old["source_uids"] + group["source_uids"])
        )
        if len(old["body"]) < 6000:
            old["body"] = old["body"].rstrip() + "\n\n" + group["body"].lstrip()
        old["urgent"] = old["urgent"] or group["urgent"]
        old["important"] = old["important"] or group["important"]

    return result


# ============================================================
# Queue
# ============================================================

def enqueue_group(group: Dict[str, Any], label: str = "news") -> bool:
    if is_recent_duplicate(group["title"], group["event_key"]):
        log.info("Duplicate رد شد: %s", group["title"])
        return False

    photo_url = None
    video_url = None

    # media از اولین source item گروه
    with db_connect() as conn:
        placeholders = ",".join("?" for _ in group["source_uids"])
        rows = conn.execute(
            f"""
            SELECT photo_url,video_url,published_at
            FROM source_items
            WHERE uid IN ({placeholders})
            ORDER BY CASE WHEN published_at IS NULL THEN 1 ELSE 0 END,
                     published_at ASC
            """,
            tuple(group["source_uids"]),
        ).fetchall()

    for row in rows:
        if not photo_url and row["photo_url"]:
            photo_url = row["photo_url"]
        if not video_url and row["video_url"]:
            video_url = row["video_url"]

    priority = calculate_priority(group)

    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO queue
            (title,body,photo_url,video_url,label,priority,urgent,important,
             region,event_key,source_time,queued_at,available_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                group["title"],
                group["body"],
                photo_url,
                video_url,
                label,
                priority,
                int(group["urgent"]),
                int(group["important"]),
                group["region"],
                group["event_key"],
                None,
                now_ts(),
                now_ts(),
            ),
        )

    return True


def queue_count() -> int:
    with db_connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0])


def choose_next_queue_item() -> Optional[Dict[str, Any]]:
    """
    اولویت‌بندی با تنوع جغرافیایی:
    - urgent همیشه جلو است.
    - سپس priority.
    - اگر آخرین پست از همان region بوده و منطقه دیگری با اختلاف معقول وجود دارد،
      منطقه دیگر انتخاب می‌شود تا کانال یکنواخت نشود.
    """
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM queue
            WHERE available_at <= ?
            ORDER BY urgent DESC, priority DESC, important DESC, queued_at ASC
            LIMIT 30
            """,
            (now_ts(),),
        ).fetchall()

    if not rows:
        return None

    last_region = kv_get("last_post_region", "")

    if last_region:
        alternatives = [dict(r) for r in rows if r["region"] != last_region]
        if alternatives:
            top = dict(rows[0])
            alt = alternatives[0]

            # فقط وقتی تنوع را اعمال کن که اختلاف اولویت خیلی زیاد نباشد.
            if (
                not top["urgent"]
                and alt["priority"] >= top["priority"] - 25
            ):
                return alt

    return dict(rows[0])


def delete_queue_item(item_id: int) -> None:
    with db_connect() as conn:
        conn.execute("DELETE FROM queue WHERE id=?", (item_id,))


def compute_spacing_minutes() -> int:
    """
    فاصله:
    - صف خلوت => 60 دقیقه
    - صف شلوغ => به سمت 30 دقیقه
    - هیچ‌وقت کمتر از 30 دقیقه
    """
    count = queue_count()
    if count <= 1:
        return STANDARD_SPACING_MINUTES

    # تعداد پست‌هایی که باید در بازه هدف جا شوند.
    target_slots = max(
        1.0,
        QUEUE_TARGET_HOURS * 60 / MIN_SPACING_MINUTES
    )

    # نسبت فشار صف؛ هرچه صف بزرگ‌تر باشد، spacing کمتر می‌شود.
    pressure = min(1.0, count / target_slots)

    spacing = (
        STANDARD_SPACING_MINUTES
        - pressure * (STANDARD_SPACING_MINUTES - MIN_SPACING_MINUTES)
    )

    return int(round(
        max(MIN_SPACING_MINUTES, min(MAX_SPACING_MINUTES, spacing))
    ))


def process_queue() -> None:
    if is_quiet_hour():
        return

    item = choose_next_queue_item()
    if not item:
        return

    last_release = float(kv_get("last_queue_release_ts", "0") or "0")
    elapsed = (time.time() - last_release) / 60
    spacing = compute_spacing_minutes()

    # اگر این آیتم urgent نیست، فاصله استاندارد/پویا رعایت شود.
    if not item["urgent"] and last_release > 0 and elapsed < spacing:
        return

    title = item["title"]
    if item["label"] == "night_leftover":
        title = "🕛 خبر دیشب | " + title

    final_msg = html_safe_message(title, item["body"])

    try:
        dispatch_post(final_msg, item.get("photo_url"), item.get("video_url"))

        delete_queue_item(item["id"])
        remember_posted_event(item["title"], item.get("event_key") or "")
        kv_set("last_queue_release_ts", str(time.time()))
        kv_set("last_post_region", item.get("region") or "other")

        log.info(
            "پست صف منتشر شد | priority=%s | region=%s | spacing=%s min | title=%s",
            item["priority"],
            item["region"],
            spacing,
            item["title"],
        )
    except Exception as exc:
        # حذف نمی‌کنیم؛ آیتم در صف می‌ماند تا retry شود.
        log.error("ارسال آیتم صف ناموفق بود: %s", exc)


# ============================================================
# Telegram output
# ============================================================

def telegram_request(
    method: str,
    data: Optional[Dict[str, Any]] = None,
    files: Optional[Dict[str, Any]] = None,
    attempts: int = 4,
) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    last_exc = None

    for attempt in range(attempts):
        try:
            if files:
                resp = requests.post(
                    url,
                    data=data or {},
                    files=files,
                    timeout=120,
                )
            else:
                resp = requests.post(
                    url,
                    data=data or {},
                    timeout=30,
                )

            if resp.status_code == 429:
                retry_after = 30
                try:
                    body = resp.json()
                    retry_after = int(
                        body.get("parameters", {}).get("retry_after", 30)
                    )
                except Exception:
                    pass

                wait = min(max(retry_after, 1), 300)
                log.warning("Telegram rate limit؛ %s ثانیه صبر.", wait)
                time.sleep(wait)
                continue

            if resp.status_code >= 500:
                time.sleep(min(10 * (attempt + 1), 60))
                continue

            resp.raise_for_status()
            result = resp.json()

            if not result.get("ok"):
                raise RuntimeError(str(result))

            return result

        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            time.sleep(min(5 * (attempt + 1), 30))
        except requests.HTTPError as exc:
            last_exc = exc
            break

    raise RuntimeError(f"Telegram request failed: {last_exc}")


def download_bytes(url: str, max_bytes: int, timeout: int = 60) -> bytes:
    """
    دانلود stream شده برای جلوگیری از مصرف بی‌دلیل RAM.
    """
    with requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
        stream=True,
        allow_redirects=True,
    ) as resp:
        resp.raise_for_status()

        chunks = []
        total = 0

        for chunk in resp.iter_content(chunk_size=256 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(
                    f"رسانه بزرگ‌تر از حد مجاز است: {total} bytes"
                )
            chunks.append(chunk)

        return b"".join(chunks)


def send_text_to_telegram(text: str) -> None:
    telegram_request(
        "sendMessage",
        data={
            "chat_id": TARGET_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        },
    )


def send_photo_to_telegram(caption: str, photo_url: str) -> None:
    """
    ابتدا upload مستقیم.
    اگر عکس در دسترس نبود، پست را بدون عکس نمی‌فرستیم؛ caller fallback می‌کند.
    """
    photo_bytes = download_bytes(photo_url, MAX_PHOTO_BYTES, timeout=60)

    telegram_request(
        "sendPhoto",
        data={
            "chat_id": TARGET_CHAT_ID,
            "caption": caption,
            "parse_mode": "HTML",
        },
        files={
            "photo": ("photo.jpg", photo_bytes),
        },
    )


def send_video_to_telegram(caption: str, video_url: str) -> None:
    video_bytes = download_bytes(video_url, MAX_VIDEO_BYTES, timeout=120)

    telegram_request(
        "sendVideo",
        data={
            "chat_id": TARGET_CHAT_ID,
            "caption": caption,
            "parse_mode": "HTML",
            "supports_streaming": "true",
        },
        files={
            "video": ("video.mp4", video_bytes),
        },
    )


def dispatch_post(final_msg: str, photo_url: Optional[str], video_url: Optional[str]) -> None:
    """
    نکته مهم:
    Caption تلگرام محدود است. اگر متن بیش از 1024 کاراکتر باشد،
    رسانه را جداگانه می‌فرستیم و متن را جداگانه تا خبر ناقص نشود.
    """
    if video_url:
        if len(final_msg) <= 1024:
            try:
                send_video_to_telegram(final_msg, video_url)
                return
            except Exception as exc:
                log.warning("ارسال ویدئو با caption شکست خورد: %s", exc)
        else:
            try:
                send_video_to_telegram("", video_url)
                send_text_to_telegram(final_msg)
                return
            except Exception as exc:
                log.warning("ارسال ویدئو شکست خورد: %s", exc)

    if photo_url:
        if len(final_msg) <= 1024:
            try:
                send_photo_to_telegram(final_msg, photo_url)
                return
            except Exception as exc:
                log.warning("ارسال عکس شکست خورد؛ fallback به متن: %s", exc)
        else:
            try:
                send_photo_to_telegram("", photo_url)
                send_text_to_telegram(final_msg)
                return
            except Exception as exc:
                log.warning("ارسال عکس جداگانه شکست خورد: %s", exc)

    # در نبود/خرابی رسانه، خبر هرگز از دست نمی‌رود.
    send_text_to_telegram(final_msg)


# ============================================================
# پردازش منابع
# ============================================================

def source_batch_ready(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    برای تجمیع پست‌های پشت‌سرهم یک منبع:
    - آیتم‌ها را به دسته‌های کوچک تقسیم می‌کنیم.
    - Gemini خودش تشخیص می‌دهد کدام‌ها یک رویدادند.
    """
    items = sorted(
        items,
        key=lambda x: x.get("published_at") or x.get("discovered_at") or ""
    )

    batches = []
    current = []

    for item in items:
        if len(current) >= MAX_GROUP_POSTS:
            batches.append(current)
            current = []

        if current:
            # اگر timestamp قابل تبدیل باشد، فاصله را بررسی می‌کنیم.
            # در صورت نبود زمان، فقط سقف تعداد را ملاک می‌گیریم.
            prev = current[-1].get("published_at")
            cur = item.get("published_at")

            if prev and cur:
                try:
                    p = datetime.fromisoformat(prev.replace("Z", "+00:00"))
                    c = datetime.fromisoformat(cur.replace("Z", "+00:00"))
                    if abs((c - p).total_seconds()) > GROUP_WINDOW_MINUTES * 60:
                        batches.append(current)
                        current = []
                except Exception:
                    pass

        current.append(item)

    if current:
        batches.append(current)

    return batches


def process_new_items() -> None:
    items = get_new_source_items(limit=100)
    if not items:
        return

    # ابتدا بر اساس source جدا می‌کنیم؛ پست‌های پشت‌سرهم هر منبع کنار هم قرار می‌گیرند.
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        by_source.setdefault(item["source_key"], []).append(item)

    for source_key, source_items in by_source.items():
        for batch in source_batch_ready(source_items):
            uids = [x["uid"] for x in batch]

            try:
                groups = analyze_batch(batch)
                known_uids = set(uids)

                validated = []
                for group in groups:
                    v = validate_group(group, known_uids)
                    if v:
                        validated.append(v)

                validated = merge_same_event_groups(validated)

                # اگر Gemini چیزی برنگرداند، خبرها را failed نمی‌کنیم؛
                # دوباره در اجرای بعدی retry می‌شوند.
                if not validated:
                    raise ValueError("Gemini هیچ گروه خبری معتبر برنگرداند.")

                assigned = set()

                for group in validated:
                    if enqueue_group(group, label="news"):
                        assigned.update(group["source_uids"])

                # UIDهایی که Gemini صراحتاً به گروهی نسبت نداده، به عنوان processed
                # ثبت نمی‌شوند؛ تا خبر از بین نرود و در اجرای بعدی دوباره بررسی شود.
                for uid in assigned:
                    mark_source_status(uid, "processed")

                unassigned = set(uids) - assigned

                # اگر Gemini گروهی را relevant=false تشخیص داده باشد، در خروجی source_uids
                # ندارد؛ برای جلوگیری از پردازش بی‌نهایت، باید این موارد نیز به شکل ردشده ثبت شوند.
                # اما فقط وقتی مطمئنیم پاسخ Gemini معتبر بوده.
                for uid in unassigned:
                    mark_source_status(uid, "rejected")

                log.info(
                    "منبع %s: %s آیتم → %s گروه خبری",
                    source_key,
                    len(batch),
                    len(validated),
                )

            except Exception as exc:
                log.error(
                    "پردازش batch منبع %s شکست خورد؛ آیتم‌ها برای retry باقی می‌مانند: %s",
                    source_key,
                    exc,
                )
                # عمداً status را تغییر نمی‌دهیم.


# ============================================================
# Scheduled messages
# ============================================================

def check_scheduled_messages() -> None:
    now = tehran_now()
    today = now.strftime("%Y-%m-%d")

    if now.hour == MORNING_HOUR and kv_get("last_morning_date") != today:
        try:
            send_text_to_telegram(html_safe_message("", MORNING_MESSAGE))
            kv_set("last_morning_date", today)
            log.info("پیام صبح‌بخیر ارسال شد.")
        except Exception as exc:
            log.error("پیام صبح‌بخیر ارسال نشد: %s", exc)

    if now.hour == NIGHT_HOUR and kv_get("last_night_date") != today:
        try:
            send_text_to_telegram(html_safe_message("", NIGHT_MESSAGE))
            kv_set("last_night_date", today)
            log.info("پیام شب‌بخیر ارسال شد.")
        except Exception as exc:
            log.error("پیام شب‌بخیر ارسال نشد: %s", exc)


# ============================================================
# Fetch cycle
# ============================================================

def collect_sources() -> None:
    for channel in SOURCE_CHANNELS:
        try:
            posts = fetch_channel_posts(channel)
            for post in posts:
                if not source_exists(post["uid"]):
                    save_source_item(post)
        except Exception as exc:
            log.error("خطا در منبع تلگرام %s: %s", channel, exc)

    for feed_url in SOURCE_WEBSITES:
        try:
            posts = fetch_website_posts(feed_url)
            for post in posts:
                if not source_exists(post["uid"]):
                    save_source_item(post)
        except Exception as exc:
            log.error("خطا در RSS %s: %s", feed_url, exc)


# ============================================================
# Main
# ============================================================

def validate_config() -> List[str]:
    missing = []

    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not TARGET_CHAT_ID:
        missing.append("TARGET_CHAT_ID")
    if not GEMINI_API_KEYS:
        missing.append("GEMINI_API_KEYS")
    if not SOURCE_CHANNELS and not SOURCE_WEBSITES:
        missing.append("SOURCE_CHANNELS یا SOURCE_WEBSITES")

    return missing


def main() -> None:
    init_db()

    missing = validate_config()
    if missing:
        log.error("متغیرهای لازم تنظیم نشده‌اند: %s", ", ".join(missing))
        return

    log.info("==============================================")
    log.info("Military News Bot V2 started")
    log.info("Telegram sources: %s", len(SOURCE_CHANNELS))
    log.info("RSS sources: %s", len(SOURCE_WEBSITES))
    log.info("Gemini keys configured: %s", len(GEMINI_API_KEYS))
    log.info(
        "Posting spacing: standard=%s min=%s max=%s",
        STANDARD_SPACING_MINUTES,
        MIN_SPACING_MINUTES,
        MAX_SPACING_MINUTES,
    )
    log.info(
        "Event grouping: window=%s min max_posts=%s",
        GROUP_WINDOW_MINUTES,
        MAX_GROUP_POSTS,
    )
    log.info("Persistent DB: %s", DB_FILE)
    log.info("==============================================")

    while True:
        cycle_started = time.time()

        try:
            collect_sources()
        except Exception as exc:
            log.error("خطای collect_sources: %s", exc)

        try:
            process_new_items()
        except Exception as exc:
            log.error("خطای process_new_items: %s", exc)

        try:
            check_scheduled_messages()
        except Exception as exc:
            log.error("خطای scheduled messages: %s", exc)

        try:
            process_queue()
        except Exception as exc:
            log.error("خطای process_queue: %s", exc)

        elapsed = time.time() - cycle_started
        sleep_for = max(5, POLL_INTERVAL_SECONDS - int(elapsed))
        log.info(
            "چرخه تمام شد؛ صف=%s؛ اجرای بعدی حدود %s ثانیه دیگر.",
            queue_count(),
            sleep_for,
        )
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
