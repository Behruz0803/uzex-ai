import os
import logging
import tempfile
import contextvars
import httpx
from datetime import datetime
from dotenv import load_dotenv

from telegram import (
    Update, BotCommand, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    TypeHandler, filters, ContextTypes
)
from user_store import save_user
from ai_engine import (
    matn_javob, media_javob, tarixni_tozala, set_joylashuv, get_joylashuv, get_tarix,
    GeminiQuotaError,
)
# UZEX integration
try:
    from uzex.agent import answer as uzex_answer, detect_language
    from uzex.db import get_db_path, get_schema_summary
    UZEX_ENABLED = True
except Exception as e:
    print(f"UZEX import warning: {e}")
    uzex_answer = None
    UZEX_ENABLED = False

load_dotenv()

# Bir jarayon — bitta bot. UZEX va ta'mir ustasi bir-biriga ulanmaydi.
# BOT_MODE=uzex  -> faqat birja (shu deploy, @uzexaihackaton_bot)
# BOT_MODE=usta  -> faqat ta'mir (boshqa token, boshqa manzil; bu yerda yoqilmagan)
BOT_MODE = os.getenv("BOT_MODE", "uzex").strip().lower()
IS_USTA = BOT_MODE == "usta"
IS_UZEX = not IS_USTA
# Bir jarayonda ikki bot bo'lsa ham, har update o'z botiga qarab rejim tanlaydi.
request_mode = contextvars.ContextVar("request_mode", default="")
USTA_BOT_ID = 8731876262
EXPECTED_BOT_USERNAME = os.getenv(
    "EXPECTED_BOT_USERNAME",
    "ai_usta_bot" if IS_USTA else "uzexaihackaton_bot",
).lstrip("@")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════
# GURUH ID RAQAMINI PARSING QILISH
# ═══════════════════════════════════════
def _chat_id(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


LOG_CHAT_ID = _chat_id(os.getenv("LOG_CHAT_ID"))


def usta_log_chat_id():
    """AI Usta mijoz xabarlari. Alohida guruh bo'lsa USTA_LOG_CHAT_ID, aks holda LOG_CHAT_ID."""
    return _chat_id(os.getenv("USTA_LOG_CHAT_ID")) or _chat_id(os.getenv("LOG_CHAT_ID")) or LOG_CHAT_ID


def log_chat_id():
    if in_usta_mode():
        return usta_log_chat_id()
    return _chat_id(os.getenv("LOG_CHAT_ID")) or LOG_CHAT_ID


def mijoz_xato(exc) -> str:
    """Mijozga xom API JSON ko'rsatilmaydi."""
    if isinstance(exc, GeminiQuotaError) or "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
        return (
            "⚠️ Hozircha javob bera olmadim. AI xizmati limiti vaqtincha tugagan. "
            "Bir necha daqiqadan so'ng qayta yozing."
        )
    matn = str(exc).lower()
    if "timed out" in matn or "timeout" in matn:
        return "⚠️ Javob vaqtida uzildi. Qisqaroq yozing yoki 15–20 soniyalik video yuboring."
    return "⚠️ Xatolik yuz berdi. Qayta yozib ko'ring."

MAX_MEDIA = 20 * 1024 * 1024  # 20 MB

BTN_YORDAM = "❓ Qanday ishlataman?"
BTN_YANGI = "🔄 Yangi so'rov"
BTN_JOY = "📍 Joylashuvni yuborish"
BTN_TEL = "📱 Telefon raqamni yuborish"
BTN_UZEX_EXAMPLES = "💡 Savol namunalari"

# UZEX quick buttons
BTN_TOP_BROKER = "🏆 Top brokerlar"
BTN_EXPORT = "💵 Eksport statistikasi"
BTN_CEMENT = "🏗 Sement narxlari"
BTN_DBSTATS = "📊 Baza statistikasi"

buyurtma_kutilmoqda = set()

def in_usta_mode() -> bool:
    forced = request_mode.get()
    if forced:
        return forced == "usta"
    return IS_USTA

def in_uzex_mode() -> bool:
    return not in_usta_mode()

async def bind_request_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Qaysi bot update oldi — shu botning rejimi. Ikkinchisiga o'tmaydi."""
    bot_id = context.bot.id if context.bot else 0
    request_mode.set("usta" if bot_id == USTA_BOT_ID else "uzex")

def klaviatura():
    if in_usta_mode():
        rows = [
            [KeyboardButton(BTN_YANGI), KeyboardButton(BTN_YORDAM)],
            [KeyboardButton(BTN_JOY, request_location=True)],
        ]
    else:
        rows = [
            [KeyboardButton(BTN_YANGI), KeyboardButton(BTN_YORDAM)],
            [KeyboardButton(BTN_TOP_BROKER), KeyboardButton(BTN_EXPORT)],
            [KeyboardButton(BTN_CEMENT), KeyboardButton(BTN_DBSTATS)],
            [KeyboardButton(BTN_UZEX_EXAMPLES)],
        ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def telefon_klaviatura():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_TEL, request_contact=True)],
            [KeyboardButton(BTN_YANGI), KeyboardButton(BTN_YORDAM)],
        ],
        resize_keyboard=True
    )

