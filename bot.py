import os
import re
import json
import time
import html
import logging
import difflib
import hashlib
from datetime import datetime
from urllib.parse import quote

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("repost-bot")

# =========================================================================
# بخش ۱: تنظیمات — همه از Environment Variables خونده می‌شن.
# هیچ کلید، توکن یا رمزی داخل خود کد نوشته نشده (اصل امنیتی مهم).
# =========================================================================

SOURCE_CHANNELS = [c.strip().lstrip("@") for c in os.environ.get("SOURCE_CHANNELS", "").split(",") if c.strip()]
TARGET_CHAT_ID = os.environ.get("TARGET_CHAT_ID", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# چند کلید Gemini (چرخش خودکار بین همه وقتی یکی به مشکل بخوره)
_raw_keys = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]

# چند مدل Gemini هم پشتیبانی می‌شود: اگر یک مدل مدام ۵۰۳/سقف رایگان بدهد،
# خودکار مدل بعدی امتحان می‌شود. مدل‌های lite معمولاً سقف درخواست بیشتر
# و در دسترس‌بودن بهتری نسبت به gemini-2.5-flash معمولی دارند.
_raw_models = os.environ.get("GEMINI_MODELS") or os.environ.get("GEMINI_MODEL", "")
GEMINI_MODELS = [m.strip() for m in _raw_models.split(",") if m.strip()] or [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

# کلید سرویس عکس استوک Pexels (رایگان، اختیاری). اگر پست تلگرامی خودش عکس/فیلم
# نداشت، از این برای یک عکس استوکِ مرتبط از نظر موضوعی استفاده می‌شود.
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))

# جلوگیری از پست تکراری یک رویداد از چند کانال مختلف
DEDUP_WINDOW_MINUTES = int(os.environ.get("DEDUP_WINDOW_MINUTES", "240"))
DEDUP_SIMILARITY_THRESHOLD = float(os.environ.get("DEDUP_SIMILARITY_THRESHOLD", "0.72"))

# فاصله‌ی زمانی پخش صفِ باقی‌مانده‌ی شب در طول روز — بازه‌ی متغیر بین MIN و MAX
MAX_QUEUE_SPACING_MINUTES = int(os.environ.get("SITE_POST_SPACING_MINUTES", "60"))
MIN_QUEUE_SPACING_MINUTES = int(os.environ.get("SITE_POST_MIN_SPACING_MINUTES", "30"))

# اگر یک کانال در یک دور بررسی، این تعداد پست جدید یا بیشتر پشت‌سرهم منتشر کرده باشد،
# احتمالاً همه درباره‌ی یک رویداد واحدند — این‌ها ادغام و جمع‌بندی می‌شوند.
BURST_MIN_POSTS = int(os.environ.get("BURST_MIN_POSTS", "4"))

# در نیمه‌شب، فقط خبرهای اولویت ۱ و ۲ (طبق پیش‌فرض) در صف باقی می‌مانند؛ بقیه حذف می‌شوند.
QUEUE_PURGE_KEEP_PRIORITY_MAX = int(os.environ.get("QUEUE_PURGE_KEEP_PRIORITY_MAX", "2"))

# اگر پردازش یک پست چند بار پشت‌سرهم با خطا مواجه شود، پس از این تعداد تلاش
# به‌طور نهایی کنار گذاشته می‌شود (تا یک پست خراب برای همیشه صف را قفل نکند).
MAX_RETRY_ATTEMPTS = int(os.environ.get("MAX_RETRY_ATTEMPTS", "3"))

# اگر یک کانال پشت‌سرهم در گرفتن اطلاعات خطا بدهد، به‌جای تلاش مجدد در هر چرخه،
# با فاصله‌ی فزاینده (تا سقف یک ساعت) موقتاً کنار گذاشته می‌شود.
SOURCE_COOLDOWN_BASE_SECONDS = int(os.environ.get("SOURCE_COOLDOWN_BASE_SECONDS", "300"))
SOURCE_COOLDOWN_MAX_SECONDS = int(os.environ.get("SOURCE_COOLDOWN_MAX_SECONDS", "3600"))

STATE_FILE = "/data/state.json" if os.path.isdir("/data") else "state.json"
BOT_VERSION = os.environ.get("VERSION", "4.0.1")
RESET_STATE = os.environ.get("RESET_STATE", "0").strip().lower() in {"1", "true", "yes"}

FOOTER = "#raptor\n————————\n@khaatshekaan"
TEHRAN_TZ = ZoneInfo("Asia/Tehran") if ZoneInfo else None

# ساعت پیام صبح‌بخیر: ۸ صبح | ساعت پیام شب‌بخیر و شروع سکوت شبانه: ۰۰:۰۰ (۱۲ شب)
MORNING_HOUR = 8
NIGHT_HOUR = 0
QUIET_START_HOUR = 0
QUIET_END_HOUR = 8

MORNING_MESSAGE = "🌅 صبح بخیر به همراهان کانال\nروزتون پر از آرامش و اخبار دقیق باشه 🫡\n\n" + FOOTER
NIGHT_MESSAGE = "🌙 شب بخیر رپتوری‌های عزیز\nفردا با اخبار تازه در خدمتتون هستیم 🛡️\n\n" + FOOTER

_AD_KEYWORDS = [
    "تبلیغ", "اسپانسر", "تخفیف ویژه", "لینک عضویت", "خرید از", "کد تخفیف",
    "دعوت از دوستان", "جوین شوید", "کانال ما را دنبال کنید", "پروموشن",
]


# =========================================================================
# بخش ۲: پرامپت اصلی Gemini
# =========================================================================

