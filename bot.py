import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! 👋\n"
        "من Vexa هستم 🤖\n"
        "بات با موفقیت فعال شد!"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "دستورات موجود:\n"
        "/start - شروع بات\n"
        "/help - راهنما\n"
        "/testpost - تست ارسال پیام به کانال"
    )


async def test_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id="@fcnewsss",
        text="🤖 Vexa با موفقیت به کانال متصل شد! 🚀"
    )
    await update.message.reply_text(
        "✅ پیام آزمایشی در کانال ارسال شد."
    )


def main():
    token = os.getenv("BOT_TOKEN")

    if not token:
        raise ValueError("BOT_TOKEN is not set!")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("testpost", test_post))

    print("Vexa bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
