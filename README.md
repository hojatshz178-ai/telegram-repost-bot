# Raptor Telegram News Bot — v4

نسخه 4 یک ارتقای معماری از v3.8 است و قابلیت‌های اصلی نسخه قبلی را نگه می‌دارد: جمع‌آوری از کانال‌های تلگرام، حذف نویز، خوشه‌بندی رویداد، صف، زمان‌بندی ثابت، سبد محتوایی، خروجی طبیعی، fallback رسانه و state پایدار. نسخه v3.8 همچنین برای خطاهای رسانه fallback به متن و برای Gemini کنترل 429 داشت؛ این اصول در v4 حفظ و عمومی‌تر شده‌اند. 

## معماری هوش مصنوعی

ترتیب اصلی و failover:

```text
Groq  →  Gemini  →  Mistral
PRIMARY   BACKUP 1   BACKUP 2
```

### Stage 1 — News Brain

- ادغام چند منبع درباره یک رویداد
- تشخیص خبر تکراری با بیان متفاوت
- تشخیص update واقعی
- event_key و حافظه رویداد
- تشخیص ارزش خبری و اهمیت
- انتخاب سبک خبر و طول مناسب
- عنوان‌سازی غیرکلیشه‌ای
- تولید متن فارسی حرفه‌ای

### Stage 2 — Final Editor

پس از Stage 1، ویراستار نهایی **هوشمند و شرطی** است: برای خبرهای چندمنبعی، update، مهم/فوری، تحلیلی، full یا موارد حساس از نظر وضعیت منبع اجرا می‌شود؛ خبرهای ساده بدون درخواست دوم هم می‌توانند مستقیم منتشر شوند. وقتی Editor لازم باشد، ترتیب provider برای توزیع بار چرخانده می‌شود؛ اگر Stage 1 با Groq موفق شده باشد، Editor ابتدا provider بعدی را امتحان می‌کند. اگر Editor در دسترس نباشد، draft مرحله اول حفظ می‌شود و خبر از بین نمی‌رود.

هیچ provider ای نمی‌تواند با retryهای بی‌نهایت چرخه را قفل کند:

- حداکثر 2 تلاش داخلی برای هر provider در هر درخواست
- 429 → cooldown همان provider + رفتن فوری به provider بعدی
- 401/403 → کنار گذاشتن همان key
- 400/404 → provider موقتاً کنار گذاشته می‌شود و failover انجام می‌شود
- 5xx و خطای شبکه → retry محدود، سپس provider بعدی
- اگر هر سه provider موقتاً unavailable باشند، batch برای retry بعدی نگه داشته می‌شود

## اصلاح اصلی تکراری‌ها

v4 سه لایه کنترل تکرار دارد:

1. **Raw near-duplicate**: پست‌های تقریباً یکسان قبل از AI حذف می‌شوند.
2. **Event memory**: رویدادهای منتشرشده با event_key و امضای رویداد تا 72 ساعت به‌صورت پایدار نگه داشته می‌شوند.
3. **Queue guard + final publish guard**: حتی اگر یک خبر somehow وارد صف شود، قبل از انتشار دوباره بررسی می‌شود.
4. **Entity/source anchors**: اشتراک نام‌های کلیدی رویداد و UID پست منبع نیز برای تشخیص تکراری استفاده می‌شود.

تکرار عادی حداقل 24 ساعت بسته می‌شود؛ Railway variable قدیمی `DEDUP_WINDOW_MINUTES=360` دیگر نمی‌تواند این حداقل را به 6 ساعت پایین بیاورد.

برای update واقعی:

- همان رویداد باید مشخص شود
- novelty باید به حد کافی بالا باشد
- در 24 ساعت بیشتر از `EVENT_MAX_UPDATES_PER_24H` update منتشر نمی‌شود
- update جدید در صف، نسخه قبلی همان event را جایگزین می‌کند
- سقف update روزانه در منطق محلی نیز enforce می‌شود تا مدل نتواند با event_key جدید همان رویداد را بی‌نهایت تازه‌سازی کند

## سبک زبان v4

- فارسی خبرگزاری‌گونه و طبیعی
- جمله‌های کوتاه و مستقیم
- نیم‌فاصله و نشانه‌گذاری کنترل‌شده
- حذف لحن ماشینی و کلیشه‌ای
- تنوع ساختار تیتر و جمله
- تشخیص clickbait
- جلوگیری از اغراق
- سبک جداگانه برای جنگ/درگیری، ژئوپلیتیک، فناوری دفاعی، ایران، خبر فوری و خبر مهم
- طول هوشمند: brief / standard / full
- پاراگراف `📌` فقط در صورت وجود significance واقعی
- حافظه سبک کانال برای جلوگیری از تکرار ساختار تیتر و جمله

## مدل‌ها

پیش‌فرض v4:

```text
GROQ_MODEL=openai/gpt-oss-120b
GEMINI_MODEL=gemini-3.8-flash
MISTRAL_MODEL=mistral-small-latest
```

Groq مدل `openai/gpt-oss-120b` را با JSON Object/Structured Outputs پشتیبانی می‌کند. Mistral در مستندات رسمی Free mode، نمونه API را با `mistral-small-latest` نشان می‌دهد. Gemini نیز در متغیر مستقل خود باقی می‌ماند.

## Railway Variables

### ضروری

