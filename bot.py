import os
import feedparser
import html
import re

from deep_translator import GoogleTranslator

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)


CHANNEL_ID = "@fcnewsss"

RSS_FEEDS = {
    "BBC Sport": "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "The Guardian": "https://www.theguardian.com/football/rss",
    "ESPN": "https://www.espn.com/espn/rss/soccer/news",
}

sent_links = set()

translator = GoogleTranslator(
    source="en",
    target="fa"
)


def clean_text(text):
    if not text:
        return ""

    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def translate_to_persian(text):
    if not text:
        return ""

    try:
        translated = translator.translate(text)
        return clean_text(translated)

    except Exception as e:
        print(f"Translation error: {e}")
        return text


def get_summary(article):
    summary = article.get("summary", "")

    if not summary:
        summary = article.get("description", "")

    summary = clean_text(summary)

    if len(summary) > 500:
        summary = summary[:500].rsplit(" ", 1)[0] + "..."

    return summary


def translate_news(title, summary):
    persian_title = translate_to_persian(title)

    if summary:
        persian_summary = translate_to_persian(summary)
    else:
        persian_summary = ""

    return persian_title, persian_summary


# =========================
# Telegram Command Menu
# =========================

async def post_init(application: Application):

    commands = [
        BotCommand("start", "شروع Vexa"),
        BotCommand("news", "آخرین اخبار فوتبال"),
        BotCommand("help", "راهنمای Vexa"),
        BotCommand("testpost", "تست ارسال به کانال"),
    ]

    await application.bot.set_my_commands(commands)


# =========================
# Commands
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
# News Command
# =========================

async def news(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    messages = []

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

            persian_title, persian_summary = translate_news(
                title,
                summary
            )

            if persian_summary:

                message = (
                    f"⚽️ {persian_title}\n\n"
                    f"📝 {persian_summary}\n\n"
                    f"🏷 منبع: {source}"
                )

            else:

                message = (
                    f"⚽️ {persian_title}\n\n"
                    f"🏷 منبع: {source}"
                )

            messages.append(message)

    if not messages:

        await update.message.reply_text(
            "❌ فعلاً خبری پیدا نکردم."
        )

        return

    text = (
        "🌍🇮🇷 آخرین اخبار فوتبال\n\n"
        + "\n\n".join(messages[:9])
    )

    await update.message.reply_text(text)


# =========================
# Automatic News
# =========================

async def automatic_news(
    context: ContextTypes.DEFAULT_TYPE
):

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

            link = article.get("link", "")

            if not link:
                continue

            # جلوگیری از ارسال خبر تکراری
            if link in sent_links:
                continue

            sent_links.add(link)

            summary = get_summary(article)

            # ترجمه به فارسی
            persian_title, persian_summary = translate_news(
                title,
                summary
            )

            if persian_summary:

                message = (
                    "🌍🇮🇷⚽️ خبر جدید فوتبال\n\n"
                    f"📰 {persian_title}\n\n"
                    f"📝 {persian_summary}\n\n"
                    f"🏷 منبع: {source}"
                )

            else:

                message = (
                    "🌍🇮🇷⚽️ خبر جدید فوتبال\n\n"
                    f"📰 {persian_title}\n\n"
                    f"🏷 منبع: {source}"
                )

            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=message
            )


# =========================
# Main
# =========================

def main():

    token = os.getenv("BOT_TOKEN")

    if not token:
        raise ValueError(
            "BOT_TOKEN is not set!"
        )

    app = (
        Application.builder()
        .token(token)
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

    # اخبار خودکار هر ۱۰ دقیقه

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
