import os
import re
import json
import time
import html
import calendar
import logging
import difflib
from datetime import datetime
from urllib.parse import quote

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

try:
    import feedparser
except ImportError:
    feedparser = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("repost-bot")

# =========================================================================
# بخش ۱: تنظیمات — همه از Environment Variables خونده می‌شن.
# هیچ کلید، توکن یا رمزی داخل خود کد نوشته نشده (اصل امنیتی مهم).
# =========================================================================

SOURCE_CHANNELS = [c.strip().lstrip("@") for c in os.environ.get("SOURCE_CHANNELS", "").split(",") if c.strip()]
SOURCE_WEBSITES = [u.strip() for u in os.environ.get("SOURCE_WEBSITES", "").split(",") if u.strip()]
TARGET_CHAT_ID = os.environ.get("TARGET_CHAT_ID", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# چند کلید Gemini (چرخش خودکار بین همه وقتی یکی به مشکل بخوره)
_raw_keys = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]

# نسخه‌ی مدل Gemini را عمداً «پین» (ثابت) می‌کنیم، نه یک نام مستعار مثل
# gemini-flash-latest که بدون اطلاع شما ممکن است به مدل دیگری اشاره کند.
# اگر گوگل این نسخه را بازنشسته کرد، کافی است همین متغیر محیطی را در Railway عوض کنید.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

# کلید سرویس عکس استوک Pexels (رایگان). اگر ست نشود، پست‌های سایتیِ بدون عکس
# اصلی صرفاً بدون عکس منتشر می‌شوند (به‌جای تلاش برای سرویس از‌کارافتاده‌ی قبلی).
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))

# جلوگیری از پست تکراری یک رویداد از چند منبع مختلف
DEDUP_WINDOW_MINUTES = int(os.environ.get("DEDUP_WINDOW_MINUTES", "240"))
DEDUP_SIMILARITY_THRESHOLD = float(os.environ.get("DEDUP_SIMILARITY_THRESHOLD", "0.72"))

# فاصله‌ی زمانی پخش پست‌های صف (سایت‌ها + باقی‌مانده‌ی شب) در طول روز — بازه‌ی متغیر:
# وقتی صف خلوته به سقف (MAX) نزدیک می‌شه؛ وقتی صف شلوغ می‌شه خودکار کم می‌شه؛
# ولی هیچ‌وقت از کف (MIN) پایین‌تر نمی‌ره.
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

# اگر خلاصه‌ی RSS کوتاه‌تر از این مقدار باشد، تلاش می‌کنیم متن کامل مقاله را از خود صفحه بگیریم.
RSS_MIN_SUMMARY_CHARS = int(os.environ.get("RSS_MIN_SUMMARY_CHARS", "400"))
# مقاله‌های قدیمی‌تر از این تعداد ساعت، حتی اگر تازه در فید ظاهر شده باشند، پردازش نمی‌شوند.
RSS_MAX_ARTICLE_AGE_HOURS = int(os.environ.get("RSS_MAX_ARTICLE_AGE_HOURS", "48"))

# اگر یک منبع (کانال/سایت) پشت‌سرهم در گرفتن اطلاعات خطا بدهد، به‌جای تلاش مجدد
# در هر چرخه، با فاصله‌ی فزاینده (تا سقف یک ساعت) موقتاً کنار گذاشته می‌شود.
SOURCE_COOLDOWN_BASE_SECONDS = int(os.environ.get("SOURCE_COOLDOWN_BASE_SECONDS", "300"))
SOURCE_COOLDOWN_MAX_SECONDS = int(os.environ.get("SOURCE_COOLDOWN_MAX_SECONDS", "3600"))

STATE_FILE = "/data/state.json" if os.path.isdir("/data") else "state.json"

FOOTER = "#raptor\n————————\n@khaatshekaan"
TEHRAN_TZ = ZoneInfo("Asia/Tehran") if ZoneInfo else None

# ساعت پیام صبح‌بخیر: ۸ صبح | ساعت پیام شب‌بخیر و شروع سکوت شبانه: ۰۰:۰۰ (۱۲ شب)
MORNING_HOUR = 8
NIGHT_HOUR = 0
QUIET_START_HOUR = 0
QUIET_END_HOUR = 8

