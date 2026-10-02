import os
import json
import re
import asyncio
import html
import urllib.request
import feedparser

from openai import AsyncOpenAI
from telegram import (
    Update,
    BotCommand,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================================================
# SETTINGS
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

CHANNEL_USERNAME = "@fcnewsss"

AI_MODEL = "gpt-6-luna"

MAX_ARTICLES = 9

NEWS_INTERVAL = 600


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set!")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set!")


client = AsyncOpenAI(
    api_key=OPENAI_API_KEY
)


# =========================================================
# RSS SOURCES
# =========================================================

RSS_FEEDS = {

    "BBC Sport":
        "https://feeds.bbci.co.uk/sport/football/rss.xml",

    "The Guardian":
        "https://www.theguardian.com/football/rss",

    "ESPN":
        "https://www.espn.com/espn/rss/soccer/news",

}


# =========================================================
# MEMORY
# =========================================================

sent_links = set()


# =========================================================
# TEXT CLEANING
# =========================================================

def clean_text(text):

    if not text:
        return ""

    text = html.unescape(text)

    text = re.sub(
        r"<[^>]+>",
        " ",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def limit_text(text, max_length):

    if not text:
        return ""

    if len(text) <= max_length:
        return text

    return (
        text[:max_length]
        .rsplit(" ", 1)[0]
        + "..."
    )


def escape_html(text):

    if not text:
        return ""

    return html.escape(
        str(text),
        quote=False
    )


# =========================================================
# HIGH QUALITY IMAGE SYSTEM
# =========================================================

def get_image_candidates(entry):

    candidates = []

    try:

        # -------------------------------------------------
        # MEDIA CONTENT
        # -------------------------------------------------

        media_content = entry.get(
            "media_content",
            []
        )

        for media in media_content:

            if not isinstance(
                media,
                dict
            ):
                continue

            url = media.get(
                "url",
                ""
            )

            if not url:
                continue

            try:

                width = int(
                    media.get(
                        "width",
                        0
                    )
                    or 0
                )

            except Exception:

                width = 0


            try:

                height = int(
                    media.get(
                        "height",
                        0
                    )
                    or 0
                )

            except Exception:

                height = 0


            candidates.append({

                "url":
                    url,

                "width":
                    width,

                "height":
                    height,

                "score":
                    width * height,

            })


        # -------------------------------------------------
        # MEDIA THUMBNAIL
        # -------------------------------------------------

        thumbnails = entry.get(
            "media_thumbnail",
            []
        )

        for media in thumbnails:

            if not isinstance(
                media,
                dict
            ):
                continue

            url = media.get(
                "url",
                ""
            )

            if not url:
                continue

            try:

                width = int(
                    media.get(
                        "width",
                        0
                    )
                    or 0
                )

            except Exception:

                width = 0


            try:

                height = int(
                    media.get(
                        "height",
                        0
                    )
                    or 0
                )

            except Exception:

                height = 0


            candidates.append({

                "url":
                    url,

                "width":
                    width,

                "height":
                    height,

                "score":
                    width * height,

            })


        # -------------------------------------------------
        # ENCLOSURES
        # -------------------------------------------------

        enclosures = entry.get(
            "enclosures",
            []
        )

        for enclosure in enclosures:

            if not isinstance(
                enclosure,
                dict
            ):
                continue

            url = (

                enclosure.get(
                    "href",
                    ""
                )

                or

                enclosure.get(
                    "url",
                    ""
                )

            )

            if not url:
                continue


            media_type = enclosure.get(
                "type",
                ""
            ).lower()


            if (

                media_type.startswith(
                    "image/"
                )

                or

                re.search(
                    r"\.(jpg|jpeg|png|webp)(\?.*)?$",
                    url,
                    re.IGNORECASE
                )

            ):

                candidates.append({

                    "url":
                        url,

                    "width":
                        0,

                    "height":
                        0,

                    "score":
                        1,

                })


    except Exception as e:

        print(
            "RSS IMAGE ERROR:",
            repr(e)
        )


    return candidates


# =========================================================
# FIND BEST RSS IMAGE
# =========================================================

def get_best_rss_image(entry):

    candidates = get_image_candidates(
        entry
    )


    if not candidates:

        return None


    unique = {}


    for item in candidates:

        url = item["url"]


        if url not in unique:

            unique[url] = item

        else:

            if (
                item["score"]
                >
                unique[url]["score"]
            ):

                unique[url] = item


    candidates = list(
        unique.values()
    )


    candidates.sort(
        key=lambda item: item["score"],
        reverse=True
    )


    for item in candidates:

        url = item["url"]


        if url.startswith(
            (
                "http://",
                "https://"
            )
        ):

            print(
                "Selected RSS image:",
                url
            )

            return url


    return None


# =========================================================
# FIND IMAGE FROM ARTICLE PAGE
# =========================================================

def get_image_from_article_page(
    article_url
):

    if not article_url:

        return None


    try:

        request = urllib.request.Request(

            article_url,

            headers={

                "User-Agent":
                    "Mozilla/5.0 "
                    "(compatible; VexaBot/1.0)"

            }

        )


        with urllib.request.urlopen(
            request,
            timeout=8
        ) as response:

            page_html = (
                response
                .read()
                .decode(
                    "utf-8",
                    errors="ignore"
                )
            )


        # -------------------------------------------------
        # OG IMAGE
        # -------------------------------------------------

        patterns = [

            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',

            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',

            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',

            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',

        ]


        for pattern in patterns:

            match = re.search(

                pattern,

                page_html,

                re.IGNORECASE

            )


            if match:

                image_url = html.unescape(
                    match.group(1)
                ).strip()


                if image_url.startswith(
                    (
                        "http://",
                        "https://"
                    )
                ):

                    print(
                        "Selected article page image:",
                        image_url
                    )

                    return image_url


    except Exception as e:

        print(
            "ARTICLE IMAGE FETCH ERROR:",
            repr(e)
        )


    return None


# =========================================================
# FINAL IMAGE SELECTOR
# =========================================================

def get_article_image(entry):

    # اول RSS
    image_url = get_best_rss_image(
        entry
    )


    if image_url:

        return image_url


    # بعد صفحه اصلی خبر
    article_url = entry.get(
        "link",
        ""
    )


    if article_url:

        image_url = get_image_from_article_page(
            article_url
        )


        if image_url:

            return image_url


    print(
        "No suitable image found."
    )


    return None


# =========================================================
# COLLECT NEWS
# =========================================================

def collect_articles(
    limit=MAX_ARTICLES
):

    articles = []


    for source_name, feed_url in RSS_FEEDS.items():

        try:

            feed = feedparser.parse(
                feed_url
            )


            print(
                f"Reading source: {source_name}"
            )


            for entry in feed.entries[:8]:

                title = clean_text(
                    entry.get(
                        "title",
                        ""
                    )
                )


                summary = clean_text(

                    entry.get(
                        "summary",
                        ""
                    )

                    or

                    entry.get(
                        "description",
                        ""
                    )

                )


                link = entry.get(
                    "link",
                    ""
                )


                if not title or not link:

                    continue


                image_url = get_article_image(
                    entry
                )


                articles.append({

                    "source":
                        source_name,

                    "title":
                        title,

                    "summary":
                        summary,

                    "link":
                        link,

                    "image_url":
                        image_url,

                })


        except Exception as e:

            print(
                f"RSS ERROR ({source_name}): "
                f"{repr(e)}"
            )


    # =====================================================
    # REMOVE DUPLICATES
    # =====================================================

    unique_articles = []

    seen_links = set()

    seen_titles = set()


    for article in articles:

        link = article["link"]


        title_key = (

            article["title"]
            .lower()
            .strip()

        )


        if link in seen_links:

            continue


        if title_key in seen_titles:

            continue


        seen_links.add(
            link
        )

        seen_titles.add(
            title_key
        )


        unique_articles.append(
            article
        )


    # =====================================================
    # REMOVE ALREADY SENT
    # =====================================================

    new_articles = [

        article

        for article in unique_articles

        if article["link"]
        not in sent_links

    ]


    print(
        f"Collected {len(new_articles)} "
        f"new unique articles."
    )


    return new_articles[:limit]


# =========================================================
# AI NEWS EDITOR
# =========================================================

async def translate_news_with_ai(
    articles
):

    if not articles:

        return []


    news_text = ""


    for index, article in enumerate(
        articles,
        start=1
    ):

        news_text += f"""

===== NEWS {index} =====

SOURCE:
{article["source"]}

TITLE:
{limit_text(article["title"], 500)}

ARTICLE SUMMARY:
{limit_text(article["summary"], 3000)}

========================

"""


    prompt = f"""
تو سردبیر حرفه‌ای یک کانال تلگرامی اخبار فوتبال فارسی هستی.

خبرهای انگلیسی زیر را برای یک کانال فوتبال فارسی
به شکل طبیعی، جذاب، دقیق و حرفه‌ای بازنویسی کن.

این کار ترجمه کلمه‌به‌کلمه نیست.

باید مفهوم خبر را بفهمی و آن را مثل یک خبرنگار ورزشی
فارسی‌زبان بازنویسی کنی.

قوانین:

1. هیچ اطلاعاتی که در متن اصلی وجود ندارد اضافه نکن.

2. اگر اطلاعاتی ناقص است، حدس نزن.

3. اسم بازیکنان، مربیان، باشگاه‌ها، تیم‌های ملی
   و مسابقات را حفظ کن.

4. عنوان حدود 8 تا 15 کلمه باشد.

5. خلاصه 2 تا 3 جمله باشد.

6. از کلیشه‌هایی مثل:
   «در خبری مهم»
   «اتفاقی باورنکردنی»
   «هواداران شوکه شدند»
   استفاده نکن؛ مگر اینکه واقعاً در خبر وجود داشته باشد.

7. لحن حرفه‌ای، ورزشی، روان و بی‌طرف باشد.

8. خبرها نباید همه با یک لحن و ساختار نوشته شوند.

9. نوع هر خبر را مشخص کن.

دسته‌بندی‌های مجاز:

breaking
transfer
match
player
coach
injury
record
tournament
other

10. میزان اهمیت خبر را مشخص کن:

high
medium
low

11. برای هر خبر یک emoji مناسب انتخاب کن.

12. برای هر خبر یک سبک انتشار انتخاب کن:

classic
breaking
transfer
match
player
stats

13. سبک را بر اساس خود خبر انتخاب کن.
همه خبرها را classic نکن.

14. خروجی فقط JSON معتبر باشد.

15. هیچ Markdown یا توضیح اضافه ننویس.

16. تعداد آیتم‌های خروجی باید دقیقاً برابر
تعداد خبرهای ورودی باشد.

فرمت دقیق:

[
  {{
    "title": "عنوان فارسی",
    "summary": "خلاصه فارسی",
    "category": "transfer",
    "importance": "high",
    "emoji": "🔥",
    "style": "transfer"
  }}
]

خبرها:

{news_text}
"""


    try:

        print(
            f"Sending {len(articles)} articles "
            f"to OpenAI using model: {AI_MODEL}"
        )


        response = await client.responses.create(

            model=AI_MODEL,

            input=prompt,

        )


        print(
            "OpenAI response received successfully."
        )


        result = (
            response.output_text
            .strip()
        )


        print(
            "OpenAI response length:",
            len(result)
        )


        # =================================================
        # CLEAN MARKDOWN
        # =================================================

        result = re.sub(
            r"^```json\s*",
            "",
            result,
            flags=re.IGNORECASE
        )


        result = re.sub(
            r"^```\s*",
            "",
            result
        )


        result = re.sub(
            r"\s*```$",
            "",
            result
        )


        result = result.strip()


        # =================================================
        # PARSE JSON
        # =================================================

        data = json.loads(
            result
        )


        if not isinstance(
            data,
            list
        ):

            raise ValueError(
                "OpenAI response is not a list."
            )


        if len(data) != len(
            articles
        ):

            raise ValueError(
                "Wrong number of translated "
                f"articles: expected "
                f"{len(articles)}, got "
                f"{len(data)}"
            )


        translated = []


        # =================================================
        # BUILD FINAL ARTICLES
        # =================================================

        for article, item in zip(
            articles,
            data
        ):

            if not isinstance(
                item,
                dict
            ):

                raise ValueError(
                    "One AI result is not an object."
                )


            title = clean_text(
                item.get(
                    "title",
                    ""
                )
            )


            summary = clean_text(
                item.get(
                    "summary",
                    ""
                )
            )


            category = clean_text(
                item.get(
                    "category",
                    "other"
                )
            ).lower()


            importance = clean_text(
                item.get(
                    "importance",
                    "medium"
                )
            ).lower()


            emoji = clean_text(
                item.get(
                    "emoji",
                    "⚽️"
                )
            )


            style = clean_text(
                item.get(
                    "style",
                    "classic"
                )
            ).lower()


            valid_categories = {

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


            valid_importance = {

                "high",
                "medium",
                "low",

            }


            valid_styles = {

                "classic",
                "breaking",
                "transfer",
                "match",
                "player",
                "stats",

            }


            if category not in valid_categories:

                category = "other"


            if importance not in valid_importance:

                importance = "medium"


            if style not in valid_styles:

                style = "classic"


            if not title:

                title = article[
                    "title"
                ]


            if not summary:

                summary = article[
                    "summary"
                ]


            translated.append({

                "source":
                    article["source"],

                "title":
                    title,

                "summary":
                    summary,

                "link":
                    article["link"],

                "image_url":
                    article.get(
                        "image_url"
                    ),

                "category":
                    category,

                "importance":
                    importance,

                "emoji":
                    emoji,

                "style":
                    style,

            })


        print(
            f"Successfully processed "
            f"{len(translated)} articles."
        )


        return translated


    except Exception as e:

        print("")
        print(
            "===================================="
        )
        print(
            "OPENAI ERROR"
        )
        print(
            "ERROR TYPE:",
            type(e).__name__
        )
        print(
            "ERROR:",
            repr(e)
        )
        print(
            "===================================="
        )
        print("")


        return []


# =========================================================
# NEWS DESIGN
# =========================================================

def build_news_text(article):

    title = escape_html(
        clean_text(
            article["title"]
        )
    )


    summary = escape_html(
        clean_text(
            article["summary"]
        )
    )


    source = escape_html(
        clean_text(
            article["source"]
        )
    )


    emoji = escape_html(
        article.get(
            "emoji",
            "⚽️"
        )
    )


    style = article.get(
        "style",
        "classic"
    )


    # =====================================================
    # BREAKING
    # =====================================================

    if style == "breaking":

        return (

            "🚨 <b>خبر فوری</b>\n\n"

            f"{emoji} <b>{title}</b>\n\n"

            f"📝 {summary}\n\n"

            f"🏷 <i>{source}</i>"

        )


    # =====================================================
    # TRANSFER
    # =====================================================

    if style == "transfer":

        return (

            "🔄 <b>نقل‌وانتقالات</b>\n\n"

            f"{emoji} <b>{title}</b>\n\n"

            f"📝 {summary}\n\n"

            f"🏷 <i>{source}</i>"

        )


    # =====================================================
    # MATCH
    # =====================================================

    if style == "match":

        return (

            "🏟️ <b>گزارش فوتبال</b>\n\n"

            f"{emoji} <b>{title}</b>\n\n"

            f"📝 {summary}\n\n"

            f"🏷 <i>{source}</i>"

        )


    # =====================================================
    # PLAYER
    # =====================================================

    if style == "player":

        return (

            "👤 <b>دنیای بازیکنان</b>\n\n"

            f"{emoji} <b>{title}</b>\n\n"

            f"📝 {summary}\n\n"

            f"🏷 <i>{source}</i>"

        )


    # =====================================================
    # STATS
    # =====================================================

    if style == "stats":

        return (

            "📊 <b>آمار و رکورد</b>\n\n"

            f"{emoji} <b>{title}</b>\n\n"

            f"📝 {summary}\n\n"

            f"🏷 <i>{source}</i>"

        )


    # =====================================================
    # CLASSIC
    # =====================================================

    return (

        "🌍⚽️ <b>خبر جدید فوتبال</b>\n\n"

        f"{emoji} <b>{title}</b>\n\n"

        f"📝 {summary}\n\n"

        f"🏷 <i>{source}</i>"

    )


# =========================================================
# TELEGRAM POST
# =========================================================

async def post_article(
    article,
    bot
):

    text = build_news_text(
        article
    )


    image_url = article.get(
        "image_url"
    )


    try:

        # -------------------------------------------------
        # TRY HIGH QUALITY IMAGE
        # -------------------------------------------------

        if image_url:

            try:

                await bot.send_photo(

                    chat_id=CHANNEL_USERNAME,

                    photo=image_url,

                    caption=text,

                    parse_mode="HTML",

                )


                print(
                    "Posted with image:",
                    article["title"]
                )


                return True


            except Exception as image_error:

                print(
                    "IMAGE POST FAILED:",
                    repr(image_error)
                )


                print(
                    "Falling back to text post..."
                )


        # -------------------------------------------------
        # TEXT FALLBACK
        # -------------------------------------------------

        await bot.send_message(

            chat_id=CHANNEL_USERNAME,

            text=text,

            parse_mode="HTML",

        )


        print(
            "Posted as text:",
            article["title"]
        )


        return True


    except Exception as e:

        print(
            "TELEGRAM POST ERROR:",
            repr(e)
        )


        return False


# =========================================================
# KEYBOARD
# =========================================================

def get_main_keyboard():

    keyboard = [

        [
            "📰 آخرین اخبار",
            "🔥 اخبار مهم",
        ],

        [
            "🔄 نقل‌وانتقالات",
            "⚽ اخبار فوتبال",
        ],

        [
            "🤖 درباره Vexa",
            "🆘 راهنما",
        ],

    ]


    return ReplyKeyboardMarkup(

        keyboard,

        resize_keyboard=True

    )


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "سلام 👋🔥\n\n"

        "من <b>Vexa</b> هستم 🤖⚽️\n"

        "دستیار اخبار فوتبال فارسی.\n\n"

        "از منوی پایین می‌تونی بخش موردنظرت رو انتخاب کنی "
        "یا از دستورات استفاده کنی.\n\n"

        "📰 آخرین اخبار\n"
        "🔥 اخبار مهم\n"
        "🔄 نقل‌وانتقالات\n"
        "⚽ اخبار فوتبال\n\n"

        "بریم برای خبرهای جدید! 🚀",

        parse_mode="HTML",

        reply_markup=get_main_keyboard()

    )


# =========================================================
# /HELP
# =========================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "🤖 <b>راهنمای Vexa</b>\n\n"

        "📰 /news\n"
        "آخرین اخبار فوتبال\n\n"

        "🔥 /important\n"
        "خبرهای مهم‌تر\n\n"

        "🔄 /transfers\n"
        "اخبار نقل‌وانتقالات\n\n"

        "⚽ /football\n"
        "اخبار فوتبال\n\n"

        "🤖 /about\n"
        "درباره Vexa\n\n"

        "🧪 /testpost\n"
        "تست ارسال به کانال\n\n"

        "ℹ️ /help\n"
        "نمایش راهنما",

        parse_mode="HTML",

        reply_markup=get_main_keyboard()

    )


