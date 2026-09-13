import os
import re
import json
import time
import html
import logging
import requests
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("repost-bot")

# ---------- تنظیمات (از Environment Variables خونده می‌شن) ----------
SOURCE_CHANNELS = [c.strip().lstrip("@") for c in os.environ.get("SOURCE_CHANNELS", "").split(",") if c.strip()]
TARGET_CHAT_ID = os.environ.get("TARGET_CHAT_ID", "")          # مثلاً @mychannel یا -100123456789
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))  # هر ۵ دقیقه
STATE_FILE = "/data/seen_ids.json" if os.path.isdir("/data") else "seen_ids.json"

GEMINI_MODEL = "gemini-flash-latest"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

FOOTER = "#raptor\n————————\n@khaatshekaan"

REWRITE_PROMPT = """متن زیر یک پست خبری از یک کانال تلگرامی انگلیسی‌زبان است. آن را برای یک پست تلگرامی جدید بازآفرینی کن با این قواعد دقیق:

۱. محتوا را به فارسی برگردان و کاملاً بازنویسی کن — نباید هیچ شباهت جمله‌بندی یا ساختاری با متن اصلی یا ترجمه‌ی مستقیم آن داشته باشد؛ فقط اطلاعات و وقایع اصلی دقیقاً حفظ شوند.
۲. لحن نوشته باید شبیه یک گزارش نظامی/اطلاعاتی (Military Intelligence Briefing) باشد: دقیق، مستقیم، با کلمات کوتاه و ضربتی، بدون احساسات‌گرایی یا صفت‌های اضافه.
۳. کوتاه باشد — حداکثر ۴ تا ۶ خط — و فقط مهم‌ترین اطلاعات (چه کسی، چه اتفاقی، کجا، چه زمانی، پیامد) را بیاورد.
۴. در عین حال باید جذاب و خوانا برای یک پست تلگرامی باشد؛ می‌توانی از عناوین کوتاه یا خط اول ضربتی (مثل یک تیتر) استفاده کنی.
۵. فقط از ۱ تا ۲ ایموجی مرتبط با موضوع (مثل 🚨 ⚔️ 🛰️) در کل متن استفاده کن، نه بیشتر. از پر کردن متن با ایموجی خودداری کن.
۶. هیچ هشتگ یا امضایی داخل متن اضافه نکن — این بخش جداگانه اضافه خواهد شد.
۷. فقط متن نهایی فارسی را برگردان، بدون توضیح، بدون مقدمه، بدون گیومه دور متن.

متن اصلی:
---
{content}
---"""


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

    # پیدا کردن نقطه‌ی شروع هر پست
    starts = [m.start() for m in re.finditer(r'data-post="' + re.escape(channel) + r'/(\d+)"', html_text)]
    ids = re.findall(r'data-post="' + re.escape(channel) + r'/(\d+)"', html_text)

    posts = []
    for i, msg_id in enumerate(ids):
        start = starts[i]
        end = starts[i + 1] if i + 1 < len(starts) else len(html_text)
        block = html_text[start:end]

        # متن پست
        text = ""
        text_match = re.search(
            r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.DOTALL
        )
        if text_match:
            raw_text = text_match.group(1)
            text = re.sub(r"<br\s*/?>", "\n", raw_text)
            text = re.sub(r"<[^>]+>", "", text)
            text = html.unescape(text).strip()

        # عکس پست (از background-image استخراج می‌شه)
        photo_url = None
        photo_match = re.search(
            r'tgme_widget_message_photo_wrap[^"]*"\s+style="[^"]*background-image:url\(\'([^\']+)\'\)',
            block,
        )
        if photo_match:
            photo_url = html.unescape(photo_match.group(1))

        # فیلم پست (فقط اگه صفحه‌ی عمومی خود فایل رو در دسترس گذاشته باشه)
        video_url = None
        video_match = re.search(r'<video[^>]*class="[^"]*tgme_widget_message_video[^"]*"[^>]*src="([^"]+)"', block)
        if video_match:
            video_url = html.unescape(video_match.group(1))

        if text or photo_url or video_url:
            posts.append({
                "id": int(msg_id),
                "text": text,
                "photo": photo_url,
                "video": video_url,
            })
    return posts