MORNING_MESSAGE = "🌅 صبح بخیر به همراهان کانال\nروزتون پر از آرامش و اخبار دقیق باشه 🫡\n\n" + FOOTER
NIGHT_MESSAGE = "🌙 شب بخیر رپتوری‌های عزیز\nفردا با اخبار تازه در خدمتتون هستیم 🛡️\n\n" + FOOTER

# پیش‌فیلتر ارزان قبل از صرف یک درخواست Gemini — فقط برای فیلتر کردن موارد
# آشکارا تبلیغاتی/بی‌محتوا؛ تشخیص نهایی ارتباط همچنان بر عهده‌ی خود Gemini است.
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

۱۴. مقالات سایت: اگر محتوا صرفاً یک مقاله‌ی خبری/تحلیلی از یک سایت است (نه چند خبر جدا)، به‌جای پست طولانی، فقط یک چکیده‌ی کوتاه و آماده‌ی انتشار از آن بساز.

۱۵. تشخیص فوریت («urgent»): مقدار "urgent" را فقط برای خبرهای واقعاً فوری و لحظه‌ای علامت true بزن — شروع ناگهانی جنگ یا درگیری، حمله‌ی نظامی مستقیم، تشدید حاد بحران، یا وقتی خود منبع آن را «فوری»/«breaking» اعلام کرده. برای اخبار عادی یا تحلیلی این مقدار را false بگذار.

۱۶. سطح‌بندی اولویت («priority» از ۱ تا ۴)، به این ترتیب اهمیت:
   - سطح ۱: رویداد نظامی در حال وقوع (جنگ، حمله، درگیری مسلحانه فعال).
   - سطح ۲: مرتبط با خاورمیانه یا ایران (و سطح ۱ نیست).
   - سطح ۳: مرتبط با قدرت‌های بزرگ جهانی (آمریکا، روسیه، چین، ناتو و مشابه) ولی مرتبط با خاورمیانه/ایران یا رویداد نظامی فعال نیست.
   - سطح ۴: سایر اخبار عادی جهان.
   توجه: این فقط برای اولویت *زمان انتشار* است؛ اخبار سطح ۴ باید همچنان (برای حفظ تنوع پوشش) منتشر شوند، فقط ممکن است دیرتر نوبتشان برسد.

۱۷. اعتبارسنجی خودت: پیش از نهایی‌کردن پاسخ، یک‌بار مرور کن که تیتر و متن با محتوای ورودی همخوانی کامل دارند و هیچ نام، عدد، تاریخ یا مکانی اشتباه/جابه‌جا نشده باشد.

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
    """نوشتن اتمیک: اول در یک فایل موقت نوشته می‌شود، بعد جایگزین فایل اصلی می‌شود —
    تا اگر برنامه وسط نوشتن متوقف شد، فایل state هرگز نیمه‌نوشته/خراب نماند."""
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    os.replace(tmp_path, STATE_FILE)


# ---- کمک‌تابع‌های شمارنده‌ی تلاش مجدد (برای جلوگیری از گم‌شدن دائمی پست هنگام خطا) ----
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


# ---- کمک‌تابع‌های cooldown برای منابعی که پشت‌سرهم خطا می‌دهند ----
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
# بخش ۴: گرفتن پست از کانال‌های تلگرام (صفحه‌ی پیش‌نمایش عمومی)
# =========================================================================