REWRITE_PROMPT = """تو یک خبرنگار حرفه‌ای حوزه‌ی نظامی، امنیتی و ژئوپلیتیکی هستی که برای یک کانال تلگرامی گزارش می‌نویسی. این کانال باید مثل یک مرجع خبری رسمی و تمیز به‌نظر برسد؛ نه یک کانال هیجانی که پیاپی پست‌های کوتاه می‌زند.

متن ورودی زیر یک محتوای خام از منبع «{source_name}» است. ممکن است شامل یک پیام، چند خبر جداگانه، یا چند بروزرسانی پی‌در‌پی درباره‌ی یک رویداد باشد.

قوانین کار:

۱. تفکیک یا ادغام خبرها: اگر متن ورودی شامل چند خبر کاملاً جداگانه و بی‌ربط به هم است، هرکدام را جدا پردازش کن و هیچ‌کدام را با دیگری ادغام نکن. اما اگر متن ورودی شامل چند بروزرسانی پی‌در‌پی درباره‌ی یک رویداد واحد است (مثلاً چند پیام کوتاه که همه درباره‌ی یک حمله یا یک درگیری در حال وقوع هستند)، آن‌ها را ادغام کن و به‌جای چند پست کوتاه، فقط یک پست جامع (یا حداکثر دو پست، اگر واقعاً دو جنبه‌ی متفاوت و مهم دارد) بساز که کل ماجرا را یکجا و منسجم پوشش دهد.

۲. تشخیص ارتباط: مشخص کن آیا واقعاً خبر یا تحلیل نظامی/امنیتی/تسلیحاتی/عملیاتی/ژئوپلیتیکی مهم است. اگر محتوا تبلیغاتی، متفرقه، غیرمرتبط یا بی‌ارزش برای یک کانال میلیتاری است، آن را نامرتبط علامت بزن و پردازشش نکن.

۳. بدون ترجمه‌ی تحت‌اللفظی: متن را هرگز کلمه‌به‌کلمه ترجمه نکن؛ از ابتدا و با ادبیات خودت بازنویسی کن، طوری که اصلاً شبیه متن منبع یا ترجمه‌ی ماشینی نباشد.

۴. سبک نوشتار: گزارش نظامی — خبری، دقیق، حرفه‌ای، تحلیلی اما کوتاه، جذاب برای مخاطب تلگرام، بدون ادبیات زرد یا اغراق‌آمیز، بدون جملات تبلیغاتی یا شعاری.

۵. طول هر پست (بدون تیتر و بدون امضا): معمولاً ۱۰۰ تا ۱۸۰ کلمه کافی است، مگر خبر اطلاعات مهم زیادی داشته باشد که در این صورت فقط اطلاعات ضروری را نگه دار.

۶. استخراج اطلاعات کلیدی: چه اتفاقی افتاده، کجا، چه طرف‌هایی درگیرند، چه سلاح/سامانه/هواگرد/شناور/تجهیزاتی استفاده شده، اعداد و ارقام مهم، نتیجه یا پیامد احتمالی، اهمیت نظامی یا راهبردی خبر.

۷. احتیاط در ادعاهای تأییدنشده: اگر خبر شامل ادعا یا اطلاعات تأییدنشده است، آن را واقعیت قطعی جلوه نده. از عباراتی مثل «بر اساس گزارش‌ها»، «به گفته منابع»، «این گروه مدعی شده»، «در صورت تأیید» استفاده کن.

۸. ذکر منبع: نام منبع را فقط زمانی بیاور که برای اعتبار یا فهم خبر ضروری باشد.

۹. ایموجی کنترل‌شده: در هر پست ۲ تا ۳ ایموجی رسمی و مرتبط با موضوع کافی است؛ متن را با ایموجی پر نکن.

۱۰. تیتر: کوتاه، جذاب و نظامی، شبیه این نمونه‌ها:
«آمریکا سامانه جدیدی را وارد خدمت می‌کند»
«حمله به زیرساخت نفتی عربستان؛ تصاویر ماهواره‌ای چه می‌گویند؟»
«ژاپن به دنبال گسترش ناوگان پهپادی خود»
«ادعای سرنگونی دو پهپاد سعودی توسط حوثی‌ها»
از تیترهای بیش‌ازحد هیجانی مثل «وحشتناک» یا «فاجعه بزرگ» استفاده نکن مگر خود خبر واقعاً چنین چیزی را ثابت کند.

۱۱. ساختار پیشنهادی: پاراگراف اول = اصل خبر و مهم‌ترین اتفاق. پاراگراف دوم = جزئیات مهم (اعداد، سامانه‌ها، مکان، طرف‌های درگیر). پاراگراف سوم (در صورت نیاز) = اهمیت نظامی یا پیامد احتمالی. از بولت‌پوینت فقط برای فهرست چند عدد/مشخصات مهم استفاده کن.

۱۲. حفظ کامل واقعیت‌ها: هیچ واقعیت، عدد، نام سامانه، مکان، تاریخ یا ادعای اصلی موجود در متن ورودی را حذف یا تغییر نده؛ فقط جزئیات تکراری و فاقد ارزش خبری را حذف کن. هرگز چیزی را از خودت به‌عنوان واقعیت اضافه نکن. اگر می‌خواهی یک جمله‌ی تحلیلی/برداشتی اضافه کنی (نه واقعیت مستقیم خبر)، آن جمله را با عبارت «🔎 تحلیل:» شروع کن تا کاملاً از خودِ خبر متمایز باشد.

۱۳. لحن طبیعی: متن باید طبیعی و شبیه نوشته‌ی یک خبرنگار حوزه‌ی دفاعی باشد، نه ترجمه‌ی گوگل. از تکرار عبارت‌های کلیشه‌ای خودداری کن. متن را برای خواندن در تلگرام پاراگراف‌بندی کن.

۱۴. تشخیص فوریت («urgent»): مقدار "urgent" را فقط برای خبرهای واقعاً فوری و لحظه‌ای علامت true بزن — شروع ناگهانی جنگ یا درگیری، حمله‌ی نظامی مستقیم، تشدید حاد بحران، یا وقتی خود منبع آن را «فوری»/«breaking» اعلام کرده. برای اخبار عادی یا تحلیلی این مقدار را false بگذار.

۱۵. سطح‌بندی اولویت («priority» از ۱ تا ۴)، به این ترتیب اهمیت:
   - سطح ۱: رویداد نظامی در حال وقوع (جنگ، حمله، درگیری مسلحانه فعال).
   - سطح ۲: مرتبط با خاورمیانه یا ایران (و سطح ۱ نیست).
   - سطح ۳: مرتبط با قدرت‌های بزرگ جهانی (آمریکا، روسیه، چین، ناتو و مشابه) ولی مرتبط با خاورمیانه/ایران یا رویداد نظامی فعال نیست.
   - سطح ۴: سایر اخبار عادی جهان.
   توجه: این فقط برای اولویت *زمان انتشار* است؛ اخبار سطح ۴ باید همچنان (برای حفظ تنوع پوشش) منتشر شوند، فقط ممکن است دیرتر نوبتشان برسد.

۱۶. اعتبارسنجی خودت: پیش از نهایی‌کردن پاسخ، یک‌بار مرور کن که تیتر و متن با محتوای ورودی همخوانی کامل دارند و هیچ نام، عدد، تاریخ یا مکانی اشتباه/جابه‌جا نشده باشد.

خروجی را دقیقاً و فقط به‌شکل یک آبجکت JSON معتبر برگردان (بدون Markdown، بدون بک‌تیک، بدون هیچ توضیح اضافه)، با این فرمت دقیق:
{{
  "items": [
    {{
      "relevant": true یا false,
      "urgent": true یا false,
      "priority": 1 تا 4 (عدد صحیح),
      "title": "تیتر کوتاه فارسی (اگر relevant=false رشته خالی)",
      "body": "متن کامل بازنویسی‌شده شامل پاراگراف‌ها (اگر relevant=false رشته خالی)",
      "image_query": "۳ تا ۵ کلمه‌ی انگلیسی کوتاه برای جستجوی عکس استوک مرتبط (اگر relevant=false رشته خالی)"
    }}
  ]
}}
اگر ورودی فقط یک خبر (یا یک رویداد ادغام‌شده) دارد، آرایه‌ی items فقط یک آیتم خواهد داشت.

متن ورودی:
---
{content}
---"""


