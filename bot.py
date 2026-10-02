import os
import json
import re
import asyncio
import feedparser

from openai import AsyncOpenAI
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes


# =========================
# SETTINGS
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

CHANNEL_USERNAME = "@fcnewsss"

AI_MODEL = "gpt-6-luna"


if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set!")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set!")


client = AsyncOpenAI(
    api_key=OPENAI_API_KEY
)


# =========================
# RSS SOURCES
# =========================

RSS_FEEDS = {
    "BBC Sport": "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "The Guardian": "https://www.theguardian.com/football/rss",
    "ESPN": "https://www.espn.com/espn/rss/soccer/news",
}


# جلوگیری از ارسال دوباره خبرها
sent_links = set()


# =========================
# CLEAN TEXT
# =========================

def clean_text(text):
    if not text:
        return ""

    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


# =========================
# COLLECT ARTICLES
# =========================

def collect_articles(limit=9):

    articles = []

    for source_name, feed_url in RSS_FEEDS.items():

        try:

            feed = feedparser.parse(feed_url)

            for entry in feed.entries[:6]:

                title = clean_text(
                    entry.get("title", "")
                )

                summary = clean_text(
                    entry.get("summary", "")
                    or entry.get("description", "")
                )

                link = entry.get("link", "")

                if not title or not link:
                    continue

                articles.append({
                    "source": source_name,
                    "title": title,
                    "summary": summary,
                    "link": link,
                })

        except Exception as e:

            print(
                f"RSS ERROR ({source_name}): {repr(e)}"
            )


    # حذف خبرهای تکراری

    unique_articles = []
    seen_links = set()

    for article in articles:

        if article["link"] in seen_links:
            continue

        seen_links.add(article["link"])
        unique_articles.append(article)


    return unique_articles[:limit]


# =========================
# AI TRANSLATION
# =========================

async def translate_news_with_ai(articles):

    if not articles:
        return []


    news_text = ""


    for i, article in enumerate(
        articles,
        start=1
    ):

        news_text += f"""

NEWS {i}

SOURCE:
{article["source"]}

TITLE:
{article["title"]}

SUMMARY:
{article["summary"][:2500]}

"""


    prompt = f"""
تو یک مترجم و ویراستار حرفه‌ای اخبار فوتبال هستی.

خبرهای زیر انگلیسی هستند.
آن‌ها را برای یک کانال تلگرامی فارسی‌زبان
به فارسی روان و طبیعی تبدیل کن.

قوانین:

- اطلاعات جدید از خودت اضافه نکن.
- اسم بازیکنان، مربیان، باشگاه‌ها و مسابقات را درست حفظ کن.
- ترجمه کلمه‌به‌کلمه نباشد.
- فارسی طبیعی و خبری بنویس.
- عنوان کوتاه و خبری باشد.
- خلاصه هر خبر حدود 2 تا 4 جمله باشد.
- چیزی را حدس نزن.
- خروجی فقط JSON معتبر باشد.
- هیچ Markdown ننویس.
- هیچ توضیح اضافه‌ای خارج از JSON ننویس.

فرمت دقیق خروجی:

[
  {{
    "title": "عنوان فارسی",
    "summary": "خلاصه فارسی"
  }}
]

تعداد خروجی باید دقیقاً برابر تعداد NEWSهای ورودی باشد.

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


        print("OpenAI response received successfully.")


        result = response.output_text.strip()


        print(
            "OpenAI raw response length:",
            len(result)
        )


        # حذف احتمالی Markdown

        result = re.sub(
            r"^```json\s*",
            "",
            result
        )

        result = re.sub(
            r"\s*```$",
            "",
            result
        )

        result = result.strip()


        data = json.loads(result)


        if not isinstance(data, list):

            raise ValueError(
                "OpenAI response is not a list."
            )


        if len(data) != len(articles):

            raise ValueError(
                f"Wrong number of articles. "
                f"Expected {len(articles)}, "
                f"got {len(data)}"
            )


        translated = []


        for article, item in zip(
            articles,
            data
        ):

            title = clean_text(
                item.get("title", "")
            )

            summary = clean_text(
                item.get("summary", "")
            )


            if not title:

                title = article["title"]


            if not summary:

                summary = article["summary"]


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
            f"Successfully translated "
            f"{len(translated)} articles."
        )


        return translated


    except Exception as e:

        print("")
        print("====================================")
        print("OPENAI ERROR")
        print("ERROR TYPE:", type(e).__name__)
        print("ERROR:", repr(e))
        print("====================================")
        print("")

        return []


# =========================
# POST TO TELEGRAM
# =========================

async def post_article(
    article,
    bot
):

    text = (

        "🌍⚽️ <b>خبر جدید فوتبال</b>\n\n"

        f"📰 <b>{article['title']}</b>\n\n"

        f"📝 {article['summary']}\n\n"

        f"🏷 منبع: {article['source']}"

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


# =========================
# START
# =========================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "سلام 👋\n\n"

        "من Vexa هستم 🤖⚽️\n"

        "ربات اخبار فوتبال فارسی.\n\n"

        "/news - آخرین اخبار فوتبال\n"

        "/help - راهنما"

    )


# =========================
# HELP
# =========================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "🤖 راهنمای Vexa\n\n"

        "/start - شروع Vexa\n"

        "/news - آخرین اخبار فوتبال\n"

        "/testpost - تست ارسال به کانال\n"

        "/help - راهنما"

    )


# =========================
# TEST POST
# =========================

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


# =========================
# NEWS COMMAND
# =========================

async def news_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "⏳ دارم آخرین اخبار رو به فارسی آماده می‌کنم... 🤖⚽️"

    )


    articles = collect_articles(
        limit=9
    )


    if not articles:

        await update.message.reply_text(

            "❌ فعلاً خبری از منابع دریافت نکردم."

        )

        return


    translated = await translate_news_with_ai(
        articles
    )


    if not translated:

        await update.message.reply_text(

            "❌ فعلاً نتونستم اخبار رو ترجمه کنم. "
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


        await asyncio.sleep(1)


    await update.message.reply_text(

        f"✅ {posted} خبر آماده و در کانال ارسال شد. ⚽️🔥"

    )


# =========================
# AUTOMATIC NEWS
# =========================

async def automatic_news(
    context: ContextTypes.DEFAULT_TYPE
):

    print(
        "Checking for new football news..."
    )


    articles = collect_articles(
        limit=9
    )


    if not articles:

        print(
            "No articles found."
        )

        return


    new_articles = [

        article

        for article in articles

        if article["link"]
        not in sent_links

    ]


    if not new_articles:

        print(
            "No new articles."
        )

        return


    print(
        f"Found {len(new_articles)} "
        f"new articles."
    )


    translated = await translate_news_with_ai(

        new_articles

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


# =========================
# TELEGRAM MENU
# =========================

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


# =========================
# MAIN
# =========================

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


    # بررسی خودکار هر 10 دقیقه

    app.job_queue.run_repeating(

        automatic_news,

        interval=600,

        first=30,

    )


    print(
        "Vexa bot is running..."
    )


    app.run_polling()


if __name__ == "__main__":

    main()
