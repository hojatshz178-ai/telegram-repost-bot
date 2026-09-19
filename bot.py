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

# ============================================================
# AI providers — v4
# ============================================================
# ترتیب عمداً ثابت است:
#   1) Groq = موتور اصلی
#   2) Gemini = کمک اول
#   3) Mistral = کمک دوم
# Failover بر اساس availability/cooldown انجام می‌شود و یک provider خراب
# نمی‌تواند کل چرخه را متوقف کند.
GROQ_API_KEYS = split_csv(
    os.environ.get("GROQ_API_KEYS") or os.environ.get("GROQ_API_KEY", "")
)
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b").strip()
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

_raw_gemini_keys = (
    os.environ.get("GEMINI_API_KEYS")
    or os.environ.get("GEMINI_API_KEY", "")
)
GEMINI_API_KEYS = split_csv(_raw_gemini_keys)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip()
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    f"models/{GEMINI_MODEL}:generateContent"
)

MISTRAL_API_KEYS = split_csv(
    os.environ.get("MISTRAL_API_KEYS") or os.environ.get("MISTRAL_API_KEY", "")
)
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-small-latest").strip()
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"

AI_PROVIDER_ORDER = ("groq", "gemini", "mistral")
AI_PROVIDER_LABELS = {
    "groq": "Groq",
    "gemini": "Gemini",
    "mistral": "Mistral",
}
AI_PROVIDER_KEYS = {
    "groq": GROQ_API_KEYS,
    "gemini": GEMINI_API_KEYS,
    "mistral": MISTRAL_API_KEYS,
}
AI_PROVIDER_MODELS = {
    "groq": GROQ_MODEL,
    "gemini": GEMINI_MODEL,
    "mistral": MISTRAL_MODEL,
}

POLL_INTERVAL_SECONDS = max(30, env_int("POLL_INTERVAL_SECONDS", 180))

