import asyncio
import difflib
import hashlib
import json
import os
import re
import urllib.parse
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
from openai import AsyncOpenAI
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

CHANNEL_USERNAME = "@fcnewsss"

AI_MODEL = "gpt-6-luna"

NEWS_INTERVAL = 600
FIRST_NEWS_DELAY = 30

DAILY_DIGEST_HOUR = 21
DAILY_DIGEST_MINUTE = 0

LOCAL_TZ = ZoneInfo("Europe/Budapest")

STATE_FILE = Path("vexa_state.json")
IMAGE_CACHE_DIR = Path("vexa_images")

MAX_ARTICLES = 8
MAX_RECENT_ARTICLES = 200


# =========================================================
# RSS SOURCES
# =========================================================

RSS_FEEDS = [
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
]


# =========================================================
# GLOBAL STATE
# =========================================================

sent_links = set()
sent_title_keys = set()
sent_content_keys = set()

recent_articles = []

users = {}

last_digest_date = ""

openai_client = None


# =========================================================
# BASIC HELPERS
# =========================================================

def now_local():
    return datetime.now(LOCAL_TZ)


def safe_text(value):
    if value is None:
        return ""

    return str(value).strip()


def normalize_title(title):
    title = safe_text(title).lower()

    title = re.sub(
        r"https?://\S+",
        "",
        title,
    )

    title = re.sub(
        r"[^\w\s]",
        " ",
        title,
        flags=re.UNICODE,
    )

    title = re.sub(
        r"\s+",
        " ",
        title,
    ).strip()

    return title


def title_words(title):
    return set(
        word
        for word in normalize_title(title).split()
        if len(word) >= 3
    )


def title_similarity(title1, title2):
    a = normalize_title(title1)
    b = normalize_title(title2)

    if not a or not b:
        return 0.0

    sequence_score = difflib.SequenceMatcher(
        None,
        a,
        b,
    ).ratio()

    words_a = title_words(a)
    words_b = title_words(b)

    if words_a and words_b:
        jaccard = len(
            words_a & words_b
        ) / len(
            words_a | words_b
        )
    else:
        jaccard = 0.0

    return max(
        sequence_score,
        jaccard,
    )


def make_content_key(title):
    normalized = normalize_title(title)

    if not normalized:
        return ""

    return hashlib.sha256(
        normalized.encode("utf-8")
    ).hexdigest()


# =========================================================
# URL NORMALIZATION
# =========================================================

def canonicalize_url(url):
    if not url:
        return ""

    try:
        parsed = urllib.parse.urlsplit(
            url.strip()
        )

        query = urllib.parse.parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )

        ignored = {
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_term",
            "utm_content",
            "utm_id",
            "fbclid",
            "gclid",
            "mc_cid",
            "mc_eid",
        }

        filtered = [
            (key, value)
            for key, value in query
            if key.lower() not in ignored
        ]

        new_query = urllib.parse.urlencode(
            filtered
        )

        return urllib.parse.urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                new_query,
                "",
            )
        )

    except Exception:
        return url.strip()


# =========================================================
# STATE
# =========================================================

def default_state():
    return {
        "sent_links": [],
        "sent_title_keys": [],
        "sent_content_keys": [],
        "users": {},
        "last_digest_date": "",
    }


def load_state():
    global sent_links
    global sent_title_keys
    global sent_content_keys
    global users
    global last_digest_date

    if not STATE_FILE.exists():
        print("STATE: no state file found.")
        return

    try:
        with STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        sent_links = set(
            data.get(
                "sent_links",
                [],
            )
        )

        sent_title_keys = set(
            data.get(
                "sent_title_keys",
                [],
            )
        )

        sent_content_keys = set(
            data.get(
                "sent_content_keys",
                [],
            )
        )

        users = data.get(
            "users",
            {},
        )

        last_digest_date = data.get(
            "last_digest_date",
            "",
        )

        print(
            "STATE: loaded",
            len(sent_links),
            "links,",
            len(sent_title_keys),
            "titles."
        )

    except Exception as e:
        print(
            "STATE LOAD ERROR:",
            type(e).__name__,
            e,
        )