def inline_uzex_klaviatura():
    """UZEX javoblariga inline tugmalar"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 Top brokerlar", callback_data="uzex_top_brokers")],
        [InlineKeyboardButton("💵 Eksport 2024", callback_data="uzex_export_2024")],
        [InlineKeyboardButton("🏗 Sement narxi (2023)", callback_data="uzex_cement_2023")],
        [InlineKeyboardButton("🔄 Yangi so'rov", callback_data="btn_yangi_suhbat")],
    ])

def inline_tashxis_klaviatura():
    """Tashxis javoblariga biriktiriladigan inline tugmalar (AI Usta)"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Tokdan uzdim / Suvni yopdim ✅", callback_data="btn_xavfsizlik_ok")],
        [InlineKeyboardButton("📞 Ustani chaqirish", callback_data="btn_ustani_chaqirish")],
        [
            InlineKeyboardButton("🔍 Usta topish", callback_data="btn_usta_topish"),
            InlineKeyboardButton("🛒 Zapchast narxi", callback_data="btn_zapchast_narxi"),
        ],
        [InlineKeyboardButton("🔄 Yangi suhbat", callback_data="btn_yangi_suhbat")],
    ])

def user_info(update: Update) -> str:
    u = update.effective_user
    username = f"@{u.username}" if u and u.username else "(username yo'q)"
    first = u.first_name if u and u.first_name else ""
    last = u.last_name if u and u.last_name else ""
    uid = u.id if u else 0
    return f"{first} {last} {username} [ID: {uid}]".strip()

def chiroyli_log(user_str: str, tur: str, matn: str = ""):
    chiziq = "═" * 60
    print(f"\n{chiziq}\n👤 {user_str}\n📨 Tur: {tur}")
    if matn:
        print(f"💬 Matn: {matn}")
    print(chiziq)

async def manzilni_aniqla(lat: float, lon: float) -> str:
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {"format": "json", "lat": lat, "lon": lon, "accept-language": "uz,ru,en"}
    headers = {"User-Agent": "AIUstaBot/1.0"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client_http:
            res = await client_http.get(url, params=params, headers=headers)
            if res.status_code == 200:
                data = res.json()
                manzil = data.get("display_name", "")
                return ", ".join(manzil.split(",")[:4]) if manzil else ""
    except Exception as e:
        logger.error(f"Geocoding xatosi: {e}")
    return ""

async def xavfsiz_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int, action=ChatAction.TYPING):
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=action)
    except Exception as e:
        logger.warning(f"Typing statusini yuborishda xato: {e}")