# =========================================================================
# بخش ۳: مدیریت حافظه‌ی وضعیت (state) — با نوشتن اتمیک برای جلوگیری از خرابی فایل
# =========================================================================

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            corrupted_backup = STATE_FILE + f".corrupted-{int(time.time())}"
            try:
                os.replace(STATE_FILE, corrupted_backup)
            except Exception:
                pass
            log.error(
                f"فایل state.json خراب بود و قابل‌خواندن نبود ({e}). "
                f"یک نسخه‌ی خراب در «{corrupted_backup}» نگه داشته شد و state خالی شروع می‌شود."
            )
            return {}
    return {}


def save_state(state):
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(tmp_path, STATE_FILE)


def get_retry_count(state, retry_key):
    return state.get("_retry_counts", {}).get(retry_key, 0)


def bump_retry_count(state, retry_key):
    counts = state.setdefault("_retry_counts", {})
    counts[retry_key] = counts.get(retry_key, 0) + 1
    return counts[retry_key]


def clear_retry_count(state, retry_key):
    counts = state.get("_retry_counts", {})
    if retry_key in counts:
        del counts[retry_key]


def is_source_in_cooldown(state, source_key):
    cooldowns = state.get("_source_cooldowns", {})
    until = cooldowns.get(source_key, 0)
    return time.time() < until


def register_source_failure(state, source_key):
    failures = state.setdefault("_source_failure_counts", {})
    failures[source_key] = failures.get(source_key, 0) + 1
    count = failures[source_key]
    delay = min(SOURCE_COOLDOWN_BASE_SECONDS * (2 ** (count - 1)), SOURCE_COOLDOWN_MAX_SECONDS)
    cooldowns = state.setdefault("_source_cooldowns", {})
    cooldowns[source_key] = time.time() + delay
    save_state(state)
    log.warning(f"منبع {source_key} برای {round(delay/60)} دقیقه به حالت استراحت رفت (خطای پشت‌سرهم شماره {count}).")


def register_source_success(state, source_key):
    failures = state.get("_source_failure_counts", {})
    cooldowns = state.get("_source_cooldowns", {})
    changed = False
    if source_key in failures:
        del failures[source_key]
        changed = True
    if source_key in cooldowns:
        del cooldowns[source_key]
        changed = True
    if changed:
        save_state(state)


# =========================================================================
# بخش ۴: گرفتن پست از کانال‌های تلگرام (صفحه‌ی پیش‌نمایش عمومی) — تنها منبع ربات
# =========================================================================