def save_state():
    data = {
        "sent_links": list(
            sent_links
        )[-5000:],
        "sent_title_keys": list(
            sent_title_keys
        )[-5000:],
        "sent_content_keys": list(
            sent_content_keys
        )[-5000:],
        "users": users,
        "last_digest_date": last_digest_date,
    }

    try:
        with STATE_FILE.open(
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2,
            )

    except Exception as e:
        print(
            "STATE SAVE ERROR:",
            type(e).__name__,
            e,
        )


# =========================================================
# DUPLICATE CHECK
# =========================================================

def is_duplicate_article(
    link,
    title,
    source=None,
    existing_articles=None,
):
    canonical_link = canonicalize_url(link)

    title_key = normalize_title(
        title
    )

    content_key = make_content_key(
        title
    )

    # فقط چیزهایی که واقعاً قبلاً ارسال شده‌اند
    # اینجا duplicate محسوب می‌شوند.
    if (
        canonical_link
        and canonical_link in sent_links
    ):
        return True

    if (
        title_key
        and title_key in sent_title_keys
    ):
        return True

    if (
        content_key
        and content_key in sent_content_keys
    ):
        return True

    # بررسی duplicate داخل همین batch
    if existing_articles is not None:

        for article in existing_articles:

            old_link = canonicalize_url(
                article.get(
                    "link",
                    "",
                )
            )

            if (
                canonical_link
                and old_link
                and canonical_link == old_link
            ):
                return True

            old_title = (
                article.get(
                    "title_original",
                    "",
                )
                or
                article.get(
                    "title",
                    "",
                )
            )

            if (
                title_similarity(
                    title,
                    old_title,
                )
                >= 0.90
            ):
                return True

    return False


def mark_article_sent(article):
    link = canonicalize_url(
        article.get(
            "link",
            "",
        )
    )

    title = (
        article.get(
            "title_original",
            "",
        )
        or
        article.get(
            "title",
            "",
        )
    )

    title_key = normalize_title(
        title
    )

    content_key = make_content_key(
        title
    )

    if link:
        sent_links.add(link)

    if title_key:
        sent_title_keys.add(
            title_key
        )

    if content_key:
        sent_content_keys.add(
            content_key
        )

    save_state()


# =========================================================
# RSS
# =========================================================

def parse_feed(feed_info):
    try:
        print(
            "RSS: checking",
            feed_info["name"]
        )

        feed = feedparser.parse(
            feed_info["url"]
        )

        articles = []

        for entry in feed.entries[:20]:

            title = safe_text(
                entry.get(
                    "title",
                    "",
                )
            )

            link = safe_text(
                entry.get(
                    "link",
                    "",
                )
            )

            summary = safe_text(
                entry.get(
                    "summary",
                    "",
                )
            )

            published = safe_text(
                entry.get(
                    "published",
                    "",
                )
            )

            if not title or not link:
                continue

            articles.append(
                {
                    "source": feed_info["name"],
                    "title_original": title,
                    "link": link,
                    "summary_original": summary,
                    "published": published,
                }
            )

        print(
            "RSS:",
            feed_info["name"],
            "->",
            len(articles),
            "items",
        )

        return articles

    except Exception as e:
        print(
            "RSS ERROR:",
            feed_info["name"],
            type(e).__name__,
            e,
        )

        return []


async def collect_raw_articles():
    all_articles = []

    for feed_info in RSS_FEEDS:

        articles = await asyncio.to_thread(
            parse_feed,
            feed_info,
        )

        for article in articles:

            if is_duplicate_article(
                article.get(
                    "link",
                    "",
                ),
                article.get(
                    "title_original",
                    "",
                ),
            ):
                continue

            all_articles.append(
                article
            )

    # حذف duplicate داخل batch
    unique = []

    for article in all_articles:

        if is_duplicate_article(
            article.get(
                "link",
                "",
            ),
            article.get(
                "title_original",
                "",
            ),
            existing_articles=unique,
        ):
            continue

        unique.append(article)

        if len(unique) >= MAX_ARTICLES:
            break

    print(
        "RSS: total new articles:",
        len(unique),
    )

    return unique


