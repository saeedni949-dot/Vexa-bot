import os
import feedparser
import html
import re
import json

from openai import AsyncOpenAI

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)


# =========================
# Settings
# =========================

CHANNEL_ID = "@fcnewsss"

RSS_FEEDS = {
    "BBC Sport": "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "The Guardian": "https://www.theguardian.com/football/rss",
    "ESPN": "https://www.espn.com/espn/rss/soccer/news",
}

sent_links = set()


# =========================
# OpenAI
# =========================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY is not set!")

client = AsyncOpenAI(
    api_key=OPENAI_API_KEY
)

AI_MODEL = "gpt-5.6-luna"


# =========================
# Text Cleaning
# =========================

def clean_text(text):
    if not text:
        return ""

    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def get_summary(article):
    summary = article.get("summary", "")

    if not summary:
        summary = article.get("description", "")

    summary = clean_text(summary)

    if len(summary) > 700:
        summary = summary[:700].rsplit(" ", 1)[0] + "..."

    return summary


# =========================
# AI Translation + Summary
# =========================

async def translate_news_with_ai(articles):

    if not articles:
        return []

    news_text = ""

    for index, article in enumerate(articles, start=1):

        news_text += (
            f"\n\n--- NEWS {index} ---\n"
            f"TITLE: {article['title']}\n"
            f"SUMMARY: {article['summary']}\n"
        )

    prompt = f"""
تو ویراستار خبری فارسی برای یک کانال فوتبال هستی.

خبرهای زیر به زبان انگلیسی هستند.

برای هر خبر:

1. یک تیتر فارسی طبیعی، کوتاه و خبری بنویس.
2. خلاصه خبر را به فارسی روان و قابل فهم بنویس.
3. ترجمه تحت‌اللفظی نکن.
4. اطلاعات جدیدی به خبر اضافه نکن.
5. نام بازیکنان، باشگاه‌ها، مربیان و مسابقات را درست حفظ کن.
6. اگر متن ناقص است، چیزی از خودت حدس نزن.
7. خلاصه هر خبر حداکثر حدود 3 جمله باشد.

حتماً پاسخ را فقط به صورت JSON معتبر بده.

فرمت دقیق:

[
  {{
    "title": "تیتر فارسی",
    "summary": "خلاصه فارسی"
  }}
]

تعداد آیتم‌های خروجی باید دقیقاً برابر تعداد خبرهای ورودی باشد.

خبرها:
{news_text}
"""

    try:

        response = await client.responses.create(
            model=AI_MODEL,
            input=prompt
        )

        result = response.output_text.strip()

        # حذف احتمالی ```json
        result = re.sub(
            r"^```json\s*",
            "",
            result,
            flags=re.IGNORECASE
        )

        result = re.sub(
            r"\s*```$",
            "",
            result
        )

        translated = json.loads(result)

        if not isinstance(translated, list):
            raise ValueError("AI response is not a list")

        if len(translated) != len(articles):
            raise ValueError(
                "AI returned wrong number of news items"
            )

        return translated

    except Exception as e:

        print(
            f"AI translation error: {e}"
        )

        return None


# =========================
# Get Latest Articles
# =========================

def collect_articles():

    articles = []

    for source, url in RSS_FEEDS.items():

        feed = feedparser.parse(url)

        if not feed.entries:
            continue

        for article in feed.entries[:3]:

            title = clean_text(
                article.get(
                    "title",
                    "بدون عنوان"
                )
            )

            summary = get_summary(article)

            link = article.get(
                "link",
                ""
            )

            if not link:
                continue

            articles.append({
                "source": source,
                "title": title,
                "summary": summary,
                "link": link,
            })

    return articles


# =========================
# Create Persian Messages
# =========================