```text
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
TARGET_CHAT_ID=@khaatshekaan
SOURCE_CHANNELS=MaxOsintIntel,OSINTWarfare,defenseexpress_ua,WarPeaceAndYou,RazmAvaran_org

GROQ_API_KEYS=YOUR_GROQ_KEY
GROQ_MODEL=openai/gpt-oss-120b

GEMINI_API_KEYS=YOUR_EXISTING_GEMINI_KEYS
GEMINI_MODEL=gemini-3.8-flash

MISTRAL_API_KEYS=YOUR_MISTRAL_KEY
MISTRAL_MODEL=mistral-small-latest

POLL_INTERVAL_SECONDS=180
STATE_FILE=/data/state.json
TEST_MODE=false
```

### تنظیمات v4

```text
AI_EDITOR_ENABLED=true
# true یعنی ویراستار فعال است؛ اجرای آن بر اساس اهمیت/نوع خبر شرطی است تا ظرفیت رایگان بی‌دلیل مصرف نشود.
AI_BATCH_MAX_POSTS=8
AI_EDITOR_MAX_ITEMS=8
AI_MAX_ATTEMPTS_PER_PROVIDER=2
AI_MIN_REQUEST_INTERVAL_SECONDS=1.2
AI_MAX_OUTPUT_TOKENS=1400
AI_PROVIDER_COOLDOWN_FLOOR_SECONDS=45
AI_429_MAX_COOLDOWN_SECONDS=600
AI_FAILURE_RETRY_MINUTES=10
AI_INPUT_MAX_CHARS_PER_POST=2400
AI_RECENT_INPUT_WINDOW_MINUTES=1440
AI_LOCAL_DUP_THRESHOLD=0.94

DEDUP_WINDOW_MINUTES=1440
DEDUP_SIMILARITY_THRESHOLD=0.82
EVENT_MEMORY_WINDOW_MINUTES=4320
EVENT_UPDATE_MIN_NOVELTY=62
EVENT_MAX_UPDATES_PER_24H=2
EVENT_SIMILARITY_THRESHOLD=0.62

EVENT_CLUSTER_WINDOW_MINUTES=90
EVENT_MAX_ITEMS_PER_CLUSTER=6
EVENT_MAX_OUTPUT_ITEMS=6

MAX_IMAGE_BYTES=10485760
MAX_VIDEO_BYTES=47185920
```

## زمان‌بندی انتشار

همان برنامه نسخه قبل حفظ شده است:

- 08:00 تا قبل از 14:00 → هر 60 دقیقه
- 14:00 تا قبل از 24:00 → هر 45 دقیقه
- 00:00 تا 07:59 → ساعت سکوت

حجم صف این فاصله را کمتر نمی‌کند.

## سبد محتوا

همان پنجره چرخشی 20 پستی نسخه قبلی حفظ شده است:

- ایران: 40٪ هدف
- جنگ/درگیری خاورمیانه: 20٪
- تحولات خاورمیانه: 10٪
- روسیه-اوکراین: 10٪
- نظامی/دفاعی آمریکا، روسیه و چین: 20٪

این درصدها هدف rolling window هستند و فقط زمانی قابل رسیدن‌اند که ورودی مناسب از منابع وجود داشته باشد.

## رسانه

- رسانه و متن در یک پیام Telegram می‌مانند.
- اگر ویدیو خراب باشد → عکس/متن
- اگر عکس خراب باشد → متن
- `PHOTO_INVALID_DIMENSIONS` باعث گم شدن خبر نمی‌شود.
- Telegram 429 به fallback رسانه تبدیل نمی‌شود؛ چون آن خطا محدودیت نرخ Telegram است، نه خرابی فایل.

## TEST_MODE

برای تست مسیر پردازش بدون انتشار واقعی:

```text
TEST_MODE=true
```

برای انتشار واقعی:

```text
TEST_MODE=false
```

در حالت تست، خبرها از نظر صف «موفق» تلقی می‌شوند؛ بنابراین برای تست محدود از آن استفاده کن و قبل از اجرای واقعی آن را `false` کن.

## نکته درباره کلیدها

می‌توانی برای هر provider چند API key قرار دهی و با کاما جدا کنی. سیستم داخل همان provider کلیدها را مدیریت می‌کند و بعد از cooldown به provider بعدی می‌رود.

برای Gemini داشتن چند key از یک Project به‌تنهایی quota پروژه را چند برابر نمی‌کند؛ v4 عمداً بعد از 429 همه keyهای آن provider را بی‌جهت پشت‌سرهم امتحان نمی‌کند.

## نصب

requirements نسخه v4 عمداً همان سه کتابخانه نسخه پایه را نگه می‌دارد:

```text
requests>=2.31.0,<3
beautifulsoup4>=4.12.3,<5
tzdata>=2025.2
```

فایل `state.json` را روی Railway Volume در مسیر `/data/state.json` نگه دار تا حافظه رویدادها، تکرارها، cooldownها و صف بعد از restart از بین نروند.

## مدیریت سه موتور

در حالت عادی فقط Stage 1 با ترتیب Groq → Gemini → Mistral شروع می‌شود و 429 یک provider باعث تلاش بی‌پایان روی همان provider نمی‌شود. Stage 2 فقط برای مواردی که واقعاً به ویراستاری دوباره نیاز دارند فعال می‌شود و از provider بعدی شروع می‌کند؛ بنابراین Gemini و Mistral هم نقش پشتیبان دارند و هم در موارد حساس به‌عنوان ویراستار دوم به کار می‌روند.