def fetch_channel_posts(channel):
    """
    صفحه‌ی پیش‌نمایش عمومی کانال را می‌گیرد و پست‌ها (متن + همه‌ی عکس‌های آلبوم + فیلم) را
    استخراج می‌کند. در صورت خطای شبکه/HTTP مقدار None برمی‌گرداند (نه لیست خالی).
    توجه: این روش بر پایه‌ی اسکرپ HTML صفحه‌ی عمومی است، نه API رسمی تلگرام؛ فقط برای
    کانال‌های پابلیک کار می‌کند.
    """
    url = f"https://t.me/s/{channel}"
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"خطا در گرفتن کانال {channel}: {e}")
        return None

    html_text = resp.text
    starts = [m.start() for m in re.finditer(r'data-post="' + re.escape(channel) + r'/(\d+)"', html_text)]
    ids = re.findall(r'data-post="' + re.escape(channel) + r'/(\d+)"', html_text)

    posts = []
    for i, msg_id in enumerate(ids):
        start = starts[i]
        end = starts[i + 1] if i + 1 < len(starts) else len(html_text)
        block = html_text[start:end]

        text = ""
        text_match = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.DOTALL)
        if text_match:
            raw_text = text_match.group(1)
            text = re.sub(r"<br\s*/?>", "\n", raw_text)
            text = re.sub(r"<[^>]+>", "", text)
            text = html.unescape(text).strip()

        photo_urls = re.findall(
            r'tgme_widget_message_photo_wrap[^"]*"\s+style="[^"]*background-image:url\(\'([^\']+)\'\)', block
        )
        photo_urls = [html.unescape(u) for u in photo_urls]

        video_url = None
        video_match = re.search(r'<video[^>]*class="[^"]*tgme_widget_message_video[^"]*"[^>]*src="([^"]+)"', block)
        if video_match:
            video_url = html.unescape(video_match.group(1))

        if text or photo_urls or video_url:
            posts.append({
                "uid": f"tg:{channel}:{msg_id}",
                "text": text,
                "photo": photo_urls[0] if photo_urls else None,
                "photos": photo_urls if len(photo_urls) > 1 else None,
                "video": video_url,
                "source_name": f"کانال {channel}",
            })
    log.info(f"کانال {channel}: {len(posts)} پست قابل‌خواندن از صفحه عمومی دریافت شد.")
    return posts


# =========================================================================
# بخش ۵: پیش‌فیلتر ارزان قبل از صرف درخواست Gemini
# =========================================================================

def looks_like_spam_or_trivial(text):
    if not text or len(text.strip()) < 15:
        return True
    hits = sum(1 for kw in _AD_KEYWORDS if kw in text)
    return hits >= 2


def validate_item(result):
    if not result.get("relevant"):
        return True
    title = (result.get("title") or "").strip()
    body = (result.get("body") or "").strip()
    if not title or not body:
        return False
    word_count = len(body.split())
    if word_count < 15 or word_count > 400:
        return False
    return True


# =========================================================================
# بخش ۶: ارتباط با Gemini — چرخش بین چند کلید *و* چند مدل + مدیریت کامل خطاها
# =========================================================================

# ترکیب همه‌ی (کلید، مدل)ها را یک‌بار می‌سازیم: اول همه‌ی کلیدها با مدل اول،
# بعد همه‌ی کلیدها با مدل دوم. چون این cursor سطح ماژول است و بین فراخوانی‌های
# مختلف باقی می‌ماند، در طول زمان به‌طور طبیعی هم بین کلیدها و هم بین مدل‌ها می‌چرخد.
def _build_key_model_combos():
    return [(k, m) for m in GEMINI_MODELS for k in GEMINI_API_KEYS]


_COMBOS = _build_key_model_combos()
_combo_cursor = {"i": 0}

_ROTATABLE_STATUS_CODES = (400, 401, 403, 404, 408, 409, 429, 500, 502, 503, 504)


def _gemini_url_for_model(model_name):
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"


def analyze_and_rewrite(text, source_name):
    if not GEMINI_API_KEYS:
        raise RuntimeError("هیچ GEMINI_API_KEY/GEMINI_API_KEYS تنظیم نشده است.")
    if not _COMBOS:
        raise RuntimeError("ترکیب کلید/مدل Gemini ساخته نشد.")

    payload = {
        "contents": [{"parts": [{"text": REWRITE_PROMPT.format(content=text, source_name=source_name)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "items": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "relevant": {"type": "BOOLEAN"},
                                "urgent": {"type": "BOOLEAN"},
                                "priority": {"type": "INTEGER"},
                                "title": {"type": "STRING"},
                                "body": {"type": "STRING"},
                                "image_query": {"type": "STRING"}
                            },
                            "required": ["relevant", "urgent", "priority", "title", "body", "image_query"]
                        }
                    }
                },
                "required": ["items"]
            }
        },
    }

    wait_seconds = 15
    max_cycles = 3
    last_response = None
    last_error = None
    last_model_used = None

    for cycle in range(max_cycles):
        got_success = False
        for _ in range(len(_COMBOS)):
            key, model = _COMBOS[_combo_cursor["i"] % len(_COMBOS)]
            _combo_cursor["i"] += 1
            headers = {"Content-Type": "application/json", "X-goog-api-key": key}
            url = _gemini_url_for_model(model)
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=60)
            except requests.RequestException as e:
                last_error = e
                log.warning(f"خطای شبکه هنگام تماس با Gemini (مدل {model})؛ ({e})؛ تلاش با ترکیب بعدی...")
                continue

            last_response = resp
            last_model_used = model
            if resp.status_code not in _ROTATABLE_STATUS_CODES:
                got_success = True
                break

            if resp.status_code == 429:
                log.warning(f"سقف رایگان مدل {model} با یکی از کلیدها پر شد؛ سوییچ به ترکیب بعدی...")
            elif resp.status_code in (401, 403):
                log.warning(f"یکی از کلیدهای Gemini نامعتبر/بی‌اعتبارشده است (کد {resp.status_code})؛ سوییچ به ترکیب بعدی...")
            else:
                log.warning(f"سرور Gemini (مدل {model}) موقتاً در دسترس نیست (کد {resp.status_code})؛ تلاش با ترکیب بعدی...")

        if got_success:
            break

        log.warning(f"در این دور با هیچ ترکیب کلید/مدلی موفق نشدیم؛ {wait_seconds} ثانیه صبر می‌کنیم...")
        time.sleep(wait_seconds)
        wait_seconds = min(wait_seconds * 2, 120)

    if last_response is None:
        raise RuntimeError(f"تماس با Gemini برای همه‌ی ترکیب‌ها با خطای شبکه مواجه شد: {last_error}")

    last_response.raise_for_status()
    data = last_response.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise ValueError(f"پاسخ نامعتبر از Gemini (مدل {last_model_used}): {data}")
    parts = candidates[0].get("content", {}).get("parts", [])
    raw = "\n".join(p.get("text", "") for p in parts).strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError(f"خروجی Gemini قابل‌پارس نبود: {raw[:300]}")
        parsed = json.loads(match.group(0))

    items = parsed.get("items", [])
    if not items and "relevant" in parsed:
        items = [parsed]
    return items


