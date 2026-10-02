import os
import re
import json
import time
import html
import sqlite3
import logging
import asyncio
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
import feedparser
from bs4 import BeautifulSoup

from openai import OpenAI

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =========================================================
# VEXA - FOOTBALL NEWS BOT
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# کانال مقصد
CHANNEL_ID = os.getenv("CHANNEL_ID", "@fcnewsss").strip()

# کلید OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# مدل قابل تغییر از Environment
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5-mini").strip()

# فاصله بررسی اخبار
NEWS_INTERVAL_MINUTES = int(
    os.getenv("NEWS_INTERVAL_MINUTES", "10")
)

# حداکثر تعداد خبر در هر چرخه
MAX_ARTICLES_PER_CYCLE = int(
    os.getenv("MAX_ARTICLES_PER_CYCLE", "9")
)

# حداکثر تعداد خبر برای هر منبع
MAX_PER_SOURCE = int(
    os.getenv("MAX_PER_SOURCE", "3")
)

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("Vexa")


# =========================================================
# RSS SOURCES
# =========================================================

RSS_SOURCES = [
    {
        "name": "BBC Sport",
        "url": "https://feeds.bbci.co.uk/sport/football/rss.xml",
    },
    {
        "name": "The Guardian",
        "url": "https://www.theguardian.com/football/rss",
    },
    {
        "name": "ESPN",
        "url": "https://www.espn.com/espn/rss/soccer/news",
    },
    {
        "name": "Sky Sports",
        "url": "https://www.skysports.com/rss/12040",
    },
]


# =========================================================
# DATABASE
# =========================================================

DB_FILE = "vexa.db"


def init_database():
    conn = sqlite3.connect(DB_FILE)

    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE,
            title TEXT,
            source TEXT,
            sent_at TEXT
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_stats (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )

    conn.commit()
    conn.close()


def is_sent(url: str) -> bool:
    conn = sqlite3.connect(DB_FILE)

    cursor = conn.cursor()

    cursor.execute(
        "SELECT 1 FROM sent_news WHERE url = ? LIMIT 1",
        (url,),
    )

    result = cursor.fetchone()

    conn.close()

    return result is not None


def mark_sent(url: str, title: str, source: str):
    conn = sqlite3.connect(DB_FILE)

    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT OR IGNORE INTO sent_news
        (url, title, source, sent_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            url,
            title,
            source,
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    conn.commit()
    conn.close()


def get_sent_count():
    conn = sqlite3.connect(DB_FILE)

    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM sent_news")

    count = cursor.fetchone()[0]

    conn.close()

    return count


# =========================================================
# HTTP SESSION
# =========================================================

SESSION = requests.Session()

SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/130 Safari/537.36"
        )
    }
)


# =========================================================
# TEXT HELPERS
# =========================================================

def clean_html(text: str) -> str:
    if not text:
        return ""

    soup = BeautifulSoup(text, "html.parser")

    return soup.get_text(" ", strip=True)


def clean_text(text: str, limit: int = 4000) -> str:
    text = clean_html(text)

    text = re.sub(r"\s+", " ", text)

    text = html.unescape(text)

    return text[:limit].strip()


def escape_markdown(text: str) -> str:
    if not text:
        return ""

    replacements = [
        ("\\", "\\\\"),
        ("_", "\\_"),
        ("*", "\\*"),
        ("[", "\\["),
        ("]", "\\]"),
        ("(", "\\("),
        (")", "\\)"),
        ("~", "\\~"),
        ("`", "\\`"),
        (">", "\\>"),
        ("#", "\\#"),
        ("+", "\\+"),
        ("-", "\\-"),
        ("=", "\\="),
        ("|", "\\|"),
        ("{", "\\{"),
        ("}", "\\}"),
        (".", "\\."),
        ("!", "\\!"),
    ]

    for old, new in replacements:
        text = text.replace(old, new)

    return text


# =========================================================
# IMAGE EXTRACTION
# =========================================================

