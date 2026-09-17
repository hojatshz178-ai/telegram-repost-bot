import os
import re
import json
import time
import html
import hashlib
import logging
import tempfile
import difflib
from datetime import datetime, timezone, timedelta

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("raptor-news-bot")


# ============================================================
# Configuration
# ============================================================

def env_int(name, default):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} باید عدد صحیح باشد؛ مقدار فعلی: {value!r}") from exc


def env_float(name, default):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} باید عدد اعشاری باشد؛ مقدار فعلی: {value!r}") from exc


def split_csv(value):
    return [x.strip() for x in (value or "").split(",") if x.strip()]


BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TARGET_CHAT_ID = os.environ.get("TARGET_CHAT_ID", "").strip()

SOURCE_CHANNELS = [
    x.lstrip("@").strip()
    for x in split_csv(os.environ.get("SOURCE_CHANNELS", ""))
]

# هر تعداد کلید را قبول می‌کند؛ برای تنظیم فعلی کاربر 14 کلید را در همین متغیر قرار بده.
_raw_gemini_keys = (
    os.environ.get("GEMINI_API_KEYS")
    or os.environ.get("GEMINI_API_KEY", "")
)
GEMINI_API_KEYS = split_csv(_raw_gemini_keys)

# مدل قابل تنظیم و pinned؛ alias متغیر latest پیش‌فرض نیست.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    f"models/{GEMINI_MODEL}:generateContent"
)

POLL_INTERVAL_SECONDS = max(30, env_int("POLL_INTERVAL_SECONDS", 180))

# تشخیص تکرار بین منابع.
DEDUP_WINDOW_MINUTES = max(60, env_int("DEDUP_WINDOW_MINUTES", 360))
DEDUP_SIMILARITY_THRESHOLD = min(
    0.95,
    max(0.72, env_float("DEDUP_SIMILARITY_THRESHOLD", 0.82)),
)

# زمان‌بندی ثابت انتشار نسخه 3.5:
# 08:00 تا قبل از 14:00 -> هر 60 دقیقه
# 14:00 تا قبل از 24:00 -> هر 45 دقیقه
# حجم صف هیچ‌وقت فاصله انتشار را کاهش نمی‌دهد.
MORNING_POST_SPACING_MINUTES = 60
AFTERNOON_POST_SPACING_MINUTES = 45

# گروه‌بندی خبرهای مربوط به یک رویداد.
EVENT_CLUSTER_WINDOW_MINUTES = max(
    30, env_int("EVENT_CLUSTER_WINDOW_MINUTES", 90)
)
EVENT_MAX_ITEMS_PER_CLUSTER = max(
    2, min(10, env_int("EVENT_MAX_ITEMS_PER_CLUSTER", 6))
)
EVENT_MAX_OUTPUT_ITEMS = max(
    1, min(6, env_int("EVENT_MAX_OUTPUT_ITEMS", 6))
)

# کاهش مصرف Gemini: چند پست/رویداد مستقل در یک درخواست بررسی می‌شوند.
GEMINI_BATCH_MAX_POSTS = max(2, min(10, env_int("GEMINI_BATCH_MAX_POSTS", 6)))
GEMINI_INPUT_MAX_CHARS_PER_POST = max(800, min(3500, env_int("GEMINI_INPUT_MAX_CHARS_PER_POST", 2200)))
GEMINI_RECENT_INPUT_WINDOW_MINUTES = max(60, env_int("GEMINI_RECENT_INPUT_WINDOW_MINUTES", 720))
GEMINI_LOCAL_DUP_THRESHOLD = min(0.96, max(0.80, env_float("GEMINI_LOCAL_DUP_THRESHOLD", 0.88)))

# فقط برای ساعت سکوت؛ خبرهای عادی در صف می‌روند.
MORNING_HOUR = 8
NIGHT_HOUR = 0
QUIET_START_HOUR = 0
QUIET_END_HOUR = 8

# حجم رسانه.
MAX_IMAGE_BYTES = max(
    1_000_000, env_int("MAX_IMAGE_BYTES", 10 * 1024 * 1024)
)
MAX_VIDEO_BYTES = max(
    5_000_000, env_int("MAX_VIDEO_BYTES", 45 * 1024 * 1024)
)

# وضعیت روی volume دائمی قرار بگیرد.
DEFAULT_STATE_FILE = "/data/state.json" if os.path.isdir("/data") else "state.json"
STATE_FILE = os.environ.get("STATE_FILE", DEFAULT_STATE_FILE)

# User-Agent ثابت برای صفحات عمومی و منابع تلگرامی.
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; RaptorNewsBot/2.0; "
        "+https://t.me/khaatshekaan)"
    )
}

FOOTER = "#raptor\n————————\n@khaatshekaan"

TEHRAN_TZ = None
if ZoneInfo is not None:
    try:
        TEHRAN_TZ = ZoneInfo("Asia/Tehran")
    except Exception:
        TEHRAN_TZ = timezone.utc

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


# ============================================================
# Constants: geography / priority
# ============================================================

IRAN_TERMS = {
    "iran", "ایران", "تهران", "tehran", "isfahan", "اصفهان", "تبریز",
    "tbriz", "shiraz", "شیراز", "سپاه", "ارتش", "نیروی قدس", "iranian",
    "iranian forces", "persian gulf", "خلیج فارس",
}

MIDDLE_EAST_TERMS = {
    "iran", "ایران", "iraq", "عراق", "syria", "سوریه", "lebanon", "لبنان",
    "israel", "اسرائیل", "palestine", "فلسطین", "gaza", "غزه", "yemen",
    "یمن", "saudi", "saudi arabia", "عربستان", "uae", "امارات", "qatar",
    "قطر", "bahrain", "بحرین", "jordan", "اردن", "egypt", "مصر", "turkey",
    "ترکیه", "iraqi", "syrian", "israeli", "levant", "middle east",
    "خاورمیانه", "غزه", "کرانه باختری", "دریای سرخ", "red sea",
    "عراق", "عمانی", "oman", "عمان",
}

SUPERPOWER_TERMS = {
    "united states", "usa", "u.s.", "america", "آمریکا", "ایالات متحده",
    "russia", "روسیه", "moscow", "مسکو", "china", "چین", "beijing", "پکن",
    "united states", "france", "فرانسه", "uk", "بریتانیا", "britain",
    "germany", "آلمان", "japan", "ژاپن", "india", "هند",
}

MILITARY_EVENT_TERMS = {
    "war", "جنگ", "attack", "حمله", "strike", "حملات", "airstrike",
    "bombing", "بمباران", "invasion", "تهاجم", "clash", "درگیری",
    "conflict", "تنش نظامی", "missile", "موشک", "drone", "پهپاد",
    "fighter", "جنگنده", "air defense", "پدافند", "shootdown", "سرنگونی",
    "intercept", "رهگیری", "navy", "نیروی دریایی", "military", "نظامی",
    "army", "ارتش", "operation", "عملیات", "offensive", "تهاجمی",
    "ceasefire", "آتش‌بس", "escalation", "تشدید درگیری", "mobilization",
    "بسیج", "deployment", "استقرار", "base", "پایگاه", "explosion",
    "انفجار", "casualties", "تلفات", "front", "جبهه", "battle", "نبرد",
}

BREAKING_TERMS = {
    "breaking", "urgent", "فوری", "خبر فوری", "عاجل", "العاجل",
    "لحظه‌ای", "همین حالا", "در حال وقوع", "breaking news",
}

WORLD_VARIETY_TERMS = {
    "europe", "اروپا", "africa", "آفریقا", "asia", "آسیا", "pacific",
    "اقیانوس آرام", "latin america", "آمریکای لاتین", "ukraine", "اوکراین",
    "taiwan", "تایوان", "korea", "کره", "north korea", "کره شمالی",
}


# سبد محتوایی نسخه 3.5:
# ایران 40% | جنگ/درگیری خاورمیانه 20% | تحولات خاورمیانه 10%
# روسیه-اوکراین 10% | نظامی/تسلیحاتی آمریکا-روسیه-چین 20%
CONTENT_BUCKET_TARGETS = {
    "iran": 0.40,
    "middle_east_war": 0.20,
    "middle_east_developments": 0.10,
    "russia_ukraine": 0.10,
    "superpower_military": 0.20,
}
CONTENT_BUCKET_WINDOW = 20
ALLOWED_CONTENT_BUCKETS = set(CONTENT_BUCKET_TARGETS)

SUPERPOWER_ALLOWED_TERMS = {
    "آمریکا", "ایالات متحده", "usa", "united states", "u.s.",
    "روسیه", "russia", "moscow", "مسکو",
    "چین", "china", "beijing", "پکن",
}

STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with",
    "from", "at", "by", "is", "are", "was", "were", "be", "as", "that",
    "this", "it", "its", "after", "before", "into", "over", "under",
    "خبر", "گزارش", "گفت", "گفته", "اعلام", "اعلام کرد", "بر اساس",
    "در", "از", "به", "برای", "با", "که", "این", "آن", "یک", "و", "یا",
    "اما", "هم", "نیز", "است", "هست", "شد", "شده", "می", "شود", "کرد",
    "کرده", "خواهد", "روی", "درون", "درباره", "بر", "تا", "را", "از سوی",
}


# ============================================================
# Prompt
# ============================================================