def build_messages(
    articles,
    translated
):

    messages = []

    for article, translation in zip(
        articles,
        translated
    ):

        title = clean_text(
            translation.get(
                "title",
                article["title"]
            )
        )

        summary = clean_text(
            translation.get(
                "summary",
                ""
            )
        )

        if summary:

            message = (
                "🌍🇮🇷⚽️ خبر جدید فوتبال\n\n"
                f"📰 {title}\n\n"
                f"📝 {summary}\n\n"
                f"🏷 منبع: {article['source']}"
            )

        else:

            message = (
                "🌍🇮🇷⚽️ خبر جدید فوتبال\n\n"
                f"📰 {title}\n\n"
                f"🏷 منبع: {article['source']}"
            )

        messages.append(message)

    return messages


# =========================
# Telegram Command Menu
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
# Start
# =========================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "سلام! 👋\n"
        "من Vexa هستم 🤖⚽\n\n"
        "بات با موفقیت فعال شد! 🚀\n\n"
        "از منوی پایین می‌تونی دستورات مختلف رو انتخاب کنی."
    )


# =========================
# Help
# =========================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "📚 راهنمای Vexa\n\n"
        "/start - شروع بات\n"
        "/news - دریافت آخرین اخبار فوتبال\n"
        "/help - نمایش راهنما\n"
        "/testpost - تست ارسال پیام به کانال"
    )


# =========================
# Test Post
# =========================

async def test_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text="🤖 Vexa با موفقیت به کانال متصل شد! 🚀"
    )

    await update.message.reply_text(
        "✅ پیام آزمایشی در کانال ارسال شد."
    )


# =========================
# /news
# =========================

async def news(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "⏳ دارم آخرین اخبار رو به فارسی آماده می‌کنم... 🤖⚽️"
    )

    articles = collect_articles()

    if not articles:

        await update.message.reply_text(
            "❌ فعلاً خبری پیدا نکردم."
        )

        return

    articles = articles[:9]

    translated = await translate_news_with_ai(
        articles
    )

    if translated is None:

        await update.message.reply_text(
            "❌ فعلاً نتونستم اخبار رو ترجمه کنم. "
            "چند دقیقه دیگه دوباره امتحان کن."
        )

        return

    messages = build_messages(
        articles,
        translated
    )

    text = (
        "🌍🇮🇷 آخرین اخبار فوتبال\n\n"
        + "\n\n".join(messages)
    )

    await update.message.reply_text(
        text
    )


# =========================
# Automatic News
# =========================

async def automatic_news(
    context: ContextTypes.DEFAULT_TYPE
):

    articles = collect_articles()

    new_articles = []

    for article in articles:

        if article["link"] in sent_links:
            continue

        new_articles.append(article)

    if not new_articles:
        return

    # حداکثر 9 خبر در هر بررسی
    new_articles = new_articles[:9]

    translated = await translate_news_with_ai(
        new_articles
    )

    if translated is None:

        print(
            "News translation failed. "
            "Articles will be retried later."
        )

        return

    messages = build_messages(
        new_articles,
        translated
    )

    for article, message in zip(
        new_articles,
        messages
    ):

        try:

            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=message
            )

            # فقط بعد از ارسال موفق
            # خبر را به عنوان ارسال‌شده ثبت می‌کنیم

            sent_links.add(
                article["link"]
            )

        except Exception as e:

            print(
                f"Telegram send error: {e}"
            )


# =========================
# Main
# =========================

def main():

    bot_token = os.getenv(
        "BOT_TOKEN"
    )

    if not bot_token:

        raise ValueError(
            "BOT_TOKEN is not set!"
        )

    app = (
        Application.builder()
        .token(bot_token)
        .post_init(post_init)
        .build()
    )

    # Commands

    app.add_handler(
        CommandHandler(
            "start",
            start
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
            test_post
        )
    )

    app.add_handler(
        CommandHandler(
            "news",
            news
        )
    )

    # Automatic news every 10 minutes

    app.job_queue.run_repeating(
        automatic_news,
        interval=600,
        first=30
    )

    print(
        "Vexa bot is running..."
    )

    app.run_polling()


if __name__ == "__main__":
    main()
