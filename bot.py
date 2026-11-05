"""
UZEX AI — Telegram bot (@uzexaihackaton_bot).

Audit fixes:
  #4  Telegram 4096-char limit -> automatic chunking at 4000 chars, split on
      newline/word boundaries, Markdown -> plain-text fallback.
  #2  Per-user language memory + robust detection (never defaults to Russian).
  #1/#3  All answering goes through UzexAgent (retry/backoff, NO_QUERY guard,
      near-match relaxation, read-only SQL).

Run:
  TELEGRAM_BOT_TOKEN=... python bot.py
"""
from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from agent import STRINGS, UzexAgent, detect_language, format_footer

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("uzex.bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHUNK_SIZE = 4000  # safe margin under Telegram's 4096 limit

agent = UzexAgent()
_chat_lang: dict[int, str] = {}


# ---------------------------------------------------------------------------
# Fix #4 — split long messages and send them safely
# ---------------------------------------------------------------------------
def split_message(text: str, size: int = CHUNK_SIZE) -> list[str]:
    """Split text into <=size chunks, preferring newline then space boundaries."""
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > size:
        cut = rest.rfind("\n", 0, size)
        if cut < size // 2:
            cut = rest.rfind(" ", 0, size)
        if cut < size // 2:
            cut = size
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


async def send_split_message(target, text: str, parse_mode: str | None = ParseMode.MARKDOWN):
    """Send a (possibly long) message as several Telegram messages.

    Falls back to plain text if Markdown parsing fails (audit fix #4).
    """
    for part in split_message(text):
        try:
            await target.reply_text(part, parse_mode=parse_mode)
        except Exception:  # noqa: BLE001 — BadRequest on bad markdown, etc.
            try:
                await target.reply_text(part)
            except Exception as e:  # noqa: BLE001
                log.error("Failed to send chunk: %s", e)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _chat_lang.get(update.effective_chat.id, "uz")
    text = STRINGS[lang]["greeting"]
    text += ("\n\n/til uz — o'zbekcha\n/til ru — русский" if lang == "uz"
             else "\n\n/til uz — o'zbekcha\n/til ru — русский")
    await send_split_message(update.message, text, parse_mode=None)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = _chat_lang.get(update.effective_chat.id, "uz")
    if lang == "uz":
        text = ("UZEX AI — birja bitimlari tahlilchisi.\n\n"
                "• O'zbek yoki rus tilida savol bering.\n"
                "• Har bir javob ostida SQL so'rov ko'rsatiladi.\n"
                "• Aniq moslik topilmasa, yaqin natijalar taklif qilinadi.\n\n"
                "Misollar:\n"
                "— 2024-yilda jami qancha so'mlik bitim tuzilgan?\n"
                "— Eng ko'p bitim o'tkazgan TOP 3 broker qaysilar?\n"
                "— BK001 brokeri bo'yicha statistika\n"
                "— Крупнейший покупатель за всё время?\n\n"
                "/til uz|ru — javob tilini tanlash")
    else:
        text = ("UZEX AI — аналитик биржевых сделок.\n\n"
                "• Задавайте вопросы на узбекском или русском.\n"
                "• Под каждым ответом показывается SQL-запрос.\n"
                "• Если точного совпадения нет, предлагаются близкие результаты.\n\n"
                "Примеры:\n"
                "— Какой общий объём сделок за 2024 год?\n"
                "— Какие ТОП-3 брокера провели больше всего сделок?\n"
                "— Статистика по брокеру BK001\n"
                "— 2024-yilda jami aylanma qancha?\n\n"
                "/til uz|ru — выбор языка ответа")
    await send_split_message(update.message, text, parse_mode=None)


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    choice = args[0].lower() if args else ""
    chat_id = update.effective_chat.id
    if choice in ("uz", "ru"):
        _chat_lang[chat_id] = choice
        msg = ("Javob tili: o'zbekcha 🇺🇿" if choice == "uz"
               else "Язык ответов: русский 🇷🇺")
    else:
        msg = ("Tilni tanlang: /til uz yoki /til ru\n"
               "Выберите язык: /til uz или /til ru")
    await send_split_message(update.message, msg, parse_mode=None)


async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = (update.message.text or "").strip()
    if not question:
        return
    chat_id = update.effective_chat.id
    default_lang = _chat_lang.get(chat_id, "uz")
    lang = detect_language(question, default=default_lang)
    _chat_lang[chat_id] = lang  # remember the user's language

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    loop = asyncio.get_running_loop()
    try:
        answer = await loop.run_in_executor(None, agent.answer, question, lang, default_lang)
    except Exception as e:  # noqa: BLE001
        log.exception("agent failed")
        err = STRINGS[lang]["api_down"]
        await send_split_message(update.message, err, parse_mode=None)
        return

    text = format_footer(answer, include_sql=True)
    await send_split_message(update.message, text)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Telegram error: %s", context.error)


def main():
    if not TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Add it to .env (get it from @BotFather).\n"
            "The web UI (webapp/server.py) works without a Telegram token.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler(["til", "lang", "language"], cmd_lang))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_question))
    app.add_error_handler(on_error)
    log.info("UZEX AI bot starting (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