def get_rss_image(entry):
    """
    تلاش برای پیدا کردن بهترین تصویر موجود داخل RSS
    """

    candidates = []

    # media_content
    media_content = entry.get("media_content", [])

    if isinstance(media_content, list):
        for item in media_content:
            if isinstance(item, dict):
                url = item.get("url")
                if url:
                    candidates.append(url)

    # media_thumbnail
    media_thumbnail = entry.get("media_thumbnail", [])

    if isinstance(media_thumbnail, list):
        for item in media_thumbnail:
            if isinstance(item, dict):
                url = item.get("url")
                if url:
                    candidates.append(url)

    # enclosure
    enclosures = entry.get("enclosures", [])

    if isinstance(enclosures, list):
        for item in enclosures:
            if isinstance(item, dict):
                url = item.get("href") or item.get("url")
                mime = item.get("type", "")

                if url and (
                    "image" in mime
                    or url.lower().endswith(
                        (".jpg", ".jpeg", ".png", ".webp")
                    )
                ):
                    candidates.append(url)

    # links
    links = entry.get("links", [])

    if isinstance(links, list):
        for item in links:
            if not isinstance(item, dict):
                continue

            href = item.get("href", "")
            mime = item.get("type", "")

            if href and "image" in mime:
                candidates.append(href)

    # حذف تکراری‌ها
    unique = []

    for url in candidates:
        if url and url not in unique:
            unique.append(url)

    return unique[0] if unique else None


def extract_og_image(url: str):
    """
    دریافت تصویر اصلی صفحه خبر از og:image.
    """

    try:
        response = SESSION.get(
            url,
            timeout=12,
            allow_redirects=True,
        )

        if response.status_code != 200:
            return None

        content_type = response.headers.get(
            "content-type",
            "",
        ).lower()

        if "text/html" not in content_type:
            return None

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        # اولویت با og:image
        for prop in [
            "og:image",
            "og:image:url",
            "twitter:image",
            "twitter:image:src",
        ]:
            tag = soup.find(
                "meta",
                attrs={"property": prop},
            )

            if not tag:
                tag = soup.find(
                    "meta",
                    attrs={"name": prop},
                )

            if tag and tag.get("content"):
                image = tag["content"].strip()

                return urljoin(
                    response.url,
                    image,
                )

    except Exception as e:
        logger.warning(
            "Image extraction failed: %s",
            e,
        )

    return None


def validate_image(url: str):
    """
    بررسی اینکه URL واقعاً تصویر قابل استفاده برای تلگرام است.
    """

    if not url:
        return None

    try:
        response = SESSION.get(
            url,
            timeout=10,
            stream=True,
            allow_redirects=True,
        )

        content_type = response.headers.get(
            "content-type",
            "",
        ).lower()

        response.close()

        if "image/" in content_type:
            return url

    except Exception:
        pass

    return None


def get_best_image(entry):
    """
    ترتیب اولویت:

    1. og:image صفحه اصلی
    2. media_content
    3. media_thumbnail
    4. enclosure
    """

    article_url = entry.get("link")

    # بهترین حالت: تصویر اصلی صفحه
    if article_url:
        og_image = extract_og_image(article_url)

        if og_image:
            valid = validate_image(og_image)

            if valid:
                return valid

    # تصویر RSS
    rss_image = get_rss_image(entry)

    if rss_image:
        valid = validate_image(rss_image)

        if valid:
            return valid

    return None


# =========================================================
# NEWS CATEGORY
# =========================================================

def detect_category(title: str, description: str):
    text = (
        f"{title} {description}"
    ).lower()

    transfer_words = [
        "transfer",
        "transfers",
        "signing",
        "joins",
        "deal",
        "loan",
        "contract",
        "moves",
        "انتقال",
        "قرارداد",
    ]

    injury_words = [
        "injury",
        "injured",
        "fitness",
        "out",
        "مصدوم",
        "مصدومیت",
    ]

    match_words = [
        "match",
        "fixture",
        "win",
        "loss",
        "draw",
        "defeat",
        "victory",
        "game",
        "بازی",
        "دیدار",
    ]

    record_words = [
        "record",
        "milestone",
        "رکورد",
        "رکوردشکنی",
    ]

    coach_words = [
        "manager",
        "coach",
        "managerial",
        "مربی",
        "سرمربی",
    ]

    quote_words = [
        "said",
        "says",
        "reveals",
        "claims",
        "admits",
        "گفت",
        "اظهار",
    ]

    if any(word in text for word in transfer_words):
        return "TRANSFER"

    if any(word in text for word in injury_words):
        return "INJURY"

    if any(word in text for word in record_words):
        return "RECORD"

    if any(word in text for word in match_words):
        return "MATCH"

    if any(word in text for word in coach_words):
        return "COACH"

    if any(word in text for word in quote_words):
        return "QUOTE"

    return "NEWS"