async def xavfsiz_reply(update: Update, text: str, reply_markup=None, parse_mode="Markdown"):
    target = update.message if update.message else (update.callback_query.message if update.callback_query else None)
    if not target:
        return
    # Telegram max 4096 chars, split if needed
    if len(text) > 4000:
        # Split by lines
        parts = []
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > 4000:
                parts.append(current)
                current = line + "\n"
            else:
                current += line + "\n"
        if current:
            parts.append(current)
        for i, part in enumerate(parts):
            try:
                await target.reply_text(part, parse_mode=parse_mode, reply_markup=reply_markup if i==len(parts)-1 else None)
            except BadRequest:
                await target.reply_text(part, reply_markup=reply_markup if i==len(parts)-1 else None)
        return
    try:
        await target.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except BadRequest as e:
        logger.warning(f"Markdown parse xatosi, oddiy matnda yuborilmoqda: {e}")
        await target.reply_text(text, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Xabar yuborishda kutilmagan xatolik: {e}")
        await target.reply_text(text, reply_markup=reply_markup)

async def guruhga_yubor(context: ContextTypes.DEFAULT_TYPE, user_str: str, tur: str, matn: str = "", fayl_path: str = None, message=None):
    chat = log_chat_id()
    if not chat:
        logger.warning("Log chat sozlanmagan, mijoz xabari yuborilmadi")
        return
    try:
        belgi = "🔧 AI Usta" if in_usta_mode() else "📈 UZEX"
        caption = f"{belgi}\n👤 {user_str}\n📨 {tur}"
        if matn:
            qisqa = matn[:800] + "..." if len(matn) > 800 else matn
            caption += f"\n💬 {qisqa}"
        if message is not None:
            await context.bot.send_message(chat, caption[:1000])
            try:
                await context.bot.copy_message(
                    chat_id=chat,
                    from_chat_id=message.chat_id,
                    message_id=message.message_id,
                )
                return
            except Exception as copy_err:
                logger.warning("Asl xabarni nusxalab bo'lmadi, fayl yuboriladi: %s", copy_err)
                if not (fayl_path and os.path.exists(fayl_path)):
                    return
        if fayl_path and os.path.exists(fayl_path):
            with open(fayl_path, "rb") as f:
                if fayl_path.endswith((".jpg", ".jpeg", ".png")):
                    await context.bot.send_photo(chat, photo=f, caption=caption[:1000])
                elif fayl_path.endswith(".ogg"):
                    await context.bot.send_voice(chat, voice=f, caption=caption[:1000])
                elif fayl_path.endswith((".mp4", ".mov")):
                    await context.bot.send_video(chat, video=f, caption=caption[:1000])
                elif fayl_path.endswith(".mp3"):
                    await context.bot.send_audio(chat, audio=f, caption=caption[:1000])
                else:
                    await context.bot.send_document(chat, document=f, caption=caption[:1000])
        else:
            await context.bot.send_message(chat, caption[:4000])
    except Exception as e:
        logger.warning("Admin guruhiga yuborishda xato (chat=%s): %s", chat, e)

async def ai_javobini_yubor(context: ContextTypes.DEFAULT_TYPE, javob: str):
    chat = log_chat_id()
    if not chat:
        return
    try:
        belgi = "🔧 AI Usta" if in_usta_mode() else "📈 UZEX"
        matn = f"{belgi}\n🤖 AI javobi:\n{javob}"
        if len(matn) > 4000:
            matn = matn[:4000] + "\n...(qisqartirildi)"
        await context.bot.send_message(chat, matn)
    except Exception as e:
        logger.warning("AI javobini guruhga yuborishda xato (chat=%s): %s", chat, e)

async def buyurtma_yubor(context: ContextTypes.DEFAULT_TYPE, update: Update, phone_number: str):
    user_id = update.effective_user.id
    user_str = user_info(update)
    manzil = get_joylashuv(user_id) or "Ko'rsatilmagan"
    tarix = get_tarix(user_id)
    muammo = "Maishiy texnika nosozligi bo'yicha usta chaqirish"
    if tarix:
        for content in reversed(tarix):
            if content.role == "user":
                if content.parts and hasattr(content.parts[0], "text"):
                    muammo = content.parts[0].text[:300]
                    break
    zayavka_matn = (
        f"🚨 *YANGI USTA CHAQIRISH ZAYAVKASI!*\n\n"
        f"👤 *Mijoz:* {user_str}\n"
        f"📞 *Telefon:* `{phone_number}`\n"
        f"📍 *Manzil:* {manzil}\n"
        f"🛠 *Muammo tavsifi:* {muammo}\n"
        f"⏰ *Vaqt:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    chat = log_chat_id()
    if chat:
        try:
            await context.bot.send_message(chat, zayavka_matn, parse_mode="Markdown")
        except Exception as e:
            logger.error("Zayavkani admin guruhga yuborishda xato (chat=%s): %s", chat, e)
            try:
                await context.bot.send_message(chat, zayavka_matn)
            except Exception as e2:
                logger.error("Zayavka oddiy matnda ham ketmadi: %s", e2)
    buyurtma_kutilmoqda.discard(user_id)
    await xavfsiz_reply(
        update,
        "✅ *Arizangiz qabul qilindi!*\n\n"
        "Yaqin oradagi usta 10 daqiqa ichida siz bilan bog'lanadi va muammoni hal qilishga yordam beradi. 🔧",
        reply_markup=klaviatura()
    )

async def media_ishla(update: Update, context: ContextTypes.DEFAULT_TYPE, fayl_id: str, kengaytma: str, tur: str, izoh: str = ""):
    if in_uzex_mode():
        return await xavfsiz_reply(update, NOT_UZEX, reply_markup=klaviatura())
    user_id = update.effective_user.id
    user_str = user_info(update)
    chat_id = update.effective_chat.id
    action = ChatAction.RECORD_VOICE if tur == "ovoz" else (ChatAction.UPLOAD_VIDEO if tur == "video" else ChatAction.UPLOAD_PHOTO)
    await xavfsiz_typing(context, chat_id, action=action)
    path = None
    try:
        if tur == "video":
            await xavfsiz_reply(update, "🎬 Video qabul qilindi. Tahlil qilinmoqda, biroz kuting.")
        fayl = await context.bot.get_file(
            fayl_id, read_timeout=90, write_timeout=90, connect_timeout=20, pool_timeout=20,
        )
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{kengaytma}") as tmp:
            path = tmp.name
        await fayl.download_to_drive(
            path, read_timeout=120, write_timeout=120, connect_timeout=20, pool_timeout=20,
        )
        logger.info(f"💾 Vaqtinchalik fayl saqlandi: {path}")
        tur_emoji = {"rasm": "📸 RASM", "ovoz": "🎤 OVOZ", "video": "🎬 VIDEO"}
        await guruhga_yubor(
            context, user_str, tur_emoji.get(tur, tur), izoh, path, message=update.message,
        )
        await xavfsiz_typing(context, chat_id, action=ChatAction.TYPING)
        javob = await media_javob(user_id, path, izoh, tur)
        print(f"🤖 AI javobi:\n{javob}\n")
        await xavfsiz_reply(update, javob, reply_markup=inline_tashxis_klaviatura())
        await ai_javobini_yubor(context, javob)
    except Exception as e:
        logger.error(f"{tur} xatosi: {e}")
        xato = str(e).lower()
        if "timed out" in xato or "timeout" in xato:
            await xavfsiz_reply(
                update,
                "⚠️ Video tahlili vaqtida uzildi. Qisqaroq video (15–20 soniya) yoki nosoz joyning rasmini yuboring.",
            )
        else:
            await xavfsiz_reply(update, mijoz_xato(e))
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
                logger.info(f"🧹 Vaqtinchalik fayl o'chirildi: {path}")
            except Exception as cleanup_err:
                logger.error(f"Faylni o'chirishda xato ({path}): {cleanup_err}")

# ═══════════════════════════════════════
# INLINE CALLBACK HANDLER
# ═══════════════════════════════════════
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    user_id = query.from_user.id
    data = query.data
    chat_id = query.message.chat_id if query.message else update.effective_chat.id

    if in_uzex_mode() and data in {
        "btn_xavfsizlik_ok", "btn_ustani_chaqirish", "btn_usta_topish", "btn_zapchast_narxi",
    }:
        await query.answer()
        return await xavfsiz_reply(update, NOT_UZEX, reply_markup=klaviatura())

    if data == "btn_xavfsizlik_ok":
        await query.answer(text="Ajoyib! Endi xavfsiz holda ta'mirlashni davom ettirishingiz mumkin. ✅", show_alert=True)
        try:
            await query.message.reply_text(
                "✅ *Xavfsizlik chorasi tasdiqlandi!*\n\n"
                "Qurilma elektr manbaidan uzildi / suv yopildi. Endi ta'mirlash va tekshirish bosqichlarini xavfsiz bajarishingiz mumkin! 🛠",
                parse_mode="Markdown"
            )
        except Exception:
            await query.message.reply_text("✅ Xavfsizlik chorasi tasdiqlandi! Endi xavfsiz holda davom ettirishingiz mumkin.")

    elif data == "btn_ustani_chaqirish":
        await query.answer()
        chiroyli_log(user_info(update), "🔘 INLINE: USTANI CHAQIRISH")
        buyurtma_kutilmoqda.add(user_id)
        try:
            await query.message.reply_text(
                "📞 *Usta chaqirish uchun aloqa raqamingizni yuboring:*\n\n"
                "Pastdagi *'📱 Telefon raqamni yuborish'* tugmasini bosing yoki raqamingizni yozib yuboring (masalan: _+998901234567_).",
                parse_mode="Markdown",
                reply_markup=telefon_klaviatura()
            )
        except Exception:
            await query.message.reply_text("📞 Usta chaqirish uchun telefon raqamingizni yozing yoki pastdagi tugmani bosing:", reply_markup=telefon_klaviatura())

    elif data == "btn_usta_topish":
        await query.answer()
        chiroyli_log(user_info(update), "🔘 INLINE: USTA TOPISH")
        manzil = get_joylashuv(user_id)
        if not manzil:
            try:
                await query.message.reply_text(
                    "📍 *Yaqin atrofdagi ustalarni topish uchun:*\n\n"
                    "1. Pastdagi *'📍 Joylashuvni yuborish'* tugmasini bosing;\n"
                    "2. Yoki shahringiz va tumaningiz nomini yozib yuboring (masalan: _Toshkent, Chilonzor_).",
                    parse_mode="Markdown",
                    reply_markup=klaviatura()
                )
            except Exception:
                await query.message.reply_text("📍 Yaqin atrofdagi ustalarni topish uchun pastdagi 'Joylashuvni yuborish' tugmasini bosing yoki shahringiz nomini yozib yuboring.", reply_markup=klaviatura())
            return
        await xavfsiz_typing(context, chat_id, ChatAction.TYPING)
        prompt = f"Mening joylashuvim: {manzil}. Ushbu texnika nosozligi bo'yicha menga eng yaqin ustaxona va usta telefon raqamlarini, manzilini topib ber."
        try:
            javob = await matn_javob(user_id, prompt)
            await xavfsiz_reply(update, javob, reply_markup=inline_tashxis_klaviatura())
            await ai_javobini_yubor(context, javob)
        except Exception as e:
            logger.error(f"Usta topish inline xatosi: {e}")
            await xavfsiz_reply(update, mijoz_xato(e))

    elif data == "btn_zapchast_narxi":
        await query.answer()
        chiroyli_log(user_info(update), "🔘 INLINE: ZAPCHAST NARXI")
        await xavfsiz_typing(context, chat_id, ChatAction.TYPING)
        prompt = "Ushbu nosozlik uchun kerak bo'ladigan ehtiyot qismlar (zapchastlar) nomini, O'zbekistondagi bozor va do'konlardagi (olx.uz, uzum.uz va ehtiyot qism do'konlaridagi) taxminiy narxlarini so'mda topib aytib ber."
        try:
            javob = await matn_javob(user_id, prompt)
            await xavfsiz_reply(update, javob, reply_markup=inline_tashxis_klaviatura())
            await ai_javobini_yubor(context, javob)
        except Exception as e:
            logger.error(f"Zapchast narxi inline xatosi: {e}")
            await xavfsiz_reply(update, mijoz_xato(e))

    elif data == "btn_yangi_suhbat":
        await query.answer()
        chiroyli_log(user_info(update), "🔘 INLINE: YANGI SUHBAT")
        tarixni_tozala(user_id)
        buyurtma_kutilmoqda.discard(user_id)
        try:
            await query.message.reply_text(
                "🔄 *Suhbat tarixi tozalandi!*\n\n"
                "Yangi UZEX savolini yozing. Men tayyorman.",
                parse_mode="Markdown",
                reply_markup=klaviatura()
            )
        except Exception:
            await query.message.reply_text("🔄 Suhbat tarixi tozalandi!\nYangi savol bering.", reply_markup=klaviatura())

    # UZEX inline handlers
    elif data == "uzex_top_brokers":
        await query.answer()
        chiroyli_log(user_info(update), "🔘 UZEX: TOP BROKERS")
        await handle_uzex_query(update, context, "Qaysi broker eng ko'p bitim o'tkazgan? 2024 yilda")

    elif data == "uzex_export_2024":
        await query.answer()
        chiroyli_log(user_info(update), "🔘 UZEX: EXPORT 2024")
        await handle_uzex_query(update, context, "Экспортные сделки в 2024 году количество и сумма в сумах?")

    elif data == "uzex_cement_2023":
        await query.answer()
        chiroyli_log(user_info(update), "🔘 UZEX: CEMENT 2023")
        await handle_uzex_query(update, context, "Sement bo'yicha eng qimmat bitim qaysi 2023 yilda?")

async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if in_uzex_mode():
        return await xavfsiz_reply(update, NOT_UZEX, reply_markup=klaviatura())
    if not update.message or not update.message.contact:
        return
    phone = update.message.contact.phone_number
    chiroyli_log(user_info(update), "📱 KONTAKT YUBORILDI", phone)
    await guruhga_yubor(context, user_info(update), "📱 KONTAKT", phone, message=update.message)
    await buyurtma_yubor(context, update, phone)

# ═══════════════════════════════════════
# MATNLAR
# ═══════════════════════════════════════
NOT_UZEX = (
    "Bu bot faqat UZEX birja savollariga javob beradi: bitim, broker, STIR, narx, eksport.\n\n"
    "Maishiy texnika va ta'mir bu botga tegishli emas. Savolni birja bo'yicha yozing.\n\n"
    "Этот бот отвечает только на вопросы по бирже UZEX."
)

SALOM_UZEX = """
📈 *UZEX AI Agent* — O'zbekiston Tovar-Xomashyo Birjasi bo'yicha yordamchi.

Men *faqat birja savollariga* javob beraman (500 ming bitim, 2022–2026):
• bitim, narx, broker, kompaniya, STIR/INN
• eksport, eng qimmat/arzon, statistika
• har javobda SQL va hisob-kitob

*Namunalar:*
_\"2024 yilda eng ko'p bitim qilgan broker kim?\"_
_\"STIR 921912462 bo'yicha jami savdo qancha?\"_
_\"Экспортные сделки в 2024 сумма в сумах?\"_

O'zbek va rus tillarini tushunaman. Ta'mir va maishiy texnika savollari bu botga tegishli emas.
"""

SALOM_USTA = """
🔧 *AI Usta* — maishiy texnika va elektronika bo'yicha yordamchi.

Muzlatgich, televizor, kir mashinasi, kompyuter va boshqa qurilmalar.
Muammoni matn, rasm, ovoz yoki video bilan yuboring.

Birja va UZEX savollari bu botga tegishli emas.
"""

SALOM = SALOM_UZEX if IS_UZEX else SALOM_USTA

YORDAM = """
📖 *UZEX AI Agent — Qanday foydalanish kerak*

*UZEX savollar (asosiy):*
Yozing, men bazadan (500 ming bitim) aniq topib beraman:
• *Broker bo'yicha:* \"Qaysi broker eng ko'p bitim qilgan?\"
• *STIR bo'yicha:* \"INN 972345678 xaridlari qancha 2024 yilda?\"
• *Tovar bo'yicha:* \"Paxta yog'i eng arzon qachon?\" / \"Цемент самый дорогой?\"
• *Yil bo'yicha:* \"2025 yilda nechta eksport bitim bo'lgan?\"
• *Narx bo'yicha:* \"Eng qimmat sement shartnomasi qaysi?\"
• *Region bo'yicha:* \"Toshkentda nechta bitim?\"

Til: O'zbek (lotin/krill) va Rus — har ikkalasida ham tushunaman.

*Muhim qoida:*
Agar ma'lumot bazada yo'q bo'lsa, men *to'qimayman*, aniq aytaman:
_\"Ma'lumotlar bazasida bunday ma'lumot mavjud emas\"_

*Javob formati:*
✅ Jadval + SQL so'rovi + hisob-kitob formulasi
• Hajm = Soni × Lot
• Birlik narxi = Summa ÷ (Soni × Lot)
• Eksport USD→so'm = Summa × kurs (usd_rates)

Bu bot maishiy texnika va ta'mir savollariga javob bermaydi.

*Buyruqlar:*
/start — Boshlash
/yangi — Yangi so'rov (tarixni tozalash)
/yordam — Shu sahifa
/dbstats — Baza statistikasi
/misol — Tasodifiy savol namunasi

⚡️ *Tezlik:* 90 soniyadan kam, odatda 2-3 sek!
"""

# ═══════════════════════════════════════
# UZEX detection helper
# ═══════════════════════════════════════
UZEX_KEYWORDS = [
    "uzex", "birja", "биржа", "bitim", "сделк", "broker", "брокер", "bk0", "bk1",
    "stir", "инн", "inn", "kontrakt", "контракт", "shartnoma", "tovar", "товар",
    "mahsulot", "продукт", "sement", "цемент", "yog", "масло", "paxta", "хлопок",
    "soda", "karbamid", "polietilen", "truba", "sotuvchi", "xaridor", "продавец", "покупатель",
    "eksport", "экспорт", "narx", "цена", "summa", "сумма", "qancha", "сколько", "nechta",
    "qaysi", "какой", "yilda", "году", "region", "viloyat", "oblast", "hajm", "объем",
    "qimmat", "arzon", "дорог", "дешев", "aksiya", "акция", "aylantir", "оборот",
    "avgust", "август", "yanvar", "fevral", "mart", "aprel", "sentabr", "oktabr",
    "noyabr", "dekabr", "top broker", "топ брокер",
    "dollar", "доллар", "usd", "$", "shartom", "so'm", "so‘m", "сум", "kurs", "курс",
]

def is_uzex_query(text: str) -> bool:
    tl = text.lower()
    # Explicit UZEX triggers
    if any(kw in tl for kw in UZEX_KEYWORDS):
        return True
    # Year pattern 2022-2026
    import re
    if re.search(r"\b20(2[2-6])\b", text):
        # if contains year + any data-like word, treat as UZEX
        if any(w in tl for w in ["yil", "год", "bitim", "сделк", "broker", "tovar", "товар", "narx", "цена", "summa", "сумма"]):
            return True
        # Even standalone year with question mark might be UZEX? Be cautious
        # But if text is short and contains year, likely UZEX
        if len(text.split()) <= 10:
            return True
    # INN pattern
    if re.search(r"\b\d{9}\b", text):
        return True
    # Broker code
    if re.search(r"\bBK\d{3}\b", text, re.I):
        return True
    return False

async def handle_uzex_query(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str):
    if in_usta_mode():
        return await xavfsiz_reply(
            update,
            "Bu bot faqat ta'mir savollariga javob beradi. Birja savollari bu botga tegishli emas.",
            reply_markup=klaviatura(),
        )
    user_id = update.effective_user.id
    user_str = user_info(update)
    chat_id = update.effective_chat.id
    if not UZEX_ENABLED or not uzex_answer:
        await xavfsiz_reply(update, "⚠️ UZEX bazasi yuklanmagan. Iltimos, `python -m uzex.etl --src /tmp/uzex_spot_data --db ./data/uzex.db` buyrug'ini ishga tushiring.\nBaza mavjud emas: data/uzex.db")
        return
    chiroyli_log(user_str, "📊 UZEX SO'ROV", question)
    await guruhga_yubor(context, user_str, "📊 UZEX", question)
    await xavfsiz_typing(context, chat_id, ChatAction.TYPING)
    try:
        from uzex.db import db_is_ready
        if not db_is_ready():
            await xavfsiz_reply(update, "⏳ Baza ochilmoqda (500 000 bitim). Birinchi savol 15–30 soniya olishi mumkin...")
        javob = await uzex_answer(question, user_id)
        print(f"🤖 UZEX javobi:\n{javob}\n")
        # HTML <pre> — Telegram Markdown '_' ni yeb qo'yardi va SQL buzilib ko'rinardi
        from uzex.textnorm import as_telegram_html
        await xavfsiz_reply(update, as_telegram_html(javob), reply_markup=inline_uzex_klaviatura(), parse_mode="HTML")
        await ai_javobini_yubor(context, javob)
    except Exception as e:
        logger.exception(f"UZEX xatosi: {e}")
        await xavfsiz_reply(update, f"⚠️ Xatolik: {str(e)[:500]}")

# ═══════════════════════════════════════
# BUYRUQLAR
# ═══════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chiroyli_log(user_info(update), "🚀 START")
    if update.effective_chat and update.effective_chat.type == "private":
        await save_user(update.effective_chat.id)
    if in_usta_mode():
        await guruhga_yubor(context, user_info(update), "🚀 START", message=update.message)
    text = SALOM_USTA if in_usta_mode() else SALOM_UZEX
    await xavfsiz_reply(update, text, reply_markup=klaviatura())

async def yangi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tarixni_tozala(user_id)
    buyurtma_kutilmoqda.discard(user_id)
    msg = (
        "🔄 *Yangi so'rov boshlandi!*\n\n"
        "UZEX savolingizni yozing 👇" if in_uzex_mode() else "Texnika muammosini yozing 👇"
    )
    if update.message:
        try:
            await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=klaviatura())
        except Exception:
            await update.message.reply_text(msg, reply_markup=klaviatura())

async def yordam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if in_usta_mode():
        return await xavfsiz_reply(
            update,
            "🔧 *AI Usta*\n\nMuammoni matn, rasm, ovoz yoki video bilan yuboring. Birja savollari bu botga tegishli emas.",
            reply_markup=klaviatura(),
        )
    await xavfsiz_reply(update, YORDAM, reply_markup=klaviatura())

async def dbstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if in_usta_mode():
        return await xavfsiz_reply(
            update,
            "Bu bot faqat ta'mir savollariga javob beradi. Birja savollari bu botga tegishli emas.",
            reply_markup=klaviatura(),
        )
    if not UZEX_ENABLED:
        await xavfsiz_reply(update, "⚠️ UZEX moduli yuklanmagan.")
        return
    try:
        summary = get_schema_summary()
        db_path = get_db_path()
        size_mb = db_path.stat().st_size / 1024 / 1024 if db_path.exists() else 0
        msg = f"📊 *Baza statistikasi:*\n\n"
        msg += f"• Fayl: `{db_path}` ({size_mb:.1f} MB)\n"
        msg += f"• {summary}\n"
        # Get more details
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM deals;")
        deals = cur.fetchone()[0]
        cur.execute("SELECT min(deal_date), max(deal_date) FROM deals;")
        min_d, max_d = cur.fetchone()
        cur.execute("SELECT count(*) FROM participants;")
        parts = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM contracts;")
        conts = cur.fetchone()[0]
        conn.close()
        msg += f"• Bitimlar: {deals:,} ({min_d} → {max_d})\n"
        msg += f"• Ishtirokchilar: {parts:,}\n"
        msg += f"• Shartnomalar: {conts:,}\n"
        msg += f"\n✅ Baza tayyor! Savol bering."
        await xavfsiz_reply(update, msg)
    except Exception as e:
        await xavfsiz_reply(update, f"⚠️ DB xatosi: {e}")

async def misol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    examples = [
        "2024 yilda eng ko'p bitim qilgan broker kim?",
        "BK001 brokeri 2025 yilda nechta bitimda ishtirok etgan?",
        "Экспортные сделки в 2024 году сумма в сумах?",
        "Sement bo'yicha eng qimmat bitim qaysi 2023 yilda?",
        "STIR 921912462 bo'yicha jami savdolar qancha?",
        "Paxta yog'i bo'yicha 2025 yilda eng arzon shartnoma qaysi?",
        "Qaysi tovar eng ko'p sotilgan 2024 yilda?",
    ]
    import random
    ex = random.choice(examples)
    await xavfsiz_reply(update, f"💡 *Tasodifiy namuna:*\n\n`{ex}`\n\nShu savolni yuboring yoki o'zingiznikini yozing!", reply_markup=inline_uzex_klaviatura())

# ═══════════════════════════════════════
# XABAR HANDLERLARI
# ═══════════════════════════════════════
async def matn_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    matn = update.message.text if update.message else ""
    if matn == BTN_YORDAM:
        return await yordam(update, context)
    if matn == BTN_YANGI:
        return await yangi(update, context)
    if matn == BTN_TOP_BROKER:
        return await handle_uzex_query(update, context, "2024 yilda eng ko'p bitim qilgan broker kim?")
    if matn == BTN_EXPORT:
        return await handle_uzex_query(update, context, "Экспортные сделки в 2024 году количество и сумма в сумах?")
    if matn == BTN_CEMENT:
        return await handle_uzex_query(update, context, "Sement bo'yicha eng qimmat bitim qaysi 2023 yilda?")
    if matn == BTN_DBSTATS:
        return await dbstats(update, context)
    if matn == BTN_UZEX_EXAMPLES:
        return await misol(update, context)

    user_id = update.effective_user.id
    user_str = user_info(update)
    chat_id = update.effective_chat.id

    if in_usta_mode() and user_id in buyurtma_kutilmoqda and matn and not matn.startswith("/"):
        chiroyli_log(user_str, "📱 MATNLI TELEFON RAQAM", matn)
        return await buyurtma_yubor(context, update, matn)

    if in_uzex_mode():
        if UZEX_ENABLED and is_uzex_query(matn):
            return await handle_uzex_query(update, context, matn)
        return await xavfsiz_reply(update, NOT_UZEX, reply_markup=klaviatura())

    chiroyli_log(user_str, "✍️ MATN", matn)
    await guruhga_yubor(context, user_str, "✍️ MATN", matn, message=update.message)
    await xavfsiz_typing(context, chat_id, action=ChatAction.TYPING)
    try:
        javob = await matn_javob(user_id, matn)
        print(f"🤖 AI javobi:\n{javob}\n")
        await xavfsiz_reply(update, javob, reply_markup=inline_tashxis_klaviatura())
        await ai_javobini_yubor(context, javob)
    except Exception as e:
        logger.exception("Matn xatosi")
        await xavfsiz_reply(update, mijoz_xato(e))

async def rasm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    izoh = update.message.caption or ""
    chiroyli_log(user_info(update), "📸 RASM", izoh)
    await media_ishla(update, context, update.message.photo[-1].file_id, "jpg", "rasm", izoh)

async def ovoz_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chiroyli_log(user_info(update), "🎤 OVOZLI XABAR")
    await media_ishla(update, context, update.message.voice.file_id, "ogg", "ovoz")

async def audio_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chiroyli_log(user_info(update), "🎵 AUDIO FAYL")
    await media_ishla(update, context, update.message.audio.file_id, "mp3", "ovoz", update.message.caption or "")

async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = update.message.video
    izoh = update.message.caption or ""
    chiroyli_log(user_info(update), "🎬 VIDEO", izoh)
    if video.file_size and video.file_size > MAX_MEDIA:
        return await xavfsiz_reply(update, "⚠️ Video juda katta (20 MB dan ortiq).\nQisqaroq video yuboring yoki muammoli joyni rasmga oling.")
    await media_ishla(update, context, video.file_id, "mp4", "video", izoh)

async def dumaloq_video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    vn = update.message.video_note
    chiroyli_log(user_info(update), "⭕️ DUMALOQ VIDEO")
    if vn.file_size and vn.file_size > MAX_MEDIA:
        return await xavfsiz_reply(update, "⚠️ Video juda katta. Qisqaroq yuboring.")
    await media_ishla(update, context, vn.file_id, "mp4", "video")

async def fayl_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    mime = (doc.mime_type or "").lower()
    izoh = update.message.caption or ""
    if mime.startswith("image/"):
        tur, kengaytma = "rasm", "jpg"
    elif mime.startswith("video/"):
        tur, kengaytma = "video", "mp4"
    elif mime.startswith("audio/"):
        tur, kengaytma = "ovoz", "mp3"
    else:
        return await xavfsiz_reply(update, "⚠️ Bu fayl turini tushunmayman.\nIltimos, rasm, video yoki ovozli xabar yuboring.")
    chiroyli_log(user_info(update), f"📎 FAYL ({tur})", izoh)
    if doc.file_size and doc.file_size > MAX_MEDIA:
        return await xavfsiz_reply(update, "⚠️ Fayl juda katta (20 MB dan ortiq).")
    await media_ishla(update, context, doc.file_id, kengaytma, tur, izoh)

async def joylashuv_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if in_uzex_mode():
        return await xavfsiz_reply(update, NOT_UZEX, reply_markup=klaviatura())
    user_id = update.effective_user.id
    loc = update.message.location
    user_str = user_info(update)
    chat_id = update.effective_chat.id
    chiroyli_log(user_str, "📍 JOYLASHUV", f"{loc.latitude}, {loc.longitude}")
    await guruhga_yubor(
        context, user_str, "📍 JOYLASHUV",
        f"Lat: {loc.latitude}, Lon: {loc.longitude}",
        message=update.message,
    )
    await xavfsiz_typing(context, chat_id, action=ChatAction.FIND_LOCATION)
    await xavfsiz_reply(update, "📍 Manzilni aniqlayapman...")
    try:
        manzil = await manzilni_aniqla(loc.latitude, loc.longitude)
        if not manzil:
            return await xavfsiz_reply(update, "⚠️ Manzil aniqlanmadi. Shahringiz nomini yozib yuboring.")
        set_joylashuv(user_id, manzil)
        print(f"📍 Manzil saqlandi: {manzil}")
        await xavfsiz_reply(update, f"✅ Joylashuv saqlandi:\n*{manzil}*\n\nEndi usta yoki ehtiyot qism kerak bo'lsa — yaqin atrofdagi joylarni topib beraman! 🔧")
    except Exception as e:
        logger.error(f"Joylashuv xatosi: {e}")
        await xavfsiz_reply(update, "⚠️ Xatolik yuz berdi. Shahringiz nomini yozib yuboring.")

async def boshqa_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if in_uzex_mode():
        return await xavfsiz_reply(update, NOT_UZEX, reply_markup=klaviatura())
    await xavfsiz_reply(update, "🤔 Bu turdagi xabarni tushunmayman.\n\nMenga quyidagilarni yuboring:\n✍️ Matn  📸 Rasm  🎤 Ovoz  🎬 Video  📍 Joylashuv\n\nYordam uchun /yordam")

# ═══════════════════════════════════════
# ISHGA TUSHIRISH
# ═══════════════════════════════════════
async def post_init(app: Application):
    me = await app.bot.get_me()
    if EXPECTED_BOT_USERNAME and (me.username or "") != EXPECTED_BOT_USERNAME:
        logger.error(
            "Noto'g'ri bot: @%s, kutilgan @%s. Buyruqlar o'rnatilmaydi.",
            me.username, EXPECTED_BOT_USERNAME,
        )
        return
    if IS_UZEX:
        await app.bot.set_my_commands([
            BotCommand("start", "Boshlash"),
            BotCommand("yangi", "Yangi UZEX savoli"),
            BotCommand("yordam", "Yordam"),
            BotCommand("dbstats", "Baza statistikasi"),
            BotCommand("misol", "Namuna savol"),
        ])
        try:
            await app.bot.set_my_short_description(
                short_description="Faqat UZEX birja savollari. Ta'mir boti emas."
            )
            await app.bot.set_my_description(
                description=(
                    "UZEX Tovar-Xomashyo Birjasi bo'yicha savollarga javob beraman "
                    "(2022–2026). Bitim, broker, STIR, narx va eksport. "
                    "Maishiy texnika va ta'mir bu botga tegishli emas."
                )
            )
        except Exception:
            logger.exception("Bot tavsifi yangilanmadi")
        return
    await app.bot.set_my_commands([
        BotCommand("start", "Boshlash"),
        BotCommand("yangi", "Yangi muammo"),
        BotCommand("yordam", "Yordam"),
    ])

def register_handlers(app: Application) -> Application:
    """Barcha handlerlarni ro'yxatdan o'tkazish."""
    app.add_handler(TypeHandler(Update, bind_request_mode), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("yangi", yangi))
    app.add_handler(CommandHandler("yordam", yordam))
    app.add_handler(CommandHandler("dbstats", dbstats))
    app.add_handler(CommandHandler("misol", misol))
    app.add_handler(CallbackQueryHandler(callback_query_handler))
    app.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    app.add_handler(MessageHandler(filters.PHOTO, rasm_handler))
    app.add_handler(MessageHandler(filters.VOICE, ovoz_handler))
    app.add_handler(MessageHandler(filters.AUDIO, audio_handler))
    app.add_handler(MessageHandler(filters.VIDEO, video_handler))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, dumaloq_video_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, fayl_handler))
    app.add_handler(MessageHandler(filters.LOCATION, joylashuv_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, matn_handler))
    app.add_handler(MessageHandler(filters.Sticker.ALL, boshqa_handler))
    return app

def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        print("❌ TELEGRAM_TOKEN o'rnatilmagan! .env faylni tekshiring (.env.example ga qarang).")
        return
    print(f"🤖 BOT_MODE={BOT_MODE} kutilgan=@{EXPECTED_BOT_USERNAME}")
    if UZEX_ENABLED:
        try:
            dbp = get_db_path()
            if dbp.exists():
                print(f"✅ UZEX DB topildi: {dbp} ({dbp.stat().st_size/1024/1024:.1f} MB)")
            else:
                print(f"⚠️ UZEX DB topilmadi: {dbp} — `python -m uzex.etl --src /tmp/uzex_spot_data --db ./data/uzex.db` ni ishga tushiring")
        except Exception as e:
            print(f"⚠️ DB tekshiruv xatosi: {e}")
    else:
        print("⚠️ UZEX moduli o'chirilgan (import xatosi)")
    app = Application.builder().token(token).post_init(post_init).build()
    register_handlers(app)
    print("✅ Bot tayyor! Telegram'da sinab ko'ring.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