def find_stock_image(query):
    if not query or not PEXELS_API_KEY:
        return None
    try:
        resp = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": query, "per_page": 1, "orientation": "landscape"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        photos = data.get("photos") or []
        if photos:
            src = photos[0].get("src", {})
            return src.get("large") or src.get("original") or src.get("medium")
    except Exception as e:
        log.warning(f"خطا در گرفتن عکس استوک از Pexels: {e}")
    return None


# =========================================================================
# بخش ۷: ارتباط با تلگرام — با مدیریت flood-control (retry_after) و آپلود مستقیم فایل
# =========================================================================

def download_bytes(url, max_bytes, timeout=30):
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}, stream=True)
    resp.raise_for_status()
    chunks = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"فایل رسانه بزرگ‌تر از حد مجاز است (بیش از {max_bytes} بایت)")
        chunks.append(chunk)
    return b"".join(chunks)


def telegram_api_call(method, data=None, files=None, timeout=30, _retried=False):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    resp = requests.post(url, data=data, files=files, timeout=timeout)

    if resp.status_code == 429 and not _retried:
        retry_after = 5
        try:
            retry_after = int(resp.json().get("parameters", {}).get("retry_after", 5))
        except Exception:
            pass
        retry_after = min(max(retry_after, 1), 120)
        log.warning(f"تلگرام درخواست flood-control داد ({method})؛ {retry_after} ثانیه صبر و یک‌بار تلاش مجدد...")
        time.sleep(retry_after)
        return telegram_api_call(method, data=data, files=files, timeout=timeout, _retried=True)

    return resp


def send_to_telegram(text):
    resp = telegram_api_call(
        "sendMessage",
        data={"chat_id": TARGET_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False},
        timeout=20,
    )
    if not resp.ok:
        log.error(f"خطا در ارسال پیام: {resp.text}")
    resp.raise_for_status()


