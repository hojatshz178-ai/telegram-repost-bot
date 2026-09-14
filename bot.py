import os
import re
import json
import time
import html
import logging
import difflib
from datetime import datetime
from urllib.parse import quote

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # پایتون‌های خیلی قدیمی
    ZoneInfo = None

try:
    import feedparser
except ImportError:
    feedparser = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("repost-bot")

# ---------- تنظیمات (از Environment Variables خونده می‌شن) ----------
SOURCE_CHANNELS = [c.strip().lstrip("@") for c in os.environ.get("SOURCE_CHANNELS", "").split(",") if c.strip()]
SOURCE_WEBSITES = [u.strip() for u in os.environ.get("SOURCE_WEBSITES", "").split(",") if u.strip()]
TARGET_CHAT_ID = os.environ.get("TARGET_CHAT_ID", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
DEDUP_WINDOW_MINUTES = int(os.environ.get("DEDUP_WINDOW_MINUTES", "240"))
DEDUP_SIMILARITY_THRESHOLD = float(os.environ.get("DEDUP_SIMILARITY_THRESHOLD", "0.6"))
STATE_FILE = "/data/state.json" if os.path.isdir("/data") else "state.json"

GEMINI_MODEL = "gemini-flash-latest"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

FOOTER = "#raptor\n————————\n@khaatshekaan"
TEHRAN_TZ = ZoneInfo("Asia/Tehran") if ZoneInfo else None

MORNING_MESSAGE = "🌅 صبح بخیر به همراهان کانال\nروزتون پر از آرامش و اخبار دقیق باشه 🫡\n\n" + FOOTER
NIGHT_MESSAGE = "🌙 شب بخیر رپتوری‌های عزیز\nفردا با اخبار تازه در خدمتتون هستیم 🛡️\n\n" + FOOTER

REWRITE_PROMPT = """تو یک خبرنگار حرفه‌ای حوزه‌ی نظامی، امنیتی و ژئوپلیتیکی هستی که برای یک کانال تلگرامی گزارش می‌نویسی.

متن ورودی زیر یک محتوای خام از منبع «{source_name}» است.

مرحله ۱ - تشخیص ارتباط:
مشخص کن آیا این محتوا واقعاً خبر یا تحلیل نظامی/امنیتی/تسلیحاتی/عملیاتی/ژئوپلیتیکی مهم است یا نه.
اگر پست تبلیغاتی، متفرقه، غیرمرتبط، یا محتوای بی‌ارزش برای یک کانال میلیتاری است، آن را نامرتبط (relevant=false) علامت بزن.

مرحله ۲ - بازنویسی (فقط اگر relevant=true):
۱. متن را ترجمه‌ی تحت‌اللفظی نکن؛ کاملاً از نو و با ادبیات خودت بازنویسی کن، طوری که اصلاً شبیه ترجمه‌ی ماشینی یا کپی متن منبع نباشد. اطلاعات خبر باید کاملاً حفظ شود.
۲. سبک نوشتار: خبری، دقیق، حرفه‌ای، تحلیلی اما کوتاه، جذاب برای مخاطب تلگرام، بدون ادبیات زرد یا اغراق‌آمیز، بدون جملات تبلیغاتی یا شعاری.
۳. تمرکز روی: چه اتفاقی افتاده، کجا، چه طرف‌هایی درگیرند، چه سلاح/سامانه/هواگرد/شناور/تجهیزاتی استفاده شده، اعداد و ارقام مهم، پیامد یا اهمیت راهبردی.
۴. طول متن (بدون تیتر و بدون امضا): حدود ۱۰۰ تا ۱۸۰ کلمه، مگر خبر اطلاعات مهم زیادی داشته باشد که در این صورت فقط اطلاعات ضروری را نگه دار.
۵. اگر خبر شامل ادعا، اتهام یا اطلاعات تأییدنشده است، آن را قطعی جلوه نده؛ از عباراتی مثل «بر اساس گزارش‌ها»، «به گفته منابع»، «این گروه مدعی شده»، «در صورت تأیید» استفاده کن.
۶. نام منبع (رسانه/ارتش/وزارت دفاع/مقام رسمی) را فقط در صورت لزوم برای اعتبار یا فهم خبر بیاور.
۷. از ۲ تا ۳ ایموجی رسمی و مرتبط با موضوع استفاده کن؛ نه بیشتر، و از پرکردن متن با ایموجی خودداری کن.
۸. تیتر باید کوتاه، جذاب و خبری/نظامی باشد (نه هیجانی یا اغراق‌آمیز مثل «فاجعه» یا «وحشتناک» مگر خود خبر واقعاً چنین چیزی را ثابت کند).
۹. ساختار متن: پاراگراف اول = اصل خبر و مهم‌ترین اتفاق. پاراگراف دوم = جزئیات (اعداد، سامانه‌ها، مکان، طرف‌های درگیر). پاراگراف سوم (در صورت نیاز) = اهمیت نظامی یا پیامد احتمالی. از بولت‌پوینت فقط برای فهرست چند عدد/مشخصات مهم استفاده کن.
۱۰. هیچ اطلاعات مهمی را بدون دلیل حذف نکن، ولی جزئیات کم‌اهمیت و تکراری را حذف کن. چیزی از خودت به‌عنوان واقعیت اضافه نکن.
۱۱. اگر ورودی حاوی چند خبر جداگانه بود، فقط مهم‌ترین/اولین خبر را پردازش کن (هر خبر پست جداگانه‌ی خودش را دارد).

خروجی را دقیقاً و فقط به‌شکل یک آبجکت JSON معتبر برگردان (بدون Markdown، بدون بک‌تیک، بدون توضیح اضافه):
{{
  "relevant": true یا false,
  "title": "تیتر کوتاه فارسی (اگر relevant=false رشته خالی)",
  "body": "متن کامل بازنویسی‌شده شامل پاراگراف‌ها، بدون تکرار تیتر در ابتدای متن (اگر relevant=false رشته خالی)",
  "image_query": "۳ تا ۵ کلمه‌ی انگلیسی کوتاه برای جستجوی یک عکس استوک مرتبط با موضوع خبر (اگر relevant=false رشته خالی)"
}}

متن ورودی:
---
{content}
---"""


# ---------- state ----------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- گرفتن پست از کانال‌های تلگرام ----------
def fetch_channel_posts(channel):
    """صفحه‌ی پیش‌نمایش عمومی کانال رو می‌گیره و پست‌ها (متن + عکس/فیلم) رو استخراج می‌کنه."""
    url = f"https://t.me/s/{channel}"
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"خطا در گرفتن کانال {channel}: {e}")
        return []

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

        photo_url = None
        photo_match = re.search(
            r'tgme_widget_message_photo_wrap[^"]*"\s+style="[^"]*background-image:url\(\'([^\']+)\'\)', block
        )
        if photo_match:
            photo_url = html.unescape(photo_match.group(1))

        video_url = None
        video_match = re.search(r'<video[^>]*class="[^"]*tgme_widget_message_video[^"]*"[^>]*src="([^"]+)"', block)
        if video_match:
            video_url = html.unescape(video_match.group(1))

        if text or photo_url or video_url:
            posts.append({
                "uid": f"tg:{channel}:{msg_id}",
                "text": text,
                "photo": photo_url,
                "video": video_url,
                "source_name": f"کانال {channel}",
            })
    return posts


