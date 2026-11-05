import logging
import os
import re

from dotenv import load_dotenv
from google import genai
from google.genai import types
from prompts import SYSTEM_PROMPT

load_dotenv()
logger = logging.getLogger(__name__)

# gemini-3-flash-preview bepul kvotasi tez tugaydi (429). AI Usta avval
# kvotasi kengroq modellarni sinaydi. UZEX o'z model ro'yxatini o'zi tutadi.
_USTA_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-3-flash-preview",
)
_preferred_model = None

_client = None


def get_client():
    """AI Usta o'z Gemini kalitini ishlatadi. UZEX kalitiga tushmaydi."""
    global _client
    if _client is None:
        api_key = os.getenv("USTA_GEMINI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "USTA_GEMINI_API_KEY o'rnatilmagan. AI Usta va UZEX kalitlari alohida."
            )
        _client = genai.Client(
            api_key=api_key,
            http_options={"timeout": 120000},
        )
    return _client


suhbat_tarixi = {}
joylashuvlar = {}

QIDIRUV_SOZLARI = [
    "usta", "ustaxona", "narx", "narxi", "do'kon", "dokon", "qayer",
    "qayerdan", "topib", "top", "olx", "bozor", "ehtiyot", "ehtiyot qism",
    "zapchast", "zapas", "qism", "sotib", "sotib olish", "olsam", "yaqin",
    "manzil", "telefon", "raqam", "xizmat"
]


def qidiruv_kerakmi(matn, user_id=None):
    matn = (matn or "").lower()
    bor = any(soz in matn for soz in QIDIRUV_SOZLARI)
    if bor:
        return True
    if user_id and get_joylashuv(user_id) and any(w in matn for w in ["qayer", "narx", "usta", "do'kon", "sotib"]):
        return True
    return False


def get_tarix(user_id):
    if user_id not in suhbat_tarixi:
        suhbat_tarixi[user_id] = []
    return suhbat_tarixi[user_id]


def tarixga_qosh(user_id, rol, matn):
    tarix = get_tarix(user_id)
    tarix.append(types.Content(role=rol, parts=[types.Part(text=matn)]))
    # Oxirgi 10 ta xabarni saqlash (kontekstni toza va ixcham tutish)
    if len(tarix) > 10:
        suhbat_tarixi[user_id] = tarix[-10:]


def set_joylashuv(user_id, manzil):
    joylashuvlar[user_id] = manzil


def get_joylashuv(user_id):
    return joylashuvlar.get(user_id)


class GeminiQuotaError(RuntimeError):
    """Barcha modellar 429 qaytarganda. Mijozga xom JSON ko'rsatilmaydi."""


def usta_model_nomlari():
    """Ishlaydigan modelni birinchi qo'yib, qolganlarini zaxira sifatida qaytaradi."""
    nomlar = []
    for nom in (
        os.getenv("USTA_GEMINI_MODEL", ""),
        _preferred_model or "",
        *_USTA_MODELS,
        os.getenv("GEMINI_MODEL", ""),
    ):
        nom = (nom or "").strip()
        if nom and nom not in nomlar:
            nomlar.append(nom)
    return nomlar


def _xato_matni(exc) -> str:
    return str(exc or "").lower()


def kvota_xatosimi(exc) -> bool:
    matn = _xato_matni(exc)
    return any(belgi in matn for belgi in (
        "429", "resource_exhausted", "quota", "rate limit", "rate_limit",
    ))


def model_yoqmi(exc) -> bool:
    matn = _xato_matni(exc)
    return any(belgi in matn for belgi in (
        "404", "not_found", "not found", "is not found", "is not supported",
        "invalid model", "unknown model",
    ))


def _qayta_urinish_soniyasi(exc):
    mos = re.search(r"retry in ([0-9]+(?:\.[0-9]+)?)s", _xato_matni(exc))
    if not mos:
        return None
    try:
        return float(mos.group(1))
    except ValueError:
        return None


def get_system_instruction(user_id):
    """System Prompt va joylashuv ma'lumotlarini birlashtirish"""
    manzil = get_joylashuv(user_id)
    if manzil:
        return (
            f"{SYSTEM_PROMPT}\n\n"
            f"📍 FOYDALANUVCHINING ANIQ JOYLASHUVI:\n"
            f"Foydalanuvchi manzili: {manzil}. Ustaxona, usta, do'kon va ehtiyot qism "
            f"so'ralganda shu joy yaqinidan qidir va aniq tavsiya ber."
        )
    return SYSTEM_PROMPT


async def _generate(client, model, contents, config):
    return await client.aio.models.generate_content(
        model=model,
        contents=contents,
        config=config,
    )


