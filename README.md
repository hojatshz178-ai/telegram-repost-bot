# Raptor Telegram News Bot — v3.8

نسخه 3.8 بر پایه نسخه 3.5 ساخته شده و قوانین اصلی جمع‌آوری، فیلتر، خوشه‌بندی، صف و زمان‌بندی را حفظ می‌کند.

## اصلاحات اصلی v3.8

### 1) خطای رسانه و fallback
- اگر دریافت/ارسال عکس یا ویدیو شکست بخورد، ربات همان خبر را به‌صورت متن منتشر می‌کند.
- خطای `PHOTO_INVALID_DIMENSIONS` باعث از دست رفتن خبر نمی‌شود.
- اگر Telegram خطای 429 بدهد، fallback رسانه به متن انجام نمی‌شود؛ چون 429 مربوط به محدودیت نرخ است.
- متن رسانه‌ای شامل تیتر + بدنه + footer در همان یک پیام است.
- caption رسانه در سقف 1024 کاراکتر نگه داشته می‌شود و بدنه در صورت نیاز کوتاه می‌شود.

### 2) کاهش شدید تلاش‌های تکراری Gemini
در نسخه‌های قبلی، اگر کلیدها 429 می‌گرفتند، انتخاب fallback می‌توانست دوباره نزدیک‌ترین کلید cooldown‌شده را امتحان کند و تعداد تلاش‌ها بسیار بالا برود.

در v3.8:
- بعد از 429 یک **cooldown سراسری Gemini** ثبت می‌شود.
- کلیدهای دیگر بلافاصله برای همان درخواست امتحان نمی‌شوند.
- در هر درخواست حداکثر 3 تلاش انجام می‌شود.
- وضعیت cooldown فوراً روی state ذخیره می‌شود.
- فاصله حداقلی بین درخواست‌های Gemini اعمال می‌شود.
- تعداد output token محدود شده تا پاسخ‌های غیرضروری مصرف را بالا نبرند.

نکته: محدودیت‌های Gemini در سطح Project اعمال می‌شوند؛ بنابراین داشتن چند API key از یک Project لزوماً ظرفیت Project را چند برابر نمی‌کند.

### 3) خروجی طبیعی‌تر
- برچسب‌هایی مثل `🛰️ دفاعی |`، `🌍 ژئوپلیتیک |` و `⚠️ گزارش اولیه` دیگر به‌صورت جداگانه به پست اضافه نمی‌شوند.
- تیتر مستقیم منتشر می‌شود.
- وضعیت گزارش، در صورت نیاز، داخل نثر طبیعی خبر بیان می‌شود.

### 4) TEST_MODE
برای آزمایش قبل از انتشار واقعی:

```text
TEST_MODE=true
```

در این حالت ربات مسیر انتخاب و صف را اجرا می‌کند ولی پیام واقعی در کانال ارسال نمی‌کند.

برای اجرای واقعی:

```text
TEST_MODE=false
```

## Variables اصلی

```text
BOT_TOKEN=...
TARGET_CHAT_ID=@khaatshekaan
SOURCE_CHANNELS=MaxOsintIntel,OSINTWarfare,defenseexpress_ua,WarPeaceAndYou,RazmAvaran_org
GEMINI_API_KEYS=...
GEMINI_MODEL=gemini-3.8-flash
POLL_INTERVAL_SECONDS=180
DEDUP_WINDOW_MINUTES=360
DEDUP_SIMILARITY_THRESHOLD=0.82
EVENT_CLUSTER_WINDOW_MINUTES=90
EVENT_MAX_ITEMS_PER_CLUSTER=6
EVENT_MAX_OUTPUT_ITEMS=6
GEMINI_BATCH_MAX_POSTS=6
GEMINI_INPUT_MAX_CHARS_PER_POST=2200
GEMINI_RECENT_INPUT_WINDOW_MINUTES=720
GEMINI_LOCAL_DUP_THRESHOLD=0.88
STATE_FILE=/data/state.json
TEST_MODE=false
```

### Variables اختیاری برای کنترل مصرف Gemini

```text
GEMINI_MAX_ATTEMPTS_PER_REQUEST=3
GEMINI_MIN_REQUEST_INTERVAL_SECONDS=6
GEMINI_MAX_OUTPUT_TOKENS=900
GEMINI_GLOBAL_COOLDOWN_FLOOR_SECONDS=60
```

مقادیر بالا پیش‌فرض دارند و لازم نیست حتماً در Railway اضافه شوند.

## requirements

همان requirements نسخه 3.5 است و نیاز به کتابخانه جدیدی ندارد.
