# Raptor Telegram News Bot — v4.5

نسخه 4.5 روی همان معماری v4 ساخته شده و تمرکز آن روی **کیفیت تحریریه، دقت ترجمه، کاهش خطاهای factual، کاهش متن‌های کلیشه‌ای، ارزش خبری و انتخاب امن‌تر رسانه** است. هسته‌های اصلی v4 مثل صف، زمان‌بندی، dedup/event memory، failover و fallback رسانه حفظ شده‌اند.

## مهم‌ترین تغییرات v4.5

### 1) نگارش فارسی طبیعی‌تر
مدل دیگر قرار نیست برای همه پست‌ها از یک قالب ثابت مثل:

```text
بر اساس گزارش‌های اولیه...
...
📌 این موضوع نشان می‌دهد...
```

استفاده کند.

ساختار تیتر و پاراگراف‌ها بر اساس نوع خبر تغییر می‌کند و عبارت‌های تکراری به‌صورت صریح محدود شده‌اند.

### 2) حذف تحلیل کلیشه‌ای انتهای پست
فیلد significance از انتشار عمومی حذف شده است.

یعنی دیگر ربات در انتهای تقریباً همه پست‌ها یک جمله عمومی از جنس «این موضوع نشان‌دهنده...» یا `📌` اضافه نمی‌کند.

چرا مهم است، فقط وقتی داده کافی وجود داشته باشد، داخل خود متن خبر و به‌صورت طبیعی نوشته می‌شود.

### 3) کنترل ترجمه و اصطلاحات نظامی
یک واژه‌نامه داخلی برای اصطلاحات رایج نظامی اضافه شده است؛ مانند:

```text
air defense → پدافند هوایی
surface-to-air missile → موشک زمین‌به‌هوا
electronic warfare → جنگ الکترونیک
loitering munition → مهمات سرگردان
infantry fighting vehicle → خودروی رزمی پیاده‌نظام
main battle tank → تانک اصلی میدان نبرد
```

نام مدل‌ها و designationهای نظامی مثل `F-16V`، `B-52` و `MiG-29` نباید ترجمه یا خراب شوند.

برای نام یا اصطلاح نامطمئن، نگه داشتن شکل لاتین امن‌تر از ساختن آوانویسی عجیب است.

### 4) کنترل املا و نیم‌فاصله
لایه آخر ویرایش فارسی، غلط‌های رایج نگارشی و شکل‌های نامناسبی مثل `میتواند`، `میشود`، `تایید` و موارد مشابه را به شکل استاندارد اصلاح می‌کند.

این لایه عمداً محافظه‌کار است تا نام‌های خاص را خراب نکند.

### 5) امتیاز واقعی ارزش خبری
هر خبر توسط AI از 0 تا 100 امتیاز `news_score` می‌گیرد:

- اهمیت نظامی/امنیتی: 25
- اعتبار و کیفیت شواهد موجود: 25
- تازگی: 15
- اثر ژئوپلیتیکی/عملیاتی: 15
- جذابیت و ارزش آموزشی: 20

قاعده انتشار:

```text
کمتر از 50      → حذف
50 تا 64       → فقط وقتی گزینه 65+ در صف نباشد
65 تا 79        → قابل انتشار
80 تا 100       → اولویت بالا
```

این به حذف مواردی مثل مشاهده مبهم دود/نور/صدای انفجار بدون جزئیات کافی کمک می‌کند و جلوی این را می‌گیرد که یک observation ضعیف با یک جمله حدسی به خبر بزرگ تبدیل شود.

### 6) تفکیک خبر از تحلیل
- اتفاق اصلی باید در تیتر و جمله اول مشخص باشد.
- جزئیات فقط از ورودی بیاید.
- وضعیت تأیید/ادعا به‌صورت طبیعی بیان شود.
- تحلیل مستقل و کلیشه‌ای به انتهای پست چسبانده نمی‌شود.
- برای خبرهای حساس، `fact_check` و `needs_editor` می‌تواند فعال شود.

### 7) تنوع فرمت
سبک‌های داخلی شامل:

```text
breaking
brief
standard
important
conflict
geopolitics
defense_tech
fact_check
comparison
context
iran
analysis
```

مدل باید style را بر اساس خود خبر انتخاب کند و تیترهای پشت‌سرهم شبیه هم نسازد.