# ---------- گرفتن مطالب از سایت‌های خبری (RSS) ----------
def fetch_website_posts(feed_url):
    if feedparser is None:
        log.error("کتابخانه feedparser نصب نیست؛ نمی‌توان فید سایت را خواند.")
        return []
    try:
        parsed = feedparser.parse(feed_url)
    except Exception as e:
        log.warning(f"خطا در خواندن فید {feed_url}: {e}")
        return []

    posts = []
    for entry in parsed.entries[:20]:
        uid = f"web:{feed_url}:{entry.get('id') or entry.get('link')}"
        title = entry.get("title", "").strip()
        summary = entry.get("summary", "") or entry.get("description", "")
        summary = re.sub(r"<[^>]+>", " ", summary)
        summary = html.unescape(summary).strip()
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
                "video": None,
                "source_name": parsed.feed.get("title", feed_url),
            })
    return posts


# ---------- Gemini ----------
def analyze_and_rewrite(text, source_name):
    headers = {"Content-Type": "application/json", "X-goog-api-key": GEMINI_API_KEY}
    payload = {
        "contents": [{"parts": [{"text": REWRITE_PROMPT.format(content=text, source_name=source_name)}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    resp = requests.post(GEMINI_API_URL, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise ValueError(f"پاسخ نامعتبر از Gemini: {data}")
    parts = candidates[0].get("content", {}).get("parts", [])
    raw = "\n".join(p.get("text", "") for p in parts).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise ValueError(f"خروجی Gemini قابل‌پارس نبود: {raw[:300]}")


def find_stock_image(query):
    """تلاش برای گرفتن یک عکس استوک مرتبط از Unsplash Source (بدون کلید API)."""
    if not query:
        return None
    try:
        url = f"https://source.unsplash.com/1600x900/?{quote(query)}"
        resp = requests.get(url, timeout=15, allow_redirects=True)
        if resp.ok and resp.url and "unsplash" in resp.url:
            return resp.url
    except Exception as e:
        log.warning(f"خطا در گرفتن عکس استوک: {e}")
    return None


# ---------- ارسال به تلگرام ----------
def send_to_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TARGET_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
    resp = requests.post(url, json=payload, timeout=20)
    if not resp.ok:
        log.error(f"خطا در ارسال پیام: {resp.text}")
    resp.raise_for_status()


def send_photo_to_telegram(caption, photo_url):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    payload = {"chat_id": TARGET_CHAT_ID, "photo": photo_url, "caption": caption[:1024], "parse_mode": "HTML"}
    resp = requests.post(url, json=payload, timeout=30)
    if not resp.ok:
        log.error(f"خطا در ارسال عکس: {resp.text}")
    resp.raise_for_status()


def send_video_to_telegram(caption, video_url):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
    payload = {"chat_id": TARGET_CHAT_ID, "video": video_url, "caption": caption[:1024], "parse_mode": "HTML"}
    resp = requests.post(url, json=payload, timeout=60)
    if not resp.ok:
        log.error(f"خطا در ارسال فیلم: {resp.text}")
    resp.raise_for_status()


def build_final_message(title, body):
    parts = []
    if title:
        parts.append(f"<b>{html.escape(title)}</b>")
    if body:
        parts.append(body)
    parts.append(FOOTER)
    return "\n\n".join(parts)


# ---------- جلوگیری از پست تکراری ----------
def is_duplicate(state, title):
    now = time.time()
    recent = state.get("_recent_titles", [])
    recent = [r for r in recent if now - r["ts"] <= DEDUP_WINDOW_MINUTES * 60]
    for r in recent:
        ratio = difflib.SequenceMatcher(None, title, r["title"]).ratio()
        if ratio >= DEDUP_SIMILARITY_THRESHOLD:
            return True
    return False


def remember_title(state, title):
    now = time.time()
    recent = state.get("_recent_titles", [])
    recent = [r for r in recent if now - r["ts"] <= DEDUP_WINDOW_MINUTES * 60]
    recent.append({"title": title, "ts": now})
    state["_recent_titles"] = recent[-200:]


# ---------- پردازش یک منبع ----------
def process_posts(state, source_key, posts):
    seen_ids = set(state.get(source_key, []))
    new_posts = [p for p in posts if p["uid"] not in seen_ids]

    if source_key not in state:
        state[source_key] = [p["uid"] for p in posts]
        save_state(state)
        log.info(f"منبع {source_key} برای اولین‌بار ثبت شد، از پست بعدی پردازش می‌شود.")
        return

    for post in new_posts:
        uid = post["uid"]
        text = post["text"]
        log.info(f"در حال پردازش {uid}...")

        try:
            if text:
                result = analyze_and_rewrite(text, post["source_name"])
            else:
                result = {"relevant": False}

            if not result.get("relevant"):
                log.info(f"{uid} نامرتبط تشخیص داده شد و رد شد.")
            else:
                title = (result.get("title") or "").strip()
                body = (result.get("body") or "").strip()
                image_query = (result.get("image_query") or "").strip()

                if is_duplicate(state, title):
                    log.info(f"{uid} به‌عنوان پست تکراری (رویداد مشابه اخیر) رد شد.")
                else:
                    final_msg = build_final_message(title, body)
                    photo_url = post.get("photo")
                    video_url = post.get("video")
                    if not photo_url and not video_url and image_query:
                        photo_url = find_stock_image(image_query)

                    sent = False
                    if video_url:
                        try:
                            send_video_to_telegram(final_msg, video_url)
                            sent = True
                        except Exception as e:
                            log.warning(f"ارسال فیلم {uid} ناموفق بود: {e}")
                    if not sent and photo_url:
                        try:
                            send_photo_to_telegram(final_msg, photo_url)
                            sent = True
                        except Exception as e:
                            log.warning(f"ارسال عکس {uid} ناموفق بود: {e}")
                    if not sent:
                        send_to_telegram(final_msg)

                    remember_title(state, title)
                    log.info(f"{uid} با موفقیت پست شد.")
        except Exception as e:
            log.error(f"خطا در پردازش {uid}: {e}")

        state[source_key] = state.get(source_key, []) + [uid]
        state[source_key] = state[source_key][-300:]
        save_state(state)
        time.sleep(3)


# ---------- پیام‌های زمان‌بندی‌شده ----------
def check_scheduled_messages(state):
    if not TEHRAN_TZ:
        return
    now = datetime.now(TEHRAN_TZ)
    today_str = now.strftime("%Y-%m-%d")

    if now.hour == 7 and state.get("_last_morning_date") != today_str:
        try:
            send_to_telegram(MORNING_MESSAGE)
            state["_last_morning_date"] = today_str
            save_state(state)
            log.info("پیام صبح‌بخیر ارسال شد.")
        except Exception as e:
            log.error(f"خطا در ارسال پیام صبح‌بخیر: {e}")

    if now.hour == 0 and state.get("_last_night_date") != today_str:
        try:
            send_to_telegram(NIGHT_MESSAGE)
            state["_last_night_date"] = today_str
            save_state(state)
            log.info("پیام شب‌بخیر ارسال شد.")
        except Exception as e:
            log.error(f"خطا در ارسال پیام شب‌بخیر: {e}")


# ---------- حلقه‌ی اصلی ----------
def process_once(state):
    for channel in SOURCE_CHANNELS:
        posts = fetch_channel_posts(channel)
        if posts:
            process_posts(state, f"tg:{channel}", posts)

    for feed_url in SOURCE_WEBSITES:
        posts = fetch_website_posts(feed_url)
        if posts:
            process_posts(state, f"web:{feed_url}", posts)

    check_scheduled_messages(state)


def main():
    missing = [name for name, val in [
        ("BOT_TOKEN", BOT_TOKEN),
        ("TARGET_CHAT_ID", TARGET_CHAT_ID),
        ("GEMINI_API_KEY", GEMINI_API_KEY),
    ] if not val]
    if not SOURCE_CHANNELS and not SOURCE_WEBSITES:
        missing.append("SOURCE_CHANNELS یا SOURCE_WEBSITES (حداقل یکی)")
    if missing:
        log.error(f"این متغیرها تنظیم نشده‌اند: {', '.join(missing)}")
        return

    state = load_state()
    log.info(f"ربات شروع به کار کرد. کانال‌ها: {SOURCE_CHANNELS} | سایت‌ها: {len(SOURCE_WEBSITES)} فید")
    while True:
        try:
            process_once(state)
        except Exception as e:
            log.error(f"خطای عمومی در حلقه: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
