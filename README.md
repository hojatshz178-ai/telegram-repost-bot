# Raptor News Bot — نسخه نهایی

این نسخه فقط از کانال‌های عمومی تلگرام به‌عنوان **منبع خبر** استفاده می‌کند. بخش Website/RSS به‌طور کامل از مسیر اجرای ربات حذف شده و متغیر `SOURCE_WEBSITES` دیگر لازم نیست.

## تغییرات این نسخه

- رفع خطای `KeyError: '\n  "items"'` در `REWRITE_PROMPT`: به‌جای `.format()` فقط placeholderهای واقعی `content` و `source_name` جایگزین می‌شوند؛ بنابراین نمونه JSON داخل Prompt باعث خطا نمی‌شود.
- فقط کانال‌های تلگرام منبع خبر هستند.
- لینک یا نام منبع داخل پست نهایی کانال نمایش داده نمی‌شود.
- `source_note` و `source_url` در خروجی منتشرشده نمایش داده نمی‌شوند؛ اطلاعات آن‌ها فقط برای پردازش داخلی باقی می‌ماند.
- پیام‌های متنی طولانی به قطعات امن حداکثر ۴۰۰۰ کاراکتری تقسیم می‌شوند تا از سقف پیام تلگرام عبور نکنند.
- عبارت پایانی زیر فقط یک‌بار و در انتهای آخرین قطعه قرار می‌گیرد:

```text
#raptor
————————
@khaatshekaan
```

- برای عکس و ویدیو، کپشن زیر سقف ۱۰۲۴ کاراکتر نگه داشته می‌شود و ادامه متن در پیام‌های جداگانه ارسال می‌شود.
- چرخش چندکلیدی Gemini از طریق `GEMINI_API_KEYS` حفظ شده و هر تعداد کلید را می‌پذیرد.

## Railway → Variables

متغیرهای ضروری:

```text
BOT_TOKEN=توکن_ربات
TARGET_CHAT_ID=@khaatshekaan
SOURCE_CHANNELS=MaxOsintIntel,OSINTWarfare,defenseexpress_ua,WarPeaceAndYou,RazmAvaran_org
GEMINI_API_KEYS=KEY_01,KEY_02,KEY_03
```

کلیدهای Gemini باید با کاما از هم جدا شوند و داخل مقدار متغیر هیچ `curl`، کوتیشن یا خط جدیدی قرار نگیرد.

### متغیرهای اختیاری

```text
GEMINI_MODEL=gemini-3.8-flash
POLL_INTERVAL_SECONDS=180
DEDUP_WINDOW_MINUTES=360
DEDUP_SIMILARITY_THRESHOLD=0.82
EVENT_CLUSTER_WINDOW_MINUTES=90
EVENT_MAX_ITEMS_PER_CLUSTER=6
EVENT_MAX_OUTPUT_ITEMS=2
STATE_FILE=/data/state.json
```

`SOURCE_WEBSITES` را اصلاً در Railway اضافه نکن.

## Railway Volume

برای نگه‌داشتن `state.json` بعد از restart/deploy بهتر است یک Volume به `/data` متصل شود و این متغیر تنظیم شود:

```text
STATE_FILE=/data/state.json
```

## اجرا

```bash
python bot.py
```

## نکته امنیتی

توکن Telegram و کلیدهای Gemini اطلاعات محرمانه‌اند. آن‌ها را داخل GitHub، README یا `bot.py` قرار نده. چون کلیدها و توکن در متن گفتگو افشا شده‌اند، قبل از استفاده واقعی بهتر است آن‌ها را در سرویس مربوطه **تعویض/rotate** کنی و فقط مقادیر جدید را در Railway → Variables قرار دهی.