async def ai_sorov(user_id, contents, qidiruv=False):
    """Gemini'ga so'rov. 429 yoki yo'q model bo'lsa keyingi modelga o'tadi."""
    global _preferred_model
    client = get_client()
    sys_instruction = get_system_instruction(user_id)
    oddiy = types.GenerateContentConfig(system_instruction=sys_instruction)
    qidiruvli = types.GenerateContentConfig(
        system_instruction=sys_instruction,
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )
    oxirgi = None
    for model in usta_model_nomlari():
        try:
            javob = await _generate(
                client, model, contents, qidiruvli if qidiruv else oddiy,
            )
        except Exception as e:
            oxirgi = e
            if qidiruv:
                logger.warning("Gemini %s qidiruvsiz qayta uriniladi: %s", model, type(e).__name__)
                try:
                    javob = await _generate(client, model, contents, oddiy)
                except Exception as e2:
                    oxirgi = e2
                    e = e2
                else:
                    _preferred_model = model
                    logger.info("Gemini model (qidiruvsiz): %s", model)
                    return javob
            if kvota_xatosimi(e) or model_yoqmi(e):
                kutish = _qayta_urinish_soniyasi(e)
                if kutish is not None and kutish <= 3:
                    import asyncio
                    await asyncio.sleep(kutish)
                    try:
                        javob = await _generate(client, model, contents, oddiy)
                    except Exception as e3:
                        oxirgi = e3
                        logger.warning("Gemini %s limiti, keyingi model: %s", model, type(e3).__name__)
                        continue
                    else:
                        _preferred_model = model
                        return javob
                logger.warning("Gemini %s ishlamadi, keyingi model: %s", model, type(e).__name__)
                continue
            raise
        else:
            _preferred_model = model
            logger.info("Gemini model: %s", model)
            return javob
    if kvota_xatosimi(oxirgi):
        raise GeminiQuotaError("AI limiti vaqtincha tugagan") from oxirgi
    raise oxirgi or RuntimeError("Gemini javob bermadi")


async def matn_javob(user_id, xabar):
    """Matnli xabarga asinxron AI javobi"""
    tarixga_qosh(user_id, "user", xabar)
    contents = get_tarix(user_id)
    need_search = qidiruv_kerakmi(xabar, user_id)

    javob = await ai_sorov(user_id, contents, qidiruv=need_search)
    ai_javobi = javob.text or "⚠️ Bo'sh javob keldi. Qayta yozib ko'ring."
    tarixga_qosh(user_id, "model", ai_javobi)
    return ai_javobi


async def media_javob(user_id, fayl_path, izoh="", fayl_turi="rasm"):
    """Rasm, video va ovozli xabarlarga asinxron AI javobi"""
    if fayl_turi == "ovoz":
        mime_type = "audio/ogg" if fayl_path.endswith(".ogg") else "audio/mpeg"
        asosiy_savol = (
            "Bu ovozli xabarni tingla. Foydalanuvchi maishiy texnika yoki elektronika "
            "muammosini tushuntiryapti. Xabarni tushun va AI Usta sifatida tashxis qo'y."
        )
    elif fayl_turi == "video":
        mime_type = "video/mp4"
        asosiy_savol = (
            "Bu videoni ko'rib chiq. Qanday qurilma va qanday nosozlik ko'rsatilgan? "
            "Tovushlar va harakatlarga e'tibor berib, AI Usta sifatida tashxis qo'y."
        )
    else:
        mime_type = "image/png" if fayl_path.endswith(".png") else "image/jpeg"
        asosiy_savol = (
            "Bu rasmni tahlil qil. Qanday qurilma, brend/model va qanday nosozlik "
            "ko'rinyapti? AI Usta sifatida tashxis va yechim ber."
        )

    savol = f"{asosiy_savol} Foydalanuvchi izohi: {izoh}" if izoh else asosiy_savol

    with open(fayl_path, "rb") as f:
        fayl_bytes = f.read()

    media_part = types.Part.from_bytes(data=fayl_bytes, mime_type=mime_type)
    media_xabar = types.Content(
        role="user",
        parts=[media_part, types.Part(text=savol)]
    )

    # Oldingi suhbat tarixi + joriy media fayl
    contents = get_tarix(user_id) + [media_xabar]
    # Video/ovozda qidiruvni yoqmaslik — u javobni sekinlashtirib, timeout qiladi.
    need_search = False if fayl_turi in ("video", "ovoz") else qidiruv_kerakmi(izoh, user_id)

    javob = await ai_sorov(user_id, contents, qidiruv=need_search)
    ai_javobi = javob.text or "⚠️ Bo'sh javob keldi. Qayta yuborib ko'ring."

    # Tarixga media haqida ixcham matnli ma'lumot qo'shish (xotirani og'irlashtirmaslik uchun)
    user_summary = f"[Foydalanuvchi {fayl_turi} yubordi] {izoh}".strip()
    tarixga_qosh(user_id, "user", user_summary)
    tarixga_qosh(user_id, "model", ai_javobi)

    return ai_javobi


def tarixni_tozala(user_id):
    suhbat_tarixi[user_id] = []
    return "Suhbat tozalandi. Yangi muammo haqida gaplashishimiz mumkin!"