# =========================================================
# ABOUT
# =========================================================

async def about_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "🤖 <b>Vexa</b>\n\n"

        "یک ربات خبری فوتبال است که اخبار منابع مختلف "
        "را جمع‌آوری می‌کند، با کمک هوش مصنوعی بازنویسی "
        "می‌کند و به کانال @fcnewsss ارسال می‌کند. ⚽️🔥\n\n"

        "📰 اخبار فوتبال\n"
        "🔄 نقل‌وانتقالات\n"
        "🏟️ مسابقات\n"
        "👤 بازیکنان\n"
        "📊 آمار و رکوردها\n\n"

        "هدف Vexa اینه که خبرها کوتاه، خوانا و جذاب باشن. 🚀",

        parse_mode="HTML",

        reply_markup=get_main_keyboard()

    )


# =========================================================
# FILTERED NEWS
# =========================================================

async def send_filtered_news(
    update,
    context,
    filter_type=None
):

    await update.message.reply_text(

        "⏳ دارم خبرهای جدید رو بررسی می‌کنم... "
        "🤖⚽️"

    )


    articles = collect_articles(
        limit=MAX_ARTICLES
    )


    if not articles:

        await update.message.reply_text(

            "❌ فعلاً خبر جدیدی پیدا نکردم."

        )

        return


    translated = await translate_news_with_ai(
        articles
    )


    if not translated:

        await update.message.reply_text(

            "❌ فعلاً نتونستم اخبار رو آماده کنم. "
            "چند دقیقه دیگه دوباره امتحان کن."

        )

        return


    # =====================================================
    # FILTER
    # =====================================================

    if filter_type == "important":

        filtered = [

            article

            for article in translated

            if article.get(
                "importance"
            ) == "high"

        ]


    elif filter_type == "transfers":

        filtered = [

            article

            for article in translated

            if article.get(
                "category"
            ) == "transfer"

        ]


    else:

        filtered = translated


    if not filtered:

        await update.message.reply_text(

            "ℹ️ در این بررسی خبر مناسبی برای این بخش "
            "پیدا نشد.\n\n"
            "چند دقیقه دیگه دوباره امتحان کن. ⚽️"

        )

        return


    posted = 0


    for article in filtered:

        success = await post_article(

            article,

            context.bot

        )


        if success:

            posted += 1

            sent_links.add(
                article["link"]
            )


        await asyncio.sleep(1)


    await update.message.reply_text(

        f"✅ {posted} خبر ارسال شد. ⚽️🔥"

    )