# هر provider برای هر درخواست حداکثر دو تلاش داخلی دارد؛ بعد فوراً provider بعدی
# در زنجیره وارد می‌شود. 429 باعث چرخش به provider بعدی می‌شود، نه ده‌ها retry.
AI_MAX_ATTEMPTS_PER_PROVIDER = max(
    1, min(2, env_int("AI_MAX_ATTEMPTS_PER_PROVIDER", 2))
)
AI_MIN_REQUEST_INTERVAL_SECONDS = max(
    0.0, env_float("AI_MIN_REQUEST_INTERVAL_SECONDS", 1.2)
)
AI_MAX_OUTPUT_TOKENS = max(
    700, min(2200, env_int("AI_MAX_OUTPUT_TOKENS", 1400))
)
AI_PROVIDER_COOLDOWN_FLOOR_SECONDS = max(
    20, env_int("AI_PROVIDER_COOLDOWN_FLOOR_SECONDS", 45)
)
AI_429_MAX_COOLDOWN_SECONDS = max(
    60, env_int("AI_429_MAX_COOLDOWN_SECONDS", 600)
)
AI_FAILURE_RETRY_MINUTES = max(
    3, env_int("AI_FAILURE_RETRY_MINUTES", 10)
)
EDITOR_ENABLED = os.environ.get("AI_EDITOR_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
TEST_MODE = os.environ.get("TEST_MODE", "false").strip().lower() in {
    "1", "true", "yes", "on"
}

# Batch مشترک برای کاهش مصرف و جلوگیری از burst.
AI_BATCH_MAX_POSTS = max(2, min(10, env_int("AI_BATCH_MAX_POSTS", 8)))
AI_EDITOR_MAX_ITEMS = max(1, min(8, env_int("AI_EDITOR_MAX_ITEMS", 8)))
AI_INPUT_MAX_CHARS_PER_POST = max(
    900, min(4000, env_int("AI_INPUT_MAX_CHARS_PER_POST", 2400))
)
AI_RECENT_INPUT_WINDOW_MINUTES = max(
    24 * 60, env_int("AI_RECENT_INPUT_WINDOW_MINUTES", 24 * 60)
)
AI_LOCAL_DUP_THRESHOLD = min(
    0.97, max(0.90, env_float("AI_LOCAL_DUP_THRESHOLD", 0.94))
)

# ============================================================
# Dedup / event memory — deliberately stronger than v3.8
# ============================================================
# 24 ساعت حداقل فاصله برای تکرار عادی. اگر Railway مقدار قدیمی 360 را داشته
# باشد، این نسخه عمداً آن را به حداقل 24 ساعت ارتقا می‌دهد.
DEDUP_WINDOW_MINUTES = max(
    24 * 60, env_int("DEDUP_WINDOW_MINUTES", 24 * 60)
)
DEDUP_SIMILARITY_THRESHOLD = min(
    0.96,
    max(0.78, env_float("DEDUP_SIMILARITY_THRESHOLD", 0.82)),
)
EVENT_MEMORY_WINDOW_MINUTES = max(
    48 * 60, env_int("EVENT_MEMORY_WINDOW_MINUTES", 72 * 60)
)
EVENT_UPDATE_MIN_NOVELTY = max(
    50, min(95, env_int("EVENT_UPDATE_MIN_NOVELTY", 62))
)
EVENT_MAX_UPDATES_PER_24H = max(
    1, min(4, env_int("EVENT_MAX_UPDATES_PER_24H", 2))
)
EVENT_SIMILARITY_THRESHOLD = min(
    0.95, max(0.50, env_float("EVENT_SIMILARITY_THRESHOLD", 0.62))
)

# ============================================================
# Fixed publishing schedule — unchanged
# ============================================================
MORNING_POST_SPACING_MINUTES = 60
AFTERNOON_POST_SPACING_MINUTES = 45

# ============================================================
# Event clustering
# ============================================================
EVENT_CLUSTER_WINDOW_MINUTES = max(
    30, env_int("EVENT_CLUSTER_WINDOW_MINUTES", 90)
)
EVENT_MAX_ITEMS_PER_CLUSTER = max(
    2, min(10, env_int("EVENT_MAX_ITEMS_PER_CLUSTER", 6))
)
EVENT_MAX_OUTPUT_ITEMS = max(
    1, min(6, env_int("EVENT_MAX_OUTPUT_ITEMS", 6))
)

# Only used by a quiet-hour window; normal news stays in the queue.
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

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; RaptorNewsBot/4.0; "
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

REWRITE_PROMPT = """تو «سردبیر ارشد» یک کانال خبری فارسی در حوزه نظامی، امنیتی و ژئوپلیتیکی هستی.
وظیفه تو ترجمه ساده نیست؛ باید از چند ورودی خام، یک خبر فارسی طبیعی، دقیق، حرفه‌ای و قابل انتشار بسازی.

اصل‌های تحریریه:
- لحن: خبرنگار فارسی‌زبان واقعی؛ روشن، مستقیم، حرفه‌ای و بدون لحن ماشینی.
- جمله‌ها کوتاه و خبری باشند. از جمله‌های بسیار بلند و ترجمه‌وار دوری کن.
- از مقدمه‌های کلیشه‌ای مانند «در تحولات اخیر»، «در ادامه تحولات»، «این موضوع نشان می‌دهد» و عبارت‌های تکراری خودداری کن.
- تیتر باید بر اساس «اتفاق اصلی» ساخته شود، نه بر اساس یک قالب ثابت. تیترها باید بین پست‌های مختلف تنوع ساختاری داشته باشند.
- کلیک‌خور بودن فقط با وضوح و اهمیت واقعی خبر ایجاد شود، نه با اغراق.
- هیچ ادعای تأییدنشده‌ای را به‌عنوان واقعیت قطعی ننویس.
- اگر منبع ادعایی را مطرح کرده، وضعیت آن ادعا را طبیعی و کوتاه در متن بیان کن؛ هرگز برچسب‌هایی مثل «⚠️ گزارش اولیه» را جداگانه ننویس.
- نام کانال، یوزرنیم، آیدی، لینک یا عبارت‌هایی مثل «به نقل از کانال ...»، «کانال ... گزارش داد»، «به گفته کانال ...» یا «این خبر از کانال ... برداشته شده» را هرگز در title، body یا significance نیاور. نام منبع فقط برای پردازش داخلی است و نباید وارد متن قابل انتشار شود.
- اگر لازم است وضعیت اعتبار یک ادعا بیان شود، بدون ذکر نام کانال یا یوزرنیم و با عباراتی عمومی مثل «بر اساس گزارش‌های اولیه» یا «این ادعا هنوز تأیید مستقل نشده» بیانش کن.
- محتوایی که در ورودی نیست را نساز. عدد، نام، تاریخ، مکان و توانایی‌ها را حدس نزن.
- خبر کم‌ارزش، تبلیغاتی، دعوت به عضویت، کپشن تکراری یا محتوای بدون ارزش خبری را relevant=false کن.

فارسی‌سازی عمیق:
- نیم‌فاصله طبیعی و صحیح.
- نشانه‌گذاری فارسی و فاصله‌گذاری درست.
- ساختار طبیعی فعل‌های فارسی.
- حذف ترکیب‌های تحت‌اللفظی و ترجمه‌ای.
- نام‌های تخصصی و اصطلاحات نظامی را دقیق و یکدست نگه دار.
- اگر نام یک شخص، سازمان، کشور، سامانه یا محل در ورودی وجود دارد، همان را به شکل استاندارد و سازگار استفاده کن.

ادغام و حافظه رویداد:
- چند ورودی درباره یک اتفاق را به یک خبر واحد تبدیل کن.
- «جزئیات تازه»، «تصاویر جدید»، «بیانیه طرف مقابل» یا «آمار جدید» فقط وقتی خبر مستقل شوند که واقعاً اطلاعات تازه و مهمی اضافه کنند.
- اگر رویداد قبلاً در حافظه منتشر شده و ورودی جدید چیز مهم و تازه‌ای اضافه نمی‌کند، relevant=false و duplicate=true بده.
- اگر همان رویداد اطلاعات مهم تازه‌ای دارد، relevant=true، is_update=true و novelty_score بالا بده و همان event_key قبلی را تا حد امکان حفظ کن.
- اگر رویداد جدید است، event_key جدید و مشخصی بساز.
- event_key باید کوتاه، مشخص و وابسته به همان رویداد باشد؛ از کلیدهای عمومی مثل iran_news، military_news، breaking_news استفاده نکن.

سبک نوشتار بر اساس نوع خبر:
- جنگ/درگیری: سریع، مستقیم و واقعیت‌محور.
- ژئوپلیتیک: زمینه‌دارتر، با توضیح کوتاه درباره اهمیت واقعی رویداد.
- فناوری/تجهیزات دفاعی: فنی‌تر اما قابل‌فهم و بدون حدس.
- خبر مرتبط با ایران: زمینه لازم را کوتاه و روشن ارائه کن.
- خبر فوری: عنوان کوتاه و اطلاعات اصلی در جمله اول.
- خبر مهم: می‌تواند یک پاراگراف توضیحی بیشتر داشته باشد.

طول هوشمند:
- brief: حدود 45 تا 90 کلمه.
- standard: حدود 90 تا 160 کلمه.
- full: حدود 150 تا 230 کلمه.
- فقط اگر واقعاً ارزش اطلاعاتی دارد از full استفاده کن.
- اگر خبر چندمنبعی، update، مهم/فوری، تحلیلی یا از نظر وضعیت منبع حساس است، needs_editor=true بده؛ برای خبرهای ساده false کافی است.

اهمیت و پاراگراف 📌:
- important=true را فقط برای خبرهایی بده که واقعاً ارزش قرار گرفتن زودتر در صف را دارند.
- significance فقط وقتی پر شود که توضیح واقعی و فشرده‌ای درباره اهمیت رویداد بدهد؛ جمله عمومی و مصنوعی نباشد.
- significance نباید صرفاً تکرار بدنه باشد.

جلوگیری از تکرار زبانی:
از عبارت‌های پرتکرار مانند «این در حالی است که»، «در همین راستا»، «بر اساس گزارش‌های منتشرشده» و «این موضوع نشان می‌دهد» بیش از حد استفاده نکن.
اگر وضعیت منبع باید بیان شود، تنوع طبیعی ایجاد کن: «بر اساس گزارش اولیه»، «این ادعا هنوز تأیید مستقل نشده»، «به گفته منابع»، «منابع موجود می‌گویند» و مانند آن؛ فقط زمانی که از نظر محتوایی لازم است.

محتوای کانال:
content_bucket دقیقاً یکی از این پنج مقدار باشد:
- iran
- middle_east_war
- middle_east_developments
- russia_ukraine
- superpower_military
هر چیز خارج از این پنج سبد relevant=false شود.

region دقیقاً یکی از این چهار مقدار:
- iran
- middle_east
- world
- superpower

نوع رویداد:
event_type یکی از:
- military_event
- security
- defense
- geopolitics
- routine

category یکی از:
- military
- security
- defense
- geopolitics
- technology
- general

verification یکی از:
- reported
- developing
- confirmed
- analysis

style_mode یکی از:
- breaking
- conflict
- geopolitics
- defense_tech
- iran
- important
- analysis
- standard

length_class یکی از:
- brief
- standard
- full

post_indices را با شماره ورودی‌هایی که برای همان خروجی استفاده شده‌اند مشخص کن.
entities فهرست کوتاه نام‌های کلیدی همان رویداد باشد؛ حداکثر 10 مورد.

ورودی‌های خام:
{content}

حافظه کوتاه‌مدت رویدادهای قبلی:
{event_memory}

حافظه سبک کانال:
{style_memory}

فقط JSON معتبر و بدون Markdown برگردان:
{
  "items": [
    {
      "relevant": true,
      "duplicate": false,
      "is_update": false,
      "novelty_score": 0,
      "event_key": "unique_event_key",
      "title": "...",
      "body": "...",
      "significance": "",
      "image_query": "3 to 6 English words",
      "post_indices": [1],
      "content_bucket": "iran|middle_east_war|middle_east_developments|russia_ukraine|superpower_military",
      "region": "iran|middle_east|world|superpower",
      "event_type": "military_event|security|defense|geopolitics|routine",
      "category": "military|security|defense|geopolitics|technology|general",
      "verification": "reported|developing|confirmed|analysis",
      "urgent": false,
      "important": false,
      "needs_editor": false,
      "style_mode": "breaking|conflict|geopolitics|defense_tech|iran|important|analysis|standard",
      "length_class": "brief|standard|full",
      "priority_hint": 0,
      "source_note": "...",
      "entities": ["..."],
      "duplicate_reason": ""
    }
  ]
}
"""

EDITOR_PROMPT = """تو سردبیر نهایی همان کانال خبری فارسی هستی.
ورودی شامل پیش‌نویس‌هایی است که قبلاً از منابع اصلی استخراج شده‌اند و بخشی از ورودی خام نیز برای کنترل واقعیت در اختیار توست.

فقط کارهای زیر را انجام بده:
1) فارسی را طبیعی‌تر، حرفه‌ای‌تر و خبرگزاری‌گونه کن.
2) جمله‌های ماشینی، کلیشه‌ای و ترجمه‌وار را حذف کن.
3) تیتر را انسانی و غیرکلیشه‌ای کن، بدون کلیک‌بیت.
4) غلط نگارشی، نیم‌فاصله، نشانه‌گذاری و ساختار جمله را اصلاح کن.
5) هیچ واقعیت جدیدی اضافه نکن و هیچ عدد، نام یا ادعایی را بدون پشتوانه تغییر نده.
6) وضعیت تأیید خبر را شفاف ولی طبیعی نگه دار.
7) اگر significance خالی است فقط در صورتی پرش کن که واقعاً برای درک اهمیت خبر لازم باشد.
8) اطلاعات اصلی باید در جمله اول باقی بماند.
9) event_key، post_indices، content_bucket، region و verification را فقط در صورت وجود خطای واضح اصلاح کن.
10) یک خبر مستقل را با قالب قبلی تکرار نکن.

JSON معتبر و بدون Markdown برگردان:
{
  "items": [
    {
      "draft_id": 1,
      "relevant": true,
      "duplicate": false,
      "is_update": false,
      "novelty_score": 0,
      "event_key": "...",
      "title": "...",
      "body": "...",
      "significance": "",
      "image_query": "...",
      "post_indices": [1],
      "content_bucket": "iran|middle_east_war|middle_east_developments|russia_ukraine|superpower_military",
      "region": "iran|middle_east|world|superpower",
      "event_type": "military_event|security|defense|geopolitics|routine",
      "category": "military|security|defense|geopolitics|technology|general",
      "verification": "reported|developing|confirmed|analysis",
      "urgent": false,
      "important": false,
      "style_mode": "breaking|conflict|geopolitics|defense_tech|iran|important|analysis|standard",
      "length_class": "brief|standard|full",
      "priority_hint": 0,
      "source_note": "...",
      "entities": ["..."]
    }
  ]
}
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


def normalize_persian_text(text):
    """ویراستاری سطح سیستم برای خروجی فارسی؛ عمداً محافظه‌کار است."""
    text = str(text or "")
    if not text:
        return ""

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
        "،,": "،",
        ",": "،",
        ";": "؛",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\s+([،؛؟!])", r"\1", text)
    text = re.sub(r"([،؛:])(?=\S)", r"\1 ", text)

    # نیم‌فاصله‌های پرتکرار فارسی.
    text = re.sub(r"\bمی\s+([\u0600-\u06ffA-Za-z][^\s\n]*)", r"می‌\1", text)
    text = re.sub(r"\bنمی\s+([\u0600-\u06ffA-Za-z][^\s\n]*)", r"نمی‌\1", text)
    text = re.sub(r"([\u0600-\u06ff])\s+(ها|های|تر|ترین)\b", r"\1‌\2", text)
    text = re.sub(r"\bبه\s+روز\b", "به‌روز", text)
    text = re.sub(r"\bهم\s+چنین\b", "همچنین", text)

    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def key_id(key):
    return sha1_text(key)[:16]


def normalize_event_key(value):
    value = normalize_for_match(str(value or ""))
    value = re.sub(r"[^\w\- ]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip().replace(" ", "_")
    generic = {
        "iran_news", "iran_event", "iran_update", "military_news",
        "military_event", "breaking_news", "defense_news", "middle_east_news",
        "news_update", "world_news", "latest_news", "general_news",
    }
    if value in generic or len(value) < 6:
        return ""
    return value[:140]


def event_signature(text):
    """امضای واژگانی رویداد برای dedup محلی، مستقل از متن کامل."""
    normalized = normalize_for_match(text)
    raw = re.findall(r"[\w\u0600-\u06ff]{2,}", normalized, flags=re.UNICODE)
    generic = {
        "خبر", "گزارش", "منبع", "اعلام", "اعلامی", "گفت", "گفته",
        "تازه", "جدید", "امروز", "امشب", "امروز", "درگیری", "تحولات",
        "این", "آن", "است", "شد", "شده", "می", "شود", "خواهد",
        "the", "news", "report", "reports", "latest", "new", "update",
    }
    seen = []
    for tok in raw:
        if tok in STOPWORDS or tok in generic or tok.isdigit():
            continue
        if len(tok) < 3:
            continue
        if tok not in seen:
            seen.append(tok)
        if len(seen) >= 14:
            break
    return " ".join(seen)


def event_key_or_signature(title, body, event_key=""):
    normalized_key = normalize_event_key(event_key)
    if normalized_key:
        return normalized_key
    return sha1_text(event_signature(f"{title} {body}") or normalize_for_match(f"{title} {body}"))[:20]


# ============================================================
# State
# ============================================================

def default_state():
    return {
        "version": 4,
        "_source_initialized": {},
        "_recent_titles": [],
        "_recent_events": [],
        "_recent_inputs": [],
        "_event_memory": {},
        "_pending_queue": [],
        "_retry_uids": {},
        "_last_queue_release_ts": 0,
        "_last_morning_date": "",
        "_last_night_date": "",
        "_last_queue_purge_date": "",
        "_last_posted_region": "",
        "_last_posted_regions": [],
        "_last_posted_buckets": [],
        "_ai": {
            "providers": {
                provider: {
                    "cooldown_until": 0,
                    "last_request_ts": 0,
                    "fail_count": 0,
                    "last_status": 0,
                }
                for provider in AI_PROVIDER_ORDER
            },
            "keys": {provider: {} for provider in AI_PROVIDER_ORDER},
        },
        "_style_memory": {
            "recent_titles": [],
            "recent_openings": [],
            "entity_counts": {},
        },
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

        if not isinstance(base.get("_pending_queue"), list):
            base["_pending_queue"] = []
        if not isinstance(base.get("_recent_titles"), list):
            base["_recent_titles"] = []
        if not isinstance(base.get("_recent_events"), list):
            base["_recent_events"] = []
        if not isinstance(base.get("_recent_inputs"), list):
            base["_recent_inputs"] = []
        if not isinstance(base.get("_recent_events"), list):
            base["_recent_events"] = []
        if not isinstance(base.get("_last_posted_regions"), list):
            base["_last_posted_regions"] = []
        if not isinstance(base.get("_last_posted_buckets"), list):
            base["_last_posted_buckets"] = []
        if not isinstance(base.get("_retry_uids"), dict):
            base["_retry_uids"] = {}
        if not isinstance(base.get("_event_memory"), dict):
            base["_event_memory"] = {}
        if not isinstance(base.get("_style_memory"), dict):
            base["_style_memory"] = default_state()["_style_memory"]

        ai = base.get("_ai")
        if not isinstance(ai, dict):
            ai = default_state()["_ai"]
            base["_ai"] = ai
        ai.setdefault("providers", {})
        ai.setdefault("keys", {})
        for provider in AI_PROVIDER_ORDER:
            ai["providers"].setdefault(
                provider,
                {
                    "cooldown_until": 0,
                    "last_request_ts": 0,
                    "fail_count": 0,
                    "last_status": 0,
                },
            )
            if not isinstance(ai["keys"].get(provider), dict):
                ai["keys"][provider] = {}

        # Migration from v3.8 Gemini-only state.
        legacy_gemini = state.get("_gemini_keys", {})
        if isinstance(legacy_gemini, dict):
            for kid, info in legacy_gemini.items():
                if kid not in ai["keys"].setdefault("gemini", {}):
                    ai["keys"]["gemini"][kid] = info
        if "_gemini_global_cooldown_until" in state:
            ai["providers"]["gemini"]["cooldown_until"] = max(
                ai["providers"]["gemini"].get("cooldown_until", 0),
                float(state.get("_gemini_global_cooldown_until", 0) or 0),
            )

        return base
    except Exception as exc:
        log.exception("خواندن state شکست خورد؛ state خالی ساخته می‌شود: %s", exc)
        return default_state()


def save_state(state):
    """Atomic write: temp -> flush -> fsync -> replace."""
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


def infer_content_bucket(text, region=""):
    """طبقه‌بندی محافظه‌کارانه برای حفظ سبد محتوایی نسخه‌های قبل."""
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


def recent_bucket_counts(state):
    recent = state.get("_last_posted_buckets", [])[-CONTENT_BUCKET_WINDOW:]
    return {bucket: recent.count(bucket) for bucket in ALLOWED_CONTENT_BUCKETS}


def bucket_deficit(state, bucket):
    recent = state.get("_last_posted_buckets", [])[-CONTENT_BUCKET_WINDOW:]
    total = len(recent)
    if total == 0:
        return CONTENT_BUCKET_TARGETS.get(bucket, 0)
    target = CONTENT_BUCKET_TARGETS.get(bucket, 0) * min(
        CONTENT_BUCKET_WINDOW, total + 1
    )
    return target - recent.count(bucket)


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
# AI provider engine — Groq primary / Gemini backup / Mistral backup
# ============================================================

class AIProviderError(RuntimeError):
    def __init__(self, provider, status_code, message, retry_after=0, fatal=False):
        self.provider = provider
        self.status_code = status_code
        self.retry_after = retry_after
        self.fatal = fatal
        super().__init__(f"{provider} HTTP {status_code}: {message}")


class AIAllProvidersUnavailable(RuntimeError):
    pass


def provider_state(state, provider):
    return state["_ai"]["providers"][provider]


def provider_key_states(state, provider):
    return state["_ai"]["keys"].setdefault(provider, {})


def initialize_ai_state(state):
    for provider in AI_PROVIDER_ORDER:
        pstate = provider_state(state, provider)
        pstate.setdefault("cooldown_until", 0)
        pstate.setdefault("last_request_ts", 0)
        pstate.setdefault("fail_count", 0)
        pstate.setdefault("last_status", 0)
        key_states = provider_key_states(state, provider)
        for key in AI_PROVIDER_KEYS[provider]:
            key_states.setdefault(
                key_id(key),
                {
                    "cooldown_until": 0,
                    "fail_count": 0,
                    "last_status": 0,
                    "last_used": 0,
                },
            )
        valid = {key_id(k) for k in AI_PROVIDER_KEYS[provider]}
        for kid in list(key_states):
            if kid not in valid:
                del key_states[kid]


def choose_available_provider_keys(state, provider):
    keys = AI_PROVIDER_KEYS[provider]
    if not keys:
        return []
    initialize_ai_state(state)
    now = now_ts()
    info = provider_key_states(state, provider)
    available = []
    for idx, key in enumerate(keys):
        kid = key_id(key)
        entry = info[kid]
        if float(entry.get("cooldown_until", 0) or 0) <= now:
            available.append((idx, key))
    if available:
        return available

    # اگر همه کلیدهای همان provider cooldown هستند، provider را هم bypass می‌کنیم؛
    # کل زنجیره نباید روی یک کلید منتظر بماند.
    return []


def mark_provider_cooldown(state, provider, seconds, status=0):
    pstate = provider_state(state, provider)
    pstate["cooldown_until"] = max(
        float(pstate.get("cooldown_until", 0) or 0),
        now_ts() + max(0, seconds),
    )
    pstate["fail_count"] = int(pstate.get("fail_count", 0)) + 1
    pstate["last_status"] = status


def mark_key_cooldown(state, provider, key, seconds, status=0):
    entry = provider_key_states(state, provider).setdefault(
        key_id(key),
        {"cooldown_until": 0, "fail_count": 0, "last_status": 0, "last_used": 0},
    )
    entry["cooldown_until"] = now_ts() + max(0, seconds)
    entry["fail_count"] = int(entry.get("fail_count", 0)) + 1
    entry["last_status"] = status
    entry["last_used"] = now_ts()


def mark_provider_success(state, provider, key):
    pstate = provider_state(state, provider)
    pstate["cooldown_until"] = 0
    pstate["fail_count"] = 0
    pstate["last_status"] = 200
    pstate["last_request_ts"] = now_ts()
    entry = provider_key_states(state, provider).setdefault(
        key_id(key),
        {"cooldown_until": 0, "fail_count": 0, "last_status": 0, "last_used": 0},
    )
    entry["cooldown_until"] = 0
    entry["fail_count"] = 0
    entry["last_status"] = 200
    entry["last_used"] = now_ts()


def retry_after_seconds(response, default_seconds):
    raw = response.headers.get("Retry-After")
    if raw:
        try:
            return max(1, int(float(raw)))
        except Exception:
            pass
    try:
        payload = response.json()
        raw = payload.get("retry_after") or payload.get("parameters", {}).get("retry_after")
        if raw:
            return max(1, int(float(raw)))
    except Exception:
        pass
    return default_seconds


def provider_model_name(provider):
    return AI_PROVIDER_MODELS[provider]


def provider_prompt_payload(provider, prompt, max_output_tokens):
    if provider in {"groq", "mistral"}:
        temperature = 0.15 if provider == "groq" else 0.18
        payload = {
            "model": provider_model_name(provider),
            "messages": [
                {
                    "role": "system",
                    "content": "Output only valid JSON. No Markdown. No extra commentary.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        return payload

    return {
        "contents": [
            {"parts": [{"text": prompt}]}
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.15,
            "maxOutputTokens": max_output_tokens,
        },
    }


def parse_provider_text(provider, data):
    if provider == "gemini":
        candidates = data.get("candidates", [])
        if not candidates:
            raise ValueError("Gemini candidate ندارد")
        parts = candidates[0].get("content", {}).get("parts", [])
        raw = "\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict)).strip()
        if not raw:
            raise ValueError("Gemini پاسخ متنی خالی برگرداند")
        return raw

    choices = data.get("choices", [])
    if not choices:
        raise ValueError(f"{AI_PROVIDER_LABELS[provider]} choices ندارد")
    message = choices[0].get("message", {}) or {}
    content = message.get("content", "")
    if isinstance(content, list):
        content = "\n".join(
            str(x.get("text", "")) for x in content if isinstance(x, dict)
        )
    raw = str(content or "").strip()
    if not raw:
        raise ValueError(f"{AI_PROVIDER_LABELS[provider]} پاسخ متنی خالی برگرداند")
    return raw


def parse_json_response(raw, provider="AI"):
    raw = (raw or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"خروجی {provider} JSON معتبر نیست: {raw[:500]}")


def provider_headers(provider, key):
    if provider == "gemini":
        return {
            "Content-Type": "application/json",
            "X-goog-api-key": key,
        }
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }


def provider_url(provider):
    return {
        "groq": GROQ_API_URL,
        "gemini": GEMINI_API_URL,
        "mistral": MISTRAL_API_URL,
    }[provider]


def wait_for_provider_slot(state, provider):
    pstate = provider_state(state, provider)
    last_request = float(pstate.get("last_request_ts", 0) or 0)
    delay = AI_MIN_REQUEST_INTERVAL_SECONDS - (now_ts() - last_request)
    if delay > 0:
        time.sleep(delay)


def call_single_provider(state, provider, prompt, task_name):
    keys = choose_available_provider_keys(state, provider)
    if not keys:
        raise AIProviderError(
            provider,
            429,
            "همه کلیدهای این provider فعلاً cooldown هستند یا کلیدی تعریف نشده است.",
        )

    last_error = None
    attempts = min(AI_MAX_ATTEMPTS_PER_PROVIDER, len(keys))

    for attempt in range(attempts):
        _, key = keys[attempt]
        wait_for_provider_slot(state, provider)
        provider_state(state, provider)["last_request_ts"] = now_ts()
        save_state(state)

        payload = provider_prompt_payload(provider, prompt, AI_MAX_OUTPUT_TOKENS)
        timeout = (15, 90) if provider != "mistral" else (15, 90)

        try:
            response = requests.post(
                provider_url(provider),
                headers=provider_headers(provider, key),
                json=payload,
                timeout=timeout,
            )
            status = response.status_code

            if status == 200:
                data = response.json()
                raw = parse_provider_text(provider, data)
                parsed = parse_json_response(raw, AI_PROVIDER_LABELS[provider])
                mark_provider_success(state, provider, key)
                save_state(state)
                log.info(
                    "%s موفق | task=%s | model=%s | key=%s",
                    AI_PROVIDER_LABELS[provider],
                    task_name,
                    provider_model_name(provider),
                    key_id(key),
                )
                return parsed

            retry_after = retry_after_seconds(
                response,
                AI_PROVIDER_COOLDOWN_FLOOR_SECONDS,
            )

            if status == 429:
                retry_after = min(
                    AI_429_MAX_COOLDOWN_SECONDS,
                    max(AI_PROVIDER_COOLDOWN_FLOOR_SECONDS, retry_after),
                )
                mark_provider_cooldown(state, provider, retry_after, status)
                mark_key_cooldown(state, provider, key, retry_after, status)
                save_state(state)
                raise AIProviderError(
                    provider,
                    status,
                    f"429؛ cooldown {retry_after}s",
                    retry_after=retry_after,
                )

            if status in (401, 403):
                mark_key_cooldown(state, provider, key, 6 * 3600, status)
                mark_provider_cooldown(state, provider, 30, status)
                save_state(state)
                last_error = AIProviderError(provider, status, "کلید مجوز معتبر ندارد")
                continue

            if status in (400, 404):
                # این خطا غالباً از مدل/درخواست است؛ provider را چند دقیقه کنار می‌گذاریم
                # و فوراً به fallback بعدی می‌رویم تا ربات قفل نشود.
                mark_provider_cooldown(state, provider, 180, status)
                save_state(state)
                raise AIProviderError(
                    provider,
                    status,
                    truncate_text(response.text, 600),
                    retry_after=180,
                )

            if status in (408, 409, 425, 500, 502, 503, 504):
                cooldown = min(90, 10 * (attempt + 1))
                mark_key_cooldown(state, provider, key, cooldown, status)
                last_error = AIProviderError(provider, status, truncate_text(response.text, 500))
                save_state(state)
                if attempt + 1 < attempts:
                    time.sleep(min(2.0, cooldown / 5.0))
                    continue
                raise last_error

            raise AIProviderError(
                provider,
                status,
                truncate_text(response.text, 700),
            )

        except AIProviderError:
            raise
        except (requests.Timeout, requests.ConnectionError, requests.RequestException) as exc:
            last_error = exc
            mark_key_cooldown(state, provider, key, 20, -1)
            save_state(state)
            if attempt + 1 < attempts:
                continue
        except Exception as exc:
            last_error = exc
            log.warning(
                "%s task=%s پاسخ نامعتبر/غیرمنتظره بود: %s",
                AI_PROVIDER_LABELS[provider], task_name, exc
            )
            mark_key_cooldown(state, provider, key, 30, -2)
            save_state(state)
            if attempt + 1 < attempts:
                continue

    raise AIProviderError(
        provider,
        -1,
        f"بعد از {attempts} تلاش داخلی ناموفق شد: {last_error}",
    )


def rotate_provider_order(start_provider=None):
    if start_provider not in AI_PROVIDER_ORDER:
        return list(AI_PROVIDER_ORDER)
    idx = AI_PROVIDER_ORDER.index(start_provider)
    return [
        AI_PROVIDER_ORDER[(idx + offset) % len(AI_PROVIDER_ORDER)]
        for offset in range(1, len(AI_PROVIDER_ORDER) + 1)
    ]


def ai_call_json(state, prompt, task_name, preferred_order=None):
    initialize_ai_state(state)
    order = list(preferred_order or AI_PROVIDER_ORDER)
    tried = []

    for provider in order:
        if not AI_PROVIDER_KEYS[provider]:
            continue
        pstate = provider_state(state, provider)
        if float(pstate.get("cooldown_until", 0) or 0) > now_ts():
            continue
        tried.append(provider)
        try:
            parsed = call_single_provider(state, provider, prompt, task_name)
            return parsed, provider
        except AIProviderError as exc:
            log.warning(
                "%s برای task=%s در دسترس نبود؛ fallback بعدی فعال می‌شود | status=%s | %s",
                AI_PROVIDER_LABELS[provider], task_name, exc.status_code, exc,
            )
            continue
        except Exception as exc:
            log.warning(
                "%s برای task=%s خطای پیش‌بینی‌نشده داد؛ fallback بعدی | %s",
                AI_PROVIDER_LABELS[provider], task_name, exc,
            )
            continue

    raise AIAllProvidersUnavailable(
        "هیچ موتور AI در این چرخه در دسترس نبود. تلاش‌شده: "
        + ", ".join(tried or ["none"])
    )


def build_style_memory_context(state):
    style = state.get("_style_memory", {})
    titles = [x for x in style.get("recent_titles", []) if x][-8:]
    openings = [x for x in style.get("recent_openings", []) if x][-6:]
    counts = style.get("entity_counts", {}) if isinstance(style.get("entity_counts"), dict) else {}
    top_entities = [k for k, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]]
    return (
        "تیترهای اخیر برای جلوگیری از ساختار تکراری:\n- "
        + "\n- ".join(titles or ["موردی ثبت نشده"])
        + "\n\nشروع جمله‌های اخیر:\n- "
        + "\n- ".join(openings or ["موردی ثبت نشده"])
        + "\n\nنام‌های پرتکرار برای یکدست‌نویسی:\n- "
        + "، ".join(top_entities or ["موردی ثبت نشده"])
    )


def build_event_memory_context(state):
    now = now_ts()
    memories = []
    for event_key, item in state.get("_event_memory", {}).items():
        ts = float(item.get("last_published_ts", 0) or 0)
        if now - ts <= EVENT_MEMORY_WINDOW_MINUTES * 60:
            memories.append((ts, event_key, item))
    memories.sort(reverse=True)
    chunks = []
    for _, event_key, item in memories[:8]:
        chunks.append(
            f"event_key={event_key}\n"
            f"title={item.get('title','')}\n"
            f"summary={truncate_text(item.get('body',''), 300)}\n"
            f"bucket={item.get('content_bucket','')}\n"
            f"last_published_ts={item.get('last_published_ts',0)}\n"
            f"updates_24h={item.get('updates_24h',0)}\n"
            f"entities={', '.join(item.get('entities', [])[:6])}"
        )
    return "\n---\n".join(chunks) if chunks else "مورد قبلی مهمی در حافظه کوتاه‌مدت وجود ندارد."


def build_analysis_content(batch_posts):
    chunks = []
    for index, post in enumerate(batch_posts, start=1):
        text = truncate_text(post.get("text", ""), AI_INPUT_MAX_CHARS_PER_POST)
        chunks.append(
            f"[پست {index}]\n"
            f"منبع: {post.get('source_name','')}\n"
            f"زمان: {post.get('published_at','')}\n"
            f"متن:\n{text}"
        )
    return "\n\n---\n\n".join(chunks)


def validate_ai_items(items, max_items=EVENT_MAX_OUTPUT_ITEMS):
    if not isinstance(items, list):
        raise ValueError("AI items باید list باشد")

    validated = []
    allowed_event_types = {"military_event", "security", "defense", "geopolitics", "routine"}
    allowed_categories = {"military", "security", "defense", "geopolitics", "technology", "general"}
    allowed_verification = {"reported", "developing", "confirmed", "analysis"}
    allowed_styles = {"breaking", "conflict", "geopolitics", "defense_tech", "iran", "important", "analysis", "standard"}
    allowed_lengths = {"brief", "standard", "full"}

    for raw in items[:max_items]:
        if not isinstance(raw, dict):
            continue
        relevant = safe_bool(raw.get("relevant"))
        if not relevant:
            validated.append({
                "relevant": False,
                "duplicate": safe_bool(raw.get("duplicate")),
                "duplicate_reason": normalize_space(str(raw.get("duplicate_reason", "")))[:400],
                "is_update": False,
                "novelty_score": 0,
                "event_key": "",
                "title": "",
                "body": "",
                "significance": "",
                "image_query": "",
                "post_indices": [],
                "content_bucket": "",
                "region": "world",
                "event_type": "routine",
                "category": "general",
                "verification": "reported",
                "urgent": False,
                "important": False,
                "style_mode": "standard",
                "length_class": "brief",
                "priority_hint": 0,
                "source_note": "",
                "entities": [],
            })
            continue

        title = normalize_persian_text(str(raw.get("title", "")))
        body = normalize_persian_text(str(raw.get("body", "")))
        if not title or not body:
            continue

        event_type = str(raw.get("event_type", "routine")).strip()
        if event_type not in allowed_event_types:
            event_type = "routine"

        category = str(raw.get("category", "general")).strip().lower()
        if category not in allowed_categories:
            category = "general"

        verification = str(raw.get("verification", "reported")).strip().lower()
        if verification not in allowed_verification:
            verification = "reported"

        style_mode = str(raw.get("style_mode", "standard")).strip().lower()
        if style_mode not in allowed_styles:
            style_mode = "standard"

        length_class = str(raw.get("length_class", "standard")).strip().lower()
        if length_class not in allowed_lengths:
            length_class = "standard"

        region = str(raw.get("region", "world")).strip().lower()
        if region not in {"iran", "middle_east", "world", "superpower"}:
            region = infer_region(f"{title}\n{body}")

        bucket = str(raw.get("content_bucket", "")).strip().lower()
        if bucket not in ALLOWED_CONTENT_BUCKETS:
            bucket = infer_content_bucket(f"{title}\n{body}", region)

        raw_indices = raw.get("post_indices", [])
        if not isinstance(raw_indices, list):
            raw_indices = []
        post_indices = []
        for value in raw_indices:
            idx = safe_int(value, 0)
            if idx > 0 and idx not in post_indices:
                post_indices.append(idx)
        post_indices = post_indices[:AI_BATCH_MAX_POSTS]

        entities = raw.get("entities", [])
        if not isinstance(entities, list):
            entities = []
        entities = [normalize_persian_text(str(x))[:100] for x in entities if str(x).strip()][:10]

        event_key = normalize_event_key(raw.get("event_key", ""))
        if not event_key:
            event_key = event_key_or_signature(title, body, "")

        novelty = max(0, min(100, safe_int(raw.get("novelty_score"), 0)))

        validated.append({
            "draft_id": safe_int(raw.get("draft_id"), 0),
            "relevant": True,
            "duplicate": safe_bool(raw.get("duplicate")),
            "duplicate_reason": normalize_space(str(raw.get("duplicate_reason", "")))[:400],
            "is_update": safe_bool(raw.get("is_update")),
            "novelty_score": novelty,
            "event_key": event_key,
            "title": title[:240],
            "body": body[:6500],
            "significance": normalize_persian_text(str(raw.get("significance", "")))[:600],
            "image_query": normalize_space(str(raw.get("image_query", "")))[:180],
            "post_indices": post_indices,
            "content_bucket": bucket,
            "region": region,
            "event_type": event_type,
            "category": category,
            "verification": verification,
            "urgent": safe_bool(raw.get("urgent")),
            "important": safe_bool(raw.get("important")),
            "needs_editor": safe_bool(raw.get("needs_editor")),
            "style_mode": style_mode,
            "length_class": length_class,
            "priority_hint": min(100, max(0, safe_int(raw.get("priority_hint"), 0))),
            "source_note": normalize_space(str(raw.get("source_note", "")))[:500],
            "entities": entities,
        })
    return validated


def analyze_and_rewrite_safe(state, batch_posts, source_name):
    if not batch_posts:
        return [], None

    content = build_analysis_content(batch_posts)
    prompt = REWRITE_PROMPT.replace("{content}", content)
    prompt = prompt.replace("{event_memory}", build_event_memory_context(state))
    prompt = prompt.replace("{style_memory}", build_style_memory_context(state))

    parsed, provider = ai_call_json(
        state,
        prompt,
        "analysis",
        preferred_order=list(AI_PROVIDER_ORDER),
    )
    items = parsed.get("items", []) if isinstance(parsed, dict) else []
    if not items and isinstance(parsed, dict) and "title" in parsed:
        items = [parsed]
    return validate_ai_items(items), provider


def build_editor_prompt(state, drafts, source_posts):
    source_chunks = []
    for idx, post in enumerate(source_posts, start=1):
        source_chunks.append(
            f"[ورودی {idx}]\n"
            f"زمان: {post.get('published_at','')}\n"
            f"متن: {truncate_text(post.get('text',''), 1400)}"
        )
    draft_chunks = []
    for draft_id, draft in enumerate(drafts, start=1):
        draft_chunks.append(
            f"[پیش‌نویس {draft_id}]\n"
            + json.dumps({
                "relevant": True,
                "event_key": draft.get("event_key", ""),
                "title": draft.get("title", ""),
                "body": draft.get("body", ""),
                "significance": draft.get("significance", ""),
                "post_indices": draft.get("post_indices", []),
                "content_bucket": draft.get("content_bucket", ""),
                "region": draft.get("region", ""),
                "event_type": draft.get("event_type", ""),
                "category": draft.get("category", ""),
                "verification": draft.get("verification", ""),
                "urgent": draft.get("urgent", False),
                "important": draft.get("important", False),
                "style_mode": draft.get("style_mode", "standard"),
                "length_class": draft.get("length_class", "standard"),
                "priority_hint": draft.get("priority_hint", 0),
                "source_note": draft.get("source_note", ""),
                "entities": draft.get("entities", []),
            }, ensure_ascii=False)
        )

    return (
        EDITOR_PROMPT
        + "\n\nورودی‌های خام برای کنترل واقعیت:\n"
        + "\n---\n".join(source_chunks)
        + "\n\n---\nپیش‌نویس‌ها:\n"
        + "\n---\n".join(draft_chunks)
        + "\n\nحافظه سبک کانال:\n"
        + build_style_memory_context(state)
    )


def final_editor(state, drafts, source_posts, first_provider):
    if not EDITOR_ENABLED or not drafts:
        return drafts, first_provider

    drafts = drafts[:AI_EDITOR_MAX_ITEMS]
    prompt = build_editor_prompt(state, drafts, source_posts)
    order = rotate_provider_order(first_provider)
    try:
        parsed, provider = ai_call_json(
            state,
            prompt,
            "editor",
            preferred_order=order,
        )
        raw_items = parsed.get("items", []) if isinstance(parsed, dict) else []
        edited = validate_ai_items(raw_items, max_items=len(drafts))
        if not edited:
            raise ValueError("editor خروجی قابل استفاده نداد")

        # editor نباید اطلاعات ساختاری stage1 را از بین ببرد. بر اساس draft_id یا ترتیب merge می‌کنیم.
        merged = []
        for idx, original in enumerate(drafts, start=1):
            candidate = None
            for item in edited:
                if safe_int(item.get("draft_id"), 0) == idx:
                    candidate = item
                    break
            if candidate is None and idx <= len(edited):
                candidate = edited[idx - 1]
            if candidate is None or not candidate.get("relevant", False):
                merged.append(original)
                continue

            merged_item = dict(original)
            for field in (
                "title", "body", "significance", "event_key", "verification",
                "style_mode", "length_class", "entities", "source_note", "needs_editor",
                "priority_hint", "urgent", "important",
            ):
                if field in candidate and candidate.get(field) not in (None, ""):
                    merged_item[field] = candidate[field]
            merged_item["event_key"] = normalize_event_key(merged_item.get("event_key")) or original.get("event_key")
            merged_item["post_indices"] = original.get("post_indices", [])
            merged_item["content_bucket"] = original.get("content_bucket")
            merged_item["region"] = original.get("region")
            merged_item["event_type"] = original.get("event_type")
            merged_item["category"] = original.get("category")
            merged_item["is_update"] = original.get("is_update", False)
            merged_item["novelty_score"] = original.get("novelty_score", 0)
            merged.append(merged_item)

        return merged, provider
    except Exception as exc:
        log.warning(
            "Final editor در دسترس نبود؛ از پیش‌نویس مرحله اول استفاده می‌کنیم: %s",
            exc,
        )
        return drafts, first_provider

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


def strip_source_channel_attribution(text):
    """
    نام/یوزرنیم کانال‌های منبع نباید وارد پست عمومی شوند.
    این پاک‌سازی فقط attribution به کانال‌های منبع را هدف می‌گیرد و
    تلاش می‌کند نام سازمان‌ها، کشورها و منابع رسمی داخل متن خبر را دست‌نخورده نگه دارد.
    """
    text = normalize_persian_text(text)
    if not text:
        return ""

    patterns = [
        # «به نقل/گزارش کانال @name» و حالت‌های مشابه
        r"(?i)\b(?:به\s+)?(?:نقل|گزارش)\s+از\s+(?:کانال|چنل)\s+[@#]?[A-Za-z0-9_\-]+",
        r"(?i)\bبه\s+نقل\s+از\s+(?:کانال|چنل)\s+[^،.!؟\n]+",
        r"(?i)\b(?:به\s+گفته|بر\s+اساس\s+گزارش)\s+(?:کانال|چنل)\s+[@#]?[A-Za-z0-9_\-]+",
        r"(?i)\b(?:کانال|چنل)\s+[@#][A-Za-z0-9_\-]+\s+(?:گزارش\s+داد|اعلام\s+کرد|نوشت|مدعی\s+شد)",
        r"(?i)\b(?:کانال|چنل)\s+[@#][A-Za-z0-9_\-]+",
    ]
    for pattern in patterns:
        text = re.sub(pattern, "", text)

    # نام‌های منبع فعلی ربات را نیز اگر به شکل plain text داخل attribution آمده باشند، حذف کن.
    for channel in SOURCE_CHANNELS:
        if not channel:
            continue
        text = re.sub(
            rf"(?i)(?<![A-Za-z0-9_])(?:کانال|چنل)\s+@?{re.escape(channel)}\b",
            "",
            text,
        )
        text = re.sub(
            rf"(?i)(?<![A-Za-z0-9_])@{re.escape(channel)}\b",
            "",
            text,
        )

    # فاصله/نشانه‌گذاری باقی‌مانده از حذف attribution را مرتب کن.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([،؛؟!])", r"\1", text)
    text = re.sub(r"([،؛])\s*(?=[،؛])", r"\1 ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(" \n،؛")


def format_public_title(item):
    return strip_source_channel_attribution(item.get("title", ""))


def format_public_body(item):
    """خروجی نهایی طبیعی است؛ وضعیت تأیید داخل نثر و significance فقط در صورت نیاز می‌آید."""
    body = strip_source_channel_attribution(item.get("body", ""))
    verification = str(item.get("verification", "reported") or "reported").lower()
    normalized = normalize_for_match(body)

    if verification == "reported" and body:
        cues = (
            "بر اساس گزارش",
            "به گفته",
            "مدعی",
            "گزارش اولیه",
            "گزارش های اولیه",
            "تایید نشده",
            "تأیید نشده",
            "تایید رسمی",
            "تأیید رسمی",
            "منابع می گویند",
        )
        if not any(normalize_for_match(x) in normalized for x in cues):
            first = body[0].lower() + body[1:] if len(body) > 1 else body
            body = "بر اساس گزارش‌های اولیه، " + first

    elif verification == "developing" and body:
        cues = ("در حال تکمیل", "جزئیات", "هنوز")
        if not any(normalize_for_match(x) in normalized for x in cues):
            body += "\n\nجزئیات این خبر همچنان در حال تکمیل است."

    elif verification == "analysis" and body:
        cues = ("ارزیابی", "تحلیل", "به نظر", "احتمال")
        if not any(normalize_for_match(x) in normalized for x in cues):
            first = body[0].lower() + body[1:] if len(body) > 1 else body
            body = "در ارزیابی اولیه، " + first

    significance = strip_source_channel_attribution(item.get("significance", ""))
    if significance and len(significance) >= 35:
        sim = token_similarity(body, significance)
        if sim < 0.72:
            body += "\n\n📌 " + significance

    return normalize_persian_text(body)


def build_text_only_message(item):
    title = format_public_title(item)
    body = format_public_body(item)
    parts = []
    if title:
        parts.append(title)
    if body:
        parts.append(body)
    parts.append(FOOTER)
    return "\n\n".join(parts)


def is_recoverable_media_error(exc):
    """خطاهای قطعی رسانه که یعنی Telegram خود فایل را نپذیرفته است."""
    if not isinstance(exc, TelegramAPIError) or exc.status_code != 400:
        return False
    desc = normalize_for_match(exc.description or "")
    media_terms = {
        "photo_invalid_dimensions",
        "wrong file identifier",
        "bad request photo",
        "photo",
        "video",
        "file is too big",
        "wrong type of the web page content",
    }
    return any(normalize_for_match(term) in desc for term in media_terms)


def dispatch_item(item):
    if TEST_MODE:
        log.info(
            "TEST_MODE فعال است؛ انتشار واقعی انجام نشد | title=%s | media=%s",
            format_public_title(item),
            "video" if item.get("video") else ("photo" if item.get("photo") else "none"),
        )
        return True

    title = format_public_title(item)
    body = format_public_body(item)
    photo = item.get("photo")
    video = item.get("video")

    # اگر رسانه خراب/نامعتبر باشد، همان خبر بدون رسانه منتشر می‌شود.
    # 429 عمداً fallback نمی‌شود؛ چون مشکل rate limit است نه فایل.
    if video:
        try:
            send_video_to_telegram(title, body, video)
            return True
        except TelegramAPIError as exc:
            if exc.status_code == 429:
                raise
            if is_recoverable_media_error(exc):
                log.warning("ویدیو توسط Telegram رد شد؛ به عکس/متن fallback می‌کنیم: %s", exc)
            else:
                raise
        except Exception as exc:
            log.warning("دریافت/ارسال ویدیو شکست خورد؛ به عکس/متن fallback می‌کنیم: %s", exc)

    if photo:
        try:
            send_photo_to_telegram(title, body, photo)
            return True
        except TelegramAPIError as exc:
            if exc.status_code == 429:
                raise
            if is_recoverable_media_error(exc):
                log.warning("عکس توسط Telegram رد شد؛ به متن fallback می‌کنیم: %s", exc)
            else:
                raise
        except Exception as exc:
            log.warning("دریافت/ارسال عکس شکست خورد؛ به متن fallback می‌کنیم: %s", exc)

    # در صورت شکست رسانه، متن کامل خبر منتشر می‌شود؛ ادامه‌ی جداگانه‌ای برای رسانه نداریم.
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
    ][-500:]

    state["_recent_events"] = [
        x for x in state.get("_recent_events", [])
        if x.get("ts", 0) >= cutoff
    ][-500:]

    event_cutoff = now_ts() - EVENT_MEMORY_WINDOW_MINUTES * 60
    memory = state.get("_event_memory", {})
    if isinstance(memory, dict):
        state["_event_memory"] = {
            k: v for k, v in memory.items()
            if float(v.get("last_published_ts", 0) or 0) >= event_cutoff
        }


def purge_recent_inputs(state):
    cutoff = now_ts() - AI_RECENT_INPUT_WINDOW_MINUTES * 60
    state["_recent_inputs"] = [
        x for x in state.get("_recent_inputs", [])
        if x.get("ts", 0) >= cutoff
    ][-800:]


def is_input_duplicate(state, post):
    """فقط near-exact raw duplicates را قبل از AI حذف می‌کند؛ updateهای واقعی رد نمی‌شوند."""
    purge_recent_inputs(state)
    text = normalize_space(post.get("text", ""))
    if len(text) < 40:
        return False

    current_hash = sha1_text(normalize_for_match(text))
    current_tokens = tokens(text)
    for recent in state.get("_recent_inputs", []):
        if recent.get("hash") == current_hash:
            return True
        old_text = recent.get("text", "")
        if not old_text:
            continue
        sim = combined_similarity(text, "", old_text, "")
        if sim >= AI_LOCAL_DUP_THRESHOLD:
            return True
        old_tokens = set(recent.get("tokens", []))
        if current_tokens and old_tokens:
            overlap = len(current_tokens & old_tokens) / max(1, len(current_tokens | old_tokens))
            if overlap >= 0.75 and min(len(current_tokens), len(old_tokens)) >= 6:
                return True
    return False


def remember_input_posts(state, posts):
    purge_recent_inputs(state)
    for post in posts:
        text = normalize_space(post.get("text", ""))
        if len(text) < 20:
            continue
        state["_recent_inputs"].append({
            "text": text[:3500],
            "hash": sha1_text(normalize_for_match(text)),
            "tokens": list(tokens(text))[:120],
            "ts": now_ts(),
        })
    state["_recent_inputs"] = state["_recent_inputs"][-800:]


def entity_overlap_count(a, b):
    def normalize_entities(value):
        if not isinstance(value, list):
            return set()
        return {
            normalize_for_match(str(x)).strip()
            for x in value
            if str(x).strip()
        }

    ea = normalize_entities(a)
    eb = normalize_entities(b)
    return len(ea & eb)


def update_limit_reached(existing):
    last_ts = float(existing.get("last_published_ts", 0) or 0)
    updates = int(existing.get("updates_24h", 0) or 0)
    if not last_ts:
        return False
    if now_ts() - last_ts > 24 * 3600:
        return False
    return updates >= EVENT_MAX_UPDATES_PER_24H


def compare_event_relation(state, item, existing_items):
    """returns: 'duplicate', 'update', or 'new'.

    This is deliberately stronger than the v3.8 text-only gate: exact event keys,
    semantic signatures, and shared entity anchors can all connect differently
    worded reports of the same event.
    """
    title = item.get("title", "")
    body = item.get("body", "")
    event_key = normalize_event_key(item.get("event_key", ""))
    signature = event_signature(f"{title} {body}")
    is_update = safe_bool(item.get("is_update"))
    novelty = safe_int(item.get("novelty_score"), 0)
    bucket = item.get("content_bucket", "")
    event_type = item.get("event_type", "")
    entities = item.get("entities", [])
    incoming_uids = {str(x) for x in item.get("source_uids", []) if str(x).strip()}

    for existing in existing_items:
        old_uids = {str(x) for x in existing.get("source_uids", []) if str(x).strip()}
        if incoming_uids and old_uids and incoming_uids & old_uids:
            if update_limit_reached(existing):
                return "duplicate"
            if is_update and novelty >= EVENT_UPDATE_MIN_NOVELTY:
                return "update"
            return "duplicate"
        old_title = existing.get("title", "")
        old_body = existing.get("body", "")
        old_key = normalize_event_key(existing.get("event_key", ""))
        old_signature = existing.get("event_signature", "") or event_signature(f"{old_title} {old_body}")
        old_bucket = existing.get("content_bucket", "")
        old_event_type = existing.get("event_type", "")

        exact_key = bool(event_key and old_key and event_key == old_key)
        full_sim = combined_similarity(title, body, old_title, old_body)
        title_sim = title_similarity(title, old_title)
        sig_sim = token_similarity(signature, old_signature)
        shared_entities = entity_overlap_count(entities, existing.get("entities", []))
        same_bucket = bool(bucket and old_bucket and bucket == old_bucket)
        same_event_type = bool(event_type and old_event_type and event_type == old_event_type)

        related = False
        if exact_key:
            related = True
        elif full_sim >= DEDUP_SIMILARITY_THRESHOLD:
            related = True
        elif sig_sim >= EVENT_SIMILARITY_THRESHOLD:
            related = True
        elif shared_entities >= 2 and same_bucket and (same_event_type or title_sim >= 0.52):
            related = True
        elif shared_entities >= 3 and same_bucket:
            related = True

        if not related:
            continue

        # Once the allowed daily update budget is used, new reports of that same
        # event are treated as duplicate rather than endlessly refreshing the queue.
        if update_limit_reached(existing):
            return "duplicate"

        if is_update and novelty >= EVENT_UPDATE_MIN_NOVELTY:
            return "update"
        return "duplicate"

    return "new"


def recent_memory_items(state):
    purge_recent_memories(state)
    out = []
    for key, value in state.get("_event_memory", {}).items():
        item = dict(value)
        item["event_key"] = key
        out.append(item)
    return out


def is_duplicate(state, title, body, event_key="", is_update=False, novelty_score=0):
    item = {
        "title": title,
        "body": body,
        "event_key": event_key,
        "is_update": is_update,
        "novelty_score": novelty_score,
    }
    relation = compare_event_relation(state, item, recent_memory_items(state))
    return relation == "duplicate"


def update_style_memory(state, item):
    style = state.setdefault("_style_memory", {
        "recent_titles": [],
        "recent_openings": [],
        "entity_counts": {},
    })
    title = normalize_persian_text(item.get("title", ""))
    body = normalize_persian_text(item.get("body", ""))
    if title:
        style.setdefault("recent_titles", []).append(title)
        style["recent_titles"] = style["recent_titles"][-30:]
    if body:
        first_para = body.split("\n\n", 1)[0]
        words = first_para.split()
        opening = " ".join(words[:10])
        if opening:
            style.setdefault("recent_openings", []).append(opening)
            style["recent_openings"] = style["recent_openings"][-20:]

    counts = style.setdefault("entity_counts", {})
    for entity in item.get("entities", []) or []:
        entity = normalize_persian_text(str(entity)).strip()
        if not entity:
            continue
        counts[entity] = int(counts.get(entity, 0)) + 1
    if len(counts) > 300:
        counts = dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:200])
        style["entity_counts"] = counts


def remember_item(state, item):
    purge_recent_memories(state)
    title = normalize_persian_text(item.get("title", ""))
    body = normalize_persian_text(item.get("body", ""))
    event_key = normalize_event_key(item.get("event_key", "")) or event_key_or_signature(title, body)
    signature = event_signature(f"{title} {body}")
    now = now_ts()

    recent_memory = state.setdefault("_event_memory", {})
    old = recent_memory.get(event_key, {})
    updates_24h = int(old.get("updates_24h", 0))
    if old and now - float(old.get("last_published_ts", 0) or 0) <= 24 * 3600:
        if safe_bool(item.get("is_update")):
            updates_24h += 1
    else:
        updates_24h = 0

    source_uids = item.get("source_uids", [])
    if not isinstance(source_uids, list):
        source_uids = []

    recent_memory[event_key] = {
        "title": title,
        "body": body,
        "event_signature": signature,
        "region": item.get("region", "world"),
        "content_bucket": item.get("content_bucket", ""),
        "event_type": item.get("event_type", ""),
        "last_published_ts": now,
        "updates_24h": min(EVENT_MAX_UPDATES_PER_24H + 1, updates_24h),
        "entities": item.get("entities", [])[:10],
        "source_uids": source_uids[:20],
    }

    state["_recent_titles"].append({
        "title": title,
        "body": body,
        "event_key": event_key,
        "ts": now,
    })
    state["_recent_events"].append({
        "tokens": list(tokens(f"{title} {body}"))[:100],
        "event_key": event_key,
        "region": item.get("region", "world"),
        "ts": now,
    })
    state["_recent_titles"] = state["_recent_titles"][-500:]
    state["_recent_events"] = state["_recent_events"][-500:]
    update_style_memory(state, item)


def clear_retry_uids(state, posts):
    retry = state.setdefault("_retry_uids", {})
    for post in posts:
        uid = post.get("uid")
        if uid:
            retry.pop(uid, None)


def mark_retry_uids(state, posts, minutes=None):
    retry = state.setdefault("_retry_uids", {})
    until = now_ts() + 60 * (minutes or AI_FAILURE_RETRY_MINUTES)
    for post in posts:
        uid = post.get("uid")
        if uid:
            retry[uid] = until


def is_retry_blocked(state, uid):
    if not uid:
        return False
    retry = state.get("_retry_uids", {})
    until = float(retry.get(uid, 0) or 0)
    if until <= now_ts():
        retry.pop(uid, None)
        return False
    return True


# ============================================================
# Queue
# ============================================================

def item_fingerprint(item):
    base = normalize_for_match(
        f"{item.get('title','')} {item.get('body','')[:600]}"
    )
    return sha1_text(base)[:20]


def queue_relation(state, item):
    queue = state.get("_pending_queue", [])
    if not queue:
        return "new", None
    relation = compare_event_relation(state, item, queue)
    if relation == "update":
        # همان event موجود در صف را برای update تازه پیدا کن.
        for idx, existing in enumerate(queue):
            key_match = (
                normalize_event_key(item.get("event_key", ""))
                and normalize_event_key(item.get("event_key", ""))
                == normalize_event_key(existing.get("event_key", ""))
            )
            sig_match = token_similarity(
                event_signature(f"{item.get('title','')} {item.get('body','')}"),
                event_signature(f"{existing.get('title','')} {existing.get('body','')}"),
            ) >= EVENT_SIMILARITY_THRESHOLD
            if key_match or sig_match:
                return "update", idx
        return "update", None
    if relation == "duplicate":
        return "duplicate", None
    return "new", None


def enqueue_item(state, item):
    queue = state.setdefault("_pending_queue", [])
    item = dict(item)
    item["event_key"] = normalize_event_key(item.get("event_key", "")) or event_key_or_signature(
        item.get("title", ""), item.get("body", "")
    )
    item.setdefault("queued_at", now_ts())
    item.setdefault("important", False)
    item.setdefault("urgent", False)
    item.setdefault("region", "world")
    item.setdefault("entities", [])
    item["fingerprint"] = item_fingerprint(item)

    # exact fingerprint duplicate
    if any(x.get("fingerprint") == item["fingerprint"] for x in queue):
        return False

    relation, index = queue_relation(state, item)
    if relation == "duplicate":
        return False

    if relation == "update" and index is not None:
        # update جدید جایگزین نسخه قبلی در صف می‌شود؛ ترتیب زمانی صف حفظ می‌شود.
        old = queue[index]
        item["queued_at"] = old.get("queued_at", now_ts())
        queue[index] = item
    else:
        queue.append(item)

    apply_queue_diversity_score(state, item)
    queue.sort(
        key=lambda x: (
            bool(x.get("urgent")),
            x.get("priority_score", 0),
            x.get("queued_at", 0),
        ),
        reverse=True,
    )

    state["_pending_queue"] = queue[:500]
    save_state(state)
    return True


def compute_publish_spacing_minutes(now_dt):
    """زمان‌بندی ثابت: 08:00-13:59 هر 60 دقیقه؛ 14:00-23:59 هر 45 دقیقه."""
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

    # آخرین سد تکراری: هم حافظه منتشرشده و هم update logic بررسی می‌شود.
    relation = compare_event_relation(state, item, recent_memory_items(state))
    if relation == "duplicate":
        log.warning("یک آیتم تکراری در آخرین مرحله از صف حذف شد: %s", item.get("title"))
        queue.pop(index)
        state["_pending_queue"] = queue
        save_state(state)
        return

    try:
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
        log.error("خطا در انتشار آیتم صف؛ آیتم نگه داشته شد: %s", exc)
        return

    queue.pop(index)
    state["_pending_queue"] = queue
    state["_last_queue_release_ts"] = now_ts()
    mark_posted_region(state, item)
    remember_item(state, item)  # فقط پس از انتشار موفق، نه صرفاً enqueue.
    save_state(state)

    log.info(
        "پست منتشر شد | provider_pipeline=Groq>Gemini>Mistral | region=%s | score=%s | queue=%s | spacing=%s دقیقه",
        item.get("region"),
        item.get("priority_score"),
        len(queue),
        spacing,
    )


# ============================================================
# Source grouping / processing
# ============================================================
# Source grouping / processing
# ============================================================

def combine_original_media(cluster):
    photo = next((p.get("photo") for p in cluster if p.get("photo")), None)
    video = next((p.get("video") for p in cluster if p.get("video")), None)
    source_url = next((p.get("source_url") for p in cluster if p.get("source_url")), "")
    source_urls = [p.get("source_url") for p in cluster if p.get("source_url")]
    return photo, video, source_url, source_urls


def process_cluster(state, source_key, cluster):
    if not cluster:
        return

    source_names = sorted({
        p.get("source_name", source_key)
        for p in cluster
        if p.get("source_name")
    })
    source_name = "، ".join(source_names) if source_names else source_key

    # Stage 1: استخراج رویداد/خبر و نگارش اولیه با زنجیره سه‌موتوره.
    drafts, first_provider = analyze_and_rewrite_safe(state, cluster, source_name)
    if not drafts:
        raise ValueError("هیچ draft قابل انتشار از موتور اصلی/کمکی‌ها برنگشت.")

    drafts = [x for x in drafts if x.get("relevant")]
    if not drafts:
        return

    # Stage 2: ویراستار نهایی به‌صورت هوشمند و شرطی اجرا می‌شود تا ظرفیت رایگان
    # سه سرویس بی‌دلیل مصرف نشود. خبرهای چندمنبعی، update، مهم/فوری و تحلیل‌ها
    # به‌طور خودکار نیازمند بررسی دوم می‌شوند.
    needs_editor = any(
        safe_bool(x.get("needs_editor"))
        or safe_bool(x.get("important"))
        or safe_bool(x.get("urgent"))
        or safe_bool(x.get("is_update"))
        or x.get("style_mode") in {"analysis", "geopolitics"}
        or x.get("length_class") == "full"
        for x in drafts
    ) or len(cluster) > 1

    if EDITOR_ENABLED and needs_editor:
        edited_items, editor_provider = final_editor(
            state,
            drafts,
            cluster,
            first_provider,
        )
    else:
        edited_items, editor_provider = drafts, first_provider

    successful_interpretation = False
    shared_photo, shared_video, first_source_url, source_urls = combine_original_media(cluster)

    for result in edited_items[:EVENT_MAX_OUTPUT_ITEMS]:
        if not result.get("relevant"):
            continue
        if safe_bool(result.get("duplicate")):
            log.info(
                "AI خروجی را duplicate تشخیص داد و منتشر نشد: %s",
                result.get("title", "")
            )
            successful_interpretation = True
            continue

        linked_indices = [
            idx for idx in result.get("post_indices", [])
            if 1 <= safe_int(idx, 0) <= len(cluster)
        ]
        linked_posts = [cluster[idx - 1] for idx in linked_indices] if linked_indices else cluster
        result_photo, result_video, result_source_url, result_source_urls = combine_original_media(linked_posts)

        title = normalize_persian_text(result.get("title", ""))
        body = normalize_persian_text(result.get("body", ""))
        if not title or not body:
            continue

        item = {
            "title": title,
            "body": body,
            "significance": normalize_persian_text(result.get("significance", "")),
            "image_query": result.get("image_query", ""),
            "urgent": safe_bool(result.get("urgent")),
            "important": safe_bool(result.get("important")),
            "region": infer_region(f"{title}\n{body}", result.get("region")),
            "content_bucket": result.get("content_bucket") or infer_content_bucket(
                f"{title}\n{body}", result.get("region", "")
            ),
            "event_type": result.get("event_type", "routine"),
            "category": result.get("category", "general"),
            "verification": result.get("verification", "reported"),
            "style_mode": result.get("style_mode", "standard"),
            "length_class": result.get("length_class", "standard"),
            "priority_hint": safe_int(result.get("priority_hint"), 0),
            "source_note": result.get("source_note", ""),
            "event_key": normalize_event_key(result.get("event_key", "")) or event_key_or_signature(title, body),
            "event_signature": event_signature(f"{title} {body}"),
            "is_update": safe_bool(result.get("is_update")),
            "needs_editor": safe_bool(result.get("needs_editor")),
            "source_uids": [p.get("uid") for p in linked_posts if p.get("uid")],
            "novelty_score": safe_int(result.get("novelty_score"), 0),
            "entities": result.get("entities", [])[:10],
            "source_url": result_source_url or first_source_url,
            "source_urls": (result_source_urls or source_urls)[:10],
            "photo": result_photo or shared_photo,
            "video": result_video or shared_video,
            "queued_at": now_ts(),
        }

        if not is_allowed_content_bucket(item["content_bucket"]):
            log.info("خبر خارج از سبد محتوایی حذف شد: %s", title)
            successful_interpretation = True
            continue

        relation = compare_event_relation(
            state,
            item,
            recent_memory_items(state),
        )

        # update واقعی فقط در صورت novelty مناسب و سقف روزانه انجام می‌شود.
        if relation == "duplicate":
            log.info("خروجی تکراری/بدون اطلاعات تازه رد شد: %s", title)
            successful_interpretation = True
            continue

        if relation == "update" and item["novelty_score"] < EVENT_UPDATE_MIN_NOVELTY:
            log.info("update با novelty پایین رد شد: %s", title)
            successful_interpretation = True
            continue

        # اگر رسانه منبع نیست، fallback فعلی حفظ می‌شود.
        if not item["photo"] and not item["video"]:
            item["photo"] = choose_image_url(item, linked_posts)

        # قبل از enqueue یک بار صف را بررسی می‌کنیم؛ remember_item دیگر اینجا اجرا نمی‌شود.
        if enqueue_item(state, item):
            successful_interpretation = True
        else:
            log.info("آیتم به علت duplicate/update صفی کنار گذاشته شد: %s", title)
            successful_interpretation = True

    if not successful_interpretation:
        raise ValueError("هیچ خروجی معتبر از cluster تولید نشد.")


def process_source(state, source_key, posts):
    """سازگاری با ساختار قبلی؛ پردازش واقعی در process_once چندمنبعی انجام می‌شود."""
    if not posts:
        return
    processed = set(state.get(source_key, []))
    new_posts = [p for p in posts if p.get("uid") not in processed and not is_retry_blocked(state, p.get("uid"))]
    if not new_posts:
        return
    clusters = cluster_posts(new_posts)
    for cluster in clusters:
        uids = [p.get("uid") for p in cluster]
        try:
            process_cluster(state, source_key, cluster)
            current = state.setdefault(source_key, [])
            for uid in uids:
                if uid and uid not in current:
                    current.append(uid)
            state[source_key] = current[-500:]
            clear_retry_uids(state, cluster)
            save_state(state)
        except Exception as exc:
            mark_retry_uids(state, cluster)
            save_state(state)
            log.error("cluster شکست خورد؛ retry delayed source=%s error=%s", source_key, exc)


# ============================================================
# Scheduled messages / cleanup
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
    """نام قدیمی حفظ شده تا سازگاری کد بماند؛ داخلش batching سه‌موتوره انجام می‌شود."""
    batches = []
    current = []
    current_size = 0

    for cluster in clusters:
        if not cluster:
            continue
        cluster_size = len(cluster)
        if current and current_size + cluster_size > AI_BATCH_MAX_POSTS:
            batches.append(current)
            current = []
            current_size = 0

        if cluster_size > AI_BATCH_MAX_POSTS:
            for start_idx in range(0, cluster_size, AI_BATCH_MAX_POSTS):
                part = cluster[start_idx:start_idx + AI_BATCH_MAX_POSTS]
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
    چرخه v4:
    1) جمع‌آوری از همه منابع
    2) حذف near-exact duplicate خام
    3) خوشه‌بندی و batching چندمنبعی
    4) Stage 1 با Groq > Gemini > Mistral
    5) Stage 2 با provider بعدی برای توزیع بار، و fallback به draft در صورت شکست editor
    6) dedup سه‌لایه + event memory + update merge
    7) صف و زمان‌بندی ثابت
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

        for post in posts:
            uid = post.get("uid")
            if uid in processed:
                continue
            if is_retry_blocked(state, uid):
                continue
            post["_source_key"] = source_key
            all_new_posts.append(post)

    if all_new_posts:
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
            log.info("%s پست near-exact duplicate قبل از AI حذف شد.", locally_skipped)

        clusters = cluster_posts(filtered_new_posts)
        ai_batches = build_gemini_batches(clusters)
        log.info(
            "%s پست جدید -> %s cluster -> %s batch چندمنبعی | AI=Groq>Gemini>Mistral",
            len(filtered_new_posts), len(clusters), len(ai_batches)
        )

        for batch in ai_batches:
            try:
                process_cluster(state, "multi-source", batch)
                remember_input_posts(state, batch)

                for post in batch:
                    source_key = post.get("_source_key")
                    uid = post.get("uid")
                    if not source_key or not uid:
                        continue
                    current = state.setdefault(source_key, [])
                    if uid not in current:
                        current.append(uid)
                    state[source_key] = current[-500:]

                clear_retry_uids(state, batch)
                save_state(state)
                log.info(
                    "batch با %s پست پردازش شد؛ sources=%s",
                    len(batch),
                    len({p.get("_source_key") for p in batch}),
                )

            except AIAllProvidersUnavailable as exc:
                mark_retry_uids(state, batch, minutes=AI_FAILURE_RETRY_MINUTES)
                save_state(state)
                log.warning(
                    "همه موتورهای AI موقتاً در دسترس نبودند؛ batch برای retry نگه داشته شد: %s",
                    exc,
                )
            except Exception as exc:
                mark_retry_uids(state, batch)
                save_state(state)
                log.error(
                    "batch شکست خورد و UIDها تا retry بعدی مصرف‌شده تلقی نشدند: %s",
                    exc,
                )

    check_scheduled_messages(state)
    process_queue(state)
    purge_recent_memories(state)
    purge_recent_inputs(state)
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
    if not SOURCE_CHANNELS:
        missing.append("SOURCE_CHANNELS")
    if not any(AI_PROVIDER_KEYS[p] for p in AI_PROVIDER_ORDER):
        missing.append("حداقل یکی از GROQ_API_KEYS / GEMINI_API_KEYS / MISTRAL_API_KEYS")

    if missing:
        raise RuntimeError(
            "این متغیرهای ضروری تنظیم نشده‌اند: " + ", ".join(missing)
        )

    for provider in AI_PROVIDER_ORDER:
        keys = AI_PROVIDER_KEYS[provider]
        duplicates = len(keys) - len({key_id(k) for k in keys})
        if duplicates:
            raise RuntimeError(
                f"{duplicates} کلید تکراری برای {provider} پیدا شد؛ کلیدها را بررسی کن."
            )


def main():
    validate_config()

    state = load_state()
    initialize_ai_state(state)
    purge_recent_memories(state)
    purge_recent_inputs(state)
    save_state(state)

    log.info("==============================================")
    log.info("Raptor News Bot v4 شروع شد")
    log.info("Telegram sources: %s", len(SOURCE_CHANNELS))
    log.info(
        "AI pipeline: Groq -> Gemini -> Mistral | keys: Groq=%s Gemini=%s Mistral=%s",
        len(GROQ_API_KEYS), len(GEMINI_API_KEYS), len(MISTRAL_API_KEYS),
    )
    log.info(
        "Models: Groq=%s | Gemini=%s | Mistral=%s",
        GROQ_MODEL, GEMINI_MODEL, MISTRAL_MODEL,
    )
    log.info("Editor enabled: %s", EDITOR_ENABLED)
    log.info("AI batch max posts: %s", AI_BATCH_MAX_POSTS)
    log.info("AI max attempts/provider: %s", AI_MAX_ATTEMPTS_PER_PROVIDER)
    log.info("AI min request interval: %.2fs", AI_MIN_REQUEST_INTERVAL_SECONDS)
    log.info("Dedup window: %s minutes | event memory: %s minutes", DEDUP_WINDOW_MINUTES, EVENT_MEMORY_WINDOW_MINUTES)
    log.info("Publish spacing: 08-14=%s دقیقه | 14-24=%s دقیقه", MORNING_POST_SPACING_MINUTES, AFTERNOON_POST_SPACING_MINUTES)
    log.info("TEST_MODE: %s", TEST_MODE)
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