# =========================================================
# OPENAI
# =========================================================

def get_openai_client():

    global openai_client

    if openai_client is None:

        if not OPENAI_API_KEY:
            print(
                "OPENAI: API key is missing."
            )
            return None

        openai_client = AsyncOpenAI(
            api_key=OPENAI_API_KEY
        )

    return openai_client


async def translate_and_classify(
    articles
):
    client = get_openai_client()

    if not client:
        return articles

    results = []

    for article in articles:

        title = article.get(
            "title_original",
            "",
        )

        summary = article.get(
            "summary_original",
            "",
        )

        prompt = f"""
You are a football news editor for a Persian Telegram channel.

Translate and rewrite this football news into natural Persian.

Rules:
- Do not invent facts.
- Keep the important facts.
- Make the headline short and engaging.
- The final text must be suitable for Telegram.
- Do not include the source URL.
- Do not use markdown tables.
- Do not add unrelated opinions.

Return EXACTLY in this format:

TITLE:
<short Persian title>

TEXT:
<2 to 4 short Persian paragraphs>

IMPORTANT:
<YES or NO>

News source:
{article.get("source", "")}

Original title:
{title}

Original summary:
{summary}
"""

        try:

            response = await client.responses.create(
                model=AI_MODEL,
                input=prompt,
            )

            text = safe_text(
                getattr(
                    response,
                    "output_text",
                    "",
                )
            )

            if not text:
                print(
                    "AI: empty response"
                )
                continue

            translated_title = ""
            translated_text = ""
            important = False

            title_match = re.search(
                r"TITLE:\s*(.*?)(?:\n|$)",
                text,
                re.IGNORECASE,
            )

            text_match = re.search(
                r"TEXT:\s*(.*?)(?:\nIMPORTANT:|$)",
                text,
                re.IGNORECASE | re.DOTALL,
            )

            important_match = re.search(
                r"IMPORTANT:\s*(YES|NO)",
                text,
                re.IGNORECASE,
            )

            if title_match:
                translated_title = safe_text(
                    title_match.group(1)
                )

            if text_match:
                translated_text = safe_text(
                    text_match.group(1)
                )

            if important_match:
                important = (
                    important_match.group(1).upper()
                    == "YES"
                )

            if not translated_title:
                translated_title = title

            if not translated_text:
                translated_text = summary

            article["title"] = (
                translated_title
            )

            article["text"] = (
                translated_text
            )

            article["important"] = important

            results.append(
                article
            )

            print(
                "AI:",
                translated_title
            )

        except Exception as e:

            print(
                "AI ERROR:",
                type(e).__name__,
                e,
            )

            # اگر AI خطا داد، خبر را از دست نده
            article["title"] = title
            article["text"] = summary
            article["important"] = False

            results.append(
                article
            )

    return results


# =========================================================
# IMAGE
# =========================================================

async def fetch_image(article):
    """
    فعلاً از تصویر RSS استفاده می‌کنیم.
    اگر RSS تصویر داشته باشد، آن را برمی‌گرداند.
    """

    try:
        image_url = article.get(
            "image",
            "",
        )

        if image_url:
            return image_url

        return None

    except Exception:
        return None


# =========================================================
# FORMAT TELEGRAM POST
# =========================================================

def build_post_text(article):

    title = safe_text(
        article.get(
            "title",
            "",
        )
    )

    text = safe_text(
        article.get(
            "text",
            "",
        )
    )

    source = safe_text(
        article.get(
            "source",
            "",
        )
    )

    important = article.get(
        "important",
        False,
    )

    if important:
        prefix = "🚨 خبر مهم"
    else:
        prefix = "⚽ خبر فوتبال"

    post = (
        f"{prefix}\n\n"
        f"🔥 {title}\n\n"
        f"{text}\n\n"
        f"📰 منبع: {source}"
    )

    return post


# =========================================================
# TELEGRAM POST
# =========================================================