### 8) کنترل انتخاب عکس
در v4.5 ربات **به‌صورت پیش‌فرض عکس عمومی Wikimedia را برای یک خبر انتخاب نمی‌کند**.

اولویت:

```text
رسانه همان پست منبع
        ↓
og:image همان منبع
        ↓
بدون عکس
```

مدل می‌تواند `media_post_index` بدهد تا کد فقط رسانه همان ورودی را برای خبر انتخاب کند.

فقط در صورتی که عمداً این قابلیت را فعال کنی:

```text
ALLOW_GENERIC_IMAGE_FALLBACK=true
```

جست‌وجوی عمومی تصویر فعال می‌شود.

### 9) Groq و JSON
Groq برای `openai/gpt-oss-120b` اکنون از Structured Outputs با Strict JSON Schema استفاده می‌کند. همچنین:

```text
max_completion_tokens
include_reasoning=false
reasoning_effort=low
```

در مسیر Groq استفاده می‌شود.

Schema خروجی عمداً نسبت به v4 ساده‌تر شده تا سطح پیچیدگی و احتمال validation failure پایین بیاید.

مستندات فعلی Groq می‌گویند `openai/gpt-oss-120b` از JSON Schema Mode پشتیبانی می‌کند و `strict=true` برای تطابق دقیق با schema استفاده می‌شود.

## معماری AI

```text
Groq → Gemini → Mistral
PRIMARY   BACKUP 1   BACKUP 2
```

Failover، cooldown، retry محدود و state پایدار v4 حفظ شده‌اند.

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

### تنظیمات AI و Editorial

```text
AI_EDITOR_ENABLED=true
AI_BATCH_MAX_POSTS=8
AI_EDITOR_MAX_ITEMS=6

AI_MAX_ATTEMPTS_PER_PROVIDER=2
AI_MIN_REQUEST_INTERVAL_SECONDS=1.2
AI_MAX_OUTPUT_TOKENS=2600

AI_PROVIDER_COOLDOWN_FLOOR_SECONDS=45
AI_429_MAX_COOLDOWN_SECONDS=600

AI_FAILURE_RETRY_MINUTES=10
AI_RETRY_MAX_ATTEMPTS=5
AI_RETRY_MAX_DELAY_MINUTES=120
AI_RETRY_QUARANTINE_MINUTES=1440

AI_INPUT_MAX_CHARS_PER_POST=2400
AI_RECENT_INPUT_WINDOW_MINUTES=1440
AI_LOCAL_DUP_THRESHOLD=0.94

MIN_NEWS_SCORE=50
PREFERRED_NEWS_SCORE=65

ALLOW_GENERIC_IMAGE_FALLBACK=false
```

### Dedup / Event

```text
DEDUP_WINDOW_MINUTES=1440
DEDUP_SIMILARITY_THRESHOLD=0.82
EVENT_MEMORY_WINDOW_MINUTES=4320
EVENT_UPDATE_MIN_NOVELTY=62
EVENT_MAX_UPDATES_PER_24H=2
EVENT_SIMILARITY_THRESHOLD=0.62

EVENT_CLUSTER_WINDOW_MINUTES=90
EVENT_MAX_ITEMS_PER_CLUSTER=6
EVENT_MAX_OUTPUT_ITEMS=6
```

### Media

```text
MAX_IMAGE_BYTES=10485760
MAX_VIDEO_BYTES=47185920
```

## زمان‌بندی انتشار

مثل نسخه‌های قبلی:

```text
08:00 تا قبل از 14:00 → هر 60 دقیقه
14:00 تا قبل از 24:00 → هر 45 دقیقه
00:00 تا 07:59 → ساعت سکوت
```

حجم صف این فاصله را کمتر نمی‌کند.

## نکته مهم درباره جهت‌گیری تحریریه

در این نسخه لحن می‌تواند **محترمانه و دقیق نسبت به نام‌ها و عناوین رسمی** باشد، اما سامانه عمداً ادعاهای سیاسی را تبلیغاتی یا جانب‌دارانه نمی‌کند. تمرکز روی خبر دقیق، ترجمه طبیعی و تفکیک ادعا از واقعیت است.

## State روی Railway

برای حفظ صف، dedup، event memory و cooldownها، مسیر زیر را روی Railway Volume نگه دار:

```text
/data/state.json
```

## TEST_MODE

برای تست بدون انتشار:

```text
TEST_MODE=true
```

برای انتشار واقعی:

```text
TEST_MODE=false
```