# =========================================================
# CATEGORY STYLE
# =========================================================

CATEGORY_INFO = {
    "TRANSFER": {
        "emoji": "🔄",
        "label": "نقل‌وانتقالات",
    },
    "INJURY": {
        "emoji": "🚑",
        "label": "مصدومیت",
    },
    "MATCH": {
        "emoji": "⚽",
        "label": "مسابقه",
    },
    "RECORD": {
        "emoji": "🏆",
        "label": "رکورد",
    },
    "COACH": {
        "emoji": "🧠",
        "label": "مربی",
    },
    "QUOTE": {
        "emoji": "🎙️",
        "label": "اظهارنظر",
    },
    "NEWS": {
        "emoji": "📰",
        "label": "اخبار فوتبال",
    },
}


# =========================================================
# RSS FETCH
# =========================================================

def fetch_feed(source):
    try:
        feed = feedparser.parse(
            source["url"]
        )

        if getattr(
            feed,
            "bozo",
            False,
        ):
            logger.warning(
                "RSS warning from %s",
                source["name"],
            )

        return feed.entries

    except Exception as e:
        logger.error(
            "RSS error %s: %s",
            source["name"],
            e,
        )

        return []


def collect_news():
    all_articles = []

    for source in RSS_SOURCES:
        entries = fetch_feed(source)

        count = 0

        for entry in entries:
            if count >= MAX_PER_SOURCE:
                break

            title = clean_text(
                entry.get("title", ""),
                500,
            )

            link = entry.get(
                "link",
                "",
            ).strip()

            description = clean_text(
                entry.get(
                    "summary",
                    entry.get(
                        "description",
                        "",
                    ),
                ),
                3000,
            )

            if not title or not link:
                continue

            if is_sent(link):
                continue

            category = detect_category(
                title,
                description,
            )

            all_articles.append(
                {
                    "title": title,
                    "description": description,
                    "url": link,
                    "source": source["name"],
                    "category": category,
                    "entry": entry,
                }
            )

            count += 1

    return all_articles[
        :MAX_ARTICLES_PER_CYCLE
    ]


# =========================================================
# OPENAI
# =========================================================

openai_client = None

if OPENAI_API_KEY:
    openai_client = OpenAI(
        api_key=OPENAI_API_KEY
    )


def fallback_translation(article):
    """
    اگر OpenAI در دسترس نبود،
    حداقل یک خروجی تمیز تولید می‌کنیم.
    """

    title = article["title"]
    description = article["description"]

    return {
        "headline": title,
        "summary": description[:600],
    }