REWRITE_PROMPT = """تو یک دبیر ارشد و دقیق برای یک کانال خبری/تحلیلی فارسی در حوزه نظامی، امنیتی و ژئوپلیتیکی هستی.

ورودی می‌تواند شامل چند پست پشت سرهم از یک منبع باشد. هدف اصلی این است که یک کانال مرجع، تمیز و غیرهیجانی ساخته شود؛ نه کانالی که یک واقعه را با ده‌ها پست کوتاه پشت سر هم منتشر کند.

قواعد بسیار مهم:

1) اگر چند ورودی مربوط به یک رویداد واحد هستند، آن‌ها را در یک جمع‌بندی واحد ادغام کن. در صورت وجود دو تحول واقعاً متفاوت اما مرتبط، حداکثر دو خروجی بده. بیشتر از دو خروجی برای یک batch مجاز نیست.

2) پست‌های تکمیلی مثل «تصاویر تازه»، «جزئیات بیشتر»، «بیانیه طرف مقابل» یا «آمار جدید» را فقط وقتی جدا کن که واقعاً یک تحول جدید و مستقل باشند؛ وگرنه با خبر اصلی ادغامشان کن.

3) هیچ ادعای تأییدنشده‌ای را قطعی ننویس. از عباراتی مانند «بر اساس گزارش‌ها»، «به گفته منابع»، «این رسانه مدعی شده»، «در صورت تأیید» استفاده کن.

4) محتوای منبع را تحریف نکن و چیزی را که در منبع وجود ندارد به عنوان واقعیت نساز. اگر نکته‌ای تحلیل خودت است، با زبان تحلیلی و روشن از خبر جدا کن.

5) هر خروجی یک پست خبری مستقل برای تلگرام است. تیتر کوتاه، دقیق و بدون اغراق باشد.

6) متن هر پست معمولاً حدود 90 تا 170 کلمه باشد؛ اگر یک جمع‌بندی رویداد چند منبع/پست دارد، می‌توانی تا حدود 220 کلمه بروی، ولی از تکرار دوری کن.

7) ساختار:
- پاراگراف اول: اصل اتفاق
- پاراگراف دوم: مهم‌ترین جزئیات
- پاراگراف سوم: فقط در صورت نیاز، اهمیت یا پیامد احتمالی
- از جملات تبلیغاتی، شعاری و زرد پرهیز کن.

8) فوریت:
urgent=true فقط برای اتفاقی که واقعاً لحظه‌ای و فوری است؛ مانند شروع ناگهانی درگیری یا حمله در حال وقوع.
urgent برای خبر عادی، گزارش تحلیلی یا خبری که چند ساعت از آن گذشته false است.

9) اهمیت:
important=true برای خبر مهمی که باید در صف زودتر بیاید؛ حتی اگر urgent=false باشد.

10) منطقه:
region یکی از این چهار مقدار دقیق باشد:
- "iran"
- "middle_east"
- "world"
- "superpower"

اگر خبر مستقیم درباره ایران است، region=iran.
اگر درباره دیگر نقاط خاورمیانه است، region=middle_east.
اگر تمرکز اصلی روی آمریکا/روسیه/چین و مانند آن است و رویداد خارج از خاورمیانه است، region=superpower.
در غیر این صورت region=world.

11) event_type یکی از این موارد باشد:
- "military_event"
- "security"
- "defense"
- "geopolitics"
- "routine"

12) content_bucket را دقیقاً یکی از این پنج مقدار قرار بده:
- "iran" = اخبار مستقیم ایران و حوزه ایران
- "middle_east_war" = جنگ، حمله، درگیری و عملیات نظامی در خاورمیانه
- "middle_east_developments" = تحولات سیاسی/امنیتی/دیپلماتیک خاورمیانه که جنگ و درگیری مستقیم نیست
- "russia_ukraine" = اخبار جنگ و درگیری روسیه و اوکراین
- "superpower_military" = فقط اخبار نظامی، دفاعی و تسلیحاتی آمریکا، روسیه یا چین که در چهار سبد بالا قرار نمی‌گیرند
اگر خبر خارج از این پنج سبد است، relevant=false بده و منتشر نکن. اخبار عادی نظامی آلمان، فرانسه، کره و سایر کشورها خارج از این پنج سبد حذف شوند؛ مگر اینکه مستقیماً بخشی از یک رویداد ایران/خاورمیانه/روسیه-اوکراین باشند.

13) priority_hint عددی بین 0 تا 100 بده. این فقط یک راهنمای مدل است و سیستم صف‌بندی خودش دوباره اولویت را محاسبه می‌کند.

13) image_query را به انگلیسی و کوتاه، 3 تا 6 کلمه‌ای بده. باید موضوع عکس را دقیق و غیرخیالی بیان کند. اگر تصویر دقیق رویداد در منبع وجود ندارد، عبارت عمومی موضوعی بده؛ ادعا نکن که تصویر دقیق همان رویداد است.

14) source_note یک جمله کوتاه باشد که وضعیت منبع را روشن کند؛ مثال:
«این خبر بر اساس گزارش اولیه منبع است و هنوز به‌طور مستقل تأیید نشده است.»

15) category یکی از این مقادیر دقیق باشد:
- "military"
- "security"
- "defense"
- "geopolitics"
- "technology"
- "general"

16) verification یکی از این مقادیر دقیق باشد:
- "reported" = گزارش اولیه/ادعا، بدون تأیید مستقل
- "developing" = خبر در حال تکمیل و جزئیات هنوز در حال تغییر
- "confirmed" = متن ورودی صراحتاً به تأیید معتبر اشاره دارد
- "analysis" = تحلیل یا ارزیابی، نه یک ادعای قطعی خبری

17) اگر ورودی‌ها تکراری یا کم‌ارزش‌اند، relevant=false بده و منتشر نکن. تبلیغات، دعوت به عضویت، متن‌های صرفاً تکراری، کپشن‌های بی‌محتوا و پست‌های بدون ارزش خبری حذف شوند.

18) اگر چند منبع درباره یک اتفاق مشترک هستند، یک خبر واحد و منسجم بساز و اختلاف روایت‌ها را با عبارت‌هایی مثل «بر اساس گزارش‌ها» یا «روایت‌های موجود متفاوت است» روشن کن. نام یا لینک منبع را در متن خروجی ننویس.

19) تیتر باید خبری و حرفه‌ای باشد؛ از کلیک‌بیت، اغراق، علامت تعجب‌های متعدد و ادعاهای قطعی بدون پشتوانه پرهیز کن.

20) ممکن است این batch شامل چند رویداد مستقل باشد. برای هر رویداد مستقل ارزشمند حداکثر یک خروجی بده و رویداد ارزشمند را صرفاً به دلیل قرار گرفتن کنار رویداد دیگر حذف نکن.

21) برای هر خروجی، post_indices را با شماره پست‌های ورودی که آن خروجی بر اساس آن‌ها ساخته شده مشخص کن؛ اگر چند پست یک رویداد هستند، همه شماره‌های مرتبط را بده.

22) اگر هیچ ورودی ارزش خبری ندارد، items را خالی برگردان.

23) در انتخاب بین چند خبر ارزشمند، جذابیت خبری برای مخاطب را هم بسنج: شروع درگیری/حمله، تحول مهم، مقام ارشد، تغییر مهم نظامی، خسارت یا اثر راهبردی، و خبر تازه معمولاً جذاب‌تر از خبر روتین هستند. اما برای جذابیت، هیچ واقعیتی را اضافه یا اغراق نکن.

24) اولویت موضوعی کانال این است: ایران، جنگ و درگیری خاورمیانه، تحولات خاورمیانه، جنگ روسیه-اوکراین، سپس نظامی/تسلیحاتی آمریکا/روسیه/چین. در هر batch خبرهای خارج از این محدوده را حذف کن.

25) خروجی فقط JSON معتبر باشد، بدون Markdown و بدون توضیح اضافی.

فرمت:
{
  "items": [
    {
      "relevant": true,
      "urgent": false,
      "important": false,
      "title": "...",
      "body": "...",
      "image_query": "...",
      "post_indices": [1],
      "content_bucket": "iran|middle_east_war|middle_east_developments|russia_ukraine|superpower_military",
      "region": "iran|middle_east|world|superpower",
      "event_type": "military_event|security|defense|geopolitics|routine",
      "category": "military|security|defense|geopolitics|technology|general",
      "verification": "reported|developing|confirmed|analysis",
      "priority_hint": 0,
      "source_note": "..."
    }
  ]
}

متن/پست‌های ورودی:
---
{content}
---
"""


# ============================================================
# General helpers
# ============================================================

def now_ts():
    return time.time()


def now_tehran():
    return datetime.now(TEHRAN_TZ or timezone.utc)


def normalize_space(text):
    if not text:
        return ""
    text = text.replace("\u200c", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def canonical_url(url):
    if not url:
        return ""
    return url.strip()


def sha1_text(value):
    return hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()


def parse_iso_or_now(value):
    if not value:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.now(timezone.utc)


def split_sentences(text):
    text = normalize_space(text)
    if not text:
        return []
    return [
        x.strip()
        for x in re.split(r"(?<=[.!?؟。])\s+", text)
        if x.strip()
    ]


def truncate_text(text, max_chars):
    text = text or ""
    if len(text) <= max_chars:
        return text
    part = text[:max_chars]
    # نزدیک‌ترین مرز مناسب را پیدا کن.
    candidates = [
        part.rfind("\n\n"),
        part.rfind("\n"),
        part.rfind(". "),
        part.rfind("! "),
        part.rfind("؟ "),
    ]
    cut = max(candidates)
    if cut < max_chars * 0.55:
        cut = max_chars
    return part[:cut].rstrip() + "…"


def safe_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "بله"}
    return bool(value)


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


# ============================================================
# State
# ============================================================