def fetch_channel_posts(channel):
    """
    صفحه‌ی پیش‌نمایش عمومی کانال را می‌گیرد و پست‌ها (متن + همه‌ی عکس‌های آلبوم + فیلم) را
    استخراج می‌کند. در صورت خطای شبکه/HTTP مقدار None برمی‌گرداند (نه لیست خالی) تا با
    «هیچ پست جدیدی نبود» اشتباه گرفته نشود و منبع بتواند وارد چرخه‌ی cooldown شود.
    توجه: این روش بر پایه‌ی اسکرپ HTML صفحه‌ی عمومی است، نه API رسمی تلگرام؛ فقط برای
    کانال‌های پابلیک کار می‌کند و اگر تلگرام ساختار این صفحه را تغییر دهد ممکن است نیاز
    به به‌روزرسانی regexها داشته باشد.
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

        # همه‌ی عکس‌های موجود در این پست را می‌گیریم (پشتیبانی از آلبوم چندعکسی)
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
    return posts


# =========================================================================
# بخش ۵: گرفتن مطالب از سایت‌های خبری (RSS) با غنی‌سازی خلاصه‌ی کوتاه
# =========================================================================

def fetch_full_article_text(article_url):
    """اگر خلاصه‌ی RSS خیلی کوتاه بود، تلاش می‌کند متن کامل مقاله را از خود صفحه استخراج کند."""
    if BeautifulSoup is None:
        return None
    try:
        resp = requests.get(article_url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"دریافت متن کامل مقاله‌ی {article_url} ناموفق بود: {e}")
        return None
    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        paragraphs = soup.find_all("p")
        text = "\n".join(p.get_text(" ", strip=True) for p in paragraphs)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text[:6000] if text else None
    except Exception as e:
        log.warning(f"استخراج متن مقاله‌ی {article_url} ناموفق بود: {e}")
        return None


def fetch_website_posts(feed_url):
    """
    فید RSS را می‌خواند. در صورت خطا None برمی‌گرداند (نه لیست خالی).
    مقاله‌های قدیمی‌تر از RSS_MAX_ARTICLE_AGE_HOURS نادیده گرفته می‌شوند، و اگر خلاصه
    خیلی کوتاه باشد، تلاش می‌شود متن کامل مقاله از خود صفحه گرفته شود.
    """
    if feedparser is None:
        log.error("کتابخانه feedparser نصب نیست؛ نمی‌توان فید سایت را خواند.")
        return None
    try:
        parsed = feedparser.parse(feed_url)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            raise ValueError(getattr(parsed, "bozo_exception", "خطای نامشخص در پارس فید"))
    except Exception as e:
        log.warning(f"خطا در خواندن فید {feed_url}: {e}")
        return None

    posts = []
    now_ts = time.time()
    for entry in parsed.entries[:20]:
        raw_id = entry.get("id") or entry.get("link")
        if not raw_id:
            fallback_source = (entry.get("title", "") + entry.get("summary", ""))
            raw_id = f"hash:{abs(hash(fallback_source))}"
        uid = f"web:{feed_url}:{raw_id}"

        published_struct = entry.get("published_parsed") or entry.get("updated_parsed")
        if published_struct:
            try:
                published_ts = calendar.timegm(published_struct)
                age_hours = (now_ts - published_ts) / 3600
                if age_hours > RSS_MAX_ARTICLE_AGE_HOURS:
                    continue
            except Exception:
                pass

        title = entry.get("title", "").strip()
        summary = entry.get("summary", "") or entry.get("description", "")
        summary = re.sub(r"<[^>]+>", " ", summary)
        summary = html.unescape(summary).strip()

        if len(summary) < RSS_MIN_SUMMARY_CHARS and entry.get("link"):
            full_text = fetch_full_article_text(entry["link"])
            if full_text and len(full_text) > len(summary):
                summary = full_text

        content = f"{title}\n\n{summary}".strip()

        photo_url = None
        if entry.get("media_content"):
            photo_url = entry["media_content"][0].get("url")
        elif entry.get("media_thumbnail"):
            photo_url = entry["media_thumbnail"][0].get("url")
        elif entry.get("links"):
            for link in entry["links"]:
                if link.get("type", "").startswith("image"):
                    photo_url = link.get("href")
                    break

        if content:
            posts.append({
                "uid": uid,
                "text": content,
                "photo": photo_url,
                "photos": None,
                "video": None,
                "source_name": parsed.feed.get("title", feed_url),
            })
    return posts


# =========================================================================
# بخش ۶: پیش‌فیلتر ارزان قبل از صرف درخواست Gemini
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
# بخش ۷: ارتباط با Gemini — چرخش بین چند کلید + مدیریت کامل خطاها
# =========================================================================

_key_cursor = {"i": 0}

_ROTATABLE_STATUS_CODES = (401, 403, 429, 500, 503)


def analyze_and_rewrite(text, source_name):
    if not GEMINI_API_KEYS:
        raise RuntimeError("هیچ GEMINI_API_KEY/GEMINI_API_KEYS تنظیم نشده است.")

    payload = {
        "contents": [{"parts": [{"text": REWRITE_PROMPT.format(content=text, source_name=source_name)}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }

    wait_seconds = 15
    max_cycles = 3
    last_response = None
    last_error = None

    for cycle in range(max_cycles):
        got_success = False
        for _ in range(len(GEMINI_API_KEYS)):
            key = GEMINI_API_KEYS[_key_cursor["i"] % len(GEMINI_API_KEYS)]
            _key_cursor["i"] += 1
            headers = {"Content-Type": "application/json", "X-goog-api-key": key}
            try:
                resp = requests.post(GEMINI_API_URL, headers=headers, json=payload, timeout=60)
            except requests.RequestException as e:
                last_error = e
                log.warning(f"خطای شبکه هنگام تماس با Gemini ({e})؛ تلاش با کلید بعدی...")
                continue

            last_response = resp
            if resp.status_code not in _ROTATABLE_STATUS_CODES:
                got_success = True
                break

            if resp.status_code == 429:
                log.warning("یکی از کلیدهای Gemini به سقف رایگان خورد؛ سوییچ به کلید بعدی...")
            elif resp.status_code in (401, 403):
                log.warning(f"یکی از کلیدهای Gemini نامعتبر/بی‌اعتبارشده است (کد {resp.status_code})؛ سوییچ به کلید بعدی...")
            else:
                log.warning(f"سرور Gemini موقتاً در دسترس نیست (کد {resp.status_code})؛ تلاش با کلید بعدی...")

        if got_success:
            break

        log.warning(f"در این دور با هیچ کلیدی موفق نشدیم؛ {wait_seconds} ثانیه صبر می‌کنیم...")
        time.sleep(wait_seconds)
        wait_seconds = min(wait_seconds * 2, 120)

    if last_response is None:
        raise RuntimeError(f"تماس با Gemini برای همه‌ی کلیدها با خطای شبکه مواجه شد: {last_error}")

    last_response.raise_for_status()
    data = last_response.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise ValueError(f"پاسخ نامعتبر از Gemini: {data}")
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
# بخش ۸: ارتباط با تلگرام — با مدیریت flood-control (retry_after) و آپلود مستقیم فایل
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
        photo_bytes = download_bytes(photo_url, max_bytes=20 * 1024 * 1024)
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
            content = download_bytes(p_url, max_bytes=20 * 1024 * 1024)
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
# بخش ۹: جلوگیری از پست تکراری (رویداد مشابه از چند منبع مختلف)
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
# بخش ۱۰: صف پخش‌شونده در طول روز — اولویت ۴سطحی + فاصله‌ی زمانی پویا
# =========================================================================

def enqueue_post(state, title, body, photo_url, video_url, photos, label, priority):
    queue = state.get("_pending_queue", [])
    item = {
        "title": title,
        "body": body,
        "photo": photo_url,
        "photos": photos,
        "video": video_url,
        "label": label,
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

    title = item.get("title", "")
    body = item.get("body", "")
    if item.get("label") == "night_leftover":
        title = "🕛 (خبر دیشب) " + title

    try:
        dispatch_post(title, body, item.get("photo"), item.get("video"), item.get("photos"))
        log.info(
            f"یک پست از صف پخش روزانه منتشر شد "
            f"(نوع={item.get('label')}, اولویت={item.get('priority')}). "
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
# بخش ۱۱: پردازش یک نتیجه‌ی تک‌آیتمی از Gemini (منطق مشترک روتینگ)
# =========================================================================

def handle_result_item(state, result, is_website_source, photo_url, video_url, photos, source_label):
    if not result.get("relevant"):
        log.info(f"یک آیتم از {source_label} نامرتبط تشخیص داده شد و رد شد.")
        return

    if not validate_item(result):
        log.warning(f"یک آیتم از {source_label} خروجی نامعتبر/غیرمنطقی از Gemini داشت و رد شد.")
        return

    title = (result.get("title") or "").strip()
    body = (result.get("body") or "").strip()
    image_query = (result.get("image_query") or "").strip()
    urgent = bool(result.get("urgent"))
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

    if urgent:
        dispatch_post(title, body, photo_url, video_url, photos)
        remember_title(state, title, body)
        log.info(f"یک پست فوری از {source_label} بلافاصله منتشر شد (اولویت={priority}).")
        return

    if is_website_source:
        enqueue_post(state, title, body, photo_url, video_url, photos, label="site", priority=priority)
        remember_title(state, title, body)
        log.info(f"یک آیتم از {source_label} به صف پخش روزانه اضافه شد (اولویت={priority}).")
    elif is_quiet_hour(now_dt):
        enqueue_post(state, title, body, photo_url, video_url, photos, label="night_leftover", priority=priority)
        remember_title(state, title, body)
        log.info(f"یک آیتم از {source_label} به دلیل ساعت سکوت شبانه به صف اضافه شد (اولویت={priority}).")
    else:
        dispatch_post(title, body, photo_url, video_url, photos)
        remember_title(state, title, body)
        log.info(f"یک پست از {source_label} با موفقیت منتشر شد (اولویت={priority}).")


# =========================================================================
# بخش ۱۲: پردازش یک منبع — ادغام پست‌های پشت‌سرهمِ مرتبط (burst) +
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
    seen_ids = set(state.get(source_key, []))
    new_posts = [p for p in posts if p["uid"] not in seen_ids]
    if not new_posts:
        return

    if source_key not in state:
        state[source_key] = [p["uid"] for p in posts]
        save_state(state)
        log.info(f"منبع {source_key} برای اولین‌بار ثبت شد، از پست بعدی پردازش می‌شود.")
        return

    is_website_source = source_key.startswith("web:")
    text_bearing_posts = [p for p in new_posts if p["text"]]

    if not is_website_source and len(text_bearing_posts) >= BURST_MIN_POSTS:
        batch_label = f"دسته‌ی {len(text_bearing_posts)}پستی از {source_key}"
        log.info(f"{batch_label} شناسایی شد؛ به‌جای پردازش تک‌تک، ادغام و جمع‌بندی می‌شود.")

        combined_text = build_burst_content(text_bearing_posts)
        photo_url, video_url, photos = select_burst_media(text_bearing_posts)
        all_uids = [p["uid"] for p in new_posts]

        try:
            items = analyze_and_rewrite(combined_text, text_bearing_posts[0]["source_name"])
            for idx, result in enumerate(items):
                handle_result_item(
                    state, result, is_website_source,
                    photo_url if idx == 0 else None,
                    video_url if idx == 0 else None,
                    photos if idx == 0 else None,
                    source_label=batch_label,
                )
                time.sleep(4)
            _mark_uids_processed(state, source_key, all_uids)
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
            _mark_uids_processed(state, source_key, [uid])
            continue

        if looks_like_spam_or_trivial(text):
            log.info(f"{uid} با پیش‌فیلتر ارزان به‌عنوان تبلیغاتی/بی‌محتوا رد شد (بدون صرف درخواست Gemini).")
            _mark_uids_processed(state, source_key, [uid])
            continue

        log.info(f"در حال پردازش {uid}...")
        try:
            items = analyze_and_rewrite(text, post["source_name"])
            for idx, result in enumerate(items):
                handle_result_item(
                    state, result, is_website_source,
                    post.get("photo") if idx == 0 else None,
                    post.get("video") if idx == 0 else None,
                    post.get("photos") if idx == 0 else None,
                    source_label=uid,
                )
                time.sleep(4)
            _mark_uids_processed(state, source_key, [uid])
        except Exception as e:
            _handle_batch_failure(state, source_key, [uid], uid, e)

        time.sleep(6)


# =========================================================================
# بخش ۱۳: پیام‌های زمان‌بندی‌شده + پاک‌سازی نیمه‌شب صف
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
# بخش ۱۴: حلقه‌ی اصلی برنامه
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

    for feed_url in SOURCE_WEBSITES:
        source_key = f"web:{feed_url}"
        if is_source_in_cooldown(state, source_key):
            continue
        posts = fetch_website_posts(feed_url)
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
    ] if not val]
    if not SOURCE_CHANNELS and not SOURCE_WEBSITES:
        missing.append("SOURCE_CHANNELS یا SOURCE_WEBSITES (حداقل یکی)")
    if missing:
        log.error(f"این متغیرها تنظیم نشده‌اند: {', '.join(missing)}")
        return

    state = load_state()
    log.info(
        f"ربات شروع به کار کرد. کانال‌ها: {SOURCE_CHANNELS} | سایت‌ها: {len(SOURCE_WEBSITES)} فید | "
        f"تعداد کلید Gemini: {len(GEMINI_API_KEYS)} | مدل: {GEMINI_MODEL} | "
        f"فاصله‌ی پخش روزانه: {MIN_QUEUE_SPACING_MINUTES} تا {MAX_QUEUE_SPACING_MINUTES} دقیقه | "
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