async def post_article(
    bot,
    article,
    target,
):
    try:

        text = build_post_text(
            article
        )

        image_url = await fetch_image(
            article
        )

        if image_url:

            try:

                await bot.send_photo(
                    chat_id=target,
                    photo=image_url,
                    caption=text[:1024],
                )

                return True

            except Exception as image_error:

                print(
                    "IMAGE POST FAILED:",
                    type(image_error).__name__,
                    image_error,
                )

        await bot.send_message(
            chat_id=target,
            text=text,
            disable_web_page_preview=True,
        )

        return True

    except Exception as e:

        print(
            "POST ERROR:",
            type(e).__name__,
            e,
        )

        return False


# =========================================================
# PUBLISH NEWS
# =========================================================

async def publish_news(
    articles,
    bot,
    target=CHANNEL_USERNAME,
):
    posted = 0

    print(
        f"PUBLISH: {len(articles)} articles ready."
    )

    for article in articles:

        link = canonicalize_url(
            article.get(
                "link",
                "",
            )
        )

        title = (
            article.get(
                "title_original",
                "",
            )
            or
            article.get(
                "title",
                "",
            )
        )

        title_key = normalize_title(
            title
        )

        content_key = make_content_key(
            title
        )

        # فقط چیزهایی که قبلاً واقعاً
        # در کانال ارسال شده‌اند.
        if (
            (link and link in sent_links)
            or
            (
                title_key
                and title_key in sent_title_keys
            )
            or
            (
                content_key
                and content_key in sent_content_keys
            )
        ):

            print(
                "DUPLICATE BLOCKED:",
                title,
            )

            continue

        print(
            "TRYING TO POST:",
            title,
        )

        ok = await post_article(
            bot,
            article,
            target,
        )

        if ok:

            mark_article_sent(
                article
            )

            posted += 1

            print(
                "CHANNEL POST SUCCESS:",
                title,
            )

        else:

            print(
                "CHANNEL POST FAILED:",
                title,
            )

        await asyncio.sleep(
            1.2
        )

    print(
        f"PUBLISH FINISHED: {posted} posted."
    )

    return posted


# =========================================================
# NEWS PIPELINE
# =========================================================

async def collect_articles(
    max_articles=MAX_ARTICLES,
    fetch_images=True,
):
    articles = await collect_raw_articles()

    if not articles:
        return []

    articles = articles[:max_articles]

    if fetch_images:

        for article in articles:

            try:

                image = await fetch_image(
                    article
                )

                if image:
                    article["image"] = image

            except Exception:
                pass

    return articles


async def get_news_pipeline(
    max_articles=MAX_ARTICLES,
    fetch_images=True,
):
    articles = await collect_articles(
        max_articles=max_articles,
        fetch_images=fetch_images,
    )

    if not articles:
        return []

    translated = await translate_and_classify(
        articles
    )

    global recent_articles

    # فقط برای نگهداری تاریخچه محلی
    # است؛ duplicate ارسال‌شدن محسوب نمی‌شود.
    recent_articles = (
        translated
        + recent_articles
    )

    unique = {}

    for article in recent_articles:

        key = canonicalize_url(
            article.get(
                "link",
                "",
            )
        )

        if not key:

            key = normalize_title(
                article.get(
                    "title",
                    "",
                )
            )

        if key:
            unique[key] = article

    recent_articles = list(
        unique.values()
    )[:MAX_RECENT_ARTICLES]

    return translated


# =========================================================
# KEYBOARD
# =========================================================

def main_keyboard():
    keyboard = [
        [
            "📰 اخبار جدید",
            "🚨 اخبار مهم",
        ],
        [
            "⚽ فوتبال",
            "🔄 بروزرسانی",
        ],
        [
            "ℹ️ راهنما",
            "❌ بستن منو",
        ],
    ]

    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
    )


# =========================================================
# COMMANDS
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = update.effective_user

    if user:

        users[str(user.id)] = {
            "id": user.id,
            "username": user.username,
            "first_name": user.first_name,
        }

        save_state()

    await update.message.reply_text(
        "سلام داداش 👋🔥\n\n"
        "من Vexa هستم؛ ربات اخبار فوتبال.\n"
        "از منوی پایین می‌تونی اخبار جدید رو بگیری.",
        reply_markup=main_keyboard(),
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = (
        "ℹ️ راهنمای Vexa\n\n"
        "/start - شروع ربات\n"
        "/news - دریافت اخبار جدید\n"
        "/important - اخبار مهم\n"
        "/testpost - تست ارسال به کانال\n"
        "/help - راهنما"
    )

    await update.message.reply_text(
        text,
        reply_markup=main_keyboard(),
    )


async def close_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "منو بسته شد 👌",
        reply_markup=ReplyKeyboardRemove(),
    )


