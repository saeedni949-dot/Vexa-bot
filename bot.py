import os
import feedparser

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


RSS_URL = "https://feeds.bbci.co.uk/sport/football/rss.xml"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! 👋\n"
        "من Vexa هستم 🤖⚽\n"
        "بات با موفقیت فعال شد!"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "دستورات موجود:\n"
        "/start - شروع بات\n"
        "/help - راهنما\n"
        "/testpost - تست ارسال پیام به کانال\n"
        "/news - دریافت آخرین خبر فوتبال"
    )


async def test_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id="@fcnewsss",
        text="🤖 Vexa با موفقیت به کانال متصل شد! 🚀"
    )

    await update.message.reply_text(
        "✅ پیام آزمایشی در کانال ارسال شد."
    )


async def news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    feed = feedparser.parse(RSS_URL)

    if not feed.entries:
        await update.message.reply_text(
            "❌ فعلاً خبری پیدا نکردم."
        )
        return

    messages = []

    for article in feed.entries[:5]:
        title = article.get("title", "بدون عنوان")
        link = article.get("link", "")

        messages.append(
            f"⚽️ {title}\n"
            f"🔗 {link}"
        )

    text = "🌍 آخرین اخبار فوتبال\n\n" + "\n\n".join(messages)

    await update.message.reply_text(text)

    if not feed.entries:
        await update.message.reply_text(
            "❌ فعلاً خبری پیدا نکردم."
        )
        return

    article = feed.entries[0]

    title = article.get("title", "بدون عنوان")
    link = article.get("link", "")

    message = (
        "⚽️ خبر جدید فوتبال\n\n"
        f"📰 {title}\n\n"
        f"🔗 {link}"
    )

    await update.message.reply_text(message)


def main():
    token = os.getenv("BOT_TOKEN")

    if not token:
        raise ValueError("BOT_TOKEN is not set!")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("testpost", test_post))
    app.add_handler(CommandHandler("news", news))

    print("Vexa bot is running...")

    app.run_polling()


if __name__ == "__main__":
    main()
