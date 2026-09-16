# Raptor Military News Bot

ربات گردآوری، پالایش، بازنویسی و انتشار خودکار اخبار نظامی برای Telegram.

## معماری

```text
Telegram source channels ─┐
                          ├─> Ingest ─> AI classification ─> Event clustering ─> Priority queue
                                                                 ├─> source media
                                                                 └─> image fallback
                                                                          │
                                                                          v
                                                               Gemini editorial rewrite
                                                                          │
                                                                          v
                                                       Scheduler (30–60 min / Breaking)
                                                                          │
                                                                          v
                                                               Telegram target channel
```

## قابلیت‌های اصلی

- هر خبر به یک پست مستقل تبدیل می‌شود.
- متن از ابتدا به فارسی و با ادبیات نظامی بازنویسی می‌شود؛ ترجمه تحت‌اللفظی انجام نمی‌شود.
- اطلاعات مهم، عددها، سامانه‌ها، هواگردها، شناورها، مکان‌ها و طرف‌های درگیر حفظ می‌شوند.
- ادعا و گزارش تأییدنشده با انتساب مناسب بیان می‌شود.
- تبلیغات و مطالب نامرتبط فیلتر می‌شوند.
- تکرارهای یک واقعه با `event_key` و خوشه‌بندی داخلی به یک جریان خبری تبدیل می‌شوند.
- برای یک واقعه که پشت سرهم گزارش می‌شود، اطلاعات جدید به candidate موجود افزوده می‌شود تا چند پست کوچک پشت‌سرهم ساخته نشود.
- اولویت داخلی بر اساس اهمیت نظامی، وضعیت درگیری/حمله، ایران، خاورمیانه و قدرت‌های بزرگ تنظیم می‌شود؛ اولویت فقط برای زمان‌بندی است و به مخاطب نمایش داده نمی‌شود.
- فاصله عادی انتشار ۶۰ دقیقه است و با بزرگ شدن صف به ۵۵، ۴۵ و در صف‌های بزرگ‌تر به ۳۰ دقیقه کاهش می‌یابد؛ هرگز کمتر از ۳۰ دقیقه نمی‌شود.
- Breaking News می‌تواند در سکوت شبانه یا خارج از فاصله عادی منتشر شود.
- ساعات سکوت عادی: ۰۰:۰۰ تا ۰۸:۰۰ به وقت ایران.
- خبرهای شبانه از بین نمی‌روند؛ خبرهای بااهمیت داخل صف می‌مانند و خبرهای کم‌اهمیت هنگام نیمه‌شب منقضی می‌شوند.
- خبرهای شب گذشته که صبح منتشر می‌شوند با دستورالعمل «مربوط به شب گذشته» بازنویسی می‌شوند.
- عکس/ویدئوی منبع در اولویت است. اگر رسانه منبع در دسترس نبود، Wikimedia Commons به‌عنوان fallback بررسی می‌شود.
- چند رسانه مرتبط در قالب album تا سقف تنظیم‌شده قابل انتشار است.
- صبح ساعت ۰۸:۰۰ و شب ساعت ۲۴:۰۰ پیام ثابت همراه تصویر نظامی ارسال می‌شود.
- SQLite + WAL برای تحمل restart و جلوگیری از انتشار دوباره.
- health endpoint برای Railway.
- فرمان‌های ادمین: `/status`, `/queue`, `/pause`, `/resume`, `/clearqueue`.
- مدیریت ۲۹ کلید Gemini به شکل pool؛ محدودیت هم‌زمانی، چرخش کلید، cooldown و retry مستقل.

## نکته امنیتی بسیار مهم

هیچ Bot Token، Gemini API Key، Telegram API Hash یا Telethon Session در GitHub قرار نده.

توکن و کلیدهایی که در چت وارد شده‌اند را قبل از استفاده عملی **Rotate/Revoke** کن، چون دیگر نباید محرمانه فرض شوند.

## راه‌اندازی

### 1) ساخت Telethon Session

برای خواندن کانال‌های منبع، علاوه بر Bot Token به `API_ID` و `API_HASH` اکانت Telegram و یک StringSession نیاز است. Telethon رسماً `iter_messages` و `download_media` را پشتیبانی می‌کند.

روی کامپیوتر شخصی:

```bash
pip install -r requirements.txt
export TELEGRAM_API_ID=12345678
export TELEGRAM_API_HASH=YOUR_API_HASH
python tools/create_telethon_session.py
```

کدی که چاپ می‌شود را به عنوان `TELETHON_SESSION` در Railway قرار بده.

