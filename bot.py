import os
import re
import json
import html
import time
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from io import BytesIO
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

from openai import AsyncOpenAI

from telegram import (
    Update,
    BotCommand,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ============================================================
#                    VEXA CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

CHANNEL_USERNAME = os.getenv(
    "CHANNEL_USERNAME",
    "@fcnewsss"
)

# مهم:
# مدل را از Environment Variable می‌خوانیم تا اگر مدل API عوض شد
# مجبور نباشی کد را تغییر بدهی.
AI_MODEL = os.getenv(
    "AI_MODEL",
    "gpt-5-mini"
)

DB_FILE = os.getenv(
    "DB_FILE",
    "vexa.db"
)

MAX_ARTICLES = int(
    os.getenv("MAX_ARTICLES", "9")
)

AUTO_POST_LIMIT = int(
    os.getenv("AUTO_POST_LIMIT", "5")
)

NEWS_INTERVAL = int(
    os.getenv("NEWS_INTERVAL", "600")
)

MAX_NEWS_AGE_HOURS = int(
    os.getenv("MAX_NEWS_AGE_HOURS", "36")
)

POST_DELAY = float(
    os.getenv("POST_DELAY", "2.0")
)

# Telegram IDs separated by comma
# مثال:
# ADMIN_IDS=123456789,987654321
ADMIN_IDS_RAW = os.getenv(
    "ADMIN_IDS",
    ""
)

ADMIN_IDS = {
    int(x.strip())
    for x in ADMIN_IDS_RAW.split(",")
    if x.strip().isdigit()
}


if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not configured."
    )

if not OPENAI_API_KEY:
    raise RuntimeError(
        "OPENAI_API_KEY is not configured."
    )


# ============================================================
#                         LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
)

logger = logging.getLogger("VEXA")


# ============================================================
#                        AI CLIENT
# ============================================================

client = AsyncOpenAI(
    api_key=OPENAI_API_KEY
)


# ============================================================
#                          RSS SOURCES
# ============================================================

RSS_FEEDS = {
    "BBC Sport": (
        "https://feeds.bbci.co.uk/sport/football/rss.xml"
    ),

    "The Guardian": (
        "https://www.theguardian.com/football/rss"
    ),

    "ESPN": (
        "https://www.espn.com/espn/rss/soccer/news"
    ),
}


# ============================================================
#                     GLOBAL STATE
# ============================================================

news_lock = asyncio.Lock()

last_cycle_time = None
last_cycle_status = "هنوز اجرا نشده"
last_cycle_count = 0

runtime_stats = {
    "cycles": 0,
    "successful_posts": 0,
    "failed_posts": 0,
    "ai_errors": 0,
    "image_errors": 0,
}


# ============================================================
#                         DATABASE
# ============================================================

def db_connect():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():
    conn = db_connect()

    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sent_news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE,
                title TEXT,
                title_key TEXT,
                source TEXT,
                category TEXT,
                importance TEXT,
                sent_at TEXT,
                telegram_message_id INTEGER
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sent_title_key
            ON sent_news(title_key)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sent_at
            ON sent_news(sent_at)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        conn.commit()

    finally:
        conn.close()


def get_setting(key, default=None):
    conn = db_connect()

    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,)
        ).fetchone()

        if row:
            return row["value"]

        return default

    finally:
        conn.close()


def set_setting(key, value):
    conn = db_connect()

    try:
        conn.execute("""
            INSERT INTO settings(key, value)
            VALUES (?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
        """, (key, str(value)))

        conn.commit()

    finally:
        conn.close()


def is_paused():
    return get_setting(
        "paused",
        "0"
    ) == "1"


def normalize_title(title):
    if not title:
        return ""

    title = title.lower()

    # حذف لینک و HTML
    title = re.sub(
        r"https?://\S+",
        "",
        title
    )

    title = BeautifulSoup(
        title,
        "html.parser"
    ).get_text(" ")

    # حذف علائم
    title = re.sub(
        r"[^\w\s\u0600-\u06FF]",
        " ",
        title
    )

    # حذف فاصله‌های اضافی
    title = re.sub(
        r"\s+",
        " ",
        title
    ).strip()

    return title


def is_news_sent(url, title):
    conn = db_connect()

    try:
        if url:
            row = conn.execute(
                "SELECT id FROM sent_news WHERE url = ?",
                (url,)
            ).fetchone()

            if row:
                return True

        title_key = normalize_title(title)

        if title_key:
            row = conn.execute(
                """
                SELECT id
                FROM sent_news
                WHERE title_key = ?
                LIMIT 1
                """,
                (title_key,)
            ).fetchone()

            if row:
                return True

        return False

    finally:
        conn.close()


