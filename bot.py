from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = "8971114808:AAEMgvMok4X_PE40Lio5ZrqXGgVWjB8hZm8"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Hello! My bot is working.")    

app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))

app.run_polling()