### 2) Railway Variables

`.env.example` را به عنوان مرجع استفاده کن.

متغیرهای ضروری:

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_TARGET_CHANNEL=@khaatshekaan
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELETHON_SESSION=...
TELEGRAM_SOURCE_CHANNELS=https://t.me/MaxOsintIntel,https://t.me/OSINTWarfare,https://t.me/defenseexpress_ua,https://t.me/WarPeaceAndYou,https://t.me/RazmAvaran_org
GEMINI_API_KEYS=key1,key2,key3,...,key29
```

سایر متغیرها در `.env.example` مقدار پیش‌فرض دارند.

### 3) منابع خبر

این نسخه عمداً فقط از کانال‌های تلگرام تعریف‌شده در `TELEGRAM_SOURCE_CHANNELS` تغذیه می‌کند. هیچ سایت خبری یا RSS در معماری فعال نیست و خالی بودن فهرست سایت‌ها هیچ مشکلی ایجاد نمی‌کند.

### 4) اجرای محلی

```bash
pip install -r requirements.txt
cp .env.example .env
python -m app.main
```

### 5) اجرای Railway

Railway را به repository وصل کن. `Dockerfile` یا command زیر کافی است:

```bash
python -m app.main
```

`PORT` توسط Railway تأمین می‌شود و health endpoint روی `/health` فعال است.

## رفتار Gemini و خطاهای 503/429

ربات برای جلوگیری از شکست یک job به‌علت خطای موقت Gemini این اقدامات را انجام می‌دهد:

1. تعداد درخواست‌های هم‌زمان محدود است.
2. کلیدها round-robin می‌شوند.
3. کلیدی که 429/503 می‌دهد برای مدتی وارد cooldown می‌شود.
4. برای خطاهای transient تا تعداد مشخص retry انجام می‌شود.
5. retryها exponential backoff + jitter دارند.
6. مدل اصلی و مدل‌های fallback در صورت 404/عدم دسترسی قابل تنظیم‌اند.
7. شکست یک candidate کل صف را متوقف نمی‌کند؛ پس از چند شکست، candidate وارد وضعیت failed می‌شود.

نکته: تعداد کلیدها لزوماً ظرفیت را ۲۹ برابر نمی‌کند؛ محدودیت‌های Gemini ممکن است در سطح **project** اعمال شوند. بنابراین اگر همه ۲۹ کلید متعلق به یک project باشند، rate limit آن project همچنان می‌تواند عامل محدودکننده باشد.

## قانون انتشار

### عادی

- ۰۸:۰۰ تا ۲۴:۰۰: صف فعال است.
- فاصله پایه: ۶۰ دقیقه.
- صف بزرگ‌تر: فاصله به‌تدریج تا ۳۰ دقیقه کاهش می‌یابد.
- FIFO مطلق نیست؛ priority بالاتر جلو می‌آید.
- خبرهای عادی کم‌اهمیت در صورت ماندن تا نیمه‌شب حذف می‌شوند.

### Breaking

یک candidate با `urgency=breaking` می‌تواند خارج از فاصله عادی و حتی در سکوت شبانه منتشر شود، اما فقط پس از عبور از فیلتر نظامی/خبری و تعیین اهمیت توسط AI.

## قالب پست

پایان هر پست همیشه دقیقاً:

```text
#raptor
————————
@khaatshekaan
```

## فرمان‌های ادمین

بعد از تنظیم `ADMIN_USER_IDS`:

- `/status` وضعیت صف، تعداد انتشار و فاصله فعلی را نشان می‌دهد.
- `/queue` صف فعلی را نشان می‌دهد.
- `/pause` فقط انتشار را متوقف می‌کند؛ ingest و پردازش ادامه دارند.
- `/resume` انتشار را دوباره فعال می‌کند.
- `/clearqueue` candidateهای queued را منقضی می‌کند.

## محدودیت مهم Telegram

برای خواندن تاریخچه، دریافت محتوای کانال و دانلود media از source channel، این پروژه از Telethon user session استفاده می‌کند. Bot API برای این بخش به‌تنهایی کافی نیست.

برای انتشار در کانال مقصد، Bot API استفاده می‌شود و ربات باید مجوز لازم برای ارسال در کانال مقصد را داشته باشد.

## تست پایه

قبل از commit:

```bash
python -m compileall app tools
```

بعد در Railway لاگ startup را بررسی کن. اولین خطای مهم باید به تنظیمات محیطی، session یا دسترسی Telegram مربوط باشد؛ secretها در کد وجود ندارند.