def save_sent_news(article, message_id):
    conn = db_connect()

    try:
        conn.execute("""
            INSERT OR IGNORE INTO sent_news (
                url,
                title,
                title_key,
                source,
                category,
                importance,
                sent_at,
                telegram_message_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            article.get("link"),
            article.get("original_title"),
            normalize_title(
                article.get("original_title", "")
            ),
            article.get("source"),
            article.get("category"),
            article.get("importance"),
            datetime.now(
                timezone.utc
            ).isoformat(),
            message_id
        ))

        conn.commit()

    finally:
        conn.close()


def get_database_stats():
    conn = db_connect()

    try:
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM sent_news"
        ).fetchone()["c"]

        today = conn.execute("""
            SELECT COUNT(*) AS c
            FROM sent_news
            WHERE sent_at >= ?
        """, (
            datetime.now(
                timezone.utc
            ).replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0
            ).isoformat(),
        )).fetchone()["c"]

        return {
            "total": total,
            "today": today,
        }

    finally:
        conn.close()


# ============================================================
#                         TEXT HELPERS
# ============================================================

def clean_text(value):
    if not value:
        return ""

    value = BeautifulSoup(
        str(value),
        "html.parser"
    ).get_text(" ")

    value = html.unescape(value)

    value = re.sub(
        r"\s+",
        " ",
        value
    ).strip()

    return value


def limit_text(text, max_length):
    text = clean_text(text)

    if len(text) <= max_length:
        return text

    return text[:max_length].rsplit(
        " ",
        1
    )[0] + "…"


def escape_html(value):
    return html.escape(
        str(value or ""),
        quote=False
    )


# ============================================================
#                      IMAGE EXTRACTION
# ============================================================

def is_probable_image_url(url):
    if not url:
        return False

    url = url.lower()

    if url.startswith("data:"):
        return False

    image_extensions = (
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif"
    )

    parsed = urlparse(url)

    path = parsed.path.lower()

    if any(
        path.endswith(ext)
        for ext in image_extensions
    ):
        return True

    # بعض CDN ها پسوند ندارند
    image_words = (
        "image",
        "photo",
        "picture",
        "thumbnail",
        "media"
    )

    return any(
        word in url
        for word in image_words
    )


def get_rss_image(entry):
    candidates = []

    media_content = entry.get(
        "media_content",
        []
    )

    for item in media_content or []:
        url = item.get("url")

        if url:
            candidates.append(url)

    media_thumbnail = entry.get(
        "media_thumbnail",
        []
    )

    for item in media_thumbnail or []:
        url = item.get("url")

        if url:
            candidates.append(url)

    enclosures = entry.get(
        "enclosures",
        []
    )

    for item in enclosures or []:
        url = item.get("href")

        if url:
            candidates.append(url)

    links = entry.get(
        "links",
        []
    )

    for item in links or []:
        href = item.get("href")
        mime = item.get("type", "")

        if href and (
            mime.startswith("image/")
            or is_probable_image_url(href)
        ):
            candidates.append(href)

    for url in candidates:
        if url:
            return url

    return None


def scrape_article_image(url):
    """
    تلاش برای پیدا کردن تصویر اصلی مقاله:
    og:image
    og:image:url
    twitter:image
    image_src
    """

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 "
                "(compatible; VexaNewsBot/1.0)"
            )
        }

        response = requests.get(
            url,
            headers=headers,
            timeout=10,
            allow_redirects=True
        )

        if response.status_code != 200:
            return None

        content_type = (
            response.headers
            .get("content-type", "")
            .lower()
        )

        if "text/html" not in content_type:
            return None

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        meta_candidates = [
            (
                "meta",
                {
                    "property": "og:image"
                }
            ),
            (
                "meta",
                {
                    "property": "og:image:url"
                }
            ),
            (
                "meta",
                {
                    "name": "twitter:image"
                }
            ),
            (
                "meta",
                {
                    "property": "twitter:image"
                }
            ),
        ]

        for tag_name, attrs in meta_candidates:
            tag = soup.find(
                tag_name,
                attrs=attrs
            )

            if tag and tag.get("content"):
                image = urljoin(
                    response.url,
                    tag["content"]
                )

                if image.startswith("http"):
                    return image

        image_src = soup.find(
            "link",
            rel=lambda x: x and (
                "image_src" in x
                if isinstance(x, list)
                else x == "image_src"
            )
        )

        if image_src and image_src.get("href"):
            return urljoin(
                response.url,
                image_src["href"]
            )

    except Exception as exc:
        logger.debug(
            "Image scrape failed: %s",
            exc
        )

    return None


async def get_best_image(entry, article_url):
    # اول RSS
    rss_image = get_rss_image(entry)

    if rss_image:
        return rss_image

    # بعد صفحه اصلی مقاله
    image = await asyncio.to_thread(
        scrape_article_image,
        article_url
    )

    return image


# ============================================================
#                     ARTICLE COLLECTION
# ============================================================

def parse_entry_time(entry):
    parsed = (
        entry.get("published_parsed")
        or entry.get("updated_parsed")
    )

    if not parsed:
        return 0

    try:
        return time.mktime(parsed)
    except Exception:
        return 0


async def read_feed(source, feed_url):
    try:
        feed = await asyncio.to_thread(
            feedparser.parse,
            feed_url
        )

        articles = []

        for entry in feed.entries[:12]:

            title = clean_text(
                entry.get("title", "")
            )

            summary = clean_text(
                entry.get(
                    "summary",
                    entry.get(
                        "description",
                        ""
                    )
                )
            )

            link = (
                entry.get("link")
                or ""
            ).strip()

            if not title or not link:
                continue

            published_ts = parse_entry_time(
                entry
            )

            # حذف اخبار خیلی قدیمی
            if published_ts:
                age = (
                    time.time()
                    - published_ts
                )

                if age > (
                    MAX_NEWS_AGE_HOURS * 3600
                ):
                    continue

            if is_news_sent(
                link,
                title
            ):
                continue

            image = await get_best_image(
                entry,
                link
            )

            articles.append({
                "source": source,
                "original_title": title,
                "original_summary": summary,
                "link": link,
                "image_url": image,
                "published_ts": published_ts,
            })

        return articles

    except Exception as exc:
        logger.exception(
            "Feed failed: %s",
            source
        )

        return []


def similar_title(a, b):
    a = normalize_title(a)
    b = normalize_title(b)

    if not a or not b:
        return False

    ratio = SequenceMatcher(
        None,
        a,
        b
    ).ratio()

    return ratio >= 0.88


async def collect_articles():
    tasks = [
        read_feed(
            source,
            url
        )
        for source, url
        in RSS_FEEDS.items()
    ]

    results = await asyncio.gather(
        *tasks,
        return_exceptions=True
    )

    all_articles = []

    for result in results:
        if isinstance(
            result,
            Exception
        ):
            continue

        all_articles.extend(
            result
        )

    # جدیدترین‌ها اول
    all_articles.sort(
        key=lambda x: x.get(
            "published_ts",
            0
        ),
        reverse=True
    )

    unique = []
    seen_links = set()

    for article in all_articles:

        link = article["link"]

        if link in seen_links:
            continue

        duplicate = False

        for existing in unique:
            if similar_title(
                article["original_title"],
                existing["original_title"]
            ):
                duplicate = True
                break

        if duplicate:
            continue

        seen_links.add(link)
        unique.append(article)

        if len(unique) >= MAX_ARTICLES:
            break

    return unique


# ============================================================
#                         AI PROCESSING
# ============================================================

ALLOWED_CATEGORIES = {
    "breaking",
    "transfer",
    "match",
    "player",
    "coach",
    "injury",
    "record",
    "tournament",
    "other",
}

ALLOWED_IMPORTANCE = {
    "high",
    "medium",
    "low",
}

ALLOWED_STYLES = {
    "classic",
    "breaking",
    "transfer",
    "match",
    "player",
    "stats",
}


def extract_json(text):
    text = text.strip()

    text = re.sub(
        r"^```(?:json)?",
        "",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"```$",
        "",
        text
    )

    text = text.strip()

    # اگر مدل قبل/بعد JSON متن اضافه داده
    start = text.find("[")

    end = text.rfind("]")

    if start != -1 and end != -1:
        text = text[start:end + 1]

    return json.loads(text)


async def translate_news_with_ai(articles):
    if not articles:
        return []

    payload = []

    for index, article in enumerate(
        articles,
        start=1
    ):
        payload.append({
            "id": index,
            "source": article["source"],
            "title": article["original_title"],
            "summary": article["original_summary"],
        })

    prompt = f"""
You are the editorial AI for a Persian football news Telegram channel.

Process ALL {len(payload)} news items.

Rules:
- Write natural, professional Persian.
- Never invent facts.
- Never add transfer fees, dates, scores, quotes,
  clubs, players or details that are not supported.
- Preserve names correctly.
- Title must be concise, around 8-15 Persian words.
- Summary must be 2-3 useful sentences.
- Avoid clickbait.
- Avoid repeating the source headline word-for-word.
- Make every item stylistically natural.
- Use neutral sports journalism.

Classify every item:

category:
breaking
transfer
match
player
coach
injury
record
tournament
other

importance:
high
medium
low

style:
classic
breaking
transfer
match
player
stats

Choose one suitable emoji.

IMPORTANT:
Return ONLY valid JSON.
No markdown.
No explanation.
The number of output items MUST be exactly {len(payload)}.

JSON structure:

[
  {{
    "id": 1,
    "title": "...",
    "summary": "...",
    "category": "transfer",
    "importance": "high",
    "emoji": "🔄",
    "style": "transfer"
  }}
]

NEWS:
{json.dumps(payload, ensure_ascii=False)}
"""

    try:
        response = await client.responses.create(
            model=AI_MODEL,
            input=prompt
        )

        raw = response.output_text

        data = extract_json(raw)

        if not isinstance(
            data,
            list
        ):
            raise ValueError(
                "AI response is not a list"
            )

        if len(data) != len(
            articles
        ):
            raise ValueError(
                "AI returned wrong number of items"
            )

        processed = []

        for article, item in zip(
            articles,
            data
        ):

            if not isinstance(
                item,
                dict
            ):
                raise ValueError(
                    "Invalid AI item"
                )

            category = item.get(
                "category",
                "other"
            )

            importance = item.get(
                "importance",
                "medium"
            )

            style = item.get(
                "style",
                "classic"
            )

            if category not in ALLOWED_CATEGORIES:
                category = "other"

            if importance not in ALLOWED_IMPORTANCE:
                importance = "medium"

            if style not in ALLOWED_STYLES:
                style = "classic"

            title = limit_text(
                item.get(
                    "title",
                    article["original_title"]
                ),
                180
            )

            summary = limit_text(
                item.get(
                    "summary",
                    article["original_summary"]
                ),
                700
            )

            processed.append({
                **article,
                "title": title,
                "summary": summary,
                "category": category,
                "importance": importance,
                "emoji": item.get(
                    "emoji",
                    "⚽"
                ),
                "style": style,
            })

        return processed

    except Exception as exc:
        runtime_stats["ai_errors"] += 1

        logger.exception(
            "AI processing failed: %s",
            exc
        )

        # تلاش دوم با prompt ساده‌تر
        try:
            simple_prompt = """
Rewrite these football news items in natural Persian.

Return ONLY JSON array.
Each item:
{
"title": "...",
"summary": "..."
}

Do not invent information.

NEWS:
""" + json.dumps(
                payload,
                ensure_ascii=False
            )

            retry = await client.responses.create(
                model=AI_MODEL,
                input=simple_prompt
            )

            data = extract_json(
                retry.output_text
            )

            if len(data) != len(
                articles
            ):
                raise ValueError(
                    "Retry returned wrong count"
                )

            processed = []

            for article, item in zip(
                articles,
                data
            ):
                processed.append({
                    **article,
                    "title": limit_text(
                        item.get(
                            "title",
                            article["original_title"]
                        ),
                        180
                    ),
                    "summary": limit_text(
                        item.get(
                            "summary",
                            article["original_summary"]
                        ),
                        700
                    ),
                    "category": "other",
                    "importance": "medium",
                    "emoji": "⚽",
                    "style": "classic",
                })

            return processed

        except Exception as retry_exc:

            runtime_stats["ai_errors"] += 1

            logger.exception(
                "AI retry failed: %s",
                retry_exc
            )

            return []


# ============================================================
#                       NEWS FORMATTING
# ============================================================

def style_header(article):
    style = article.get(
        "style",
        "classic"
    )

    emoji = article.get(
        "emoji",
        "⚽"
    )

    headers = {
        "breaking": (
            "🚨 <b>خبر فوری فوتبال</b>"
        ),
        "transfer": (
            "🔄 <b>نقل‌وانتقالات</b>"
        ),
        "match": (
            "🏟️ <b>گزارش فوتبال</b>"
        ),
        "player": (
            "👤 <b>دنیای بازیکنان</b>"
        ),
        "stats": (
            "📊 <b>آمار و رکورد</b>"
        ),
        "classic": (
            f"{emoji} <b>خبر جدید فوتبال</b>"
        ),
    }

    return headers.get(
        style,
        headers["classic"]
    )


def build_news_text(article):
    header = style_header(
        article
    )

    title = escape_html(
        article.get("title")
    )

    summary = escape_html(
        article.get("summary")
    )

    source = escape_html(
        article.get("source")
    )

    category = article.get(
        "category",
        "other"
    )

    category_names = {
        "breaking": "خبر فوری",
        "transfer": "نقل‌وانتقالات",
        "match": "بازی",
        "player": "بازیکن",
        "coach": "مربی",
        "injury": "مصدومیت",
        "record": "رکورد",
        "tournament": "مسابقات",
        "other": "فوتبال",
    }

    category_text = category_names.get(
        category,
        "فوتبال"
    )

    return (
        f"{header}\n\n"
        f"🔥 <b>{title}</b>\n\n"
        f"{summary}\n\n"
        f"🏷️ <b>دسته:</b> "
        f"{category_text}\n"
        f"📰 <b>منبع:</b> {source}\n\n"
        f"🤖 <b>Vexa</b>"
    )


def source_keyboard(url):
    if not url:
        return None

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔗 مشاهده خبر اصلی",
                url=url
            )
        ]
    ])


# ============================================================
#                      TELEGRAM POSTING
# ============================================================

async def post_article(
    bot,
    article
):
    text = build_news_text(
        article
    )

    keyboard = source_keyboard(
        article.get("link")
    )

    image_url = article.get(
        "image_url"
    )

    try:

        if image_url:

            try:

                message = await bot.send_photo(
                    chat_id=CHANNEL_USERNAME,
                    photo=image_url,
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )

                return message

            except Exception as image_exc:

                runtime_stats[
                    "image_errors"
                ] += 1

                logger.warning(
                    "Image post failed: %s",
                    image_exc
                )

        # fallback بدون عکس
        message = await bot.send_message(
            chat_id=CHANNEL_USERNAME,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

        return message

    except RetryAfter as exc:

        logger.warning(
            "Telegram rate limit. Waiting %s seconds.",
            exc.retry_after
        )

        await asyncio.sleep(
            exc.retry_after
        )

        try:
            message = await bot.send_message(
                chat_id=CHANNEL_USERNAME,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

            return message

        except Exception:
            runtime_stats[
                "failed_posts"
            ] += 1

            return None

    except TelegramError as exc:

        logger.error(
            "Telegram error: %s",
            exc
        )

        runtime_stats[
            "failed_posts"
        ] += 1

        return None

    except Exception as exc:

        logger.exception(
            "Post failed: %s",
            exc
        )

        runtime_stats[
            "failed_posts"
        ] += 1

        return None


# ============================================================
#                     NEWS CYCLE ENGINE
# ============================================================

async def run_news_cycle(
    bot,
    mode="automatic",
    limit=None,
    category=None,
    importance=None
):
    global last_cycle_time
    global last_cycle_status
    global last_cycle_count

    async with news_lock:

        runtime_stats[
            "cycles"
        ] += 1

        last_cycle_time = datetime.now(
            timezone.utc
        )

        if is_paused() and mode == "automatic":

            last_cycle_status = (
                "⏸️ متوقف شده"
            )

            return 0

        try:

            articles = await collect_articles()

            if not articles:
                last_cycle_status = (
                    "ℹ️ خبر جدیدی پیدا نشد"
                )

                last_cycle_count = 0

                return 0

            processed = (
                await translate_news_with_ai(
                    articles
                )
            )

            if not processed:
                last_cycle_status = (
                    "❌ خطا در پردازش AI"
                )

                last_cycle_count = 0

                return 0

            if category:
                processed = [
                    x for x in processed
                    if x.get(
                        "category"
                    ) == category
                ]

            if importance:
                processed = [
                    x for x in processed
                    if x.get(
                        "importance"
                    ) == importance
                ]

            if limit:
                processed = processed[
                    :limit
                ]

            sent = 0

            for article in processed:

                # دوباره چک می‌کنیم تا اگر همزمان
                # چرخه دیگری خبر را فرستاده بود
                # دوباره ارسال نشود.
                if is_news_sent(
                    article.get("link"),
                    article.get(
                        "original_title"
                    )
                ):
                    continue

                message = await post_article(
                    bot,
                    article
                )

                if message:

                    save_sent_news(
                        article,
                        message.message_id
                    )

                    runtime_stats[
                        "successful_posts"
                    ] += 1

                    sent += 1

                    await asyncio.sleep(
                        POST_DELAY
                    )

            last_cycle_count = sent

            last_cycle_status = (
                f"✅ {sent} خبر ارسال شد"
            )

            return sent

        except Exception as exc:

            logger.exception(
                "News cycle failed: %s",
                exc
            )

            last_cycle_status = (
                "❌ خطای داخلی"
            )

            last_cycle_count = 0

            return 0


# ============================================================
#                         ADMIN CHECK
# ============================================================

def is_admin(user_id):
    return (
        user_id is not None
        and user_id in ADMIN_IDS
    )


async def admin_only(
    update,
    text="⛔ این دستور فقط برای مدیر Vexa فعال است."
):
    user = update.effective_user

    if not user or not is_admin(
        user.id
    ):
        if update.effective_message:
            await update.effective_message.reply_text(
                text
            )

        return False

    return True


# ============================================================
#                         KEYBOARD
# ============================================================

def get_main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [
                "📰 ارسال اخبار",
                "🔥 اخبار مهم",
            ],
            [
                "🔄 نقل‌وانتقالات",
                "⚽ اخبار فوتبال",
            ],
            [
                "📊 وضعیت Vexa",
                "🤖 درباره Vexa",
            ],
            [
                "🆔 شناسه من",
                "🆘 راهنما",
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


# ============================================================
#                         COMMANDS
# ============================================================

async def start(update, context):

    user = update.effective_user

    name = (
        user.first_name
        if user
        else "دوست"
    )

    admin_text = ""

    if user and is_admin(
        user.id
    ):
        admin_text = (
            "\n\n👑 <b>دسترسی مدیر فعال است.</b>"
        )

    text = (
        f"سلام {escape_html(name)} 👋🔥\n\n"
        f"به <b>Vexa</b> خوش اومدی!\n\n"
        f"🤖 موتور هوشمند اخبار فوتبال\n"
        f"📰 جمع‌آوری از چند منبع\n"
        f"🖼️ انتخاب هوشمند تصویر\n"
        f"🧠 بازنویسی فارسی با AI\n"
        f"🚫 جلوگیری از اخبار تکراری\n"
        f"📊 سیستم آمار و مدیریت\n"
        f"{admin_text}"
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_main_keyboard()
    )


async def help_command(update, context):

    user = update.effective_user

    text = (
        "🆘 <b>راهنمای Vexa</b>\n\n"
        "📰 <b>/news</b> — ارسال اخبار جدید\n"
        "🔥 <b>/important</b> — اخبار مهم\n"
        "🔄 <b>/transfers</b> — نقل‌وانتقالات\n"
        "⚽ <b>/football</b> — اخبار فوتبال\n"
        "📊 <b>/status</b> — وضعیت بات\n"
        "🆔 <b>/myid</b> — شناسه تلگرام شما\n"
        "🤖 <b>/about</b> — درباره Vexa\n\n"
        "دستورات مدیریتی:\n"
        "⏸️ /pause\n"
        "▶️ /resume\n"
        "🧪 /testpost\n"
    )

    if user and is_admin(
        user.id
    ):
        text += (
            "\n\n👑 شما مدیر Vexa هستید."
        )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML
    )


async def about_command(
    update,
    context
):
    text = (
        "🤖 <b>Vexa Football News Engine</b>\n\n"
        "Vexa یک موتور خودکار انتشار اخبار فوتبال است "
        "که خبرها را از منابع RSS دریافت می‌کند، "
        "خبرهای تکراری را حذف می‌کند، "
        "آن‌ها را با AI به فارسی طبیعی تبدیل می‌کند "
        "و در کانال منتشر می‌کند.\n\n"
        "⚡ RSS Collector\n"
        "🧠 AI Editor\n"
        "🖼️ Smart Image Finder\n"
        "🗄️ SQLite Database\n"
        "🛡️ Admin System\n"
        "📊 Statistics\n"
        "🔄 Automatic Scheduler\n\n"
        "⚽ ساخته شده برای یک تجربه خبری تمیز و حرفه‌ای."
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML
    )


async def myid_command(
    update,
    context
):
    user = update.effective_user

    if not user:
        return

    await update.message.reply_text(
        f"🆔 شناسه تلگرام شما:\n\n"
        f"<code>{user.id}</code>\n\n"
        f"اگر می‌خواهی مدیر Vexa باشی، "
        f"این عدد را در ADMIN_IDS قرار بده.",
        parse_mode=ParseMode.HTML
    )


async def news_command(
    update,
    context
):
    if not await admin_only(
        update
    ):
        return

    await update.message.reply_text(
        "🔎 در حال پیدا کردن اخبار جدید..."
    )

    count = await run_news_cycle(
        update.get_bot(),
        mode="manual",
        limit=AUTO_POST_LIMIT
    )

    await update.message.reply_text(
        f"✅ عملیات تمام شد.\n"
        f"📰 تعداد ارسال‌شده: {count}"
    )


async def important_command(
    update,
    context
):
    if not await admin_only(
        update
    ):
        return

    await update.message.reply_text(
        "🔥 در حال بررسی اخبار مهم..."
    )

    count = await run_news_cycle(
        update.get_bot(),
        mode="manual",
        limit=AUTO_POST_LIMIT,
        importance="high"
    )

    await update.message.reply_text(
        f"🔥 عملیات تمام شد.\n"
        f"📰 تعداد اخبار مهم: {count}"
    )


async def transfers_command(
    update,
    context
):
    if not await admin_only(
        update
    ):
        return

    await update.message.reply_text(
        "🔄 در حال پیدا کردن اخبار نقل‌وانتقالات..."
    )

    count = await run_news_cycle(
        update.get_bot(),
        mode="manual",
        limit=AUTO_POST_LIMIT,
        category="transfer"
    )

    await update.message.reply_text(
        f"🔄 انجام شد.\n"
        f"📰 تعداد اخبار نقل‌وانتقالات: {count}"
    )


async def football_command(
    update,
    context
):
    if not await admin_only(
        update
    ):
        return

    await update.message.reply_text(
        "⚽ در حال پیدا کردن اخبار فوتبال..."
    )

    count = await run_news_cycle(
        update.get_bot(),
        mode="manual",
        limit=AUTO_POST_LIMIT
    )

    await update.message.reply_text(
        f"⚽ انجام شد.\n"
        f"📰 تعداد ارسال‌شده: {count}"
    )


# ============================================================
#                         STATUS
# ============================================================

async def status_command(
    update,
    context
):
    if not await admin_only(
        update
    ):
        return

    stats = get_database_stats()

    paused = is_paused()

    last_time = (
        last_cycle_time.strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
        if last_cycle_time
        else "هنوز اجرا نشده"
    )

    status = (
        "⏸️ متوقف"
        if paused
        else "🟢 فعال"
    )

    text = (
        "📊 <b>Vexa Status</b>\n\n"
        f"🤖 وضعیت: {status}\n"
        f"🕐 آخرین چرخه: {last_time}\n"
        f"📌 نتیجه آخرین چرخه: "
        f"{escape_html(last_cycle_status)}\n\n"
        f"📰 کل اخبار ارسال‌شده: "
        f"<b>{stats['total']}</b>\n"
        f"📅 اخبار امروز: "
        f"<b>{stats['today']}</b>\n\n"
        f"🔄 تعداد چرخه‌ها: "
        f"{runtime_stats['cycles']}\n"
        f"✅ ارسال موفق: "
        f"{runtime_stats['successful_posts']}\n"
        f"❌ ارسال ناموفق: "
        f"{runtime_stats['failed_posts']}\n"
        f"🧠 خطاهای AI: "
        f"{runtime_stats['ai_errors']}\n"
        f"🖼️ خطاهای عکس: "
        f"{runtime_stats['image_errors']}\n\n"
        f"⏱️ فاصله بررسی: "
        f"{NEWS_INTERVAL} ثانیه\n"
        f"🧠 مدل AI: "
        f"<code>{escape_html(AI_MODEL)}</code>"
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML
    )


# ============================================================
#                      PAUSE / RESUME
# ============================================================

async def pause_command(
    update,
    context
):
    if not await admin_only(
        update
    ):
        return

    set_setting(
        "paused",
        "1"
    )

    await update.message.reply_text(
        "⏸️ انتشار خودکار Vexa متوقف شد."
    )


async def resume_command(
    update,
    context
):
    if not await admin_only(
        update
    ):
        return

    set_setting(
        "paused",
        "0"
    )

    await update.message.reply_text(
        "▶️ انتشار خودکار Vexa دوباره فعال شد."
    )


# ============================================================
#                         TEST POST
# ============================================================

async def testpost_command(
    update,
    context
):
    if not await admin_only(
        update
    ):
        return

    await update.message.reply_text(
        "🧪 در حال گرفتن یک خبر برای تست..."
    )

    articles = await collect_articles()

    if not articles:
        await update.message.reply_text(
            "❌ خبری برای تست پیدا نشد."
        )
        return

    processed = (
        await translate_news_with_ai(
            articles[:1]
        )
    )

    if not processed:
        await update.message.reply_text(
            "❌ پردازش AI ناموفق بود."
        )
        return

    message = await post_article(
        update.get_bot(),
        processed[0]
    )

    if message:

        save_sent_news(
            processed[0],
            message.message_id
        )

        await update.message.reply_text(
            "✅ پست تست با موفقیت در کانال ارسال شد."
        )

    else:

        await update.message.reply_text(
            "❌ ارسال تست ناموفق بود."
        )


# ============================================================
#                       BUTTON HANDLER
# ============================================================

async def button_handler(
    update,
    context
):
    if not update.message:
        return

    text = (
        update.message.text
        or ""
    )

    if text == "📰 ارسال اخبار":
        await news_command(
            update,
            context
        )

    elif text == "🔥 اخبار مهم":
        await important_command(
            update,
            context
        )

    elif text == "🔄 نقل‌وانتقالات":
        await transfers_command(
            update,
            context
        )

    elif text == "⚽ اخبار فوتبال":
        await football_command(
            update,
            context
        )

    elif text == "📊 وضعیت Vexa":
        await status_command(
            update,
            context
        )

    elif text == "🤖 درباره Vexa":
        await about_command(
            update,
            context
        )

    elif text == "🆔 شناسه من":
        await myid_command(
            update,
            context
        )

    elif text == "🆘 راهنما":
        await help_command(
            update,
            context
        )


# ============================================================
#                    AUTOMATIC NEWS JOB
# ============================================================

async def automatic_news(
    context: ContextTypes.DEFAULT_TYPE
):
    try:

        logger.info(
            "Automatic news cycle started."
        )

        count = await run_news_cycle(
            context.bot,
            mode="automatic",
            limit=AUTO_POST_LIMIT
        )

        logger.info(
            "Automatic cycle finished. Sent=%s",
            count
        )

    except Exception as exc:

        logger.exception(
            "Automatic job failed: %s",
            exc
        )


# ============================================================
#                     TELEGRAM CHECK
# ============================================================

async def check_channel(
    application
):
    try:

        me = await application.bot.get_me()

        member = await application.bot.get_chat_member(
            CHANNEL_USERNAME,
            me.id
        )

        logger.info(
            "Channel check: bot=%s status=%s",
            me.username,
            member.status
        )

    except Exception as exc:

        logger.warning(
            "Could not verify channel permissions: %s",
            exc
        )


# ============================================================
#                         ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):
    logger.exception(
        "Unhandled Telegram error",
        exc_info=context.error
    )


# ============================================================
#                         POST INIT
# ============================================================

async def post_init(
    application
):
    init_db()

    commands = [
        BotCommand(
            "start",
            "شروع کار با Vexa"
        ),
        BotCommand(
            "news",
            "ارسال اخبار جدید"
        ),
        BotCommand(
            "important",
            "اخبار مهم"
        ),
        BotCommand(
            "transfers",
            "نقل‌وانتقالات"
        ),
        BotCommand(
            "football",
            "اخبار فوتبال"
        ),
        BotCommand(
            "status",
            "وضعیت Vexa"
        ),
        BotCommand(
            "myid",
            "شناسه تلگرام من"
        ),
        BotCommand(
            "about",
            "درباره Vexa"
        ),
        BotCommand(
            "help",
            "راهنما"
        ),
    ]

    await application.bot.set_my_commands(
        commands
    )

    await check_channel(
        application
    )

    logger.info(
        "================================="
    )

    logger.info(
        "VEXA STARTED SUCCESSFULLY"
    )

    logger.info(
        "Channel: %s",
        CHANNEL_USERNAME
    )

    logger.info(
        "AI model: %s",
        AI_MODEL
    )

    logger.info(
        "News interval: %s seconds",
        NEWS_INTERVAL
    )

    logger.info(
        "Admins: %s",
        ADMIN_IDS
    )

    logger.info(
        "================================="
    )


# ============================================================
#                           MAIN
# ============================================================

def main():

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )

    application.add_handler(
        CommandHandler(
            "about",
            about_command
        )
    )

    application.add_handler(
        CommandHandler(
            "myid",
            myid_command
        )
    )

    application.add_handler(
        CommandHandler(
            "news",
            news_command
        )
    )

    application.add_handler(
        CommandHandler(
            "important",
            important_command
        )
    )

    application.add_handler(
        CommandHandler(
            "transfers",
            transfers_command
        )
    )

    application.add_handler(
        CommandHandler(
            "football",
            football_command
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status_command
        )
    )

    application.add_handler(
        CommandHandler(
            "pause",
            pause_command
        )
    )

    application.add_handler(
        CommandHandler(
            "resume",
            resume_command
        )
    )

    application.add_handler(
        CommandHandler(
            "testpost",
            testpost_command
        )
    )

    # Keyboard
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            button_handler
        )
    )

    # Automatic news
    if application.job_queue:

        application.job_queue.run_repeating(
            automatic_news,
            interval=NEWS_INTERVAL,
            first=30,
            name="vexa_auto_news"
        )

    else:

        logger.error(
            "JobQueue is not available. "
            "Install python-telegram-bot[job-queue]."
        )

    application.add_error_handler(
        error_handler
    )

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
