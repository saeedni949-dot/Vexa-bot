import os
import json
import re
import asyncio
import html
import feedparser

from openai import AsyncOpenAI
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes


# =========================================================
# SETTINGS
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

CHANNEL_USERNAME = "@fcnewsss"

AI_MODEL = "gpt-6-luna"

# تعداد خبرهایی که برای هر بار بررسی دریافت می‌شود
MAX_ARTICLES = 9

# فاصله بررسی خودکار اخبار: 10 دقیقه
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

# لینک خبرهایی که با موفقیت ارسال شده‌اند
sent_links = set()


# =========================================================
# TEXT CLEANING
# =========================================================

def clean_text(text):
    """
    پاک‌سازی متن RSS و HTML
    """

    if not text:
        return ""

    # Decode HTML entities
    text = html.unescape(text)

    # حذف HTML
    text = re.sub(
        r"<[^>]+>",
        " ",
        text
    )

    # حذف فاصله‌های اضافی
    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def limit_text(text, max_length):
    """
    محدود کردن طول متن
    """

    if not text:
        return ""

    if len(text) <= max_length:
        return text

    return text[:max_length].rsplit(
        " ",
        1
    )[0] + "..."


# =========================================================
# COLLECT NEWS
# =========================================================

def collect_articles(limit=MAX_ARTICLES):

    articles = []

    for source_name, feed_url in RSS_FEEDS.items():

        try:

            feed = feedparser.parse(feed_url)

            print(
                f"Reading source: {source_name}"
            )

            # تعداد محدودی از هر منبع
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

                articles.append({

                    "source":
                        source_name,

                    "title":
                        title,

                    "summary":
                        summary,

                    "link":
                        link,

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


        seen_links.add(link)

        seen_titles.add(title_key)

        unique_articles.append(
            article
        )


    # =====================================================
    # REMOVE ALREADY SENT NEWS
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


    # =====================================================
    # BUILD INPUT
    # =====================================================

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


    # =====================================================
    # PROMPT
    # =====================================================

    prompt = f"""
تو سردبیر حرفه‌ای یک کانال تلگرامی اخبار فوتبال فارسی هستی.

وظیفه تو این است که خبرهای انگلیسی زیر را به شکل
یک خبر کوتاه، حرفه‌ای و طبیعی برای مخاطب فارسی‌زبان بازنویسی کنی.

این کار «ترجمه کلمه‌به‌کلمه» نیست.
باید مفهوم خبر را درست بفهمی و سپس آن را به فارسی
روان و خبری بنویسی.

قوانین بسیار مهم:

1. هیچ اطلاعاتی که در متن اصلی وجود ندارد اضافه نکن.

2. اگر درباره موضوعی مطمئن نیستی، حدس نزن.

3. اسم بازیکنان، مربیان، باشگاه‌ها، تیم‌های ملی،
   مسابقات و رقابت‌ها را حفظ کن.

4. عنوان باید:
   - کوتاه باشد
   - خبری باشد
   - مهم‌ترین نکته خبر را منتقل کند
   - ترجیحاً حدود 8 تا 15 کلمه باشد

5. خلاصه باید:
   - حدود 2 تا 3 جمله باشد
   - مهم‌ترین اطلاعات خبر را منتقل کند
   - از حاشیه و تکرار دور باشد
   - برای خواندن سریع در تلگرام مناسب باشد

6. اگر متن RSS ناقص یا کوتاه است،
   اطلاعاتی از خودت نساز.

7. از عبارت‌های کلیشه‌ای مثل
   «در خبری مهم»،
   «هواداران فوتبال را شوکه کرد»
   یا «اتفاقی باورنکردنی»
   استفاده نکن؛ مگر اینکه خود متن واقعاً چنین چیزی را بیان کند.

8. لحن:
   حرفه‌ای، ورزشی، روان و بی‌طرف.

9. متن را با فارسی طبیعی بنویس، نه ترجمه ماشینی.

10. خروجی فقط JSON معتبر باشد.

11. هیچ Markdown، توضیح اضافی یا متن خارج از JSON ننویس.

12. تعداد آیتم‌های خروجی باید دقیقاً برابر تعداد خبرهای ورودی باشد.

فرمت دقیق:

[
  {{
    "title": "عنوان فارسی خبر",
    "summary": "خلاصه فارسی خبر"
  }}
]

خبرها:

{news_text}
"""


    # =====================================================
    # OPENAI REQUEST
    # =====================================================

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
        # CLEAN POSSIBLE MARKDOWN
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


            # Fallback
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
# TELEGRAM POST
# =========================================================

async def post_article(
    article,
    bot
):

    title = clean_text(
        article["title"]
    )

    summary = clean_text(
        article["summary"]
    )

    source = clean_text(
        article["source"]
    )


    text = (

        "🌍⚽️ <b>خبر جدید فوتبال</b>\n\n"

        f"📰 <b>{title}</b>\n\n"

        f"📝 {summary}\n\n"

        f"🏷 منبع: {source}"

    )


    try:

        await bot.send_message(

            chat_id=CHANNEL_USERNAME,

            text=text,

            parse_mode="HTML",

        )


        return True


    except Exception as e:

        print(
            "TELEGRAM POST ERROR:",
            repr(e)
        )

        return False


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "سلام 👋\n\n"

        "من Vexa هستم 🤖⚽️\n"

        "ربات اخبار فوتبال فارسی.\n\n"

        "📰 /news - آخرین اخبار فوتبال\n"

        "ℹ️ /help - راهنما"

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

        "/start - شروع Vexa\n"

        "/news - آخرین اخبار فوتبال\n"

        "/testpost - تست ارسال به کانال\n"

        "/help - راهنما",

        parse_mode="HTML"

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
            "تست ارسال خبر توسط Vexa",

        "summary":
            "اگر این پیام را می‌بینی، "
            "اتصال Vexa به کانال درست کار می‌کند. 🤖⚽️",

        "link":
            "test",

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
# /NEWS
# =========================================================

async def news_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "⏳ دارم آخرین اخبار رو "
        "با کیفیت بهتر آماده می‌کنم... 🤖⚽️"

    )


    articles = collect_articles(
        limit=MAX_ARTICLES
    )


    if not articles:

        await update.message.reply_text(

            "❌ فعلاً خبر جدیدی از منابع دریافت نکردم."

        )

        return


    translated = (
        await translate_news_with_ai(
            articles
        )
    )


    if not translated:

        await update.message.reply_text(

            "❌ فعلاً نتونستم اخبار رو آماده کنم. "
            "چند دقیقه دیگه دوباره امتحان کن."

        )

        return


    posted = 0


    for article in translated:

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

        f"✅ {posted} خبر با موفقیت "
        "آماده و در کانال ارسال شد. ⚽️🔥"

    )


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


    translated = (
        await translate_news_with_ai(
            articles
        )
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