def send_photo_to_telegram(caption, photo_url):
    data = {"chat_id": TARGET_CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
    try:
        photo_bytes = download_bytes(photo_url, max_bytes=10 * 1024 * 1024)
        resp = telegram_api_call("sendPhoto", data=data, files={"photo": ("photo.jpg", photo_bytes)}, timeout=60)
    except Exception as e:
        log.warning(f"دانلود مستقیم عکس ناموفق بود، تلاش با ارسال لینک: {e}")
        data_with_link = dict(data)
        data_with_link["photo"] = photo_url
        resp = telegram_api_call("sendPhoto", data=data_with_link, timeout=30)
    if not resp.ok:
        log.error(f"خطا در ارسال عکس: {resp.text}")
    resp.raise_for_status()


def send_video_to_telegram(caption, video_url):
    data = {"chat_id": TARGET_CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
    try:
        video_bytes = download_bytes(video_url, max_bytes=45 * 1024 * 1024)
        resp = telegram_api_call("sendVideo", data=data, files={"video": ("video.mp4", video_bytes)}, timeout=120)
    except Exception as e:
        log.warning(f"دانلود مستقیم فیلم ناموفق بود، تلاش با ارسال لینک: {e}")
        data_with_link = dict(data)
        data_with_link["video"] = video_url
        resp = telegram_api_call("sendVideo", data=data_with_link, timeout=60)
    if not resp.ok:
        log.error(f"خطا در ارسال فیلم: {resp.text}")
    resp.raise_for_status()


def send_media_group_to_telegram(caption, photo_urls):
    media = []
    files = {}
    for i, p_url in enumerate(photo_urls[:10]):
        try:
            content = download_bytes(p_url, max_bytes=10 * 1024 * 1024)
        except Exception as e:
            log.warning(f"دانلود عکس شماره {i + 1} از آلبوم ناموفق بود: {e}")
            continue
        field_name = f"photo{i}"
        files[field_name] = (f"{field_name}.jpg", content)
        item = {"type": "photo", "media": f"attach://{field_name}"}
        if i == 0 and caption:
            item["caption"] = caption[:1024]
            item["parse_mode"] = "HTML"
        media.append(item)

    if not media:
        raise ValueError("هیچ‌کدام از عکس‌های آلبوم قابل‌دانلود نبودند.")

    data = {"chat_id": TARGET_CHAT_ID, "media": json.dumps(media, ensure_ascii=False)}
    resp = telegram_api_call("sendMediaGroup", data=data, files=files, timeout=120)
    if not resp.ok:
        log.error(f"خطا در ارسال آلبوم عکس: {resp.text}")
    resp.raise_for_status()


def build_final_message(title, body):
    parts = []
    if title:
        parts.append(f"<b>{html.escape(title)}</b>")
    if body:
        parts.append(html.escape(body))
    parts.append(FOOTER)
    return "\n\n".join(parts)


def dispatch_post(title, body, photo_url=None, video_url=None, photos=None):
    full_msg = build_final_message(title, body)
    has_media = bool(video_url or photo_url or photos)

    if has_media and len(full_msg) > 1024:
        caption = f"<b>{html.escape(title)}</b>" if title else ""
        if len(caption) > 1024:
            caption = caption[:1000] + "…"
        followup_text = full_msg
    else:
        caption = full_msg
        followup_text = None

    sent = False
    if video_url:
        try:
            send_video_to_telegram(caption, video_url)
            sent = True
        except Exception as e:
            log.warning(f"ارسال فیلم ناموفق بود: {e}")
    if not sent and photos and len(photos) > 1:
        try:
            send_media_group_to_telegram(caption, photos)
            sent = True
        except Exception as e:
            log.warning(f"ارسال آلبوم عکس ناموفق بود: {e}")
    if not sent and (photo_url or photos):
        single_photo = photo_url or (photos[0] if photos else None)
        try:
            send_photo_to_telegram(caption, single_photo)
            sent = True
        except Exception as e:
            log.warning(f"ارسال عکس ناموفق بود: {e}")

    if not sent:
        send_to_telegram(full_msg)
        return

    if followup_text:
        try:
            send_to_telegram(followup_text)
        except Exception as e:
            log.error(f"ارسال متن کامل پس از رسانه (چون کپشن جا نمی‌شد) ناموفق بود: {e}")


# =========================================================================
# بخش ۸: جلوگیری از پست تکراری (رویداد مشابه از چند کانال مختلف)
# =========================================================================

def _dedup_signature(title, body):
    first_chunk = (body or "")[:150]
    return f"{title.strip()} | {first_chunk.strip()}"


def is_duplicate(state, title, body):
    now = time.time()
    recent = state.get("_recent_signatures", [])
    recent = [r for r in recent if now - r["ts"] <= DEDUP_WINDOW_MINUTES * 60]
    signature = _dedup_signature(title, body)
    for r in recent:
        ratio = difflib.SequenceMatcher(None, signature, r["signature"]).ratio()
        if ratio >= DEDUP_SIMILARITY_THRESHOLD:
            return True
    return False


def remember_title(state, title, body):
    now = time.time()
    recent = state.get("_recent_signatures", [])
    recent = [r for r in recent if now - r["ts"] <= DEDUP_WINDOW_MINUTES * 60]
    recent.append({"signature": _dedup_signature(title, body), "ts": now})
    state["_recent_signatures"] = recent[-200:]


# =========================================================================
# بخش ۹: صف پخش‌شونده در طول روز — فقط برای باقی‌مانده‌ی سکوت شبانه
# =========================================================================

def enqueue_post(state, title, body, photo_url, video_url, photos, priority):
    queue = state.get("_pending_queue", [])
    item = {
        "title": title,
        "body": body,
        "photo": photo_url,
        "photos": photos,
        "video": video_url,
        "label": "night_leftover",
        "priority": priority,
        "queued_at": time.time(),
    }
    insert_at = len(queue)
    for i, existing in enumerate(queue):
        if existing.get("priority", 4) > priority:
            insert_at = i
            break
    queue.insert(insert_at, item)
    state["_pending_queue"] = queue[-300:]
    save_state(state)


def is_quiet_hour(now_dt):
    if not now_dt:
        return False
    return QUIET_START_HOUR <= now_dt.hour < QUIET_END_HOUR


def compute_dynamic_spacing(now_dt, queue_len):
    if queue_len <= 0:
        return MAX_QUEUE_SPACING_MINUTES
    minutes_since_midnight = now_dt.hour * 60 + now_dt.minute
    remaining_minutes_to_midnight = max((24 * 60) - minutes_since_midnight, 1)
    ideal_spacing = remaining_minutes_to_midnight / queue_len
    return max(MIN_QUEUE_SPACING_MINUTES, min(MAX_QUEUE_SPACING_MINUTES, ideal_spacing))


def process_queue(state):
    if not TEHRAN_TZ:
        return
    now_dt = datetime.now(TEHRAN_TZ)
    if is_quiet_hour(now_dt):
        return

    queue = state.get("_pending_queue", [])
    if not queue:
        return

    spacing_minutes = compute_dynamic_spacing(now_dt, len(queue))
    last_release = state.get("_last_queue_release_ts", 0)
    elapsed_minutes = (time.time() - last_release) / 60
    if elapsed_minutes < spacing_minutes:
        return

    item = queue.pop(0)
    state["_pending_queue"] = queue

    title = "🕛 (خبر دیشب) " + item.get("title", "")
    body = item.get("body", "")

    try:
        dispatch_post(title, body, item.get("photo"), item.get("video"), item.get("photos"))
        log.info(
            f"یک پست از صف باقی‌مانده‌ی شب منتشر شد (اولویت={item.get('priority')}). "
            f"فاصله‌ی محاسبه‌شده تا پست بعدی: {round(spacing_minutes)} دقیقه."
        )
    except Exception as e:
        log.error(f"خطا در انتشار پست از صف: {e}")

    state["_last_queue_release_ts"] = time.time()
    save_state(state)


def purge_low_priority_queue(state, today_str):
    if state.get("_last_queue_purge_date") == today_str:
        return
    queue = state.get("_pending_queue", [])
    before_count = len(queue)
    remaining = [item for item in queue if item.get("priority", 4) <= QUEUE_PURGE_KEEP_PRIORITY_MAX]
    removed_count = before_count - len(remaining)
    state["_pending_queue"] = remaining
    state["_last_queue_purge_date"] = today_str
    save_state(state)
    if removed_count:
        log.info(
            f"در نیمه‌شب {removed_count} خبر کم‌اهمیت باقی‌مانده در صف حذف شد "
            f"(اولویت پایین‌تر از {QUEUE_PURGE_KEEP_PRIORITY_MAX})."
        )


# =========================================================================
# بخش ۱۰: پردازش یک نتیجه‌ی تک‌آیتمی از Gemini (منطق مشترک روتینگ)
# =========================================================================

def handle_result_item(state, result, photo_url, video_url, photos, source_label):
    if not result.get("relevant"):
        log.info(f"یک آیتم از {source_label} نامرتبط تشخیص داده شد و رد شد.")
        return

    if not validate_item(result):
        log.warning(f"یک آیتم از {source_label} خروجی نامعتبر/غیرمنطقی از Gemini داشت و رد شد.")
        return

    title = (result.get("title") or "").strip()
    body = (result.get("body") or "").strip()
    image_query = (result.get("image_query") or "").strip()
    urgent_value = result.get("urgent", False)
    urgent = urgent_value is True or (isinstance(urgent_value, str) and urgent_value.strip().lower() in {"true", "1", "yes"})
    try:
        priority = int(result.get("priority", 4))
    except (TypeError, ValueError):
        priority = 4
    priority = max(1, min(4, priority))

    if is_duplicate(state, title, body):
        log.info(f"یک آیتم از {source_label} به‌عنوان پست تکراری (رویداد مشابه اخیر) رد شد.")
        return

    if not photo_url and not photos and not video_url and image_query:
        photo_url = find_stock_image(image_query)

    now_dt = datetime.now(TEHRAN_TZ) if TEHRAN_TZ else None

    if urgent or not is_quiet_hour(now_dt):
        dispatch_post(title, body, photo_url, video_url, photos)
        remember_title(state, title, body)
        tag = " (فوری، در سکوت شبانه)" if urgent and is_quiet_hour(now_dt) else ""
        log.info(f"یک پست از {source_label} با موفقیت منتشر شد{tag} (اولویت={priority}).")
    else:
        enqueue_post(state, title, body, photo_url, video_url, photos, priority=priority)
        remember_title(state, title, body)
        log.info(f"یک آیتم از {source_label} به دلیل ساعت سکوت شبانه به صف اضافه شد (اولویت={priority}).")


# =========================================================================
# بخش ۱۱: پردازش یک کانال — ادغام پست‌های پشت‌سرهمِ مرتبط (burst) +
#          سیستم تلاش مجدد با سقف (retry-capped) به‌جای گم‌شدن دائمی پست هنگام خطا
# =========================================================================

def build_burst_content(posts):
    numbered_parts = []
    for i, post in enumerate(posts, start=1):
        if post["text"]:
            numbered_parts.append(f"[پیام {i}]\n{post['text']}")
    header = (
        "توجه: این‌ها چند پیام پشت‌سرهم هستند که در بازه‌ی زمانی کوتاهی از یک کانال منتشر شده‌اند. "
        "ممکن است همگی درباره‌ی یک رویداد واحد باشند (طبق قانون ۱ تصمیم بگیر ادغام کنی یا جدا نگه داری):\n\n"
    )
    return header + "\n\n".join(numbered_parts)


def select_burst_media(posts):
    photo_url, video_url, photos = None, None, None
    for post in posts:
        if not photo_url and not photos:
            if post.get("photos"):
                photos = post["photos"]
            elif post.get("photo"):
                photo_url = post["photo"]
        if not video_url and post.get("video"):
            video_url = post["video"]
    return photo_url, video_url, photos


def _post_fingerprint(post):
    payload = "\n".join([
        post.get("text", "") or "",
        post.get("photo", "") or "",
        "|".join(post.get("photos") or []),
        post.get("video", "") or "",
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mark_posts_processed(state, source_key, posts):
    uids = [p["uid"] for p in posts]
    _mark_uids_processed(state, source_key, uids)
    fingerprints = state.setdefault("_content_fingerprints", {})
    for p in posts:
        fingerprints[p["uid"]] = _post_fingerprint(p)
    # Keep state bounded.
    if len(fingerprints) > 2000:
        keep = list(fingerprints.items())[-2000:]
        state["_content_fingerprints"] = dict(keep)
    save_state(state)


def _mark_uids_processed(state, source_key, uids):
    existing = state.get(source_key, [])
    state[source_key] = (existing + list(uids))[-300:]
    for uid in uids:
        clear_retry_count(state, f"{source_key}|{uid}")
    save_state(state)


def _handle_batch_failure(state, source_key, uids, batch_label, error):
    retry_key = f"{source_key}|{batch_label}"
    attempt = bump_retry_count(state, retry_key)
    if attempt >= MAX_RETRY_ATTEMPTS:
        log.error(
            f"{batch_label} پس از {attempt} بار تلاش ناموفق ({error})، به‌طور نهایی کنار گذاشته شد "
            f"و دیگر تلاش نخواهد شد."
        )
        _mark_uids_processed(state, source_key, uids)
        clear_retry_count(state, retry_key)
    else:
        log.warning(
            f"{batch_label} با خطا مواجه شد ({error}) — تلاش {attempt}/{MAX_RETRY_ATTEMPTS}؛ "
            f"در چرخه‌ی بعدی دوباره امتحان می‌شود (به‌عنوان دیده‌شده ثبت نمی‌شود)."
        )
        save_state(state)


def process_posts(state, source_key, posts):
    # مهاجرت خودکار از state نسخه‌های قدیمی که fingerprint نداشتند.
    if source_key in state and state.get(source_key) and "_content_fingerprints" not in state:
        log.warning(f"{source_key}: state قدیمی شناسایی شد؛ شناسه‌های قبلی برای شروع صحیح نسخه جدید بازنشانی می‌شوند.")
        state[source_key] = []
        save_state(state)
    seen_ids = set(state.get(source_key, []))
    fingerprints = state.get("_content_fingerprints", {})
    new_posts = []
    edited_posts = []
    for p in posts:
        uid = p["uid"]
        fp = _post_fingerprint(p)
        if uid not in seen_ids:
            new_posts.append(p)
        elif fingerprints.get(uid) and fingerprints.get(uid) != fp:
            new_posts.append(p)
            edited_posts.append(p)

    if not new_posts:
        log.info(f"{source_key}: پست جدید یا ویرایش‌شده‌ای پیدا نشد.")
        return

    if source_key not in state:
        state[source_key] = []
        save_state(state)
        log.info(f"منبع {source_key} برای اولین‌بار دیده شد؛ {len(new_posts)} پست موجود برای پردازش بررسی می‌شود.")
    if edited_posts:
        log.info(f"{source_key}: {len(edited_posts)} پست ویرایش‌شده شناسایی شد.")

    text_bearing_posts = [p for p in new_posts if p["text"]]

    if len(text_bearing_posts) >= BURST_MIN_POSTS:
        batch_label = f"دسته‌ی {len(text_bearing_posts)}پستی از {source_key}"
        log.info(f"{batch_label} شناسایی شد؛ به‌جای پردازش تک‌تک، ادغام و جمع‌بندی می‌شود.")

        combined_text = build_burst_content(text_bearing_posts)
        photo_url, video_url, photos = select_burst_media(text_bearing_posts)
        all_uids = [p["uid"] for p in new_posts]

        try:
            items = analyze_and_rewrite(combined_text, text_bearing_posts[0]["source_name"])
            for idx, result in enumerate(items):
                handle_result_item(
                    state, result,
                    photo_url if idx == 0 else None,
                    video_url if idx == 0 else None,
                    photos if idx == 0 else None,
                    source_label=batch_label,
                )
                time.sleep(4)
            _mark_posts_processed(state, source_key, new_posts)
        except Exception as e:
            _handle_batch_failure(state, source_key, all_uids, batch_label, e)

        time.sleep(6)
        return

    for post in new_posts:
        uid = post["uid"]
        text = post["text"]

        if not text:
            if post.get("photo") or post.get("photos") or post.get("video"):
                try:
                    dispatch_post("", "", post.get("photo"), post.get("video"), post.get("photos"))
                    log.info(f"{uid} بدون کپشن بود؛ فقط رسانه با امضای کانال منتشر شد.")
                except Exception as e:
                    _handle_batch_failure(state, source_key, [uid], uid, e)
                    continue
            _mark_posts_processed(state, source_key, [post])
            continue

        if looks_like_spam_or_trivial(text):
            log.info(f"{uid} با پیش‌فیلتر ارزان به‌عنوان تبلیغاتی/بی‌محتوا رد شد (بدون صرف درخواست Gemini).")
            _mark_posts_processed(state, source_key, [post])
            continue

        log.info(f"در حال پردازش {uid}...")
        try:
            items = analyze_and_rewrite(text, post["source_name"])
            for idx, result in enumerate(items):
                handle_result_item(
                    state, result,
                    post.get("photo") if idx == 0 else None,
                    post.get("video") if idx == 0 else None,
                    post.get("photos") if idx == 0 else None,
                    source_label=uid,
                )
                time.sleep(4)
            _mark_posts_processed(state, source_key, [post])
        except Exception as e:
            _handle_batch_failure(state, source_key, [uid], uid, e)

        time.sleep(6)


# =========================================================================
# بخش ۱۲: پیام‌های زمان‌بندی‌شده + پاک‌سازی نیمه‌شب صف
# =========================================================================

def check_scheduled_messages(state):
    if not TEHRAN_TZ:
        return
    now = datetime.now(TEHRAN_TZ)
    today_str = now.strftime("%Y-%m-%d")

    if now.hour == MORNING_HOUR and state.get("_last_morning_date") != today_str:
        try:
            send_to_telegram(MORNING_MESSAGE)
            state["_last_morning_date"] = today_str
            save_state(state)
            log.info("پیام صبح‌بخیر ارسال شد.")
        except Exception as e:
            log.error(f"خطا در ارسال پیام صبح‌بخیر: {e}")

    if now.hour == NIGHT_HOUR and state.get("_last_night_date") != today_str:
        try:
            send_to_telegram(NIGHT_MESSAGE)
            state["_last_night_date"] = today_str
            save_state(state)
            log.info("پیام شب‌بخیر ارسال شد.")
        except Exception as e:
            log.error(f"خطا در ارسال پیام شب‌بخیر: {e}")
        purge_low_priority_queue(state, today_str)


# =========================================================================
# بخش ۱۳: حلقه‌ی اصلی برنامه
# =========================================================================

def process_once(state):
    for channel in SOURCE_CHANNELS:
        source_key = f"tg:{channel}"
        if is_source_in_cooldown(state, source_key):
            continue
        posts = fetch_channel_posts(channel)
        if posts is None:
            register_source_failure(state, source_key)
            continue
        register_source_success(state, source_key)
        if posts:
            process_posts(state, source_key, posts)

    check_scheduled_messages(state)
    process_queue(state)


def main():
    missing = [name for name, val in [
        ("BOT_TOKEN", BOT_TOKEN),
        ("TARGET_CHAT_ID", TARGET_CHAT_ID),
        ("GEMINI_API_KEY یا GEMINI_API_KEYS", "yes" if GEMINI_API_KEYS else ""),
        ("SOURCE_CHANNELS", "yes" if SOURCE_CHANNELS else ""),
    ] if not val]
    if missing:
        log.error(f"این متغیرها تنظیم نشده‌اند: {', '.join(missing)}")
        return

    state = load_state()
    if RESET_STATE:
        log.warning("RESET_STATE فعال است؛ state قبلی پاک و پردازش از وضعیت فعلی کانال‌ها شروع می‌شود.")
        state = {}
        save_state(state)
    log.info(f"نسخه ربات: {BOT_VERSION} | تعداد کانال‌های منبع: {len(SOURCE_CHANNELS)} | فاصله بررسی: {POLL_INTERVAL_SECONDS} ثانیه")
    log.info(
        f"ربات شروع به کار کرد (فقط کانال‌های تلگرام). کانال‌ها: {SOURCE_CHANNELS} | "
        f"تعداد کلید Gemini: {len(GEMINI_API_KEYS)} | مدل‌ها: {GEMINI_MODELS} | "
        f"مجموع ترکیب کلید×مدل: {len(_COMBOS)} | "
        f"فاصله‌ی پخش صف شب: {MIN_QUEUE_SPACING_MINUTES} تا {MAX_QUEUE_SPACING_MINUTES} دقیقه | "
        f"آستانه‌ی ادغام پست انبوه: {BURST_MIN_POSTS} | عکس استوک Pexels: {'فعال' if PEXELS_API_KEY else 'غیرفعال (بدون کلید)'}"
    )
    while True:
        try:
            process_once(state)
        except Exception as e:
            log.error(f"خطای عمومی در حلقه: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