def rewrite_with_gemini(text):
    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY,
    }
    payload = {
        "contents": [
            {"parts": [{"text": REWRITE_PROMPT.format(content=text)}]}
        ]
    }
    resp = requests.post(GEMINI_API_URL, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise ValueError(f"پاسخ نامعتبر از Gemini: {data}")
    parts = candidates[0].get("content", {}).get("parts", [])
    return "\n".join(p.get("text", "") for p in parts).strip()


def send_to_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TARGET_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    resp = requests.post(url, json=payload, timeout=20)
    if not resp.ok:
        log.error(f"خطا در ارسال به تلگرام: {resp.text}")
    resp.raise_for_status()


def send_photo_to_telegram(caption, photo_url):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    payload = {
        "chat_id": TARGET_CHAT_ID,
        "photo": photo_url,
        "caption": caption[:1024],  # محدودیت تلگرام برای کپشن رسانه
        "parse_mode": "HTML",
    }
    resp = requests.post(url, json=payload, timeout=30)
    if not resp.ok:
        log.error(f"خطا در ارسال عکس به تلگرام: {resp.text}")
    resp.raise_for_status()


def send_video_to_telegram(caption, video_url):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
    payload = {
        "chat_id": TARGET_CHAT_ID,
        "video": video_url,
        "caption": caption[:1024],
        "parse_mode": "HTML",
    }
    resp = requests.post(url, json=payload, timeout=60)
    if not resp.ok:
        log.error(f"خطا در ارسال فیلم به تلگرام: {resp.text}")
    resp.raise_for_status()


def build_final_message(rewritten_text):
    return f"{rewritten_text}\n\n{FOOTER}"


def process_once(state):
    for channel in SOURCE_CHANNELS:
        posts = fetch_channel_posts(channel)
        if not posts:
            continue

        seen_ids = set(state.get(channel, []))
        new_posts = [p for p in posts if p["id"] not in seen_ids]
        # اولین بار: فقط جدیدترین پست رو نشونه‌گذاری کن، همه‌ی آرشیو رو پست نکن
        if channel not in state:
            state[channel] = [p["id"] for p in posts]
            save_state(state)
            log.info(f"کانال {channel} برای اولین‌بار ثبت شد، از پست بعدی پردازش می‌شود.")
            continue

        for post in sorted(new_posts, key=lambda p: p["id"]):
            msg_id = post["id"]
            text = post["text"]
            photo_url = post["photo"]
            video_url = post["video"]

            log.info(f"پست جدید از {channel} (id={msg_id}) در حال پردازش...")
            try:
                rewritten = rewrite_with_gemini(text) if text else ""
                final_msg = build_final_message(rewritten) if rewritten else FOOTER

                sent = False
                if video_url:
                    try:
                        send_video_to_telegram(final_msg, video_url)
                        sent = True
                    except Exception as e:
                        log.warning(f"ارسال فیلم پست {msg_id} ناموفق بود، تلاش با متن ساده: {e}")
                if not sent and photo_url:
                    try:
                        send_photo_to_telegram(final_msg, photo_url)
                        sent = True
                    except Exception as e:
                        log.warning(f"ارسال عکس پست {msg_id} ناموفق بود، تلاش با متن ساده: {e}")
                if not sent:
                    send_to_telegram(final_msg)

                log.info(f"پست {msg_id} از {channel} با موفقیت ارسال شد.")
            except Exception as e:
                log.error(f"خطا در پردازش پست {msg_id} از {channel}: {e}")
                continue

            state[channel] = state.get(channel, []) + [msg_id]
            state[channel] = state[channel][-200:]  # فقط ۲۰۰ آی‌دی آخر نگه داشته بشه
            save_state(state)
            time.sleep(3)  # فاصله‌ی کوتاه بین پست‌ها


def main():
    missing = [name for name, val in [
        ("SOURCE_CHANNELS", SOURCE_CHANNELS),
        ("TARGET_CHAT_ID", TARGET_CHAT_ID),
        ("BOT_TOKEN", BOT_TOKEN),
        ("GEMINI_API_KEY", GEMINI_API_KEY),
    ] if not val]
    if missing:
        log.error(f"این متغیرها تنظیم نشده‌اند: {', '.join(missing)}")
        return

    state = load_state()
    log.info(f"ربات شروع به کار کرد. کانال‌های مبدأ: {SOURCE_CHANNELS}")
    while True:
        try:
            process_once(state)
        except Exception as e:
            log.error(f"خطای عمومی در حلقه: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