def generate_fa_news(article):
    if not openai_client:
        return fallback_translation(
            article
        )

    category = CATEGORY_INFO[
        article["category"]
    ]

    prompt = f"""
تو سردبیر حرفه‌ای یک کانال خبری فوتبال فارسی هستی.

خبر زیر را به فارسی روان و طبیعی تبدیل کن.

قوانین:
- اطلاعات جدید اختراع نکن.
- اگر بخشی نامشخص است، حدس نزن.
- نام بازیکنان، باشگاه‌ها و رقابت‌ها را درست نگه دار.
- تیتر کوتاه، جذاب و خبری باشد.
- خلاصه حداکثر 3 جمله باشد.
- لحن حرفه‌ای ولی صمیمی باشد.
- از اغراق استفاده نکن.
- لینک مقاله را داخل متن نیاور.
- خبر را به شکل مناسب تلگرام بنویس.

نوع خبر:
{category["label"]}

عنوان اصلی:
{article["title"]}

متن:
{article["description"]}

فقط JSON معتبر برگردان:

{{
  "headline": "...",
  "summary": "..."
}}
"""

    try:
        response = openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a professional Persian "
                        "football news editor."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            temperature=0.7,
        )

        content = response.choices[0].message.content

        if not content:
            return fallback_translation(
                article
            )

        content = content.strip()

        # حذف markdown احتمالی
        content = re.sub(
            r"^```json",
            "",
            content,
            flags=re.I,
        )

        content = re.sub(
            r"```$",
            "",
            content,
        )

        data = json.loads(
            content.strip()
        )

        headline = clean_text(
            data.get(
                "headline",
                article["title"],
            ),
            300,
        )

        summary = clean_text(
            data.get(
                "summary",
                article["description"],
            ),
            1200,
        )

        return {
            "headline": headline,
            "summary": summary,
        }

    except Exception as e:
        logger.error(
            "OpenAI error: %s",
            e,
        )

        return fallback_translation(
            article
        )


# =========================================================
# FORMAT NEWS
# =========================================================

def format_news(article, translated):
    category = CATEGORY_INFO[
        article["category"]
    ]

    emoji = category["emoji"]
    label = category["label"]

    headline = translated["headline"]
    summary = translated["summary"]

    text = (
        f"{emoji} *{escape_markdown(headline)}*\n\n"
        f"{escape_markdown(summary)}\n\n"
        f"🏷️ `{escape_markdown(label)}`\n"
        f"📰 منبع: {escape_markdown(article['source'])}\n\n"
        f"⚡ *Vexa Football*"
    )

    return text


# =========================================================
# SEND NEWS
# =========================================================

async def send_article(
    bot,
    article,
):
    translated = await asyncio.to_thread(
        generate_fa_news,
        article,
    )

    caption = format_news(
        article,
        translated,
    )

    image_url = await asyncio.to_thread(
        get_best_image,
        article["entry"],
    )

    try:

        if image_url:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=image_url,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN_V2,
            )

        else:
            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=caption,
                parse_mode=ParseMode.MARKDOWN_V2,
            )

        # فقط بعد از ارسال موفق ثبت شود
        mark_sent(
            article["url"],
            article["title"],
            article["source"],
        )

        logger.info(
            "Sent: %s",
            article["title"],
        )

        return True

    except Exception as e:
        logger.error(
            "Telegram send error: %s",
            e,
        )

        return False


# =========================================================
# NEWS UPDATE JOB
# =========================================================

async def news_update_job(
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.info(
        "Checking for new football news..."
    )

    articles = await asyncio.to_thread(
        collect_news
    )

    if not articles:
        logger.info(
            "No new articles."
        )
        return

    sent = 0

    for article in articles:

        success = await send_article(
            context.bot,
            article,
        )

        if success:
            sent += 1

        # فاصله کوتاه برای جلوگیری از فشار
        await asyncio.sleep(2)

    logger.info(
        "Cycle finished. Sent=%s",
        sent,
    )


# =========================================================
# KEYBOARD
# =========================================================

def main_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📰 آخرین اخبار",
                    callback_data="latest",
                ),
                InlineKeyboardButton(
                    "🔥 اخبار مهم روز",
                    callback_data="important",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🌍 فوتبال اروپا",
                    callback_data="europe",
                ),
                InlineKeyboardButton(
                    "⚽ فوتبال جهان",
                    callback_data="football",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔄 نقل‌وانتقالات",
                    callback_data="transfers",
                ),
                InlineKeyboardButton(
                    "🔄 آپدیت اخبار",
                    callback_data="update",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🧪 تست ارسال",
                    callback_data="test",
                ),
                InlineKeyboardButton(
                    "ℹ️ درباره Vexa",
                    callback_data="about",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📖 راهنما",
                    callback_data="help",
                ),
            ],
        ]
    )


