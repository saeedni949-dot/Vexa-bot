import os
import feedparser
import html
import re
import time

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

    if len(summary) > 500:
        summary = summary[:500].rsplit(" ", 1)[0] + "..."

    return summary


# =========================
# Batch Translation
# =========================

def translate_batch(texts):
    if not texts:
        return []

    try:
        translator = GoogleTranslator(
            source="en",
            target="fa"
        )

        results = translator.translate_batch(texts)

        return [
            clean_text(result) if result else original
            for result, original in zip(results, texts)
        ]

    except Exception as e:
        print(f"Translation error: {e}")

        # اگر ترجمه شکست خورد، متن اصلی برگردانده می‌شود
        return texts


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
# News Command
# =========================

async def news(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    articles = []

    # دریافت اخبار
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

            articles.append({
                "source": source,
                "title": title,
                "summary": summary,
            })

    if not articles:

        await update.message.reply_text(
            "❌ فعلاً خبری پیدا نکردم."
        )

        return

    # ساخت لیست برای ترجمه
    translation_texts = []

    for article in articles[:9]:

        translation_texts.append(
            article["title"]
        )

        if article["summary"]:
            translation_texts.append(
                article["summary"]
            )

    # ترجمه دسته‌ای
    translated = translate_batch(
        translation_texts
    )

    # ساخت خبرهای فارسی
    translated_index = 0
    messages = []

    for article in articles[:9]:

        persian_title = translated[
            translated_index
        ]

        translated_index += 1

        persian_summary = ""

        if article["summary"]:

            persian_summary = translated[
                translated_index
            ]

            translated_index += 1

        if persian_summary:

            message = (
                f"⚽️ {persian_title}\n\n"
                f"📝 {persian_summary}\n\n"
                f"🏷 منبع: {article['source']}"
            )

        else:

            message = (
                f"⚽️ {persian_title}\n\n"
                f"🏷 منبع: {article['source']}"
            )

        messages.append(message)

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

    articles = []

    # دریافت اخبار جدید
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

            link = article.get(
                "link",
                ""
            )

            if not link:
                continue

            if link in sent_links:
                continue

            summary = get_summary(article)

            articles.append({
                "source": source,
                "title": title,
                "summary": summary,
                "link": link,
            })

    if not articles:
        return

    # فقط خبرهای جدید
    articles = articles[:9]

    # ساخت متن‌های ترجمه
    translation_texts = []

    for article in articles:

        translation_texts.append(
            article["title"]
        )

        if article["summary"]:

            translation_texts.append(
                article["summary"]
            )

    # ترجمه دسته‌ای
    translated = translate_batch(
        translation_texts
    )

    translated_index = 0

    for article in articles:

        # اینجا خبر به عنوان پردازش‌شده ثبت می‌شود
        sent_links.add(
            article["link"]
        )

        persian_title = translated[
            translated_index
        ]

        translated_index += 1

        persian_summary = ""

        if article["summary"]:

            persian_summary = translated[
                translated_index
            ]

            translated_index += 1

        if persian_summary:

            message = (
                "🌍🇮🇷⚽️ خبر جدید فوتبال\n\n"
                f"📰 {persian_title}\n\n"
                f"📝 {persian_summary}\n\n"
                f"🏷 منبع: {article['source']}"
            )

        else:

            message = (
                "🌍🇮🇷⚽️ خبر جدید فوتبال\n\n"
                f"📰 {persian_title}\n\n"
                f"🏷 منبع: {article['source']}"
            )

        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=message
        )

        # فاصله کوتاه بین ارسال‌های تلگرام
        time.sleep(1)


# =========================
# Main
# =========================

def main():

    token = os.getenv(
        "BOT_TOKEN"
    )

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

    # Automatic news
    # هر ۱۰ دقیقه

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