# =========================================================
# PRIVATE NEWS
# =========================================================

async def news_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "⏳ دارم اخبار جدید رو بررسی می‌کنم..."
    )

    try:

        articles = await get_news_pipeline(
            max_articles=5,
            fetch_images=False,
        )

        if not articles:

            await update.message.reply_text(
                "فعلاً خبر جدیدی پیدا نکردم 😅",
                reply_markup=main_keyboard(),
            )

            return

        for article in articles:

            await post_article(
                context.bot,
                article,
                update.effective_chat.id,
            )

            await asyncio.sleep(
                0.8
            )

    except Exception as e:

        print(
            "NEWS COMMAND ERROR:",
            type(e).__name__,
            e,
        )

        await update.message.reply_text(
            "یه خطا موقع دریافت اخبار پیش اومد 😕",
            reply_markup=main_keyboard(),
        )


async def important_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    await update.message.reply_text(
        "🚨 دارم اخبار مهم رو بررسی می‌کنم..."
    )

    try:

        articles = await get_news_pipeline(
            max_articles=8,
            fetch_images=False,
        )

        important_articles = [
            article
            for article in articles
            if article.get(
                "important",
                False,
            )
        ]

        if not important_articles:

            await update.message.reply_text(
                "فعلاً خبر مهمی پیدا نکردم 👌",
                reply_markup=main_keyboard(),
            )

            return

        for article in important_articles:

            await post_article(
                context.bot,
                article,
                update.effective_chat.id,
            )

            await asyncio.sleep(
                0.8
            )

    except Exception as e:

        print(
            "IMPORTANT ERROR:",
            type(e).__name__,
            e,
        )

        await update.message.reply_text(
            "یه خطا موقع دریافت اخبار مهم پیش اومد 😕",
            reply_markup=main_keyboard(),
        )


# =========================================================
# TEST POST
# =========================================================

async def testpost_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    test_article = {
        "title": "تست Vexa",
        "text": (
            "این یک پیام آزمایشی برای بررسی "
            "ارسال ربات به کانال است."
        ),
        "source": "Vexa",
        "important": False,
    }

    try:

        ok = await post_article(
            context.bot,
            test_article,
            CHANNEL_USERNAME,
        )

        if ok:

            await update.message.reply_text(
                "✅ پیام تست با موفقیت به کانال ارسال شد.",
                reply_markup=main_keyboard(),
            )

        else:

            await update.message.reply_text(
                "❌ ارسال پیام تست ناموفق بود.",
                reply_markup=main_keyboard(),
            )

    except Exception as e:

        print(
            "TESTPOST ERROR:",
            type(e).__name__,
            e,
        )

        await update.message.reply_text(
            "❌ خطا در تست ارسال.",
            reply_markup=main_keyboard(),
        )


# =========================================================
# BUTTON HANDLER
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = safe_text(
        update.message.text
    )

    if text == "📰 اخبار جدید":

        await news_command(
            update,
            context,
        )

    elif text == "🚨 اخبار مهم":

        await important_command(
            update,
            context,
        )

    elif text == "⚽ فوتبال":

        await news_command(
            update,
            context,
        )

    elif text == "🔄 بروزرسانی":

        await news_command(
            update,
            context,
        )

    elif text == "ℹ️ راهنما":

        await help_command(
            update,
            context,
        )

    elif text == "❌ بستن منو":

        await close_menu(
            update,
            context,
        )


# =========================================================
# AUTOMATIC NEWS
# =========================================================