# =========================================================
# /START
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = """
🤖 *به Vexa خوش اومدی!*

⚽ Vexa یک دستیار خبری فوتبال است که اخبار جدید فوتبال را دریافت، بررسی، خلاصه و به فارسی آماده می‌کند.

🔥 امکانات:
• اخبار فوتبال
• فوتبال اروپا
• نقل‌وانتقالات
• تشخیص نوع خبر
• خلاصه‌سازی فارسی
• عکس خبر
• جلوگیری از خبرهای تکراری
• انتشار خودکار در کانال

از منوی زیر استفاده کن 👇
"""

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_keyboard(),
    )


# =========================================================
# /HELP
# =========================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = """
📖 *راهنمای Vexa*

/start
شروع کار با بات

/help
نمایش راهنما

/news
بررسی و ارسال اخبار جدید

/testpost
تست ارسال به کانال

⚙️ Vexa به‌صورت خودکار هر ۱۰ دقیقه اخبار جدید را بررسی می‌کند.

📡 کانال مقصد:
@fcnewsss
"""

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_keyboard(),
    )


# =========================================================
# /NEWS
# =========================================================

async def news_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    message = await update.message.reply_text(
        "🔎 دارم اخبار جدید رو بررسی می‌کنم..."
    )

    articles = await asyncio.to_thread(
        collect_news
    )

    if not articles:
        await message.edit_text(
            "✅ در حال حاضر خبر جدیدی برای ارسال پیدا نشد."
        )
        return

    sent = 0

    for article in articles:
        if await send_article(
            context.bot,
            article,
        ):
            sent += 1

        await asyncio.sleep(2)

    await message.edit_text(
        f"✅ بررسی تمام شد.\n\n"
        f"📰 تعداد ارسال موفق: {sent}"
    )


# =========================================================
# /TESTPOST
# =========================================================

async def testpost_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    test_text = """
🤖 *Vexa Test*

✅ بات با موفقیت اجرا شده است.

⚽ سیستم اخبار فوتبال فعال است.
🖼️ سیستم تصویر فعال است.
🤖 سیستم پردازش خبر فعال است.
📡 سیستم ارسال کانال فعال است.

*Vexa Football*
"""

    try:

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=test_text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )

        await update.message.reply_text(
            "✅ پیام تست با موفقیت به کانال ارسال شد."
        )

    except Exception as e:

        logger.error(
            "Test post error: %s",
            e,
        )

        await update.message.reply_text(
            "❌ ارسال تست انجام نشد.\n"
            "دسترسی ادمین بات در کانال را بررسی کن."
        )


# =========================================================
# CALLBACKS
# =========================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    data = query.data

    if data == "latest":

        articles = await asyncio.to_thread(
            collect_news
        )

        if not articles:
            await query.edit_message_text(
                "📰 فعلاً خبر جدیدی پیدا نشد.",
                reply_markup=main_keyboard(),
            )
            return

        text = "📰 *آخرین اخبار پیدا شده:*\n\n"

        for i, article in enumerate(
            articles[:7],
            start=1,
        ):

            category = CATEGORY_INFO[
                article["category"]
            ]

            text += (
                f"{i}\\. "
                f"{category['emoji']} "
                f"{escape_markdown(article['title'])}\n"
            )

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=main_keyboard(),
        )

    elif data == "update":

        await query.edit_message_text(
            "🔎 در حال بررسی اخبار جدید..."
        )

        articles = await asyncio.to_thread(
            collect_news
        )

        sent = 0

        for article in articles:
            if await send_article(
                context.bot,
                article,
            ):
                sent += 1

            await asyncio.sleep(2)

        await query.edit_message_text(
            f"✅ آپدیت انجام شد.\n\n"
            f"📰 ارسال موفق: {sent}",
            reply_markup=main_keyboard(),
        )

    elif data == "transfers":

        articles = await asyncio.to_thread(
            collect_news
        )

        transfers = [
            a for a in articles
            if a["category"] == "TRANSFER"
        ]

        if not transfers:
            text = (
                "🔄 *نقل‌وانتقالات*\n\n"
                "فعلاً خبر جدیدی در این بخش پیدا نشد."
            )

        else:

            text = "🔄 *نقل‌وانتقالات*\n\n"

            for article in transfers[:7]:

                text += (
                    f"• "
                    f"{escape_markdown(article['title'])}\n\n"
                )

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=main_keyboard(),
        )

    elif data in [
        "football",
        "europe",
        "important",
    ]:

        articles = await asyncio.to_thread(
            collect_news
        )

        if data == "football":
            filtered = articles

            title = "⚽ *فوتبال جهان*"

        elif data == "europe":

            europe_words = [
                "premier",
                "champions",
                "la liga",
                "laliga",
                "serie a",
                "bundesliga",
                "ligue 1",
                "europa",
                "arsenal",
                "chelsea",
                "liverpool",
                "manchester",
                "real madrid",
                "barcelona",
                "bayern",
                "psg",
                "juventus",
                "milan",
                "inter",
            ]

            filtered = [
                a for a in articles
                if any(
                    word in (
                        a["title"] + " " +
                        a["description"]
                    ).lower()
                    for word in europe_words
                )
            ]

            title = "🇪🇺 *فوتبال اروپا*"

        else:

            important_words = [
                "final",
                "champions",
                "transfer",
                "signing",
                "injury",
                "record",
                "breaking",
                "فینال",
                "انتقال",
                "مصدومیت",
                "رکورد",
            ]

            filtered = [
                a for a in articles
                if any(
                    word in (
                        a["title"] + " " +
                        a["description"]
                    ).lower()
                    for word in important_words
                )
            ]

            title = "🔥 *اخبار مهم روز*"

        if not filtered:
            text = (
                f"{title}\n\n"
                "خبر جدیدی پیدا نشد."
            )

        else:

            text = (
                f"{title}\n\n"
            )

            for article in filtered[:7]:

                category = CATEGORY_INFO[
                    article["category"]
                ]

                text += (
                    f"{category['emoji']} "
                    f"{escape_markdown(article['title'])}\n\n"
                )

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=main_keyboard(),
        )

    elif data == "about":

        text = """