def default_state():
    return {
        "version": 2,
        "_source_initialized": {},
        "_recent_titles": [],
        "_recent_events": [],
        "_recent_inputs": [],
        "_pending_queue": [],
        "_last_queue_release_ts": 0,
        "_last_morning_date": "",
        "_last_night_date": "",
        "_last_queue_purge_date": "",
        "_last_posted_region": "",
        "_last_posted_regions": [],
        "_last_posted_buckets": [],
        "_gemini_keys": {},
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            return default_state()

        base = default_state()
        base.update(state)

        # مهاجرت امن از حالت قبلی.
        if not isinstance(base.get("_pending_queue"), list):
            base["_pending_queue"] = []
        if not isinstance(base.get("_recent_titles"), list):
            base["_recent_titles"] = []
        if not isinstance(base.get("_recent_events"), list):
            base["_recent_events"] = []
        if not isinstance(base.get("_recent_inputs"), list):
            base["_recent_inputs"] = []
        if not isinstance(base.get("_last_posted_regions"), list):
            base["_last_posted_regions"] = []
        if not isinstance(base.get("_last_posted_buckets"), list):
            base["_last_posted_buckets"] = []

        return base
    except Exception as exc:
        log.exception("خواندن state شکست خورد؛ state خالی ساخته می‌شود: %s", exc)
        return default_state()


def save_state(state):
    """
    Atomic write:
    temp -> flush -> fsync -> replace
    در صورت قطع ناگهانی، state قبلی سالم می‌ماند.
    """
    path = os.path.abspath(STATE_FILE)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(
        prefix=".state.",
        suffix=".tmp",
        dir=directory,
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


# ============================================================
# Tokenization / similarity / event detection
# ============================================================

def normalize_for_match(text):
    text = (text or "").lower()
    replacements = {
        "ي": "ی",
        "ى": "ی",
        "ك": "ک",
        "ۀ": "ه",
        "ة": "ه",
        "ؤ": "و",
        "إ": "ا",
        "أ": "ا",
        "ٱ": "ا",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r"[^\w\s\u0600-\u06ff.-]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def tokens(text):
    text = normalize_for_match(text)
    raw = re.findall(r"[\w\u0600-\u06ff]{2,}", text, flags=re.UNICODE)
    return {
        token for token in raw
        if token not in STOPWORDS and not token.isdigit()
    }


def token_similarity(a, b):
    ta = tokens(a)
    tb = tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, len(ta | tb))


def title_similarity(a, b):
    return difflib.SequenceMatcher(
        None,
        normalize_for_match(a),
        normalize_for_match(b),
    ).ratio()


def combined_similarity(a_title, a_body, b_title, b_body):
    t = title_similarity(a_title, b_title)
    j = token_similarity(
        f"{a_title} {a_body}",
        f"{b_title} {b_body}",
    )
    return max(t * 0.75 + j * 0.25, j)


def related_post(a, b):
    """
    تشخیص ارزان و محافظه‌کارانه برای گروه‌کردن پست‌های یک واقعه.
    قرار نیست صددرصد معنایی باشد؛ هدف این است که پست‌های واضحاً مرتبط را
    قبل از Gemini به یک batch بدهیم و مصرف API کم شود.
    """
    dt_a = parse_iso_or_now(a.get("published_at"))
    dt_b = parse_iso_or_now(b.get("published_at"))

    delta_minutes = abs((dt_a - dt_b).total_seconds()) / 60.0
    if delta_minutes > EVENT_CLUSTER_WINDOW_MINUTES:
        return False

    a_title = a.get("title") or a.get("text", "")[:220]
    b_title = b.get("title") or b.get("text", "")[:220]
    a_body = a.get("text", "")
    b_body = b.get("text", "")

    sim = combined_similarity(a_title, a_body, b_title, b_body)
    if sim >= 0.34:
        return True

    at = tokens(f"{a_title} {a_body}")
    bt = tokens(f"{b_title} {b_body}")

    strong_a = {
        x for x in at
        if x in {normalize_for_match(v) for v in MILITARY_EVENT_TERMS}
    }
    strong_b = {
        x for x in bt
        if x in {normalize_for_match(v) for v in MILITARY_EVENT_TERMS}
    }

    if len(at & bt) >= 3:
        return True

    if strong_a and strong_b and len(at & bt) >= 2:
        return True

    # پست‌های تکمیلی رایج:
    combined_title = normalize_for_match(f"{a_title} {b_title}")
    continuation_terms = (
        "جزئیات", "تصاویر", "ادامه", "به روزرسانی", "بروزرسانی",
        "update", "details", "new footage", "latest",
    )
    if any(x in combined_title for x in continuation_terms) and len(at & bt) >= 1:
        return True

    return False


def cluster_posts(posts):
    """
    پست‌های جدید را به گروه‌های رویدادی تقسیم می‌کند.
    هر گروه حداکثر EVENT_MAX_ITEMS_PER_CLUSTER ورودی دارد.
    """
    ordered = sorted(
        posts,
        key=lambda p: parse_iso_or_now(p.get("published_at")),
    )

    clusters = []
    for post in ordered:
        placed = False

        # ابتدا نزدیک‌ترین خوشه زمانی را بررسی کن.
        candidate_indices = list(range(max(0, len(clusters) - 12), len(clusters)))
        for idx in reversed(candidate_indices):
            cluster = clusters[idx]
            if len(cluster) >= EVENT_MAX_ITEMS_PER_CLUSTER:
                continue

            if any(related_post(post, existing) for existing in cluster):
                cluster.append(post)
                placed = True
                break

        if not placed:
            clusters.append([post])

    return clusters


# ============================================================
# Geography / priority
# ============================================================

def contains_any(text, terms):
    normalized = normalize_for_match(text)
    return any(normalize_for_match(term) in normalized for term in terms)


def infer_region(text, model_region=None):
    model_region = (model_region or "").strip().lower()
    if model_region in {"iran", "middle_east", "world", "superpower"}:
        # ایران باید اولویت خودش را نگه دارد.
        if contains_any(text, IRAN_TERMS):
            return "iran"
        return model_region

    if contains_any(text, IRAN_TERMS):
        return "iran"
    if contains_any(text, MIDDLE_EAST_TERMS):
        return "middle_east"
    if contains_any(text, SUPERPOWER_TERMS):
        return "superpower"
    return "world"


def is_military_event(text, event_type=""):
    if event_type == "military_event":
        return True
    return contains_any(text, MILITARY_EVENT_TERMS)


def is_breaking(text):
    return contains_any(text, BREAKING_TERMS)


def calculate_priority(item):
    """
    قواعد موردنظر کاربر:
    1. واقعه نظامی / حمله / جنگ بالاتر از خبر عادی
    2. خاورمیانه بالاتر از سایر جهان
    3. ایران بالاتر از غیرایرانی
    4. ابرقدرت‌ها مهم
    5. تنوع جهانی حفظ شود، بنابراین region تکراری کمی جریمه می‌گیرد.
    """
    title = item.get("title", "")
    body = item.get("body", "")
    text = f"{title}\n{body}"

    region = infer_region(text, item.get("region"))
    event_type = item.get("event_type", "routine")

    score = 0

    bucket = item.get("content_bucket") or infer_content_bucket(text, region)
    if bucket in {"middle_east_war", "russia_ukraine"}:
        score += 70
    elif bucket == "iran":
        score += 45
    elif bucket == "middle_east_developments":
        score += 30
    elif bucket == "superpower_military":
        score += 22
    else:
        score -= 100

    if is_military_event(text, event_type):
        score += 55

    if safe_bool(item.get("urgent")):
        score += 40

    if safe_bool(item.get("important")):
        score += 20

    if region == "iran":
        score += 35
    elif region == "middle_east":
        score += 25
    elif region == "superpower":
        score += 18
    else:
        score += 5

    if contains_any(text, SUPERPOWER_TERMS):
        score += 8

    # رویدادها و حملات لحظه‌ای کمی جلوتر.
    if is_breaking(text):
        score += 18

    # راهنمای Gemini فقط نقش tie-breaker دارد.
    model_hint = min(100, max(0, safe_int(item.get("priority_hint"), 0)))
    score += int(model_hint * 0.12)

    # تازگی خبر هم در اولویت اثر می‌گذارد؛ خبر بسیار قدیمی نباید صرفاً به دلیل
    # برچسب important یک‌باره بالاتر از خبر تازه قرار بگیرد.
    published_at = item.get("published_at") or ""
    try:
        published_dt = parse_iso_or_now(published_at)
        age_minutes = max(0.0, (datetime.now(timezone.utc) - published_dt.astimezone(timezone.utc)).total_seconds() / 60.0)
        if age_minutes <= 60:
            score += 8
        elif age_minutes <= 180:
            score += 4
        elif age_minutes > 24 * 60 and not safe_bool(item.get("urgent")):
            score -= 8
    except Exception:
        pass

    # خبر جهانی صفر نمی‌شود؛ فقط نسبت به اولویت منطقه‌ای کمی عقب‌تر است.
    if region == "world" and not is_military_event(text, event_type):
        score -= 3

    return score, region


def apply_queue_diversity_score(state, item):
    score, region = calculate_priority(item)
    recent_regions = state.get("_last_posted_regions", [])[-3:]
    bucket = item.get("content_bucket") or infer_content_bucket(
        f"{item.get('title','')}\n{item.get('body','')}",
        region
    )
    deficit = bucket_deficit(state, bucket)
    score += int(max(-20, min(35, deficit * 10)))

    if recent_regions and region == recent_regions[-1]:
        score -= 10
    if len(recent_regions) >= 2 and all(x == region for x in recent_regions[-2:]):
        score -= 20

    # اگر خبر نظامی/ایرانی مهم باشد، تنوع نباید آن را کاملاً عقب بیندازد.
    if safe_bool(item.get("urgent")):
        score += 20

    item["region"] = region
    item["priority_score"] = score
    return score


# ============================================================
# Gemini key rotation with persistent cooldown
# ============================================================

def key_id(key):
    return sha1_text(key)[:16]


def initialize_gemini_key_state(state):
    state_keys = state.setdefault("_gemini_keys", {})
    for key in GEMINI_API_KEYS:
        kid = key_id(key)
        state_keys.setdefault(
            kid,
            {
                "cooldown_until": 0,
                "fail_count": 0,
                "last_status": 0,
                "last_used": 0,
            },
        )
    return state_keys


def cleanup_gemini_key_state(state):
    state_keys = initialize_gemini_key_state(state)
    valid_ids = {key_id(k) for k in GEMINI_API_KEYS}
    for kid in list(state_keys):
        if kid not in valid_ids:
            del state_keys[kid]


def choose_available_key(state, start_index):
    key_states = initialize_gemini_key_state(state)
    now = now_ts()

    ordered = []
    total = len(GEMINI_API_KEYS)

    for offset in range(total):
        idx = (start_index + offset) % total
        key = GEMINI_API_KEYS[idx]
        info = key_states[key_id(key)]
        if info.get("cooldown_until", 0) <= now:
            ordered.append((idx, key))

    if ordered:
        return ordered

    # اگر همه cooldown هستند، نزدیک‌ترین cooldown را انتخاب کن.
    fallback = []
    for idx, key in enumerate(GEMINI_API_KEYS):
        info = key_states[key_id(key)]
        fallback.append((info.get("cooldown_until", 0), idx, key))
    fallback.sort(key=lambda x: x[0])
    return [(fallback[0][1], fallback[0][2])]


def set_key_failure(state, key, status, cooldown_seconds):
    info = state["_gemini_keys"][key_id(key)]
    info["cooldown_until"] = now_ts() + cooldown_seconds
    info["fail_count"] = int(info.get("fail_count", 0)) + 1
    info["last_status"] = status
    info["last_used"] = now_ts()


def set_key_success(state, key):
    info = state["_gemini_keys"][key_id(key)]
    info["cooldown_until"] = 0
    info["fail_count"] = 0
    info["last_status"] = 200
    info["last_used"] = now_ts()


def retry_after_from_response(resp, default_seconds=30):
    value = resp.headers.get("Retry-After")
    if value:
        try:
            return max(1, int(float(value)))
        except ValueError:
            pass
    return default_seconds


# ============================================================
# Gemini analysis
# ============================================================

def parse_json_response(raw):
    raw = (raw or "").strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # حذف ```json ... ```
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if match:
        return json.loads(match.group(0))

    raise ValueError(f"خروجی Gemini JSON نیست: {raw[:500]}")


def infer_content_bucket(text, region=""):
    """طبقه‌بندی محافظه‌کارانه برای کنترل سبد محتوایی نسخه 3.5."""
    text = normalize_for_match(text)

    if contains_any(text, IRAN_TERMS):
        return "iran"

    ru_terms = {
        "روسیه", "russia", "اوکراین", "ukraine",
        "moscow", "مسکو", "کی‌یف", "کیف", "kyiv"
    }
    if contains_any(text, ru_terms) and contains_any(
        text, MILITARY_EVENT_TERMS | {"ukraine", "اوکراین"}
    ):
        return "russia_ukraine"

    if contains_any(text, MIDDLE_EAST_TERMS):
        if contains_any(text, MILITARY_EVENT_TERMS):
            return "middle_east_war"
        return "middle_east_developments"

    military_terms = {
        "نظامی", "military", "سلاح", "تسلیحات", "weapon",
        "weapons", "defense", "دفاعی", "جنگنده", "fighter",
        "تانک", "tank", "پهپاد", "drone", "موشک", "missile",
        "پدافند", "air defense", "navy", "ارتش"
    }
    if contains_any(text, SUPERPOWER_ALLOWED_TERMS) and contains_any(text, military_terms):
        return "superpower_military"

    return "world"


def is_allowed_content_bucket(bucket):
    return bucket in ALLOWED_CONTENT_BUCKETS


def recent_bucket_counts(state):
    recent = state.get("_last_posted_buckets", [])[-CONTENT_BUCKET_WINDOW:]
    return {bucket: recent.count(bucket) for bucket in ALLOWED_CONTENT_BUCKETS}


def bucket_deficit(state, bucket):
    recent = state.get("_last_posted_buckets", [])[-CONTENT_BUCKET_WINDOW:]
    total = len(recent)
    if total == 0:
        return CONTENT_BUCKET_TARGETS.get(bucket, 0)

    # برای پست بعدی، تعداد هدف را در یک پنجره حداکثر 20 پستی محاسبه می‌کنیم.
    target = CONTENT_BUCKET_TARGETS.get(bucket, 0) * min(
        CONTENT_BUCKET_WINDOW, total + 1
    )
    return target - recent.count(bucket)


def validate_gemini_items(items):
    if not isinstance(items, list):
        raise ValueError("Gemini items باید list باشد.")

    validated = []

    for raw in items[:EVENT_MAX_OUTPUT_ITEMS]:
        if not isinstance(raw, dict):
            continue

        relevant = safe_bool(raw.get("relevant"))
        if not relevant:
            validated.append(
                {
                    "relevant": False,
                    "urgent": False,
                    "important": False,
                    "title": "",
                    "body": "",
                    "image_query": "",
                    "post_indices": [],
                    "region": "world",
                    "event_type": "routine",
                    "priority_hint": 0,
                    "source_note": "",
                }
            )
            continue

        title = normalize_space(str(raw.get("title", "")))
        body = str(raw.get("body", "")).strip()
        image_query = normalize_space(str(raw.get("image_query", "")))

        if not title or not body:
            continue

        event_type = str(raw.get("event_type", "routine")).strip()
        allowed_event_types = {
            "military_event", "security", "defense", "geopolitics", "routine"
        }
        if event_type not in allowed_event_types:
            event_type = "routine"

        category = str(raw.get("category", "")).strip().lower()
        allowed_categories = {
            "military", "security", "defense", "geopolitics", "technology", "general"
        }
        if category not in allowed_categories:
            category = {
                "military_event": "military",
                "security": "security",
                "defense": "defense",
                "geopolitics": "geopolitics",
                "routine": "general",
            }.get(event_type, "general")

        content_bucket = str(raw.get("content_bucket", "")).strip().lower()
        if content_bucket not in ALLOWED_CONTENT_BUCKETS:
            content_bucket = infer_content_bucket(
                f"{title}\n{body}",
                region=str(raw.get("region", ""))
            )

        verification = str(raw.get("verification", "reported")).strip().lower()
        if verification not in {"reported", "developing", "confirmed", "analysis"}:
            verification = "reported"

        raw_indices = raw.get("post_indices", [])
        if not isinstance(raw_indices, list):
            raw_indices = []
        post_indices = []
        for value in raw_indices:
            idx = safe_int(value, 0)
            if idx > 0 and idx not in post_indices:
                post_indices.append(idx)
        post_indices = post_indices[:GEMINI_BATCH_MAX_POSTS]

        validated.append(
            {
                "relevant": True,
                "urgent": safe_bool(raw.get("urgent")),
                "important": safe_bool(raw.get("important")),
                "title": title[:220],
                "body": body[:5000],
                "image_query": image_query[:180],
                "post_indices": post_indices,
                "content_bucket": content_bucket,
                "region": str(raw.get("region", "world")).strip().lower(),
                "event_type": event_type,
                "category": category,
                "verification": verification,
                "priority_hint": min(100, max(0, safe_int(raw.get("priority_hint"), 0))),
                "source_note": normalize_space(str(raw.get("source_note", "")))[:500],
            }
        )

    return validated


_gemini_cursor = 0


def analyze_and_rewrite(batch_posts, source_name):
    global _gemini_cursor

    if not GEMINI_API_KEYS:
        raise RuntimeError("GEMINI_API_KEYS/GEMINI_API_KEY تنظیم نشده است.")

    if not batch_posts:
        return []

    source_chunks = []
    for index, post in enumerate(batch_posts, start=1):
        published = post.get("published_at", "")
        text = truncate_text(post.get("text", ""), GEMINI_INPUT_MAX_CHARS_PER_POST)

        source_chunks.append(
            f"[پست {index}]\n"
            f"زمان: {published}\n"
            f"متن:\n{text}\n"
        )

    content = (
        f"منبع: {source_name}\n"
        f"این ورودی شامل {len(batch_posts)} پست است. "
        f"اول تشخیص بده کدام‌ها یک رویداد مشترک‌اند و آن‌ها را ادغام کن.\n\n"
        + "\n---\n".join(source_chunks)
    )

    # از str.format() استفاده نمی‌کنیم چون Prompt شامل نمونه JSON با { و } است.
    # فقط دو placeholder واقعی را جایگزین می‌کنیم تا JSON باعث KeyError نشود.
    prompt = REWRITE_PROMPT.replace("{content}", content).replace("{source_name}", source_name)

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt,
                    }
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
        },
    }

    initialize_gemini_key_state_for_call = True
    del initialize_gemini_key_state_for_call

    # حداکثر چند دور کامل روی کلیدهای سالم.
    total_attempts = max(1, len(GEMINI_API_KEYS) * 2)
    last_error = None

    for attempt in range(total_attempts):
        available = choose_available_key(load_call_state_cache, _gemini_cursor)
        if not available:
            continue

        idx, key = available[0]
        _gemini_cursor = (idx + 1) % len(GEMINI_API_KEYS)

        try:
            headers = {
                "Content-Type": "application/json",
                "X-goog-api-key": key,
            }

            resp = requests.post(
                GEMINI_API_URL,
                headers=headers,
                json=payload,
                timeout=(15, 90),
            )

            status = resp.status_code

            if status == 200:
                set_key_success(load_call_state_cache, key)
                data = resp.json()
                candidates = data.get("candidates", [])
                if not candidates:
                    raise ValueError(f"پاسخ Gemini candidate ندارد: {data}")

                parts = candidates[0].get("content", {}).get("parts", [])
                raw = "\n".join(p.get("text", "") for p in parts).strip()
                parsed = parse_json_response(raw)

                items = parsed.get("items", [])
                if not items and "relevant" in parsed:
                    items = [parsed]

                return validate_gemini_items(items)

            if status == 429:
                cooldown = retry_after_from_response(resp, 60)
                set_key_failure(load_call_state_cache, key, status, cooldown)
                log.warning(
                    "کلید Gemini با 429 مواجه شد؛ کلید %s برای %s ثانیه cooldown شد.",
                    key_id(key),
                    cooldown,
                )
                continue

            if status in (500, 502, 503, 504):
                cooldown = min(120, 20 * (1 + attempt // max(1, len(GEMINI_API_KEYS))))
                set_key_failure(load_call_state_cache, key, status, cooldown)
                log.warning(
                    "Gemini موقتاً خطا داد (%s)؛ کلید %s موقتاً کنار گذاشته شد.",
                    status,
                    key_id(key),
                )
                continue

            if status in (401, 403):
                # ممکن است کلید نامعتبر یا غیرفعال باشد؛ cooldown بلندتر تا در هر poll تکرار نشود.
                set_key_failure(load_call_state_cache, key, status, 6 * 3600)
                log.error(
                    "کلید Gemini %s خطای %s داد؛ برای ۶ ساعت cooldown شد.",
                    key_id(key),
                    status,
                )
                continue

            # خطاهای غیرقابل‌حل برای این درخواست.
            raise RuntimeError(
                f"Gemini HTTP {status}: {truncate_text(resp.text, 700)}"
            )

        except (requests.Timeout, requests.ConnectionError, requests.RequestException) as exc:
            last_error = exc
            # خطای شبکه به یک کلید خاص نسبت داده نمی‌شود؛ ولی کلید فعلی را کوتاه cooldown می‌کنیم
            # تا در صورت مشکل شبکه از چرخش کلید بی‌مورد جلوگیری شود.
            try:
                set_key_failure(load_call_state_cache, key, -1, 30)
            except Exception:
                pass
            log.warning(
                "خطای شبکه هنگام اتصال به Gemini با کلید %s: %s",
                key_id(key),
                exc,
            )
            continue
        except Exception as exc:
            last_error = exc
            log.error("خطا در پردازش پاسخ Gemini: %s", exc)
            # خطای parse پاسخ به کلید ربطی ندارد؛ کلید را از چرخه خارج نمی‌کنیم.
            continue

    raise RuntimeError(
        f"پس از {total_attempts} تلاش، Gemini موفق نشد. آخرین خطا: {last_error}"
    )


# این متغیر برای آن است که state فعلی به توابع Gemini که امضای قدیمی دارند برسد.
load_call_state_cache = None


def analyze_and_rewrite_safe(state, batch_posts, source_name):
    global load_call_state_cache
    load_call_state_cache = state
    try:
        return analyze_and_rewrite(batch_posts, source_name)
    finally:
        load_call_state_cache = None


# ============================================================
# Telegram public channel scraping
# ============================================================

NOISE_PATTERNS = (
    "join our channel",
    "subscribe to our channel",
    "подписывайтесь",
    "подписаться",
    "عضو شوید",
    "به کانال ما بپیوندید",
    "تبلیغات",
)


def looks_like_low_value_post(post):
    text = normalize_space(post.get("text", ""))
    if not text and not post.get("photo") and not post.get("video"):
        return True
    if len(text) < 12 and not post.get("photo") and not post.get("video"):
        return True

    lowered = normalize_for_match(text)
    if any(normalize_for_match(pattern) in lowered for pattern in NOISE_PATTERNS):
        if not contains_any(text, MILITARY_EVENT_TERMS | BREAKING_TERMS):
            return True

    if text and re.fullmatch(r"(https?://\S+|@\w+)(\s+https?://\S+|\s+@\w+)*", text):
        return True
    return False


def filter_posts(posts):
    return [post for post in posts if not looks_like_low_value_post(post)]


def extract_telegram_posts_from_html(channel, html_text):
    if BeautifulSoup is None:
        raise RuntimeError("beautifulsoup4 نصب نیست.")

    soup = BeautifulSoup(html_text, "html.parser")
    nodes = soup.select("[data-post]")
    posts = []

    seen = set()

    for node in nodes:
        data_post = node.get("data-post", "")
        expected_prefix = f"{channel}/"
        if not data_post.startswith(expected_prefix):
            continue

        message_id = data_post[len(expected_prefix):]
        if not message_id or message_id in seen:
            continue
        seen.add(message_id)

        text_node = node.select_one(".tgme_widget_message_text")
        text = text_node.get_text("\n", strip=True) if text_node else ""

        time_node = node.select_one("time")
        published_at = time_node.get("datetime") if time_node else ""

        link_node = node.select_one(".tgme_widget_message_date")
        source_url = None
        if link_node is not None:
            anchor = link_node.find("a")
            if anchor and anchor.get("href"):
                source_url = anchor["href"]

        photo_url = None
        photo_wrap = node.select_one(".tgme_widget_message_photo_wrap")
        if photo_wrap:
            style = photo_wrap.get("style", "")
            m = re.search(
                r"background-image:url\(['\"]?([^'\")]+)",
                style,
                flags=re.IGNORECASE,
            )
            if m:
                photo_url = html.unescape(m.group(1))

        video_url = None
        video_node = node.select_one("video")
        if video_node and video_node.get("src"):
            video_url = html.unescape(video_node["src"])

        # پست‌های مدیا بدون متن هم نگه داشته می‌شوند.
        if text or photo_url or video_url:
            posts.append(
                {
                    "uid": f"tg:{channel}:{message_id}",
                    "_source_key": f"tg:{channel}",
                    "text": text,
                    "title": text.split("\n", 1)[0][:220] if text else "",
                    "photo": photo_url,
                    "video": video_url,
                    "source_name": f"کانال {channel}",
                    "source_url": source_url or f"https://t.me/{channel}/{message_id}",
                    "published_at": published_at or datetime.now(timezone.utc).isoformat(),
                }
            )

    return filter_posts(posts)


def fetch_channel_posts(channel):
    url = f"https://t.me/s/{channel}"
    try:
        resp = requests.get(
            url,
            timeout=(10, 30),
            headers=HTTP_HEADERS,
        )
        resp.raise_for_status()
        posts = extract_telegram_posts_from_html(channel, resp.text)
        return posts
    except Exception as exc:
        log.warning("خطا در گرفتن کانال %s: %s", channel, exc)
        return []


# ============================================================
# Image search / image acquisition
# ============================================================

def find_wikimedia_image(query):
    """
    fallback بدون API key:
    Wikimedia Commons MediaSearch
    """
    if not query:
        return None

    try:
        params = {
            "action": "query",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": 6,
            "gsrlimit": 8,
            "prop": "imageinfo",
            "iiprop": "url|mime|size",
            "format": "json",
        }

        resp = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params=params,
            timeout=(10, 25),
            headers=HTTP_HEADERS,
        )
        resp.raise_for_status()

        data = resp.json()
        pages = list((data.get("query") or {}).get("pages", {}).values())

        # فقط تصاویر رایج را برگردان.
        allowed = {"image/jpeg", "image/png", "image/webp"}

        candidates = []
        for page in pages:
            infos = page.get("imageinfo") or []
            if not infos:
                continue
            info = infos[0]
            url = info.get("thumburl") or info.get("url")
            mime = info.get("mime")
            width = safe_int(info.get("width"), 0)

            if url and (mime in allowed or not mime):
                candidates.append((width, url))

        candidates.sort(reverse=True)

        if candidates:
            return candidates[0][1]

    except Exception as exc:
        log.info("Wikimedia image search failed: %s", exc)

    return None


def image_query_from_item(item):
    query = normalize_space(item.get("image_query", ""))
    if query:
        return query

    title = item.get("title", "")
    # query ساده برای fallback.
    return " ".join(title.split()[:6])


def choose_image_url(item, original_posts):
    """
    اولویت:
      1) تصویر اصلی منبع
      2) og:image مقاله
      3) رسانه پست
      4) Wikimedia fallback
      5) None
    """
    for post in original_posts:
        if post.get("photo"):
            return post["photo"]

    for post in original_posts:
        if post.get("article_image"):
            return post["article_image"]

    # فقط عکس عمومی موضوعی، نه ادعای عکس همان واقعه.
    query = image_query_from_item(item)
    return find_wikimedia_image(query)


# ============================================================
# Media streaming
# ============================================================

def stream_download_to_temp(url, max_bytes, timeout, suffix):
    if not url:
        raise ValueError("URL رسانه خالی است.")

    response = requests.get(
        url,
        stream=True,
        timeout=(10, timeout),
        headers=HTTP_HEADERS,
        allow_redirects=True,
    )
    response.raise_for_status()

    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            content_length_int = int(content_length)
        except (TypeError, ValueError):
            content_length_int = None
        if content_length_int is not None and content_length_int > max_bytes:
            raise ValueError(
                f"رسانه از حد مجاز بزرگ‌تر است: {content_length_int} bytes"
            )

    fd, path = tempfile.mkstemp(prefix="media-", suffix=suffix)
    os.close(fd)

    written = 0

    try:
        with open(path, "wb") as out:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(
                        f"رسانه از حد مجاز بزرگ‌تر است: {written} bytes"
                    )
                out.write(chunk)

        return path, written
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


# ============================================================
# Telegram API
# ============================================================

class TelegramAPIError(RuntimeError):
    def __init__(self, status_code, description, retry_after=0):
        super().__init__(f"Telegram HTTP {status_code}: {description}")
        self.status_code = status_code
        self.description = description
        self.retry_after = retry_after


def telegram_request(method, data=None, files=None, timeout=30):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    response = requests.post(
        url,
        data=data,
        files=files,
        timeout=timeout,
    )

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if response.status_code == 429:
        retry_after = (
            payload.get("parameters", {}).get("retry_after")
            or response.headers.get("Retry-After")
            or 30
        )
        raise TelegramAPIError(
            response.status_code,
            payload.get("description", response.text[:500]),
            int(retry_after),
        )

    if not response.ok or payload.get("ok") is False:
        raise TelegramAPIError(
            response.status_code,
            payload.get("description", response.text[:500]),
        )

    return payload


def send_text_to_telegram(text):
    """ارسال متن با قطعه‌بندی امن زیر سقف تلگرام؛ footer فقط یک‌بار در انتها می‌آید."""
    text = (text or "").strip()
    if not text:
        return None

    # برای حاشیه امن از 4000 کاراکتر استفاده می‌کنیم، نه سقف کامل 4096.
    MAX_TEXT_CHARS = 4000
    chunks = []
    remaining = text

    while len(remaining) > MAX_TEXT_CHARS:
        part = truncate_text(remaining, MAX_TEXT_CHARS)
        cut_len = len(part.rstrip("…"))
        if cut_len <= 0 or cut_len >= len(remaining):
            cut_len = MAX_TEXT_CHARS
            part = remaining[:cut_len]
        chunks.append(part.rstrip())
        remaining = remaining[cut_len:].lstrip()

    if remaining:
        chunks.append(remaining)

    result = None
    for chunk in chunks:
        payload = {
            "chat_id": TARGET_CHAT_ID,
            "text": chunk,
            "disable_web_page_preview": False,
        }
        try:
            result = telegram_request("sendMessage", data=payload, timeout=30)
        except TelegramAPIError as exc:
            if exc.status_code == 429:
                log.warning(
                    "Telegram rate limit؛ %s ثانیه صبر می‌کنیم.",
                    exc.retry_after,
                )
                time.sleep(exc.retry_after)
                result = telegram_request("sendMessage", data=payload, timeout=30)
            else:
                raise
    return result

def build_media_caption(title, body, max_chars=1024):
    """
    نسخه 3.5:
    متن خبر و رسانه همیشه در همان یک پیام Telegram قرار می‌گیرند.
    چون caption رسانه سقف 1024 کاراکتر دارد، فقط بخش بدنه خبر
    در صورت نیاز کوتاه می‌شود؛ پیام متنی دومی ساخته نمی‌شود.
    """
    title = normalize_space(title)
    body = (body or "").strip()
    footer = FOOTER

    # ابتدا برای تیتر فضای کافی نگه می‌داریم.
    title_room = max(100, max_chars - len(footer) - 20)
    if len(title) > title_room:
        title = truncate_text(title, title_room)

    separators = 6  # فاصله‌های بین title/body/footer
    body_room = max_chars - len(title) - len(footer) - separators

    if body_room <= 0:
        return f"{title}\n\n{footer}"[:max_chars]

    body_part = truncate_text(body, body_room)
    caption = f"{title}\n\n{body_part}\n\n{footer}"

    # اگر به دلیل نحوه برش چند کاراکتر اضافه شد، فقط body را کوتاه‌تر می‌کنیم.
    if len(caption) > max_chars:
        extra = len(caption) - max_chars
        body_room = max(1, body_room - extra)
        body_part = truncate_text(body, body_room)
        caption = f"{title}\n\n{body_part}\n\n{footer}"

    return caption[:max_chars]


def send_photo_to_telegram(title, body, photo_url):
    caption = build_media_caption(title, body, 1024)
    temp_path = None
    try:
        temp_path, _ = stream_download_to_temp(
            photo_url,
            max_bytes=MAX_IMAGE_BYTES,
            timeout=45,
            suffix=".jpg",
        )

        with open(temp_path, "rb") as photo_file:
            telegram_request(
                "sendPhoto",
                data={
                    "chat_id": TARGET_CHAT_ID,
                    "caption": caption,
                },
                files={
                    "photo": (
                        "photo.jpg",
                        photo_file,
                        "image/jpeg",
                    )
                },
                timeout=90,
            )
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def send_video_to_telegram(title, body, video_url):
    caption = build_media_caption(title, body, 1024)
    temp_path = None
    try:
        temp_path, _ = stream_download_to_temp(
            video_url,
            max_bytes=MAX_VIDEO_BYTES,
            timeout=120,
            suffix=".mp4",
        )

        with open(temp_path, "rb") as video_file:
            telegram_request(
                "sendVideo",
                data={
                    "chat_id": TARGET_CHAT_ID,
                    "caption": caption,
                    "supports_streaming": "true",
                },
                files={
                    "video": (
                        "video.mp4",
                        video_file,
                        "video/mp4",
                    )
                },
                timeout=180,
            )
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def public_category_label(item):
    labels = {
        "military": "🛡️ نظامی",
        "security": "🔐 امنیتی",
        "defense": "🛰️ دفاعی",
        "geopolitics": "🌍 ژئوپلیتیک",
        "technology": "💻 فناوری",
        "general": "📰 خبر",
    }
    return labels.get(item.get("category", "general"), labels["general"])


def public_verification_label(item):
    labels = {
        "reported": "⚠️ گزارش اولیه",
        "developing": "🔄 خبر در حال تکمیل",
        "confirmed": "✅ تأییدشده در متن گزارش",
        "analysis": "📊 تحلیل",
    }
    return labels.get(item.get("verification", "reported"), labels["reported"])


def format_public_title(item):
    title = normalize_space(item.get("title", ""))
    return f"{public_category_label(item)} | {title}" if title else ""


def format_public_body(item):
    body = (item.get("body", "") or "").strip()
    status = public_verification_label(item)
    return f"{status}\n\n{body}" if body else status


def build_text_only_message(item):
    # لینک/نام منبع عمداً در پست نهایی نمایش داده نمی‌شود.
    title = format_public_title(item)
    body = format_public_body(item)

    parts = []
    if title:
        parts.append(title)
    if body:
        parts.append(body)
    parts.append(FOOTER)

    return "\n\n".join(parts)


def dispatch_item(item):
    title = format_public_title(item)
    body = format_public_body(item)
    photo = item.get("photo")
    video = item.get("video")

    # منبع/لینک منبع در خروجی کانال نمایش داده نمی‌شود.
    media_body = body

    if video:
        try:
            send_video_to_telegram(title, media_body, video)
            return True
        except TelegramAPIError:
            raise
        except Exception as exc:
            log.warning("ارسال ویدیو شکست خورد؛ به عکس/متن fallback می‌کنیم: %s", exc)

    if photo:
        try:
            send_photo_to_telegram(title, media_body, photo)
            return True
        except TelegramAPIError:
            raise
        except Exception as exc:
            log.warning("ارسال عکس شکست خورد؛ به متن fallback می‌کنیم: %s", exc)

    send_text_to_telegram(build_text_only_message(item))
    return True


# ============================================================
# Dedup / event memory
# ============================================================

def purge_recent_memories(state):
    cutoff = now_ts() - DEDUP_WINDOW_MINUTES * 60

    state["_recent_titles"] = [
        x for x in state.get("_recent_titles", [])
        if x.get("ts", 0) >= cutoff
    ][-400:]

    state["_recent_events"] = [
        x for x in state.get("_recent_events", [])
        if x.get("ts", 0) >= cutoff
    ][-400:]


def purge_recent_inputs(state):
    cutoff = now_ts() - GEMINI_RECENT_INPUT_WINDOW_MINUTES * 60
    state["_recent_inputs"] = [
        x for x in state.get("_recent_inputs", [])
        if x.get("ts", 0) >= cutoff
    ][-600:]


def is_input_duplicate(state, post):
    """
    حذف تکراری‌های خام قبل از Gemini. این مرحله عمداً محافظه‌کار است تا
    فقط پست‌هایی را حذف کند که شباهت بالایی با ورودی اخیراً پردازش‌شده دارند.
    """
    purge_recent_inputs(state)
    text = normalize_space(post.get("text", ""))
    if len(text) < 40:
        return False

    for recent in state.get("_recent_inputs", []):
        old_text = recent.get("text", "")
        if not old_text:
            continue
        sim = combined_similarity(text, "", old_text, "")
        if sim >= GEMINI_LOCAL_DUP_THRESHOLD:
            return True

        current_tokens = tokens(text)
        old_tokens = set(recent.get("tokens", []))
        if current_tokens and old_tokens:
            overlap = len(current_tokens & old_tokens) / max(1, len(current_tokens | old_tokens))
            if overlap >= 0.62:
                return True
    return False


def remember_input_posts(state, posts):
    purge_recent_inputs(state)
    for post in posts:
        text = normalize_space(post.get("text", ""))
        if len(text) < 20:
            continue
        state["_recent_inputs"].append({
            "text": text[:3000],
            "tokens": list(tokens(text))[:100],
            "ts": now_ts(),
        })
    state["_recent_inputs"] = state["_recent_inputs"][-600:]


def is_duplicate(state, title, body):
    purge_recent_memories(state)

    for recent in state.get("_recent_titles", []):
        ts = recent.get("ts", 0)
        if now_ts() - ts > DEDUP_WINDOW_MINUTES * 60:
            continue

        old_title = recent.get("title", "")
        old_body = recent.get("body", "")
        sim = combined_similarity(title, body, old_title, old_body)

        if sim >= DEDUP_SIMILARITY_THRESHOLD:
            return True

    # event fingerprints
    current_tokens = tokens(f"{title} {body}")
    for event in state.get("_recent_events", []):
        event_tokens = set(event.get("tokens", []))
        if not current_tokens or not event_tokens:
            continue

        overlap = len(current_tokens & event_tokens) / max(
            1, len(current_tokens | event_tokens)
        )

        if overlap >= 0.48:
            return True

    return False


def remember_item(state, item):
    purge_recent_memories(state)

    title = item.get("title", "")
    body = item.get("body", "")

    state["_recent_titles"].append(
        {
            "title": title,
            "body": body,
            "ts": now_ts(),
        }
    )

    state["_recent_events"].append(
        {
            "tokens": list(tokens(f"{title} {body}"))[:80],
            "region": item.get("region", "world"),
            "ts": now_ts(),
        }
    )

    state["_recent_titles"] = state["_recent_titles"][-400:]
    state["_recent_events"] = state["_recent_events"][-400:]


# ============================================================
# Queue
# ============================================================

def item_fingerprint(item):
    base = normalize_for_match(
        f"{item.get('title','')} {item.get('body','')[:600]}"
    )
    return sha1_text(base)[:20]


def enqueue_item(state, item):
    queue = state.setdefault("_pending_queue", [])
    fp = item_fingerprint(item)

    # از ثبت دوباره همان آیتم در صف جلوگیری کن.
    if any(x.get("fingerprint") == fp for x in queue):
        return False

    item = dict(item)
    item["fingerprint"] = fp
    item.setdefault("queued_at", now_ts())
    item.setdefault("important", False)
    item.setdefault("urgent", False)
    item.setdefault("region", "world")

    apply_queue_diversity_score(state, item)

    queue.append(item)

    # صف را مرتب نگه می‌داریم؛ اما برای تنوع فقط tie-breaker استفاده می‌شود.
    queue.sort(
        key=lambda x: (
            bool(x.get("urgent")),
            x.get("priority_score", 0),
            x.get("queued_at", 0),
        ),
        reverse=True,
    )

    # حداکثر اندازه صف.
    state["_pending_queue"] = queue[:500]
    save_state(state)
    return True


def compute_publish_spacing_minutes(now_dt):
    """زمان‌بندی ثابت نسخه 3.5؛ مستقل از حجم صف."""
    if 8 <= now_dt.hour < 14:
        return MORNING_POST_SPACING_MINUTES
    if 14 <= now_dt.hour < 24:
        return AFTERNOON_POST_SPACING_MINUTES
    return None


def choose_next_queue_item(state):
    queue = state.get("_pending_queue", [])
    if not queue:
        return None, None

    # امتیاز هر بار بر اساس وضعیت تنوع دوباره محاسبه می‌شود.
    candidates = []
    for index, item in enumerate(queue):
        item = dict(item)
        score = apply_queue_diversity_score(state, item)

        # سن خبر به شکل کنترل‌شده یک tie-breaker است.
        queued_at = item.get("queued_at", now_ts())
        age_hours = min(24, max(0, (now_ts() - queued_at) / 3600.0))
        score += min(12, int(age_hours))

        # خبر جهانی برای تنوع جریمه مضاعف نمی‌شود.
        candidates.append((score, index, item))

    candidates.sort(key=lambda x: (x[0], x[2].get("urgent", False)), reverse=True)
    _, index, chosen = candidates[0]
    return index, chosen


def mark_posted_region(state, item):
    region = item.get("region", "world")
    state["_last_posted_region"] = region
    recent = state.setdefault("_last_posted_regions", [])
    recent.append(region)
    state["_last_posted_regions"] = recent[-5:]

    bucket = item.get("content_bucket")
    if bucket:
        bucket_recent = state.setdefault("_last_posted_buckets", [])
        bucket_recent.append(bucket)
        state["_last_posted_buckets"] = bucket_recent[-CONTENT_BUCKET_WINDOW:]


def is_quiet_hour(now_dt):
    return QUIET_START_HOUR <= now_dt.hour < QUIET_END_HOUR


def process_queue(state):
    queue = state.get("_pending_queue", [])
    if not queue:
        return

    now_dt = now_tehran()
    if is_quiet_hour(now_dt):
        return

    spacing = compute_publish_spacing_minutes(now_dt)
    if spacing is None:
        return

    last_release = state.get("_last_queue_release_ts", 0)
    if last_release:
        elapsed_minutes = (now_ts() - last_release) / 60.0
        if elapsed_minutes < spacing:
            return

    index, item = choose_next_queue_item(state)
    if item is None:
        return

    try:
        # media URL ممکن است قدیمی شده باشد؛ عکس را قبل از پست تست می‌کنیم.
        dispatch_item(item)

    except TelegramAPIError as exc:
        if exc.status_code == 429:
            log.warning(
                "Telegram rate limit در صف؛ %s ثانیه بعد دوباره تلاش می‌شود.",
                exc.retry_after,
            )
            return
        log.error("خطای Telegram در انتشار صف: %s", exc)
        return
    except Exception as exc:
        # آیتم از صف حذف نمی‌شود؛ بنابراین با یک خطای موقت خبر گم نمی‌شود.
        log.error("خطا در انتشار آیتم صف؛ آیتم نگه داشته شد: %s", exc)
        return

    # فقط بعد از انتشار موفق حذف شود.
    queue.pop(index)
    state["_pending_queue"] = queue
    state["_last_queue_release_ts"] = now_ts()
    mark_posted_region(state, item)
    save_state(state)

    log.info(
        "پست منتشر شد | region=%s | score=%s | queue=%s | spacing=%s دقیقه",
        item.get("region"),
        item.get("priority_score"),
        len(queue),
        spacing,
    )


# ============================================================
# Source grouping / processing
# ============================================================

def combine_original_media(cluster):
    photo = next((p.get("photo") for p in cluster if p.get("photo")), None)
    video = next((p.get("video") for p in cluster if p.get("video")), None)
    source_url = next((p.get("source_url") for p in cluster if p.get("source_url")), "")
    source_urls = [
        p.get("source_url")
        for p in cluster
        if p.get("source_url")
    ]
    return photo, video, source_url, source_urls


def build_batch_text(cluster):
    chunks = []
    for idx, post in enumerate(cluster, start=1):
        chunks.append(
            f"--- پست ورودی {idx} ---\n"
            f"عنوان: {post.get('title','')}\n"
            f"زمان: {post.get('published_at','')}\n"
            f"لینک: {post.get('source_url','')}\n"
            f"متن:\n{post.get('text','')}"
        )
    return "\n\n".join(chunks)


def process_cluster(state, source_key, cluster):
    if not cluster:
        return

    source_names = sorted({
        p.get("source_name", source_key)
        for p in cluster
        if p.get("source_name")
    })
    source_name = "، ".join(source_names) if source_names else source_key

    # Gemini فقط با batchهای مرتبط کار می‌کند؛ حتی اگر یک پست باشد.
    items = analyze_and_rewrite_safe(
        state,
        cluster,
        source_name,
    )

    if not items:
        raise ValueError("Gemini هیچ آیتم قابل انتشار برنگرداند.")

    photo_url, video_url, first_source_url, source_urls = combine_original_media(cluster)

    successful_interpretation = False

    for result in items:
        if not result.get("relevant"):
            successful_interpretation = True
            continue

        linked_indices = [
            idx for idx in result.get("post_indices", [])
            if 1 <= safe_int(idx, 0) <= len(cluster)
        ]
        linked_posts = [cluster[idx - 1] for idx in linked_indices] if linked_indices else cluster
        result_photo, result_video, result_source_url, result_source_urls = combine_original_media(linked_posts)

        title = normalize_space(result.get("title", ""))
        body = (result.get("body", "") or "").strip()

        if not title or not body:
            continue

        if is_duplicate(state, title, body):
            log.info("یک خروجی به‌عنوان رویداد تکراری رد شد: %s", title)
            successful_interpretation = True
            continue

        region = infer_region(
            f"{title}\n{body}",
            result.get("region"),
        )

        item = {
            "title": title,
            "body": body,
            "image_query": result.get("image_query", ""),
            "urgent": safe_bool(result.get("urgent")),
            "important": safe_bool(result.get("important")),
            "region": region,
            "content_bucket": result.get("content_bucket") or infer_content_bucket(
                f"{title}\n{body}", region
            ),
            "event_type": result.get("event_type", "routine"),
            "category": result.get("category", "general"),
            "verification": result.get("verification", "reported"),
            "priority_hint": safe_int(result.get("priority_hint"), 0),
            "source_note": result.get("source_note", ""),
            "source_url": result_source_url or first_source_url,
            "source_urls": (result_source_urls or source_urls)[:10],
            "photo": result_photo or photo_url,
            "video": result_video or video_url,
            "queued_at": now_ts(),
        }

        if not is_allowed_content_bucket(item["content_bucket"]):
            log.info("خبر خارج از سبد محتوایی نسخه 3.5 حذف شد: %s", title)
            successful_interpretation = True
            continue

        # اگر عکس منبع وجود ندارد، fallback واقعی انجام می‌شود.
        if not item["photo"] and not item["video"]:
            item["photo"] = choose_image_url(
                item,
                linked_posts,
            )

        apply_queue_diversity_score(state, item)
        enqueue_item(state, item)
        remember_item(state, item)

        successful_interpretation = True

        # اگر یک cluster چند خروجی دارد، برای خروجی دوم رسانه اولیه را فقط در صورت
        # نبود تصویر بهتر استفاده می‌کنیم. از چندبار ارسال ویدیو جلوگیری می‌شود.
        photo_url = None
        video_url = None

    if not successful_interpretation:
        raise ValueError("هیچ خروجی معتبری از cluster تولید نشد.")


def process_source(state, source_key, posts):
    """
    نکته مهم:
    UID فقط بعد از اینکه Gemini موفق شد و نتیجه با موفقیت در صف ثبت شد mark می‌شود.
    بنابراین Timeout/429/خطای شبکه باعث گم‌شدن دائمی خبر نمی‌شود.
    """
    if not posts:
        return

    processed = set(state.get(source_key, []))

    # بار اول فقط snapshot می‌گیریم و هیچ خبر قدیمی را پردازش نمی‌کنیم.
    if not state.get("_source_initialized", {}).get(source_key):
        state.setdefault("_source_initialized", {})[source_key] = True
        state[source_key] = [p["uid"] for p in posts[-500:]]
        save_state(state)
        log.info(
            "منبع %s برای اولین بار ثبت شد؛ پست‌های موجود قدیمی پردازش نشدند.",
            source_key,
        )
        return

    new_posts = [
        p for p in posts
        if p.get("uid") not in processed
    ]

    if not new_posts:
        return

    clusters = cluster_posts(new_posts)

    for cluster in clusters:
        uids = [p["uid"] for p in cluster]
        try:
            process_cluster(
                state,
                source_key,
                cluster,
            )

            # فقط بعد از موفقیت کامل.
            current = state.setdefault(source_key, [])
            for uid in uids:
                if uid not in current:
                    current.append(uid)
            state[source_key] = current[-500:]
            save_state(state)

            log.info(
                "cluster با %s پست پردازش و به صف افزوده شد؛ source=%s",
                len(cluster),
                source_key,
            )

        except Exception as exc:
            log.error(
                "cluster شکست خورد و UIDهای آن mark نشدند؛ source=%s error=%s",
                source_key,
                exc,
            )
            # هیچ UID از این cluster مصرف‌شده تلقی نمی‌شود.
            continue


# ============================================================
# Scheduled messages / cleanup
# ============================================================

def purge_unimportant_queue(state, today_str):
    if state.get("_last_queue_purge_date") == today_str:
        return

    queue = state.get("_pending_queue", [])
    before = len(queue)

    # خبرهای مهم و urgent باقی می‌مانند؛ باقی‌مانده‌های معمولی شب حذف می‌شوند.
    state["_pending_queue"] = [
        item for item in queue
        if safe_bool(item.get("important")) or safe_bool(item.get("urgent"))
    ]
    state["_last_queue_purge_date"] = today_str

    removed = before - len(state["_pending_queue"])
    if removed:
        log.info(
            "%s خبر عادیِ باقی‌مانده در نیمه‌شب حذف شد.",
            removed,
        )

    save_state(state)


def check_scheduled_messages(state):
    now = now_tehran()
    today_str = now.strftime("%Y-%m-%d")

    if now.hour == MORNING_HOUR and state.get("_last_morning_date") != today_str:
        try:
            send_text_to_telegram(MORNING_MESSAGE)
            state["_last_morning_date"] = today_str
            save_state(state)
            log.info("پیام صبح‌بخیر ارسال شد.")
        except Exception as exc:
            log.error("ارسال صبح‌بخیر شکست خورد: %s", exc)

    if now.hour == NIGHT_HOUR and state.get("_last_night_date") != today_str:
        try:
            send_text_to_telegram(NIGHT_MESSAGE)
            state["_last_night_date"] = today_str
            save_state(state)
            log.info("پیام شب‌بخیر ارسال شد.")
        except Exception as exc:
            log.error("ارسال شب‌بخیر شکست خورد: %s", exc)

        purge_unimportant_queue(state, today_str)


# ============================================================
# Polling
# ============================================================

def build_gemini_batches(clusters):
    """
    خوشه‌های رویدادی را بدون شکستن آن‌ها در batchهای کوچک‌تر جمع می‌کند تا
    چند خبر مستقل با یک درخواست Gemini بررسی شوند.
    """
    batches = []
    current = []
    current_size = 0

    for cluster in clusters:
        if not cluster:
            continue
        cluster_size = len(cluster)

        if current and current_size + cluster_size > GEMINI_BATCH_MAX_POSTS:
            batches.append(current)
            current = []
            current_size = 0

        # یک خوشه بزرگ را به قطعات جدا تقسیم می‌کنیم؛ حالت معمول نیست.
        if cluster_size > GEMINI_BATCH_MAX_POSTS:
            for start in range(0, cluster_size, GEMINI_BATCH_MAX_POSTS):
                part = cluster[start:start + GEMINI_BATCH_MAX_POSTS]
                if current:
                    batches.append(current)
                    current = []
                    current_size = 0
                batches.append(part)
            continue

        current.extend(cluster)
        current_size += cluster_size

    if current:
        batches.append(current)
    return batches


def process_once(state):
    """
    چرخه پردازش سطح 1 تا 3:
    - جمع‌آوری از همه کانال‌های تلگرام
    - حذف نویزهای واضح
    - خوشه‌بندی مشترک بین منابع برای ادغام یک رویداد
    - بازنویسی/فیلتر با Gemini
    - صف‌بندی و انتشار با قالب حرفه‌ای
    """
    all_new_posts = []

    for channel in SOURCE_CHANNELS:
        source_key = f"tg:{channel}"
        posts = fetch_channel_posts(channel)
        if not posts:
            continue

        processed = set(state.get(source_key, []))

        if not state.get("_source_initialized", {}).get(source_key):
            state.setdefault("_source_initialized", {})[source_key] = True
            state[source_key] = [p["uid"] for p in posts[-500:]]
            save_state(state)
            log.info(
                "منبع %s برای اولین بار ثبت شد؛ پست‌های موجود قدیمی پردازش نشدند.",
                source_key,
            )
            continue

        new_posts = [p for p in posts if p.get("uid") not in processed]
        for post in new_posts:
            post["_source_key"] = source_key
        all_new_posts.extend(new_posts)

    if all_new_posts:
        # تکراری‌های خامی که قبلاً در چند منبع دیده و پردازش شده‌اند، قبل از Gemini حذف می‌شوند.
        filtered_new_posts = []
        locally_skipped = 0
        for post in all_new_posts:
            if is_input_duplicate(state, post):
                locally_skipped += 1
                source_key = post.get("_source_key")
                uid = post.get("uid")
                if source_key and uid:
                    current = state.setdefault(source_key, [])
                    if uid not in current:
                        current.append(uid)
                    state[source_key] = current[-500:]
                continue
            filtered_new_posts.append(post)

        if locally_skipped:
            log.info("%s پست تکراری قبل از Gemini حذف شد.", locally_skipped)

        clusters = cluster_posts(filtered_new_posts)
        gemini_batches = build_gemini_batches(clusters)
        log.info("%s پست جدید -> %s cluster -> %s درخواست احتمالی Gemini", len(all_new_posts), len(clusters), len(gemini_batches))

        for cluster in gemini_batches:
            try:
                process_cluster(state, "multi-source", cluster)
                remember_input_posts(state, cluster)

                # فقط بعد از موفقیت کامل، UIDهای همان منابع ثبت می‌شوند.
                for post in cluster:
                    source_key = post.get("_source_key")
                    uid = post.get("uid")
                    if not source_key or not uid:
                        continue
                    current = state.setdefault(source_key, [])
                    if uid not in current:
                        current.append(uid)
                    state[source_key] = current[-500:]

                save_state(state)
                log.info(
                    "cluster چندمنبعی با %s پست پردازش شد؛ sources=%s",
                    len(cluster),
                    len({p.get("_source_key") for p in cluster}),
                )

            except Exception as exc:
                log.error(
                    "cluster شکست خورد و UIDهای آن mark نشدند؛ error=%s",
                    exc,
                )

    check_scheduled_messages(state)
    process_queue(state)
    purge_recent_memories(state)
    save_state(state)


# ============================================================
# Configuration validation
# ============================================================

def validate_config():
    missing = []

    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")

    if not TARGET_CHAT_ID:
        missing.append("TARGET_CHAT_ID")

    if not GEMINI_API_KEYS:
        missing.append("GEMINI_API_KEYS یا GEMINI_API_KEY")

    if not SOURCE_CHANNELS:
        missing.append("SOURCE_CHANNELS")

    if missing:
        raise RuntimeError(
            "این متغیرهای ضروری تنظیم نشده‌اند: " + ", ".join(missing)
        )

    if len(GEMINI_API_KEYS) < 2:
        log.warning(
            "فقط %s کلید Gemini تنظیم شده است؛ چرخش چندکلیدی فعال می‌ماند اما "
            "مزیت failover محدود است.",
            len(GEMINI_API_KEYS),
        )

    # کلیدهای تکراری.
    duplicates = len(GEMINI_API_KEYS) - len({key_id(k) for k in GEMINI_API_KEYS})
    if duplicates:
        raise RuntimeError(
            f"{duplicates} کلید Gemini تکراری است؛ کلیدها را بررسی کن."
        )


# ============================================================
# Main
# ============================================================

def main():
    validate_config()

    state = load_state()
    cleanup_gemini_key_state(state)
    save_state(state)

    log.info("==============================================")
    log.info("Raptor News Bot v3 (Level 1-3) شروع شد")
    log.info("Telegram sources: %s", len(SOURCE_CHANNELS))
    log.info("Gemini keys: %s", len(GEMINI_API_KEYS))
    log.info("Gemini model: %s", GEMINI_MODEL)
    log.info("Publish spacing: 08-14=%s دقیقه | 14-24=%s دقیقه", MORNING_POST_SPACING_MINUTES, AFTERNOON_POST_SPACING_MINUTES)
    log.info("Event cluster max inputs: %s", EVENT_MAX_ITEMS_PER_CLUSTER)
    log.info("Event cluster max outputs: %s", EVENT_MAX_OUTPUT_ITEMS)
    log.info("Gemini batch max posts: %s", GEMINI_BATCH_MAX_POSTS)
    log.info("Gemini local duplicate threshold: %.2f", GEMINI_LOCAL_DUP_THRESHOLD)
    log.info("State file: %s", STATE_FILE)
    log.info("==============================================")

    while True:
        started = now_ts()

        try:
            process_once(state)
        except Exception as exc:
            log.exception("خطای عمومی در چرخه اصلی: %s", exc)

        elapsed = now_ts() - started
        sleep_for = max(5, POLL_INTERVAL_SECONDS - int(elapsed))
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