async def automatic_news_job(
    context: ContextTypes.DEFAULT_TYPE,
):

    print(
        "\n===================================="
    )

    print(
        "AUTO NEWS JOB STARTED"
    )

    print(
        "TIME:",
        now_local().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    )

    print(
        "CHANNEL:",
        CHANNEL_USERNAME,
    )

    try:

        articles = await get_news_pipeline(
            max_articles=MAX_ARTICLES,
            fetch_images=True,
        )

        if not articles:

            print(
                "AUTO NEWS: NO NEW ARTICLES"
            )

            print(
                "====================================\n"
            )

            return

        print(
            "AUTO NEWS:",
            f"{len(articles)} new articles",
        )

        posted = await publish_news(
            articles,
            context.bot,
            CHANNEL_USERNAME,
        )

        print(
            "AUTO NEWS FINISHED:",
            posted,
            "posted",
        )

    except Exception as e:

        print(
            "AUTO NEWS ERROR:",
            type(e).__name__,
            e,
        )

    print(
        "====================================\n"
    )


# =========================================================
# DAILY DIGEST
# =========================================================

async def daily_digest_job(
    context: ContextTypes.DEFAULT_TYPE,
):

    global last_digest_date

    today = now_local().date().isoformat()

    if last_digest_date == today:

        print(
            "DIGEST: already sent today."
        )

        return

    print(
        "DAILY DIGEST STARTED"
    )

    try:

        articles = await get_news_pipeline(
            max_articles=MAX_ARTICLES,
            fetch_images=False,
        )

        if not articles:

            print(
                "DIGEST: no articles."
            )

            return

        important = [
            article
            for article in articles
            if article.get(
                "important",
                False,
            )
        ]

        selected = (
            important[:5]
            if important
            else articles[:5]
        )

        lines = [
            "🌙 خلاصه اخبار فوتبال امروز",
            "",
        ]

        for index, article in enumerate(
            selected,
            start=1,
        ):

            title = safe_text(
                article.get(
                    "title",
                    "",
                )
            )

            lines.append(
                f"{index}. {title}"
            )

        digest_text = "\n".join(
            lines
        )

        await context.bot.send_message(
            chat_id=CHANNEL_USERNAME,
            text=digest_text,
        )

        last_digest_date = today

        save_state()

        print(
            "DAILY DIGEST SENT"
        )

    except Exception as e:

        print(
            "DIGEST ERROR:",
            type(e).__name__,
            e,
        )


# =========================================================
# POST INIT
# =========================================================

async def post_init(
    application: Application,
):

    await application.bot.set_my_commands(
        [
            (
                "start",
                "شروع ربات",
            ),
            (
                "news",
                "اخبار جدید",
            ),
            (
                "important",
                "اخبار مهم",
            ),
            (
                "testpost",
                "تست ارسال به کانال",
            ),
            (
                "help",
                "راهنما",
            ),
        ]
    )

    print(
        "BOT COMMANDS: configured"
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:

        print(
            "ERROR: BOT_TOKEN is missing."
        )

        return

    print(
        "===================================="
    )

    print(
        "VEXA BOT STARTING..."
    )

    print(
        "TIME:",
        now_local().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    )

    print(
        "CHANNEL:",
        CHANNEL_USERNAME,
    )

    print(
        "AI MODEL:",
        AI_MODEL,
    )

    print(
        "NEWS INTERVAL:",
        NEWS_INTERVAL,
        "seconds",
    )

    print(
        "===================================="
    )

    load_state()

    IMAGE_CACHE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

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
            "important",
            important_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "testpost",
            testpost_command,
        )
    )

    # Keyboard
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            button_handler,
        )
    )

    # Job Queue
    if application.job_queue:

        application.job_queue.run_repeating(
            automatic_news_job,
            interval=NEWS_INTERVAL,
            first=FIRST_NEWS_DELAY,
            name="vexa_auto_news",
        )

        application.job_queue.run_daily(
            daily_digest_job,
            time=time(
                DAILY_DIGEST_HOUR,
                DAILY_DIGEST_MINUTE,
                tzinfo=LOCAL_TZ,
            ),
            name="vexa_daily_digest",
        )

        print(
            "JOB QUEUE: automatic news enabled."
        )

        print(
            "JOB QUEUE: daily digest enabled."
        )

    else:

        print(
            "WARNING: JobQueue is not available!"
        )

    print(
        "VEXA IS RUNNING..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    main()