🤖 *Vexa*

Vexa یک بات خبری فوتبال است که برای دریافت، پردازش و انتشار اخبار فوتبال ساخته شده.

⚽ Football
📰 News
🔄 Transfers
🤖 AI Processing
🖼️ Smart Images
📡 Telegram Publishing

*Vexa Football*
"""

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=main_keyboard(),
        )

    elif data == "help":

        text = """
📖 *راهنمای Vexa*

از دکمه‌های منو برای بررسی اخبار استفاده کن.

🔄 آپدیت اخبار:
اخبار جدید را بررسی می‌کند.

📰 آخرین اخبار:
آخرین خبرهای دریافت‌شده را نمایش می‌دهد.

🔄 نقل‌وانتقالات:
اخبار مربوط به انتقال بازیکنان و قراردادها را جدا می‌کند.

🧪 تست ارسال:
برای بررسی ارتباط بات با کانال استفاده می‌شود.

⚙️ سیستم خودکار:
Vexa هر ۱۰ دقیقه اخبار را بررسی می‌کند.
"""

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=main_keyboard(),
        )

    elif data == "test":

        try:

            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=(
                    "🧪 *Vexa Test*\n\n"
                    "✅ اتصال بات به کانال فعال است."
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )

            await query.edit_message_text(
                "✅ تست ارسال با موفقیت انجام شد.",
                reply_markup=main_keyboard(),
            )

        except Exception as e:

            logger.error(
                "Callback test error: %s",
                e,
            )

            await query.edit_message_text(
                "❌ تست ارسال ناموفق بود.\n"
                "ادمین بودن بات در کانال را بررسی کن.",
                reply_markup=main_keyboard(),
            )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.error(
        "Unhandled exception:",
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    init_database()

    logger.info(
        "Starting Vexa..."
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler(
            "start",
            start_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "news",
            news_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "testpost",
            testpost_command,
        )
    )

    # Buttons
    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    # Error handler
    application.add_error_handler(
        error_handler
    )

    # Automatic news checker
    application.job_queue.run_repeating(
        news_update_job,
        interval=NEWS_INTERVAL_MINUTES * 60,
        first=10,
    )

    logger.info(
        "Vexa bot is running."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