# =========================================================
# /NEWS
# =========================================================

async def news_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await send_filtered_news(
        update,
        context,
        None
    )


# =========================================================
# /IMPORTANT
# =========================================================

async def important_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await send_filtered_news(
        update,
        context,
        "important"
    )


# =========================================================
# /TRANSFERS
# =========================================================

async def transfers_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await send_filtered_news(
        update,
        context,
        "transfers"
    )


# =========================================================
# /FOOTBALL
# =========================================================

async def football_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await send_filtered_news(
        update,
        context,
        None
    )


# =========================================================
# /TESTPOST
# =========================================================

async def testpost(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    test_article = {

        "source":
            "Vexa",

        "title":
            "تست طراحی جدید Vexa",

        "summary":
            "اگر این پیام را می‌بینی، اتصال Vexa به کانال "
            "درست کار می‌کند و سیستم انتشار فعال است. 🤖⚽️",

        "link":
            "test",

        "image_url":
            None,

        "category":
            "other",

        "importance":
            "medium",

        "emoji":
            "🧪",

        "style":
            "classic",

    }


    success = await post_article(

        test_article,

        context.bot

    )


    if success:

        await update.message.reply_text(

            "✅ پیام تست با موفقیت در کانال ارسال شد."

        )

    else:

        await update.message.reply_text(

            "❌ ارسال پیام تست ناموفق بود."

        )


# =========================================================
# BUTTON HANDLER
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = update.message.text


    if text == "📰 آخرین اخبار":

        await news_command(
            update,
            context
        )

        return


    if text == "🔥 اخبار مهم":

        await important_command(
            update,
            context
        )

        return


    if text == "🔄 نقل‌وانتقالات":

        await transfers_command(
            update,
            context
        )

        return


    if text == "⚽ اخبار فوتبال":

        await football_command(
            update,
            context
        )

        return


    if text == "🤖 درباره Vexa":

        await about_command(
            update,
            context
        )

        return


    if text == "🆘 راهنما":

        await help_command(
            update,
            context
        )

        return


# =========================================================
# AUTOMATIC NEWS
# =========================================================

async def automatic_news(
    context: ContextTypes.DEFAULT_TYPE
):

    print(
        "Checking for new football news..."
    )


    articles = collect_articles(
        limit=MAX_ARTICLES
    )


    if not articles:

        print(
            "No new articles found."
        )

        return


    print(
        f"Found {len(articles)} new articles."
    )


    translated = await translate_news_with_ai(
        articles
    )


    if not translated:

        print(
            "News translation failed. "
            "Articles will be retried later."
        )

        return


    for article in translated:

        success = await post_article(

            article,

            context.bot

        )


        if success:

            sent_links.add(
                article["link"]
            )


            print(
                "Posted:",
                article["title"]
            )


        await asyncio.sleep(1)


# =========================================================
# TELEGRAM COMMAND MENU
# =========================================================

async def post_init(
    application: Application
):

    commands = [

        BotCommand(
            "start",
            "شروع Vexa"
        ),

        BotCommand(
            "news",
            "آخرین اخبار فوتبال"
        ),

        BotCommand(
            "important",
            "اخبار مهم"
        ),

        BotCommand(
            "transfers",
            "اخبار نقل‌وانتقالات"
        ),

        BotCommand(
            "football",
            "اخبار فوتبال"
        ),

        BotCommand(
            "about",
            "درباره Vexa"
        ),

        BotCommand(
            "help",
            "راهنمای Vexa"
        ),

        BotCommand(
            "testpost",
            "تست ارسال به کانال"
        ),

    ]


    await application.bot.set_my_commands(
        commands
    )


# =========================================================
# MAIN
# =========================================================

def main():

    app = (

        Application.builder()

        .token(BOT_TOKEN)

        .post_init(post_init)

        .build()

    )


    # =====================================================
    # COMMANDS
    # =====================================================

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )


    app.add_handler(
        CommandHandler(
            "news",
            news_command
        )
    )


    app.add_handler(
        CommandHandler(
            "important",
            important_command
        )
    )


    app.add_handler(
        CommandHandler(
            "transfers",
            transfers_command
        )
    )


    app.add_handler(
        CommandHandler(
            "football",
            football_command
        )
    )


    app.add_handler(
        CommandHandler(
            "about",
            about_command
        )
    )


    app.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )


    app.add_handler(
        CommandHandler(
            "testpost",
            testpost
        )
    )


    # =====================================================
    # BUTTONS
    # =====================================================

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            button_handler
        )
    )


    # =====================================================
    # AUTOMATIC NEWS
    # =====================================================

    app.job_queue.run_repeating(

        automatic_news,

        interval=NEWS_INTERVAL,

        first=30,

    )


    print(
        "Vexa bot is running..."
    )


    app.run_polling()


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    main()
